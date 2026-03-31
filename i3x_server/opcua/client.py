from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from asyncua import Client, ua
from asyncua.ua import NodeClass

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OpcUaNodeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    node_class: str
    data_type: str | None
    event_notifier: bool


@dataclass(slots=True)
class OpcUaObjectTypeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str


@dataclass(slots=True)
class OpcUaNamespaceInfo:
    uri: str
    display_name: str


@dataclass(slots=True)
class OpcUaOperationalLimits:
    max_nodes_per_browse: int | None
    max_nodes_per_read: int | None


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        ...

    async def get_namespaces(self) -> list[str]:
        ...

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        ...

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        ...

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        ...

    async def read_value(self, node_id: str) -> Any:
        ...

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        ...

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        ...


class OpcUaClient:
    def __init__(
        self,
        endpoint: str,
        browse_concurrency: int = 16,
        metadata_cache_ttl_seconds: int = 300,
    ) -> None:
        self._endpoint = endpoint
        self._browse_concurrency = max(1, browse_concurrency)
        self._metadata_cache_ttl_seconds = max(0, metadata_cache_ttl_seconds)
        self._client = Client(url=endpoint)
        self._limits_cache: OpcUaOperationalLimits | None = None
        self._namespace_infos_cache: tuple[float, list[OpcUaNamespaceInfo]] | None = None
        self._object_types_cache: tuple[float, list[OpcUaObjectTypeInfo]] | None = None

    async def connect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA connect started endpoint=%s", self._endpoint)
        await self._client.connect()
        self._limits_cache = await self.get_operational_limits()
        logger.info(
            "OPC UA limits endpoint=%s max_nodes_per_browse=%s max_nodes_per_read=%s",
            self._endpoint,
            self._limits_cache.max_nodes_per_browse,
            self._limits_cache.max_nodes_per_read,
        )
        logger.info("OPC UA connect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

    async def disconnect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA disconnect started endpoint=%s", self._endpoint)
        await self._client.disconnect()
        self._namespace_infos_cache = None
        self._object_types_cache = None
        logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or 128
        logger.info(
            "OPC UA browse started endpoint=%s browse_concurrency=%d max_nodes_per_browse=%d",
            self._endpoint,
            self._browse_concurrency,
            max_nodes_per_browse,
        )
        root = self._client.nodes.objects
        output: list[OpcUaNodeInfo] = []
        visited: set[str] = set()
        stack: list[tuple[Any, str | None]] = [(root, None)]

        while stack:
            batch_entries = stack[:max_nodes_per_browse]
            stack = stack[max_nodes_per_browse:]

            filtered_entries: list[tuple[Any, str | None]] = []
            for node, parent_node_id in batch_entries:
                node_id = node.nodeid.to_string()
                if node_id in visited:
                    continue
                visited.add(node_id)
                filtered_entries.append((node, parent_node_id))

            if not filtered_entries:
                continue

            node_infos = await asyncio.gather(
                *[
                    self._read_node_info(node=node, parent_node_id=parent_node_id)
                    for node, parent_node_id in filtered_entries
                ]
            )
            output.extend(node_infos)

            nodes = [node for node, _ in filtered_entries]
            browsed = await self._browse_children_descriptions(nodes, max_nodes_per_browse)
            for parent_node, refs in browsed:
                parent_node_id = parent_node.nodeid.to_string()
                for ref in refs:
                    stack.append((self._client.get_node(ref.NodeId), parent_node_id))

        logger.info(
            "OPC UA browse finished endpoint=%s node_count=%d duration_s=%.3f",
            self._endpoint,
            len(output),
            perf_counter() - started,
        )
        return output

    async def _read_node_info(self, node: Any, parent_node_id: str | None) -> OpcUaNodeInfo:
        node_id = node.nodeid.to_string()
        browse_name_obj, display_name_obj, node_class_obj = await asyncio.gather(
            node.read_browse_name(),
            node.read_display_name(),
            node.read_node_class(),
        )

        data_type: str | None = None
        if node_class_obj == NodeClass.Variable:
            data_type_obj = await node.read_data_type()
            data_type = data_type_obj.to_string()

        event_notifier = bool(await node.read_event_notifier()) if node_class_obj == NodeClass.Object else False

        return OpcUaNodeInfo(
            node_id=node_id,
            parent_node_id=parent_node_id,
            browse_name=browse_name_obj.Name,
            display_name=display_name_obj.Text,
            node_class=node_class_obj.name,
            data_type=data_type,
            event_notifier=event_notifier,
        )

    async def get_operational_limits(self) -> OpcUaOperationalLimits:
        if self._limits_cache is not None:
            return self._limits_cache

        started = perf_counter()
        max_nodes_per_browse: int | None = None
        max_nodes_per_read: int | None = None

        try:
            operational_limits_node = self._client.get_node("i=11704")
            children = await operational_limits_node.get_children()
            for child in children:
                browse_name_obj = await child.read_browse_name()
                browse_name = browse_name_obj.Name
                if browse_name not in {"MaxNodesPerBrowse", "MaxNodesPerRead"}:
                    continue

                value = await child.read_value()
                if isinstance(value, int) and value > 0:
                    if browse_name == "MaxNodesPerBrowse":
                        max_nodes_per_browse = value
                    if browse_name == "MaxNodesPerRead":
                        max_nodes_per_read = value
        except Exception:
            logger.warning(
                "OPC UA operational limits read failed endpoint=%s; using internal defaults",
                self._endpoint,
                exc_info=True,
            )

        limits = OpcUaOperationalLimits(
            max_nodes_per_browse=max_nodes_per_browse,
            max_nodes_per_read=max_nodes_per_read,
        )
        self._limits_cache = limits
        logger.info(
            "OPC UA operational limits resolved endpoint=%s "
            "max_nodes_per_browse=%s max_nodes_per_read=%s duration_s=%.3f",
            self._endpoint,
            limits.max_nodes_per_browse,
            limits.max_nodes_per_read,
            perf_counter() - started,
        )
        return limits

    async def get_namespaces(self) -> list[str]:
        started = perf_counter()
        try:
            raw = await self._client.nodes.namespace_array.read_value()
            namespaces = [str(item) for item in raw] if isinstance(raw, list) else []
            logger.info(
                "OPC UA namespaces read ok endpoint=%s count=%d duration_s=%.3f",
                self._endpoint,
                len(namespaces),
                perf_counter() - started,
            )
            return namespaces
        except Exception:
            logger.exception(
                "OPC UA namespaces read failed endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )
            raise

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        now = perf_counter()
        if self._namespace_infos_cache is not None:
            cached_at, cached_value = self._namespace_infos_cache
            if self._metadata_cache_ttl_seconds == 0 or now - cached_at <= self._metadata_cache_ttl_seconds:
                logger.debug(
                    "OPC UA namespace info cache hit endpoint=%s age_s=%.3f",
                    self._endpoint,
                    now - cached_at,
                )
                return cached_value

        started = perf_counter()
        uris = await self.get_namespaces()
        display_by_uri: dict[str, str] = {}

        try:
            namespaces_node = self._client.get_node("i=11715")
            namespace_components = await namespaces_node.get_children()

            for component in namespace_components:
                component_display = (await component.read_display_name()).Text
                component_children = await component.get_children()

                for child in component_children:
                    browse_name_obj = await child.read_browse_name()
                    if browse_name_obj.Name != "NamespaceUri":
                        continue
                    uri_value = await child.read_value()
                    uri = str(uri_value)
                    if uri:
                        display_by_uri[uri] = component_display or uri
                    break
        except Exception:
            logger.warning(
                "OPC UA namespace metadata read from i=11715 failed endpoint=%s; using fallback names",
                self._endpoint,
                exc_info=True,
            )

        infos = [OpcUaNamespaceInfo(uri=uri, display_name=display_by_uri.get(uri, "")) for uri in uris]
        self._namespace_infos_cache = (perf_counter(), infos)
        logger.info(
            "OPC UA namespace infos built endpoint=%s count=%d duration_s=%.3f",
            self._endpoint,
            len(infos),
            perf_counter() - started,
        )
        return infos

    async def get_object_types(self) -> list[OpcUaObjectTypeInfo]:
        now = perf_counter()
        if self._object_types_cache is not None:
            cached_at, cached_value = self._object_types_cache
            if self._metadata_cache_ttl_seconds == 0 or now - cached_at <= self._metadata_cache_ttl_seconds:
                logger.debug(
                    "OPC UA object types cache hit endpoint=%s age_s=%.3f",
                    self._endpoint,
                    now - cached_at,
                )
                return cached_value

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes_per_browse = limits.max_nodes_per_browse or 128

        root = self._client.nodes.object_types
        output: list[OpcUaObjectTypeInfo] = []
        stack: list[tuple[Any, str | None]] = [(root, None)]
        visited: set[str] = set()

        try:
            while stack:
                batch_entries = stack[:max_nodes_per_browse]
                stack = stack[max_nodes_per_browse:]

                nodes = [node for node, _ in batch_entries]

                browsed = await self._browse_children_descriptions(nodes, max_nodes_per_browse)
                for parent_node, refs in browsed:
                    parent_node_id = parent_node.nodeid.to_string()
                    for ref in refs:
                        child_node_id = ref.NodeId.to_string()
                        if child_node_id in visited:
                            continue
                        visited.add(child_node_id)

                        child_node = self._client.get_node(ref.NodeId)
                        stack.append((child_node, parent_node_id))

                        if ref.NodeClass != NodeClass.ObjectType:
                            continue

                        browse_name = ref.BrowseName.Name
                        display_name = ref.DisplayName.Text or browse_name
                        output.append(
                            OpcUaObjectTypeInfo(
                                node_id=child_node_id,
                                parent_node_id=parent_node_id,
                                browse_name=browse_name,
                                display_name=display_name,
                            )
                        )

            logger.info(
                "OPC UA object types read ok endpoint=%s count=%d duration_s=%.3f",
                self._endpoint,
                len(output),
                perf_counter() - started,
            )
            self._object_types_cache = (perf_counter(), output)
            return output
        except Exception:
            logger.exception(
                "OPC UA object types read failed endpoint=%s duration_s=%.3f",
                self._endpoint,
                perf_counter() - started,
            )
            raise

    async def _browse_children_descriptions(
        self,
        nodes: list[Any],
        max_nodes_per_browse: int,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        if not nodes:
            return []

        results: list[tuple[Any, list[ua.ReferenceDescription]]] = []
        for node_batch in _chunked_nodes(nodes, max(1, max_nodes_per_browse)):
            browse_descriptions: list[ua.BrowseDescription] = []
            for node in node_batch:
                desc = ua.BrowseDescription()
                desc.NodeId = node.nodeid
                desc.BrowseDirection = ua.BrowseDirection.Forward
                desc.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HierarchicalReferences)
                desc.IncludeSubtypes = True
                desc.NodeClassMask = ua.NodeClass.Unspecified
                desc.ResultMask = ua.BrowseResultMask.All
                browse_descriptions.append(desc)

            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 0
            params.NodesToBrowse = browse_descriptions

            logger.info(
                "OPC UA browse batch endpoint=%s nodes_to_browse=%d",
                self._endpoint,
                len(node_batch),
            )
            browse_results = await self._client.uaclient.browse(params)
            initial_references_total = sum(len(result.References) for result in browse_results)
            logger.info(
                "OPC UA browse batch result endpoint=%s nodes=%d initial_references=%d",
                self._endpoint,
                len(node_batch),
                initial_references_total,
            )

            for node, browse_result in zip(node_batch, browse_results, strict=True):
                refs = list(browse_result.References)
                continuation_point = browse_result.ContinuationPoint
                browse_next_calls = 0
                browse_next_references_total = 0

                while continuation_point:
                    next_params = ua.BrowseNextParameters()
                    next_params.ReleaseContinuationPoints = False
                    next_params.ContinuationPoints = [continuation_point]
                    next_results = await self._client.uaclient.browse_next(next_params)
                    browse_next_calls += 1
                    if not next_results:
                        break
                    next_result = next_results[0]
                    refs.extend(next_result.References)
                    browse_next_references_total += len(next_result.References)
                    continuation_point = next_result.ContinuationPoint

                if browse_next_calls > 0:
                    logger.info(
                        "OPC UA browse-next endpoint=%s node_id=%s calls=%d "
                        "additional_references=%d total_references=%d",
                        self._endpoint,
                        node.nodeid.to_string(),
                        browse_next_calls,
                        browse_next_references_total,
                        len(refs),
                    )

                results.append((node, refs))

        return results

    async def read_value(self, node_id: str) -> Any:
        started = perf_counter()
        node = self._client.get_node(node_id)
        try:
            value = await node.read_value()
            logger.debug("OPC UA read ok node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            return value
        except Exception:
            logger.exception("OPC UA read failed node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            raise

    async def read_values(self, node_ids: list[str]) -> list[Any]:
        if not node_ids:
            return []

        started = perf_counter()
        limits = await self.get_operational_limits()
        max_nodes = limits.max_nodes_per_read or len(node_ids)
        batch_size = max(1, min(max_nodes, len(node_ids)))

        values: list[Any] = []
        for batch in _chunked(node_ids, batch_size):
            nodes = [self._client.get_node(node_id) for node_id in batch]
            batch_values = await self._client.read_values(nodes)
            values.extend(batch_values)

        logger.info(
            "OPC UA batch read ok endpoint=%s requested=%d batch_size=%d duration_s=%.3f",
            self._endpoint,
            len(node_ids),
            batch_size,
            perf_counter() - started,
        )
        return values

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        started = perf_counter()
        object_node = self._client.get_node(object_node_id)
        try:
            result = await object_node.call_method(method_node_id, *args)
            logger.info(
                "OPC UA method ok object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            return result
        except Exception:
            logger.exception(
                "OPC UA method failed object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            raise


def _chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]


def _chunked_nodes(values: list[Any], size: int) -> list[list[Any]]:
    return [values[idx : idx + size] for idx in range(0, len(values), size)]
