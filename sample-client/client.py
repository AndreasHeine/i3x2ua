"""
Generic i3X discovery client example.

Demonstrates an efficient discovery pattern against any i3X server (such as
this project's OPC UA-backed implementation):

  1. GET  /v1/info                   - capability/health check
  2. GET  /v1/objects?root=true      - seed the tree, metadata inline
  3. POST /v1/objects/list           - batched breadth-first expansion via
                                        metadata.relationships (no per-node
                                        /objects/related round-trips)
  4. POST /v1/objecttypes/query      - batched, cached type/schema resolution
  5. POST /v1/objects/value          - batched value read, matched back to
                                        schema property names via displayName

Usage:
    python client.py --base-url http://127.0.0.1:8000

    # HTTPS with a self-signed cert behind an nginx basic-auth proxy:
    python client.py --base-url https://127.0.0.1:8443 --insecure \\
        --username admin --password pw1

Options:
    --base-url URL          i3X server base URL (default: http://127.0.0.1:8000)
    --max-nodes N            Cap on total discovered objects (default: 200)
    --max-depth N            Cap on BFS levels traversed from the roots (default:
                              unlimited). Use alongside --max-nodes on large
                              address spaces to bound both breadth and depth.
    --type-element-id ID     Skip the tree walk entirely and fetch only instances
                              of this typeElementId - the fast path when the type
                              of interest is already known on a huge address space.
    --no-values               Skip the final POST /objects/value pass (useful when
                              you only care about topology/types, not live data).
    --insecure                Disable TLS certificate verification (self-signed
                              dev certs).
    --username / --password  HTTP Basic Auth credentials, e.g. for an nginx
                              reverse proxy with NGINX_BASIC_AUTH_ENABLED=1.

Run the server first (see README), e.g.:
    python run.py
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any

_BATCH_SIZE = 50  # keep request bodies small and avoid server-side partial-content limits


class I3XRequestError(Exception):
    """Raised when an HTTP request to the i3X server fails."""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@dataclass
class DiscoveredObject:
    element_id: str
    display_name: str
    type_element_id: str
    is_composition: bool
    relationships: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None  # set during BFS expansion; None means "root so far"


class I3XClient:
    """Thin REST client for the i3X API with built-in batching and caching."""

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool = True,
        timeout: float = 10.0,
        username: str | None = None,
        password: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ssl_context = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        self._auth_header: str | None = None
        if username is not None:
            credentials = base64.b64encode(f"{username}:{password or ''}".encode("utf-8")).decode("ascii")
            self._auth_header = f"Basic {credentials}"
        # Caches: resolved once, reused for the lifetime of the client.
        self.objects_by_id: dict[str, DiscoveredObject] = {}
        self.object_types_by_id: dict[str, dict[str, Any]] = {}

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._auth_header is not None:
            headers["Authorization"] = self._auth_header

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise I3XRequestError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise I3XRequestError(f"{method} {path} -> {exc.reason}") from exc

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def get_info(self) -> dict[str, Any]:
        return self._get("/v1/info")

    def get_root_objects(self) -> list[dict[str, Any]]:
        payload = self._get("/v1/objects", params={"root": "true", "includeMetadata": "true"})
        return payload.get("result", [])

    def list_objects(self, element_ids: list[str]) -> list[dict[str, Any]]:
        """Batched lookup of Objects (with metadata) by elementId, chunked to _BATCH_SIZE."""
        results: list[dict[str, Any]] = []
        for chunk in _chunks(element_ids, _BATCH_SIZE):
            payload = self._post("/v1/objects/list", {"elementIds": chunk, "includeMetadata": True})
            for item in payload.get("results", []):
                if item.get("success"):
                    results.append(item["result"])
        return results

    def query_object_types(self, type_element_ids: list[str]) -> list[dict[str, Any]]:
        """Batched, cache-aware ObjectType/schema resolution."""
        missing = [tid for tid in type_element_ids if tid not in self.object_types_by_id]
        for chunk in _chunks(missing, _BATCH_SIZE):
            payload = self._post("/v1/objecttypes/query", {"elementIds": chunk})
            for item in payload.get("results", []):
                if item.get("success"):
                    self.object_types_by_id[item["elementId"]] = item["result"]
        return [self.object_types_by_id[tid] for tid in type_element_ids if tid in self.object_types_by_id]

    def get_values(self, element_ids: list[str], max_depth: int = 0) -> dict[str, dict[str, Any]]:
        """Batched current-value read; returns elementId -> CurrentValueResult."""
        values: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(element_ids, _BATCH_SIZE):
            payload = self._post("/v1/objects/value", {"elementIds": chunk, "maxDepth": max_depth})
            for item in payload.get("results", []):
                if item.get("success"):
                    values[item["elementId"]] = item["result"]
        return values

    def discover(self, max_nodes: int = 200, max_depth: int | None = None) -> None:
        """Breadth-first discovery seeded from root objects.

        Uses metadata.relationships (HasChildren/HasComponent) returned inline
        instead of calling /objects/related per node. Stops at whichever of
        max_nodes / max_depth is hit first - both matter on large address spaces.
        """
        roots = self.get_root_objects()
        queue: deque[str] = deque()
        for raw in roots:
            self._cache_object(raw)
            queue.append(raw["elementId"])

        depth = 0
        while queue and len(self.objects_by_id) < max_nodes:
            if max_depth is not None and depth >= max_depth:
                break
            depth += 1

            # Expand one whole BFS level per batch instead of one node at a time.
            level_ids = list(queue)
            queue.clear()

            parent_by_child_id: dict[str, str] = {}
            for element_id in level_ids:
                obj = self.objects_by_id[element_id]
                for relationship in ("HasChildren", "HasComponent"):
                    targets = obj.relationships.get(relationship) or []
                    if isinstance(targets, str):
                        targets = [targets]
                    for target_id in targets:
                        if target_id and target_id not in self.objects_by_id:
                            parent_by_child_id[target_id] = element_id

            next_ids = list(parent_by_child_id)
            if not next_ids:
                break

            # Respect max_nodes mid-level too, so huge levels don't overshoot the cap.
            remaining_budget = max_nodes - len(self.objects_by_id)
            if remaining_budget <= 0:
                break
            next_ids = next_ids[:remaining_budget]

            for raw in self.list_objects(next_ids):
                self._cache_object(raw)
                self.objects_by_id[raw["elementId"]].parent_id = parent_by_child_id.get(raw["elementId"])
                queue.append(raw["elementId"])

        # Resolve schemas for every type seen, once, batched.
        type_ids = sorted({obj.type_element_id for obj in self.objects_by_id.values()})
        self.query_object_types(type_ids)

    def discover_by_type(self, type_element_id: str) -> None:
        """Targeted discovery: fetch all instances of a known type directly.

        Skips the tree walk entirely - the most efficient path when the type
        of interest is already known on a large address space.
        """
        payload = self._get("/v1/objects", params={"typeElementId": type_element_id, "includeMetadata": "true"})
        for raw in payload.get("result", []):
            self._cache_object(raw)
        self.query_object_types([type_element_id])

    def _cache_object(self, raw: dict[str, Any]) -> None:
        metadata = raw.get("metadata") or {}
        self.objects_by_id[raw["elementId"]] = DiscoveredObject(
            element_id=raw["elementId"],
            display_name=raw["displayName"],
            type_element_id=raw["typeElementId"],
            is_composition=bool(raw.get("isComposition")),
            relationships=metadata.get("relationships") or {},
        )

    def print_tree(self) -> None:
        # O(n): parent_id is set during discover(), no need to rescan all objects per node.
        roots = [o for o in self.objects_by_id.values() if o.parent_id is None]
        for root in roots:
            self._print_node(root, indent=0)

    def _print_node(self, obj: DiscoveredObject, indent: int) -> None:
        object_type = self.object_types_by_id.get(obj.type_element_id, {})
        type_name = object_type.get("displayName", obj.type_element_id)
        marker = "[composition]" if obj.is_composition else ""
        print(f"{'  ' * indent}- {obj.display_name} ({type_name}) {marker}".rstrip())

        children = list(obj.relationships.get("HasChildren") or [])
        components = list(obj.relationships.get("HasComponent") or [])
        for child_id in children + components:
            child = self.objects_by_id.get(child_id)
            if child is not None:
                self._print_node(child, indent + 1)

    def print_values_for_composition_roots(self) -> None:
        """Match component values back to their schema property names."""
        composition_roots = [o for o in self.objects_by_id.values() if o.is_composition]
        if not composition_roots:
            return

        values = self.get_values([o.element_id for o in composition_roots], max_depth=0)

        # BFS discovery may have stopped at --max-nodes before reaching some
        # component elementIds; resolve any we haven't cached yet, in one batch.
        missing_ids = sorted(
            {
                component_id
                for result in values.values()
                for component_id in (result.get("components") or {})
                if component_id not in self.objects_by_id
            }
        )
        for raw in self.list_objects(missing_ids):
            self._cache_object(raw)

        for obj in composition_roots:
            result = values.get(obj.element_id)
            if not result:
                continue
            components = result.get("components") or {}
            if not components:
                continue

            schema = self.object_types_by_id.get(obj.type_element_id, {}).get("schema", {})
            properties = schema.get("properties", {})
            print(f"\nValues for '{obj.display_name}' ({obj.type_element_id}):")
            for component_id, vqt in components.items():
                component = self.objects_by_id.get(component_id)
                property_name = component.display_name if component else component_id
                in_schema = " (declared in schema)" if property_name in properties else " (extended)"
                print(f"  {property_name}{in_schema} = {vqt['value']!r} [{vqt['quality']}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic i3X discovery client example")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="i3X server base URL")
    parser.add_argument("--max-nodes", type=int, default=200, help="Cap on discovered objects")
    parser.add_argument("--max-depth", type=int, default=None, help="Cap on BFS levels traversed from the roots")
    parser.add_argument(
        "--type-element-id",
        default=None,
        help="Skip the tree walk; fetch only instances of this typeElementId (fast path for huge address spaces)",
    )
    parser.add_argument("--no-values", action="store_true", help="Skip reading/printing current values")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--username", default=None, help="HTTP Basic Auth username (e.g. nginx proxy auth)")
    parser.add_argument("--password", default=None, help="HTTP Basic Auth password")
    args = parser.parse_args()

    client = I3XClient(
        args.base_url,
        verify_ssl=not args.insecure,
        username=args.username,
        password=args.password,
    )

    try:
        info = client.get_info()
        print(f"Connected to {info.get('serverName', 'i3X server')} (spec {info.get('specVersion')})")

        if args.type_element_id:
            client.discover_by_type(args.type_element_id)
        else:
            client.discover(max_nodes=args.max_nodes, max_depth=args.max_depth)
        print(f"\nDiscovered {len(client.objects_by_id)} object(s), {len(client.object_types_by_id)} type(s)\n")

        if args.type_element_id:
            for obj in client.objects_by_id.values():
                print(f"- {obj.display_name} ({obj.element_id})")
        else:
            client.print_tree()

        if not args.no_values:
            client.print_values_for_composition_roots()
    except I3XRequestError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
