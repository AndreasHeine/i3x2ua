"""Bounded, deterministic i3X workflows shared by the MCP entry points."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)
Query = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
MAX_OUTPUT_BYTES = 1_048_576


class WorkflowOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maxNodes: int = Field(default=100, ge=1, le=500)
    maxDepth: int = Field(default=2, ge=0, le=10, description="Graph hops; zero selects only seeds, not unlimited.")
    maxEdges: int = Field(default=500, ge=1, le=2000)
    timeoutSeconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)


class AssetContextRequest(WorkflowOptions):
    elementIds: list[str] = Field(min_length=1, max_length=100)
    includeSchemas: bool = True
    includeValues: bool = False
    includeHierarchy: bool = True
    includeGraph: bool = False

    @field_validator("elementIds")
    @classmethod
    def valid_ids(cls, ids: list[str]) -> list[str]:
        if any(not value or value != value.strip() or not value.isprintable() for value in ids):
            raise ValueError("elementIds must be nonempty printable identifiers without surrounding whitespace")
        return list(dict.fromkeys(ids))

    @model_validator(mode="after")
    def seed_budget(self) -> AssetContextRequest:
        if len(self.elementIds) > self.maxNodes:
            raise ValueError("maxNodes must cover every explicit elementId")
        return self


class AssetSnapshotRequest(AssetContextRequest):
    includeValues: bool = True


class AssetSearchRequest(WorkflowOptions):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    typeElementId: str | None = None
    namespaceUri: str | None = None
    scopeElementIds: list[str] = Field(default_factory=list, max_length=100)
    maxScan: int = Field(default=1000, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    modelRevision: str | None = None

    @field_validator("scopeElementIds")
    @classmethod
    def valid_scope(cls, ids: list[str]) -> list[str]:
        return AssetContextRequest.valid_ids(ids)

    @model_validator(mode="after")
    def scope_budget(self) -> AssetSearchRequest:
        if len(self.scopeElementIds) > self.maxNodes:
            raise ValueError("maxNodes must cover every explicit scopeElementId")
        if self.scopeElementIds and self.offset:
            raise ValueError("Scoped search does not support offset continuation")
        return self


class AssetMonitoringRequest(AssetContextRequest):
    clientId: str = Field(min_length=1, max_length=200)
    displayName: str | None = Field(default=None, max_length=200)
    includeHierarchy: bool = False
    includeSchemas: bool = False

    @field_validator("clientId")
    @classmethod
    def valid_client(cls, value: str) -> str:
        if not value.strip() or not value.isprintable():
            raise ValueError("clientId must be nonempty and printable")
        return value


class SignalHistoryRequest(AssetContextRequest):
    startTime: AwareDatetime
    endTime: AwareDatetime
    maxPointsPerSignal: int = Field(default=100, ge=1, le=1000)
    includeValues: bool = False

    @model_validator(mode="after")
    def time_budget(self) -> SignalHistoryRequest:
        duration = (self.endTime - self.startTime).total_seconds()
        if duration < 0 or duration > 86400:
            raise ValueError("History interval must be ordered and no longer than 24 hours")
        if self.maxNodes * self.maxPointsPerSignal > 10000:
            raise ValueError("maxNodes * maxPointsPerSignal must not exceed 10000")
        return self


class DataAvailabilityRequest(AssetContextRequest):
    includeValues: bool = True
    staleAfterSeconds: float = Field(default=300, gt=0, le=604800, allow_inf_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(stage: str, message: str, element_id: str | None = None) -> dict[str, Any]:
    return {"stage": stage, "elementId": element_id, "message": message}


class AssetContextService:
    def __init__(self, model: BuildResult, query: Query):
        self.model = model
        self.query = query
        self.monitoring_result: dict[str, Any] | None = None

    def graph(self, options: AssetContextRequest) -> dict[str, Any]:
        objects: dict[str, Any] = {}
        edges: list[dict[str, str]] = []
        errors: list[dict[str, Any]] = []
        reasons: set[str] = set()
        queue = deque((element_id, 0) for element_id in options.elementIds)
        scheduled = set(options.elementIds)
        examined = 0
        while queue:
            element_id, depth = queue.popleft()
            node = self.model.nodes_by_id.get(element_id)
            if node is None:
                errors.append(_error("discovery", "Object not found", element_id))
                continue
            objects[element_id] = None
            relationships = self.model.relationships_by_id.get(element_id) or node.relationships
            for relationship, targets in relationships.items():
                plane = (
                    "composition"
                    if relationship in {"HasComponent", "ComponentOf"}
                    else "hierarchy"
                    if relationship in {"HasChildren", "HasParent"}
                    else "graph"
                )
                for target in targets:
                    if examined >= options.maxEdges:
                        reasons.add("maxEdges")
                        break
                    examined += 1
                    edges.append(
                        {
                            "source": element_id,
                            "target": target,
                            "relationshipType": relationship,
                            "plane": plane,
                        }
                    )
                    follow = (
                        relationship == "HasComponent"
                        or (options.includeHierarchy and relationship == "HasChildren")
                        or (options.includeGraph and plane == "graph")
                    )
                    if not follow or target in scheduled:
                        continue
                    if depth >= options.maxDepth:
                        reasons.add("maxDepth")
                    elif len(scheduled) >= options.maxNodes:
                        reasons.add("maxNodes")
                    else:
                        scheduled.add(target)
                        queue.append((target, depth + 1))
        return {
            "objects": objects,
            "edges": edges,
            "errors": errors,
            "coverage": {
                "discoveredNodes": len(objects),
                "examinedEdges": examined,
                "truncated": bool(reasons),
                "limitReasons": sorted(reasons),
                "continuationSupported": False,
            },
        }

    async def _bulk(self, operation: str, ids: list[str], errors: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            payload = await self.query(operation, {"elementIds": batch, **extra})
            items = payload.get("results")
            if not isinstance(items, list):
                errors.append(_error(operation, json.dumps(payload, ensure_ascii=True)))
                continue
            by_id = {item.get("elementId"): item for item in items if isinstance(item, dict)}
            for element_id in batch:
                item = by_id.get(element_id)
                if item is None or item.get("success") is not True:
                    errors.append(_error(operation, json.dumps(item or {"error": "Missing result"}), element_id))
                else:
                    result[element_id] = item.get("result")
        return result

    async def context(self, options: AssetContextRequest) -> dict[str, Any]:
        result = self.graph(options)
        errors = result["errors"]
        ids = list(result["objects"])
        result["objects"] = await self._bulk("objects", ids, errors)
        type_ids = sorted({obj["typeElementId"] for obj in result["objects"].values()})
        result["types"] = await self._bulk("types", type_ids, errors) if options.includeSchemas else {}
        signals = [element_id for element_id in ids if self.model.nodes_by_id[element_id].kind == "property"]
        result["values"] = await self._bulk("values", signals, errors) if options.includeValues else {}
        result["signalElementIds"] = signals
        result["missingInformation"] = []
        if options.includeValues:
            result["missingInformation"].append(
                "Canonical current-value VQT does not distinguish source, server or normalized fallback timestamps"
            )
        if not signals:
            result["missingInformation"].append("No property-backed signals were selected within the traversal budget")
        result["interpretation"] = (
            "Last known values, not an atomic read or a health verdict. "
            "Units and provenance are available only where supplied by object metadata or type schemas."
        )
        result["coverage"]["resolvedObjects"] = len(result["objects"])
        return result

    async def search(self, options: AssetSearchRequest) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        if options.modelRevision is not None and options.modelRevision != self.model.revision:
            return {"candidates": [], "errors": [_error("search", "Model revision changed; restart search")]}
        coverage: dict[str, Any] = {}
        if options.scopeElementIds:
            graph = self.graph(
                AssetContextRequest(
                    elementIds=options.scopeElementIds,
                    maxNodes=options.maxNodes,
                    maxDepth=options.maxDepth,
                    maxEdges=options.maxEdges,
                )
            )
            ids = list(graph["objects"])
            errors.extend(graph["errors"])
            coverage.update(graph["coverage"])
        else:
            ids = list(self.model.nodes_by_id)
        examined_ids = ids[options.offset : options.offset + options.maxScan]
        candidates: list[dict[str, Any]] = []
        scanned = 0
        for offset in range(0, len(examined_ids), 50):
            batch = examined_ids[offset : offset + 50]
            objects = await self._bulk("objects", batch, errors)
            for element_id in batch:
                scanned += 1
                obj = objects.get(element_id)
                if obj is None:
                    continue
                node = self.model.nodes_by_id[element_id]
                namespace = self.model.namespace_uri_by_id.get(element_id) or node.metadata.get("namespaceUri")
                if (
                    (options.name is None or options.name.casefold() in obj["displayName"].casefold())
                    and (options.typeElementId is None or options.typeElementId == obj["typeElementId"])
                    and (options.namespaceUri is None or options.namespaceUri == namespace)
                ):
                    candidates.append(obj)
                    if len(candidates) == options.maxNodes:
                        break
            if len(candidates) == options.maxNodes:
                break
            await asyncio.sleep(0)
        next_offset = options.offset + scanned
        more = next_offset < len(ids)
        reasons = set(coverage.get("limitReasons", []))
        if more:
            reasons.add("maxNodes" if len(candidates) == options.maxNodes else "maxScan")
        coverage.update(
            {
                "scannedNodes": scanned,
                "truncated": more or coverage.get("truncated", False),
                "limitReasons": sorted(reasons),
                "nextOffset": next_offset if more and not options.scopeElementIds else None,
                "modelRevision": self.model.revision,
                "continuationSupported": not bool(options.scopeElementIds),
            }
        )
        return {"candidates": candidates, "errors": errors, "coverage": coverage}

    async def monitor(self, options: AssetMonitoringRequest) -> dict[str, Any]:
        if options.includeGraph:
            return {"errors": [_error("monitoring", "Graph expansion is not allowed for monitoring")]}
        graph = self.graph(options)
        result: dict[str, Any] = {
            "errors": graph["errors"],
            "coverage": graph["coverage"],
            "subscriptionId": None,
            "registration": None,
            "cleanup": None,
            "partialRegistrationPolicy": "rollback",
            "monitoringActive": False,
        }
        self.monitoring_result = result
        signals = [
            element_id for element_id in graph["objects"] if self.model.nodes_by_id[element_id].kind == "property"
        ]
        result["signalElementIds"] = signals
        if graph["errors"] or graph["coverage"]["truncated"] or not signals:
            result["errors"].append(
                _error("monitoring", "Complete, nonempty target expansion is required; nothing created")
            )
            return result
        registered = False
        try:
            creation = asyncio.ensure_future(
                self.query(
                    "create",
                    {
                        "clientId": options.clientId,
                        "displayName": options.displayName,
                    },
                )
            )
            try:
                created = await asyncio.shield(creation)
            except asyncio.CancelledError:
                try:
                    created = await asyncio.wait_for(asyncio.shield(creation), timeout=5)
                    result["subscriptionId"] = (created.get("result") or {}).get("subscriptionId")
                    if result["subscriptionId"]:
                        result["monitoringActive"] = None
                except TimeoutError:
                    creation.cancel()
                    await asyncio.gather(creation, return_exceptions=True)
                    result["errors"].append(
                        _error("create", "Creation outcome unknown after timeout; inspect subscriptions")
                    )
                raise
            subscription_id = (created.get("result") or {}).get("subscriptionId")
            if created.get("success") is not True or not isinstance(subscription_id, str) or not subscription_id:
                result["errors"].append(_error("create", json.dumps(created)))
                return result
            result["subscriptionId"] = subscription_id
            result["monitoringActive"] = None
            registration = await self.query(
                "register",
                {
                    "clientId": options.clientId,
                    "subscriptionId": subscription_id,
                    "elementIds": signals,
                    "maxDepth": 1,
                },
            )
            result["registration"] = registration
            items = registration.get("results")
            registered = (
                registration.get("success") is True
                and isinstance(items, list)
                and len(items) == len(signals)
                and all(
                    isinstance(item, dict) and item.get("success") is True and item.get("elementId") == element_id
                    for item, element_id in zip(items, signals, strict=True)
                )
            )
            if not registered:
                result["errors"].append(_error("register", "Registration incomplete or failed; rolling back"))
        except Exception as exc:
            logger.exception("Composite monitoring failed subscription_id=%s", result["subscriptionId"])
            result["errors"].append(_error("monitoring", str(exc)))
        finally:
            subscription_id = result["subscriptionId"]
            if not registered and subscription_id:
                cleanup_task = asyncio.ensure_future(
                    self.query(
                        "delete",
                        {
                            "clientId": options.clientId,
                            "subscriptionIds": [subscription_id],
                        },
                    )
                )
                try:
                    cleanup = await asyncio.wait_for(asyncio.shield(cleanup_task), timeout=5)
                    result["cleanup"] = cleanup
                    deleted = cleanup.get("results")
                    if (
                        cleanup.get("success") is not True
                        or not isinstance(deleted, list)
                        or len(deleted) != 1
                        or deleted[0].get("success") is not True
                    ):
                        result["errors"].append(
                            _error("cleanup", "Subscription deletion failed; handle may still be active")
                        )
                        logger.error("Composite monitoring cleanup failed subscription_id=%s", subscription_id)
                    else:
                        result["monitoringActive"] = False
                except TimeoutError:
                    cleanup_task.cancel()
                    await asyncio.gather(cleanup_task, return_exceptions=True)
                    result["errors"].append(_error("cleanup", "Deletion timed out; handle may still be active"))
                    logger.error("Composite monitoring cleanup timed out subscription_id=%s", subscription_id)
                except Exception as exc:
                    logger.exception("Composite monitoring cleanup failed subscription_id=%s", subscription_id)
                    result["errors"].append(_error("cleanup", f"Deletion failed; handle may still be active: {exc}"))
        if registered:
            result["monitoringActive"] = True
        return result

    async def history(self, options: SignalHistoryRequest) -> dict[str, Any]:
        context = await self.context(options.model_copy(update={"includeValues": False}))
        summaries: dict[str, Any] = {}
        for element_id in context["signalElementIds"]:
            payload = await self.query(
                "history",
                {
                    "elementId": element_id,
                    "startTime": options.startTime,
                    "endTime": options.endTime,
                    "maxPoints": options.maxPointsPerSignal,
                },
            )
            if payload.get("success") is not True:
                context["errors"].append(_error("history", json.dumps(payload), element_id))
                continue
            values = payload["values"]
            good = [item for item in values if item["quality"] == "Good" and item["value"] is not None]
            numeric = [
                item["value"] for item in good if type(item["value"]) in (int, float) and math.isfinite(item["value"])
            ]
            summaries[element_id] = {
                "sampleCount": len(values),
                "qualityCounts": dict(Counter(item["quality"] for item in values)),
                "goodSampleFraction": len(good) / len(values) if values else None,
                "first": values[0] if values else None,
                "last": values[-1] if values else None,
                "numericGoodSampleCount": len(numeric),
                "minimum": min(numeric) if numeric else None,
                "maximum": max(numeric) if numeric else None,
                "arithmeticMean": mean(numeric) if numeric else None,
                "truncated": payload["truncated"],
                "continuationSupported": False,
                "statisticsBasis": "Good finite numeric samples only; arithmetic, not time-weighted",
                "emptyMeaning": "No samples returned; does not prove downtime or lack of historian support",
            }
            if payload["truncated"]:
                context["coverage"]["truncated"] = True
                context["coverage"]["limitReasons"].append("historyPageOrPointLimit")
        context["history"] = summaries
        context["startTime"] = options.startTime.isoformat()
        context["endTime"] = options.endTime.isoformat()
        return context

    async def diagnose(self, options: DataAvailabilityRequest) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        for operation in ("connection", "status", "capabilities"):
            payload = await self.query(operation, {})
            evidence[operation] = payload
            if payload.get("success") is not True:
                errors.append(_error(operation, json.dumps(payload)))
        context = await self.context(options.model_copy(update={"includeValues": True}))
        findings: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for element_id, value in context["values"].items():
            if value["quality"] != "Good" or value["value"] is None:
                findings.append({"elementId": element_id, "kind": "quality", "evidence": value})
            raw_timestamp = value.get("timestamp")
            try:
                if not isinstance(raw_timestamp, str):
                    raise ValueError("Missing timestamp")
                timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("Missing timezone")
                age = (now - timestamp).total_seconds()
                if age < 0 or age > options.staleAfterSeconds:
                    findings.append(
                        {
                            "elementId": element_id,
                            "kind": "futureTimestamp" if age < 0 else "oldTimestamp",
                            "ageSeconds": age,
                            "timestamp": value["timestamp"],
                        }
                    )
            except ValueError:
                findings.append({"elementId": element_id, "kind": "invalidTimestamp", "evidence": raw_timestamp})
        context["errors"].extend(errors)
        context.update(
            {
                "diagnostics": evidence,
                "findings": findings,
                "interpretation": (
                    "Evidence only: an old source timestamp can mean an unchanged signal, not a failed connection."
                ),
            }
        )
        return context

    async def run(self, name: str, options: WorkflowOptions) -> dict[str, Any]:
        started = _now()
        try:
            result = await asyncio.wait_for(self._execute(name, options), timeout=options.timeoutSeconds)
        except TimeoutError:
            logger.warning("Composite workflow timed out name=%s", name)
            result = self.monitoring_result or {"errors": []}
            result["errors"].append(_error("timeout", "Workflow deadline exceeded; results are incomplete"))
        result.update(
            {
                "collectionStartedAt": started,
                "collectionCompletedAt": _now(),
                "atomicSnapshot": False,
                "modelRevision": self.model.revision,
            }
        )
        result["success"] = not result.get("errors")
        if len(json.dumps(result, default=str).encode("utf-8")) > MAX_OUTPUT_BYTES:
            return {
                "success": False,
                "errors": [_error("output", "Result exceeds 1 MiB; reduce scope or disable schemas/values")],
                "collectionStartedAt": started,
                "collectionCompletedAt": _now(),
                "coverage": {"truncated": True, "limitReasons": ["maxOutputBytes"]},
                "subscriptionId": result.get("subscriptionId"),
                "monitoringActive": result.get("monitoringActive"),
                "cleanup": result.get("cleanup"),
            }
        return result

    async def _execute(self, name: str, options: WorkflowOptions) -> dict[str, Any]:
        if name == "findAssets" and isinstance(options, AssetSearchRequest):
            return await self.search(options)
        if name == "startAssetMonitoring" and isinstance(options, AssetMonitoringRequest):
            return await self.monitor(options)
        if name == "summarizeSignalHistory" and isinstance(options, SignalHistoryRequest):
            return await self.history(options)
        if name == "diagnoseDataAvailability" and isinstance(options, DataAvailabilityRequest):
            return await self.diagnose(options)
        if isinstance(options, AssetContextRequest):
            if name == "getAssetSnapshot":
                options = options.model_copy(update={"includeValues": True})
            return await self.context(options)
        raise ValueError(f"Unsupported workflow {name}")
