from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any

from i3x_server.domain.utils import canonical_node_id
from i3x_server.errors import i3x_http_error
from i3x_server.mcp.model_access import MCP_MODEL_ACCESS_SCHEMAS, validate_model_access_arguments
from i3x_server.model.revision import model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult

__all__ = [
    "MCP_MODEL_CONTEXT_MAX_ITEMS",
    "MCP_MODEL_PATH_MAX_NODES",
    "MCP_MODEL_TOOL_SCHEMAS",
    "_get_element_context",
    "_resolve_element_ids_to_node_ids",
    "_resolve_node_ids_to_element_ids",
    "_search_model_elements",
    "_summarize_subtree",
    "_trace_relationship_path",
    "_validate_model_tool_arguments",
]

MCP_MODEL_CONTEXT_MAX_ITEMS = 500
MCP_MODEL_PATH_MAX_NODES = 5000
MCP_ID_RESOLUTION_MAX_ITEMS = 500
MCP_MODEL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    **MCP_MODEL_ACCESS_SCHEMAS,
    "resolveElementIdsToNodeIds": {
        "type": "object",
        "properties": {
            "elementIds": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MCP_ID_RESOLUTION_MAX_ITEMS,
            }
        },
        "required": ["elementIds"],
        "additionalProperties": False,
    },
    "resolveNodeIdsToElementIds": {
        "type": "object",
        "properties": {
            "nodeIds": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MCP_ID_RESOLUTION_MAX_ITEMS,
            }
        },
        "required": ["nodeIds"],
        "additionalProperties": False,
    },
    "searchModelElements": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Case-insensitive substring across cached names and metadata. "
                    "When omitted, returns the first page of the model."
                ),
            },
            "kind": {"type": "string", "enum": ["asset", "property", "action", "eventSource"]},
            "semanticRole": {
                "type": "string",
                "enum": ["asset", "group", "component", "datapoint", "property", "unknown"],
            },
            "namespaceUri": {"type": "string"},
            "typeElementId": {"type": "string"},
            "mappingConfidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "appliedProfileId": {"type": "string"},
            "relationshipType": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "additionalProperties": False,
    },
    "getElementContext": {
        "type": "object",
        "properties": {
            "elementId": {"type": "string", "minLength": 1},
            "ancestorDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 3},
            "childLimit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "relationshipLimit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "includeChildren": {"type": "boolean", "default": True},
            "includeRelationships": {"type": "boolean", "default": True},
            "includeTypeAndMetadata": {"type": "boolean", "default": True},
        },
        "required": ["elementId"],
        "additionalProperties": False,
    },
    "summarizeSubtree": {
        "type": "object",
        "properties": {
            "rootElementId": {"type": "string", "minLength": 1},
            "maxDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 4},
            "includeHierarchy": {"type": "boolean", "default": True},
            "includeComposition": {"type": "boolean", "default": True},
            "includeProperties": {"type": "boolean", "default": True},
            "includeActions": {"type": "boolean", "default": True},
            "maxNodes": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        },
        "required": ["rootElementId"],
        "additionalProperties": False,
    },
    "traceRelationshipPath": {
        "type": "object",
        "properties": {
            "sourceElementId": {"type": "string", "minLength": 1},
            "targetElementId": {"type": "string", "minLength": 1},
            "relationshipPlanes": {
                "type": "array",
                "items": {"type": "string", "enum": ["hierarchy", "composition", "graph"]},
                "default": ["hierarchy", "composition", "graph"],
            },
            "direction": {"type": "string", "enum": ["outgoing", "incoming", "either"], "default": "either"},
            "maxDepth": {"type": "integer", "minimum": 0, "maximum": 32, "default": 8},
        },
        "required": ["sourceElementId", "targetElementId"],
        "additionalProperties": False,
    },
}


