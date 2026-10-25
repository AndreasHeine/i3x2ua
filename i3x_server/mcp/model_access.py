from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from i3x_server.domain.utils import expanded_node_id
from i3x_server.errors import i3x_http_error
from i3x_server.model.revision import model_revision
from i3x_server.schemas.state import BuildResult, _ModelAccessIndex

if TYPE_CHECKING:
    from i3x_server.api.v1.objecttype_helpers import _ObjectTypeContext

_ID = {"type": "string", "minLength": 1, "pattern": r"\S"}
_PAGE = {
    "offset": {"type": "integer", "minimum": 0, "default": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
}
_ROLES = ["asset", "group", "component", "datapoint", "property", "unknown"]


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


MCP_MODEL_ACCESS_SCHEMAS = {
    "resolveBrowsePath": {
        **_schema(
            {
                "path": {**_ID, "description": "Exact slash-separated cached display names, including the root."},
                "segments": {"type": "array", "items": _ID, "minItems": 1},
            },
            [],
        ),
        "oneOf": [{"required": ["path"]}, {"required": ["segments"]}],
    },
    "getBrowsePath": _schema({"elementId": _ID}, ["elementId"]),
    "getInstancesOfType": {
        **_schema(
            {
                "typeElementId": _ID,
                "typeContextRevision": _ID,
                "includeSubtypes": {"type": "boolean", "default": True},
                **_PAGE,
            },
            ["typeElementId"],
        ),
        "allOf": [
            {
                "if": {"properties": {"offset": {"type": "integer", "minimum": 1}}, "required": ["offset"]},
                "then": {"required": ["typeContextRevision"]},
            }
        ],
    },
    "getChildren": _schema(
        {
            "elementId": _ID,
            "plane": {"type": "string", "enum": ["hierarchy", "composition", "graph"], "default": "hierarchy"},
            **_PAGE,
        },
        ["elementId"],
    ),
    "getCompositionProperties": _schema(
        {
            "elementIds": {"type": "array", "items": _ID, "minItems": 1, "maxItems": 50},
            **_PAGE,
        },
        ["elementIds"],
    ),
    "findElementsByName": _schema({"text": _ID, **_PAGE}, ["text"]),
    "findElementsByNamespace": _schema({"namespaceUri": _ID, **_PAGE}, ["namespaceUri"]),
    "findElementsByRole": _schema({"semanticRole": {"type": "string", "enum": _ROLES}, **_PAGE}, ["semanticRole"]),
}
_VALIDATORS = {name: Draft202012Validator(schema) for name, schema in MCP_MODEL_ACCESS_SCHEMAS.items()}


def validate_model_access_arguments(tool_name: str, arguments: Mapping[str, Any]) -> None:
    error = next(_VALIDATORS[tool_name].iter_errors(arguments), None)
    if error is not None:
        raise i3x_http_error(400, "Bad Request", error.message)
    for name in ("offset", "limit"):
        if name in arguments and type(arguments[name]) is not int:
            raise i3x_http_error(400, "Bad Request", f"{name} must be an integer")
    if tool_name == "resolveBrowsePath" and "path" in arguments:
        if any(not segment.strip() for segment in arguments["path"].split("/")):
            raise i3x_http_error(400, "Bad Request", "path must contain non-empty slash-separated segments")


def _sorted_ids(model: BuildResult, ids: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(ids), key=lambda element_id: (model.nodes_by_id[element_id].name.casefold(), element_id)))


def _index(model: BuildResult) -> _ModelAccessIndex:
    revision = model_revision(model)
    index = model._model_access_index_cache
    if index is not None and index.revision == revision:
        return index
    index = _ModelAccessIndex(revision, _sorted_ids(model, model.nodes_by_id))
    for element_id in index.ordered_ids:
        node = model.nodes_by_id[element_id]
        namespace = model.namespace_uri_by_id.get(element_id) or node.metadata.get("namespaceUri")
        if isinstance(namespace, str):
            index.namespaces.setdefault(namespace, []).append(element_id)
        role = model.semantic_role_by_id.get(element_id, node.semantic_role)
        index.roles.setdefault(role, []).append(element_id)
        name = node.name.casefold()
        for gram in {name[i : i + 3] for i in range(len(name) - 2)}:
            index.name_grams.setdefault(gram, set()).add(element_id)
    for plane, children_by_id in (
        ("hierarchy", model.hierarchy_children_by_id),
        ("composition", model.composition_children_by_id),
    ):
        index.children_by_plane[plane] = {
            parent: _sorted_ids(model, (child for child in children if child in model.nodes_by_id))
            for parent, children in children_by_id.items()
        }
    index.children_by_plane["graph"] = {
        parent: _sorted_ids(model, (target for _, target in edges if target in model.nodes_by_id))
        for parent, edges in model.graph_related_by_id.items()
    }
    model._model_access_index_cache = index
    return index