def _model_node_summary(model: BuildResult, node: ModelNode) -> dict[str, Any]:
    profile_ids = model.applied_profile_ids_by_id.get(
        node.id,
        node.metadata.get("appliedProfileIds", []),
    )
    if not isinstance(profile_ids, list):
        profile_ids = []
    summary = {
        "elementId": node.id,
        "displayName": node.name,
        "kind": node.kind,
        "semanticRole": model.semantic_role_by_id.get(node.id, node.semantic_role),
        "mappingConfidence": model.mapping_confidence_by_id.get(node.id, node.mapping_confidence),
        "typeElementId": node.type or node.source_type_id or node.kind,
        "namespaceUri": model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri"),
        "appliedProfileIds": profile_ids,
        "parentId": node.parent_id,
    }
    writable = node.metadata.get("writable")
    if isinstance(writable, bool):
        summary["writable"] = writable
    user_executable = node.metadata.get("userExecutable")
    if isinstance(user_executable, bool):
        summary["executable"] = user_executable
    return summary


def _resolve_element_ids_to_node_ids(model: BuildResult, element_ids: list[Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for element_id in element_ids:
        if not isinstance(element_id, str) or not element_id.strip():
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 400, "message": "ElementId must be a non-empty string"},
                }
            )
            continue
        node = model.nodes_by_id.get(element_id)
        if node is None:
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 404, "message": "ElementId not found"},
                }
            )
            continue
        node_id = node.metadata.get("uaNodeId")
        expanded_id = node.metadata.get("expandedNodeId")
        if not isinstance(node_id, str) or not node_id:
            items.append(
                {
                    "success": False,
                    "elementId": element_id,
                    "error": {"code": 404, "message": "OPC UA NodeId is not available in the cached model"},
                }
            )
            continue
        result: dict[str, Any] = {
            "success": True,
            "elementId": node.id,
            "nodeId": node_id,
            "name": node.name,
            "kind": node.kind,
        }
        if isinstance(expanded_id, str) and expanded_id.startswith("nsu="):
            result["expandedNodeId"] = expanded_id
        items.append(result)
    return {"success": all(item["success"] for item in items), "items": items}


def _resolve_node_ids_to_element_ids(model: BuildResult, node_ids: list[Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for node_id in node_ids:
        if not isinstance(node_id, str) or not node_id.strip():
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 400, "message": "NodeId must be a non-empty string"},
                }
            )
            continue
        canonical_id = canonical_node_id(node_id, model.namespace_uri_by_index)
        if canonical_id is None:
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 400, "message": "Invalid OPC UA NodeId format"},
                }
            )
            continue
        element_id = model.element_id_by_expanded_node_id.get(canonical_id)
        if element_id is None:
            element_id = model.element_id_by_node_id.get(node_id.strip())
        node = model.nodes_by_id.get(element_id) if element_id is not None else None
        if node is None:
            items.append(
                {
                    "success": False,
                    "inputNodeId": node_id,
                    "error": {"code": 404, "message": "NodeId not found in the cached model"},
                }
            )
            continue
        items.append(
            {
                "success": True,
                "inputNodeId": node_id,
                "elementId": node.id,
                "name": node.name,
                "kind": node.kind,
            }
        )
    return {"success": all(item["success"] for item in items), "items": items}


def _model_relationships(model: BuildResult, node: ModelNode) -> dict[str, list[str]]:
    relationships = model.relationships_by_id.get(node.id)
    if relationships is not None:
        return relationships
    return node.relationships


def _relationship_plane(relationship_type: str) -> str:
    if relationship_type in {"HasParent", "HasChildren"}:
        return "hierarchy"
    if relationship_type in {"HasComponent", "ComponentOf"}:
        return "composition"
    return "graph"


def _validate_model_tool_arguments(tool_name: str, arguments: Mapping[str, Any]) -> None:
    if tool_name in MCP_MODEL_ACCESS_SCHEMAS:
        validate_model_access_arguments(tool_name, arguments)
        return
    required = MCP_MODEL_TOOL_SCHEMAS[tool_name].get("required", [])
    missing = [field for field in required if field not in arguments]
    if missing:
        raise i3x_http_error(400, "Bad Request", f"Missing required arguments: {', '.join(missing)}")

    batch_field = {
        "resolveElementIdsToNodeIds": "elementIds",
        "resolveNodeIdsToElementIds": "nodeIds",
    }.get(tool_name)
    if batch_field is not None:
        values = arguments[batch_field]
        if not isinstance(values, list) or not values or len(values) > MCP_ID_RESOLUTION_MAX_ITEMS:
            raise i3x_http_error(
                400,
                "Bad Request",
                f"{batch_field} must contain between 1 and {MCP_ID_RESOLUTION_MAX_ITEMS} items",
            )
        return

    for field in ("elementId", "rootElementId", "sourceElementId", "targetElementId"):
        if field in arguments and (not isinstance(arguments[field], str) or not arguments[field].strip()):
            raise i3x_http_error(400, "Bad Request", f"{field} must be a non-empty string")
    for field in (
        "text",
        "kind",
        "semanticRole",
        "namespaceUri",
        "typeElementId",
        "mappingConfidence",
        "appliedProfileId",
        "relationshipType",
    ):
        if field in arguments and not isinstance(arguments[field], str):
            raise i3x_http_error(400, "Bad Request", f"{field} must be a string")
    for field in (
        "includeChildren",
        "includeRelationships",
        "includeTypeAndMetadata",
        "includeHierarchy",
        "includeComposition",
        "includeProperties",
        "includeActions",
    ):
        if field in arguments and type(arguments[field]) is not bool:
            raise i3x_http_error(400, "Bad Request", f"{field} must be a boolean")

    bounds = {
        "limit": (1, 100),
        "offset": (0, None),
        "ancestorDepth": (0, 32),
        "childLimit": (1, MCP_MODEL_CONTEXT_MAX_ITEMS),
        "relationshipLimit": (1, MCP_MODEL_CONTEXT_MAX_ITEMS),
        "maxDepth": (0, 32),
        "maxNodes": (1, 500),
    }
    for field, (minimum, maximum) in bounds.items():
        if field not in arguments:
            continue
        value = arguments[field]
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            bound_text = f"{minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
            raise i3x_http_error(400, "Bad Request", f"{field} must be an integer from {bound_text}")

    if "relationshipPlanes" in arguments:
        planes = arguments["relationshipPlanes"]
        if not isinstance(planes, list) or any(
            not isinstance(plane, str) or plane not in {"hierarchy", "composition", "graph"} for plane in planes
        ):
            raise i3x_http_error(400, "Bad Request", "relationshipPlanes must contain hierarchy, composition, or graph")
    if "direction" in arguments and (
        not isinstance(arguments["direction"], str) or arguments["direction"] not in {"outgoing", "incoming", "either"}
    ):
        raise i3x_http_error(400, "Bad Request", "direction must be outgoing, incoming, or either")

    enum_values = {
        "kind": {"asset", "property", "action", "eventSource"},
        "semanticRole": {"asset", "group", "component", "datapoint", "property", "unknown"},
        "mappingConfidence": {"high", "medium", "low"},
    }
    for field, values in enum_values.items():
        if field in arguments and arguments[field] not in values:
            raise i3x_http_error(400, "Bad Request", f"Invalid {field}: {arguments[field]}")