def _require_element(model: BuildResult, element_id: str) -> None:
    if element_id not in model.nodes_by_id:
        raise i3x_http_error(404, "Not Found", f"Model element not found: {element_id}")


def _parent(model: BuildResult, element_id: str) -> str | None:
    return (
        model.hierarchy_parent_by_id.get(element_id)
        or model.parent_id_by_id.get(element_id)
        or model.composition_parent_by_id.get(element_id)
        or model.nodes_by_id[element_id].parent_id
    )


def _validate_parent_chains(model: BuildResult, parents: Mapping[str, str | None]) -> None:
    validated: set[str] = set()
    for element_id in model.nodes_by_id:
        chain: list[str] = []
        visiting: set[str] = set()
        current: str | None = element_id
        while current is not None and current not in validated:
            if current not in model.nodes_by_id or current in visiting:
                raise i3x_http_error(500, "Invalid Model", "Canonical parent chain contains a cycle or missing element")
            visiting.add(current)
            chain.append(current)
            current = parents[current]
        validated.update(chain)


def _segments(model: BuildResult, element_id: str, known: dict[str, tuple[str, ...]] | None = None) -> tuple[str, ...]:
    chain: list[str] = []
    visited: set[str] = set()
    current: str | None = element_id
    while current is not None and (known is None or current not in known):
        if current in visited or current not in model.nodes_by_id:
            raise i3x_http_error(500, "Invalid Model", "Canonical parent chain contains a cycle or missing element")
        visited.add(current)
        chain.append(current)
        current = _parent(model, current)
    if known is None:
        return tuple(model.nodes_by_id[child].name for child in reversed(chain))
    segments = known[current] if current is not None else ()
    for child in reversed(chain):
        segments = (*segments, model.nodes_by_id[child].name)
        known[child] = segments
    return segments


def _page(model: BuildResult, ids: Iterable[str], arguments: Mapping[str, Any]) -> dict[str, Any]:
    ordered = ids if isinstance(ids, (list, tuple)) else _sorted_ids(model, ids)
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 50)
    page = ordered[offset : offset + limit]
    return {
        "total": len(ordered),
        "returned": len(page),
        "offset": offset,
        **({"nextOffset": offset + len(page)} if offset + len(page) < len(ordered) else {}),
        "items": [[element_id, model.nodes_by_id[element_id].name] for element_id in page],
    }


def _type_indexes(model: BuildResult, index: _ModelAccessIndex, context: _ObjectTypeContext) -> None:
    if index.type_context_token is context:
        return
    aliases = {item.elementId: item.elementId for item in context.items}
    aliases.update({item.sourceTypeId: item.elementId for item in context.items})
    aliases.update(context.element_ids_by_node_id)
    aliases.update(context.source_type_to_element_id)

    def type_id(source: str) -> str | None:
        resolved = aliases.get(source) or aliases.get(source.lower())
        if resolved is not None:
            return resolved
        expanded = expanded_node_id(source, context.namespace_infos)
        return aliases.get(expanded) or aliases.get(expanded.lower())

    parents: dict[str, str] = {}
    for info in context.object_types:
        child = type_id(info.node_id)
        parent = type_id(info.parent_node_id) if info.parent_node_id else None
        if child is not None and parent is not None:
            parents[child] = parent
    direct: dict[str, set[str]] = {item.elementId: set() for item in context.items}
    for source, instances in model.instances_by_type_id.items():
        resolved = type_id(source)
        if resolved is not None and resolved in direct:
            direct[resolved].update(
                element_id
                for element_id in instances
                if element_id in model.nodes_by_id and model.nodes_by_id[element_id].kind in {"asset", "eventSource"}
            )
    for node in model.nodes_by_id.values():
        if node.kind not in {"asset", "eventSource"}:
            continue
        resolved = type_id(node.source_type_id or "") or type_id(node.type or "")
        if resolved is not None and resolved in direct:
            direct[resolved].add(node.id)
    expanded = {element_id: set(instances) for element_id, instances in direct.items()}
    for element_id, instance_ids in direct.items():
        visited = {element_id}
        parent = parents.get(element_id)
        while parent is not None:
            if parent in visited:
                raise i3x_http_error(500, "Invalid Model", "Object type hierarchy contains a cycle")
            visited.add(parent)
            expanded.setdefault(parent, set()).update(instance_ids)
            parent = parents.get(parent)
    index.direct_instances = {key: _sorted_ids(model, ids) for key, ids in direct.items()}
    index.subtype_instances = {key: _sorted_ids(model, ids) for key, ids in expanded.items()}
    index.type_context_token = context