def _search_model_elements(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    text = arguments.get("text")
    search_text = text.strip().casefold() if isinstance(text, str) else ""
    matched: list[ModelNode] = []
    for node in model.nodes_by_id.values():
        semantic_role = model.semantic_role_by_id.get(node.id, node.semantic_role)
        mapping_confidence = model.mapping_confidence_by_id.get(node.id, node.mapping_confidence)
        namespace_uri = model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri")
        profile_ids = model.applied_profile_ids_by_id.get(
            node.id,
            node.metadata.get("appliedProfileIds", []),
        )
        relationship_names = _model_relationships(model, node)
        if arguments.get("kind") is not None and node.kind != arguments["kind"]:
            continue
        if arguments.get("semanticRole") is not None and semantic_role != arguments["semanticRole"]:
            continue
        if arguments.get("namespaceUri") is not None and namespace_uri != arguments["namespaceUri"]:
            continue
        if arguments.get("typeElementId") is not None and arguments["typeElementId"] not in {
            node.type,
            node.source_type_id,
        }:
            continue
        if arguments.get("mappingConfidence") is not None and mapping_confidence != arguments["mappingConfidence"]:
            continue
        if arguments.get("appliedProfileId") is not None and arguments["appliedProfileId"] not in profile_ids:
            continue
        if arguments.get("relationshipType") is not None and arguments["relationshipType"] not in relationship_names:
            continue
        if search_text:
            searchable = (
                node.id,
                node.name,
                node.kind,
                node.type or "",
                node.source_node_id,
                node.source_type_id or "",
                str(semantic_role),
                str(mapping_confidence),
                str(namespace_uri or ""),
                str(node.metadata.get("description", "")),
                " ".join(str(profile_id) for profile_id in profile_ids),
                " ".join(relationship_names),
            )
            if not any(search_text in value.casefold() for value in searchable):
                continue
        matched.append(node)

    matched.sort(key=lambda node: (node.name.casefold(), node.id))
    limit = arguments.get("limit", 25)
    offset = arguments.get("offset", 0)
    page = matched[offset : offset + limit]
    return {
        "modelRevision": model_revision(model),
        "total": len(matched),
        "returned": len(page),
        "offset": offset,
        "items": [_model_node_summary(model, node) for node in page],
        **({"nextOffset": offset + len(page)} if offset + len(page) < len(matched) else {}),
    }


def _get_element_context(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    element_id = arguments["elementId"]
    node = model.nodes_by_id.get(element_id)
    if node is None:
        raise i3x_http_error(404, "Not Found", f"Model element not found: {element_id}")

    ancestors: list[dict[str, Any]] = []
    current_id = element_id
    visited = {element_id}
    ancestor_depth = arguments.get("ancestorDepth", 3)
    for _ in range(ancestor_depth):
        parent_id = model.hierarchy_parent_by_id.get(current_id)
        if parent_id is None:
            break
        parent = model.nodes_by_id.get(parent_id)
        if parent is None or parent_id in visited:
            break
        ancestors.append(
            {
                "relationshipType": "HasParent",
                "plane": "hierarchy",
                "node": _model_node_summary(model, parent),
            }
        )
        visited.add(parent_id)
        current_id = parent_id

    result: dict[str, Any] = {
        "modelRevision": model_revision(model),
        "element": _model_node_summary(model, node),
        "ancestors": ancestors,
    }
    parents: list[dict[str, Any]] = []
    hierarchy_parent_id = model.hierarchy_parent_by_id.get(node.id)
    if hierarchy_parent_id in model.nodes_by_id:
        parents.append(
            {
                "relationshipType": "HasParent",
                "plane": "hierarchy",
                "node": _model_node_summary(model, model.nodes_by_id[hierarchy_parent_id]),
            }
        )
    composition_parent_id = model.composition_parent_by_id.get(node.id)
    if composition_parent_id in model.nodes_by_id:
        parents.append(
            {
                "relationshipType": "ComponentOf",
                "plane": "composition",
                "node": _model_node_summary(model, model.nodes_by_id[composition_parent_id]),
            }
        )
    if parents:
        result["parents"] = parents
    if arguments.get("includeChildren", True):
        hierarchy_children = model.hierarchy_children_by_id.get(node.id, [])
        composition_children = model.composition_children_by_id.get(node.id, [])
        child_limit = arguments.get("childLimit", 100)
        result["children"] = {
            "hierarchy": _bounded_context_items(model, hierarchy_children, child_limit),
            "composition": _bounded_context_items(model, composition_children, child_limit),
        }
    if arguments.get("includeRelationships", True):
        relationships = []
        for relationship_type, target_ids in sorted(_model_relationships(model, node).items()):
            for target_id in target_ids:
                target = model.nodes_by_id.get(target_id)
                relationships.append(
                    {
                        "relationshipType": relationship_type,
                        "plane": _relationship_plane(relationship_type),
                        "target": (
                            _model_node_summary(model, target) if target is not None else {"elementId": target_id}
                        ),
                    }
                )
        relationship_limit = arguments.get("relationshipLimit", 100)
        result["relationships"] = {
            "total": len(relationships),
            "returned": min(len(relationships), relationship_limit),
            "truncated": len(relationships) > relationship_limit,
            "items": relationships[:relationship_limit],
        }
    if arguments.get("includeTypeAndMetadata", True):
        result["source"] = {
            "sourceNodeId": node.source_node_id,
            "sourceTypeId": node.source_type_id,
            "description": node.metadata.get("description"),
            "namespaceUri": model.namespace_uri_by_id.get(node.id) or node.metadata.get("namespaceUri"),
            "appliedProfileIds": model.applied_profile_ids_by_id.get(
                node.id,
                node.metadata.get("appliedProfileIds", []),
            ),
            "extendedAttributes": node.metadata,
        }
    return result


def _bounded_context_items(model: BuildResult, element_ids: list[str], limit: int) -> dict[str, Any]:
    items = [
        _model_node_summary(model, model.nodes_by_id[element_id])
        for element_id in element_ids
        if element_id in model.nodes_by_id
    ]
    return {
        "total": len(items),
        "returned": min(len(items), limit),
        "truncated": len(items) > limit,
        "items": items[:limit],
    }


def _summarize_subtree(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    root_id = arguments["rootElementId"]
    root = model.nodes_by_id.get(root_id)
    if root is None:
        raise i3x_http_error(404, "Not Found", f"Model element not found: {root_id}")

    max_depth = arguments.get("maxDepth", 4)
    max_nodes = arguments.get("maxNodes", 200)
    include_hierarchy = arguments.get("includeHierarchy", True)
    include_composition = arguments.get("includeComposition", True)
    include_properties = arguments.get("includeProperties", True)
    include_actions = arguments.get("includeActions", True)
    included: dict[str, dict[str, Any]] = {}
    kind_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    truncated = False

    def visit(node: ModelNode, depth: int, path: set[str]) -> dict[str, Any]:
        nonlocal truncated
        summary = _model_node_summary(model, node)
        included[node.id] = summary
        kind_counts[node.kind] = kind_counts.get(node.kind, 0) + 1
        role = str(summary["semanticRole"])
        role_counts[role] = role_counts.get(role, 0) + 1
        children: list[dict[str, Any]] = []
        summary["children"] = children
        if depth >= max_depth:
            if _subtree_child_edges(model, node, include_hierarchy, include_composition):
                truncated = True
            return summary
        for relationship_type, child_id in _subtree_child_edges(model, node, include_hierarchy, include_composition):
            child = model.nodes_by_id.get(child_id)
            if child is None or child_id in path:
                continue
            if (child.kind == "property" and not include_properties) or (
                child.kind == "action" and not include_actions
            ):
                continue
            if len(included) >= max_nodes:
                truncated = True
                break
            children.append(
                {
                    "relationshipType": relationship_type,
                    "node": visit(child, depth + 1, path | {child_id}),
                }
            )
        return summary

    tree = visit(root, 0, {root_id})
    return {
        "modelRevision": model_revision(model),
        "rootElementId": root_id,
        "totalNodes": len(included),
        "truncated": truncated,
        "tree": tree,
        "counts": {"byKind": kind_counts, "bySemanticRole": role_counts},
    }


def _subtree_child_edges(
    model: BuildResult,
    node: ModelNode,
    include_hierarchy: bool,
    include_composition: bool,
) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    if include_hierarchy:
        edges.extend(("HasChildren", child_id) for child_id in model.hierarchy_children_by_id.get(node.id, []))
    if include_composition:
        edges.extend(("HasComponent", child_id) for child_id in model.composition_children_by_id.get(node.id, []))
    return edges


def _trace_relationship_path(model: BuildResult, arguments: Mapping[str, Any]) -> dict[str, Any]:
    source_id = arguments["sourceElementId"]
    target_id = arguments["targetElementId"]
    for element_id in (source_id, target_id):
        if element_id not in model.nodes_by_id:
            raise i3x_http_error(404, "Not Found", f"Model element not found: {element_id}")

    planes = set(arguments.get("relationshipPlanes", ["hierarchy", "composition", "graph"]))
    direction = arguments.get("direction", "either")
    max_depth = arguments.get("maxDepth", 8)

    outgoing_by_id: dict[str, set[tuple[str, str, str]]] = {element_id: set() for element_id in model.nodes_by_id}
    incoming_by_id: dict[str, set[tuple[str, str, str]]] = {element_id: set() for element_id in model.nodes_by_id}
    for relationship_source_id, source in model.nodes_by_id.items():
        for relationship_type, targets in _model_relationships(model, source).items():
            plane = _relationship_plane(relationship_type)
            if plane not in planes:
                continue
            for target_element_id in targets:
                if target_element_id not in model.nodes_by_id:
                    continue
                outgoing_by_id[relationship_source_id].add((target_element_id, relationship_type, plane))
                incoming_by_id[target_element_id].add((relationship_source_id, relationship_type, plane))

    def neighbors(element_id: str) -> list[tuple[str, str, str, str]]:
        found: set[tuple[str, str, str, str]] = set()
        if direction in {"outgoing", "either"}:
            found.update(
                (neighbor_id, relationship_type, plane, "outgoing")
                for neighbor_id, relationship_type, plane in outgoing_by_id[element_id]
            )
        if direction in {"incoming", "either"}:
            found.update(
                (neighbor_id, relationship_type, plane, "incoming")
                for neighbor_id, relationship_type, plane in incoming_by_id[element_id]
            )
        plane_priority = {"hierarchy": 0, "composition": 1, "graph": 2}
        return sorted(found, key=lambda item: (item[0], plane_priority[item[2]], item[1], item[3]))

    queue = deque([(source_id, 0, [{"element": _model_node_summary(model, model.nodes_by_id[source_id])}])])
    visited = {source_id}
    truncated = False
    while queue:
        current_id, depth, path = queue.popleft()
        if current_id == target_id:
            return {
                "modelRevision": model_revision(model),
                "found": True,
                "truncated": False,
                "pathLength": depth,
                "path": path,
            }
        adjacent = neighbors(current_id)
        if depth >= max_depth:
            if any(neighbor_id not in visited for neighbor_id, _, _, _ in adjacent):
                truncated = True
            continue
        for neighbor_id, relationship_type, plane, traversal_direction in adjacent:
            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)
            next_path = [
                *path,
                {
                    "relationship": {
                        "relationshipType": relationship_type,
                        "plane": plane,
                        "direction": traversal_direction,
                    }
                },
                {"element": _model_node_summary(model, model.nodes_by_id[neighbor_id])},
            ]
            if neighbor_id == target_id:
                return {
                    "modelRevision": model_revision(model),
                    "found": True,
                    "truncated": False,
                    "pathLength": depth + 1,
                    "path": next_path,
                }
            queue.append((neighbor_id, depth + 1, next_path))
            if len(visited) >= MCP_MODEL_PATH_MAX_NODES:
                truncated = bool(queue)
                queue.clear()
                break
    return {
        "modelRevision": model_revision(model),
        "found": False,
        "truncated": truncated,
        "pathLength": None,
        "path": [],
    }