def invoke_model_access_tool(
    model: BuildResult,
    tool_name: str,
    arguments: Mapping[str, Any],
    context: _ObjectTypeContext | None = None,
) -> dict[str, Any]:
    revision = model_revision(model)
    if tool_name == "getBrowsePath":
        element_id = arguments["elementId"]
        _require_element(model, element_id)
        segments = _segments(model, element_id)
        return {"modelRevision": revision, "path": "/".join(segments), "segments": list(segments)}

    index = _index(model)
    result: dict[str, Any]
    if tool_name == "resolveBrowsePath":
        if index.path_children is None:
            path_children: dict[str | None, dict[str, list[str]]] = {}
            parent_by_id: dict[str, str | None] = {}
            for element_id in index.ordered_ids:
                parent = _parent(model, element_id)
                parent_by_id[element_id] = parent
                path_children.setdefault(parent, {}).setdefault(model.nodes_by_id[element_id].name, []).append(
                    element_id
                )
            _validate_parent_chains(model, parent_by_id)
            index.path_children = {
                parent: {name: tuple(children) for name, children in children_by_name.items()}
                for parent, children_by_name in path_children.items()
            }
        segments = tuple(arguments["segments"]) if "segments" in arguments else tuple(arguments["path"].split("/"))
        parent_candidates: tuple[str | None, ...] = (None,)
        matches: list[str] = []
        for segment in segments:
            matches = [
                child for parent in parent_candidates for child in index.path_children.get(parent, {}).get(segment, ())
            ]
            if not matches:
                break
            parent_candidates = tuple(matches)
        if len(matches) > 1:
            raise i3x_http_error(409, "Ambiguous Path", "Browse path matches multiple elements; use an elementId")
        if not matches:
            result = {"found": False}
        else:
            node = model.nodes_by_id[matches[0]]
            result = {"found": True, "elementId": node.id, "displayName": node.name, "kind": node.kind}
    elif tool_name == "getInstancesOfType":
        if context is None:
            raise i3x_http_error(
                503, "Service Unavailable", "Revision-matched cached object type context is unavailable"
            )
        offset = arguments.get("offset", 0)
        requested_context_revision = arguments.get("typeContextRevision")
        if offset > 0 and requested_context_revision is None:
            raise i3x_http_error(400, "Bad Request", "typeContextRevision is required when offset is greater than 0")
        if requested_context_revision is not None and requested_context_revision != context.revision:
            raise i3x_http_error(409, "Conflict", "Object type context changed; restart pagination from offset 0")
        _type_indexes(model, index, context)
        element_id = arguments["typeElementId"]
        if element_id not in index.direct_instances:
            raise i3x_http_error(404, "Not Found", f"Object type not found: {element_id}")
        instances = index.subtype_instances if arguments.get("includeSubtypes", True) else index.direct_instances
        result = _page(model, instances[element_id], arguments)
        result["typeContextRevision"] = context.revision
    elif tool_name == "getChildren":
        element_id = arguments["elementId"]
        _require_element(model, element_id)
        plane = arguments.get("plane", "hierarchy")
        result = _page(model, index.children_by_plane[plane].get(element_id, ()), arguments)
    elif tool_name == "getCompositionProperties":
        roots = arguments["elementIds"]
        for root in roots:
            _require_element(model, root)
            if model.nodes_by_id[root].kind == "property":
                raise i3x_http_error(400, "Bad Request", "elementIds must identify objects, not properties")
        items = []
        for root in _sorted_ids(model, roots):
            visited = {root}
            pending = list(model.composition_children_by_id.get(root, []))
            properties: set[str] = set()
            while pending:
                element_id = pending.pop()
                if element_id in visited or element_id not in model.nodes_by_id:
                    continue
                visited.add(element_id)
                if model.nodes_by_id[element_id].kind == "property":
                    properties.add(element_id)
                pending.extend(model.composition_children_by_id.get(element_id, []))
            page = _page(model, properties, arguments)
            items.append(
                {
                    "elementId": root,
                    **{key: value for key, value in page.items() if key != "items"},
                    "properties": page["items"],
                }
            )
        result = {"items": items}
    elif tool_name == "findElementsByName":
        text = arguments["text"].casefold()
        grams = {text[i : i + 3] for i in range(len(text) - 2)}
        candidates: Iterable[str] = index.ordered_ids
        if grams:
            buckets = sorted((index.name_grams.get(gram, set()) for gram in grams), key=len)
            candidates = buckets[0].intersection(*buckets[1:])
        name_matches = _sorted_ids(model, (key for key in candidates if text in model.nodes_by_id[key].name.casefold()))
        result = _page(model, name_matches, arguments)
    elif tool_name == "findElementsByNamespace":
        result = _page(model, index.namespaces.get(arguments["namespaceUri"], []), arguments)
    elif tool_name == "findElementsByRole":
        result = _page(model, index.roles.get(arguments["semanticRole"], []), arguments)
    else:
        raise i3x_http_error(400, "Bad Request", f"Unknown cached model access tool: {tool_name}")
    return {"modelRevision": revision, **result}
