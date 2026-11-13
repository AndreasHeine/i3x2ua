"""Object type context serving for ``/v1/objecttypes`` and the object endpoint context.

One context per (model revision, namespace token, object types token) triple: heavyweight
schema items are built once, published as an LRU slot, and stale slots are served while a
background rebuild runs. Type construction lives in ``objecttype_type_resolution``, the
UA identity tables in ``objecttype_ua_names``, schema utilities in ``objecttype_schema_utils``,
model reference scanning in ``objecttype_reference_scan``, and feature flags in
``objecttype_runtime_flags``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from i3x_server.api.v1 import objecttype_reference_scan, objecttype_runtime_flags, objecttype_type_resolution
from i3x_server.api.v1.contracts import ObjectTypeResponse
from i3x_server.api.v1.object_helpers import (
    _object_type_element_ids_by_node_id,
    _to_object_type,
)
from i3x_server.api.v1.objecttype_schema_utils import _wrapped_value_schema as _wrapped_value_schema
from i3x_server.api.v1.objecttype_type_resolution import (
    _datatype_object_type_from_source_type_id as _datatype_object_type_from_source_type_id,
)
from i3x_server.api.v1.objecttype_type_resolution import (
    _generic_object_type_from_source_type_id as _generic_object_type_from_source_type_id,
)
from i3x_server.api.v1.objecttype_type_resolution import (
    _ResolvedCustomDataType as _ResolvedCustomDataType,
)
from i3x_server.api.v1.objecttype_type_resolution import (
    _scalar_schema_for_standard_ua_datatype_node_id as _scalar_schema_for_standard_ua_datatype_node_id,
)
from i3x_server.api.v1.objecttype_type_resolution import (
    _scalar_schema_from_type_hierarchy as _scalar_schema_from_type_hierarchy,
)
from i3x_server.api.v1.objecttype_type_resolution import (
    _unknown_type_placeholder as _unknown_type_placeholder,
)
from i3x_server.api.v1.objecttype_ua_names import (
    _is_builtin_ua_datatype_node_id as _is_builtin_ua_datatype_node_id,
)
from i3x_server.application.ports.opcua import OpcUaClientProtocol, OpcUaNamespaceInfo, OpcUaObjectTypeInfo
from i3x_server.application.state import ObjectTypeContextCache, ObjectTypeContextSlot, gateway_state
from i3x_server.domain.utils import canonical_namespace_uri, expanded_node_id
from i3x_server.errors import i3x_http_error
from i3x_server.http_caching import (
    NAMESPACES_PART,
    OBJECT_TYPES_PART,
    content_revision,
    mark_not_cacheable,
    record_revision,
)
from i3x_server.model.revision import model_revision
from i3x_server.schemas.objecttype_schema import deduplicate_schema_fragments, remove_opcua_schema_fields
from i3x_server.schemas.state import BuildResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ObjectTypeContext:
    namespace_infos: list[OpcUaNamespaceInfo]
    object_types: list[OpcUaObjectTypeInfo]
    element_ids_by_node_id: dict[str, str]
    items: list[ObjectTypeResponse]
    source_type_to_element_id: dict[str, str]
    # Revision of the served content, so equal tags always mean equal representations.
    revision: str = ""


_OBJECT_TYPE_KEY_FIELDS = ("node_id", "parent_node_id", "browse_name", "display_name", "description", "is_abstract")
# Member values are arbitrary OPC UA objects whose repr may not be stable, so only
# structural attributes identify a definition; value changes still reach the served revision.
_OBJECT_TYPE_MEMBER_KEY_FIELDS = (
    "node_id",
    "browse_name",
    "display_name",
    "description",
    "node_class",
    "data_type",
    "modelling_rule",
    "variant_type",
    "is_array",
    "value_rank",
    "array_dimensions",
    "parent_node_id",
    "type_definition_id",
    "reference_type_id",
    "reference_type",
    "reference_order",
)


def _object_types_token(object_types: list[OpcUaObjectTypeInfo]) -> str:
    return content_revision(
        (
            tuple(getattr(item, name, None) for name in _OBJECT_TYPE_KEY_FIELDS),
            tuple(sorted((getattr(item, "properties", None) or {}).items())),
            tuple(
                tuple(getattr(member, name, None) for name in _OBJECT_TYPE_MEMBER_KEY_FIELDS)
                for member in getattr(item, "members", None) or []
            ),
        )
        for item in object_types
    )


def _memoized_object_types_token(request: Any, object_types: list[OpcUaObjectTypeInfo]) -> str:
    # The OPC UA client serves the same cached list until its metadata reload replaces it,
    # so hashing every definition once per list keeps this off the per-request path.
    # Holding the list in the memo keeps its id from being reused by another object.
    state = gateway_state(request)
    memo = state.object_types_token_memo
    if memo is not None and memo[0] is object_types:
        return memo[1]
    token = _object_types_token(object_types)
    state.object_types_token_memo = (object_types, token)
    return token


def _object_type_context_revision(context: _ObjectTypeContext) -> str:
    return content_revision(
        (
            tuple(
                (info.uri, info.display_name, info.namespace_version, info.server_uri)
                for info in context.namespace_infos
            ),
            _object_types_token(context.object_types),
            tuple(item.model_dump_json() for item in context.items),
            tuple(sorted(context.element_ids_by_node_id.items())),
            tuple(sorted(context.source_type_to_element_id.items())),
        )
    )


async def _build_object_type_context(
    model: BuildResult,
    namespace_infos: list[OpcUaNamespaceInfo],
    opcua_client: OpcUaClientProtocol,
    object_types: list[OpcUaObjectTypeInfo],
) -> _ObjectTypeContext:
    started = perf_counter()
    object_types_by_node_id = {item.node_id: item for item in object_types}
    element_ids_by_node_id = _object_type_element_ids_by_node_id(object_types, namespace_infos)
    property_type_element_ids = objecttype_reference_scan._collect_property_type_element_ids(model, namespace_infos)
    property_type_element_ids.update(
        expanded_node_id(member.data_type, namespace_infos)
        for item in object_types
        for member in item.members
        if member.node_class.lower() == "variable" and member.data_type
    )
    resolved_custom_datatypes = await objecttype_type_resolution._resolve_custom_scalar_datatype_schemas(
        property_type_element_ids,
        namespace_infos,
        opcua_client,
    )
    resolved_data_type_schemas = {
        type_id: resolved.scalar_schema for type_id, resolved in resolved_custom_datatypes.items()
    }

    items: list[ObjectTypeResponse] = []
    chunk_size = _OBJECT_TYPE_CONTEXT_BUILD_CHUNK_SIZE
    for i in range(0, len(object_types), chunk_size):
        chunk = object_types[i : i + chunk_size]
        items.extend(
            [
                _to_object_type(
                    item,
                    model,
                    namespace_infos,
                    object_types_by_node_id,
                    element_ids_by_node_id,
                    {},
                    resolved_data_type_schemas,
                )
                for item in chunk
            ]
        )
        await asyncio.sleep(0)

    items.extend(objecttype_type_resolution._synthetic_object_types_from_structure_defs(items, namespace_infos))

    referenced_type_element_ids = objecttype_reference_scan._collect_referenced_type_element_ids(
        model, namespace_infos, element_ids_by_node_id
    )
    known_ids = {item.elementId for item in items}
    by_source_type_id: dict[str, ObjectTypeResponse] = {item.sourceTypeId.lower(): item for item in items}
    browse_name_cache: dict[str, str | None] = {}
    lookup_budget = {"remaining": objecttype_runtime_flags._live_type_name_lookup_max_per_request()}

    # Pre-resolve generic live browse-name lookups concurrently. The classification below is
    # deterministic and side-effect free, so the sequential pass afterwards keeps item order,
    # collision skips, and the shared lookup budget identical (budget decrements happen before
    # the first await in each coroutine, so gathered tasks cannot exceed the per-request cap).
    generic_lookup_targets_by_key: dict[str, str] = {}
    for unresolved_id in sorted(referenced_type_element_ids - known_ids):
        unresolved_key = unresolved_id.lower()
        if unresolved_key in by_source_type_id:
            continue
        resolved_datatype = resolved_custom_datatypes.get(unresolved_key)
        if (
            objecttype_type_resolution._datatype_object_type_from_source_type_id(
                unresolved_id,
                namespace_infos,
                resolved_datatype.scalar_schema if resolved_datatype is not None else None,
                resolved_datatype.browse_name if resolved_datatype is not None else None,
            )
            is not None
        ):
            continue
        if unresolved_id in property_type_element_ids:
            if (
                objecttype_type_resolution._opaque_datatype_object_type_from_source_type_id(
                    unresolved_id, namespace_infos
                )
                is not None
            ):
                continue
        generic_lookup_targets_by_key[unresolved_key] = unresolved_id

    generic_items_by_source_id: dict[str, ObjectTypeResponse | None] = {}
    if generic_lookup_targets_by_key:
        generic_results = await asyncio.gather(
            *(
                objecttype_type_resolution._generic_object_type_from_source_type_id(
                    unresolved_id,
                    namespace_infos,
                    opcua_client,
                    browse_name_cache,
                    lookup_budget,
                )
                for unresolved_id in generic_lookup_targets_by_key.values()
            ),
        )
        generic_items_by_source_id = dict(zip(generic_lookup_targets_by_key.keys(), generic_results, strict=True))

    for unresolved_id in sorted(referenced_type_element_ids - known_ids):
        unresolved_key = unresolved_id.lower()
        source_match = by_source_type_id.get(unresolved_key)
        if source_match is not None:
            continue

        resolved_datatype = resolved_custom_datatypes.get(unresolved_key)
        datatype_item = objecttype_type_resolution._datatype_object_type_from_source_type_id(
            unresolved_id,
            namespace_infos,
            resolved_datatype.scalar_schema if resolved_datatype is not None else None,
            resolved_datatype.browse_name if resolved_datatype is not None else None,
        )
        if datatype_item is not None:
            items.append(datatype_item)
            by_source_type_id[unresolved_key] = datatype_item
            continue

        if unresolved_id in property_type_element_ids:
            opaque_datatype_item = objecttype_type_resolution._opaque_datatype_object_type_from_source_type_id(
                unresolved_id, namespace_infos
            )
            if opaque_datatype_item is not None:
                items.append(opaque_datatype_item)
                by_source_type_id[unresolved_key] = opaque_datatype_item
                continue

        generic_item = generic_items_by_source_id.get(unresolved_key)
        if generic_item is not None:
            items.append(generic_item)
            by_source_type_id[unresolved_key] = generic_item
            continue

        unknown_item = objecttype_type_resolution._unknown_type_placeholder(unresolved_id, namespace_infos)
        items.append(unknown_item)
        by_source_type_id[unresolved_key] = unknown_item

    canonical_items = [
        item.model_copy(update={"namespaceUri": canonical_namespace_uri(item.namespaceUri, namespace_infos)})
        for item in items
    ]
    if not objecttype_runtime_flags._include_mcp_opcua_metadata():
        canonical_items = [
            item.model_copy(update={"schema_": remove_opcua_schema_fields(item.schema_)}) for item in canonical_items
        ]
    schemas = deduplicate_schema_fragments(item.schema_ for item in canonical_items)
    for item, schema in zip(canonical_items, schemas, strict=True):
        item.schema_ = schema
    source_type_to_element_id = {key: item.elementId for key, item in by_source_type_id.items()}
    logger.debug(
        "Object type context built model_nodes=%d object_types=%d items=%d duration_s=%.3f",
        len(model.nodes_by_id),
        len(object_types),
        len(canonical_items),
        perf_counter() - started,
    )
    return _ObjectTypeContext(
        namespace_infos=namespace_infos,
        object_types=object_types,
        element_ids_by_node_id=element_ids_by_node_id,
        items=canonical_items,
        source_type_to_element_id=source_type_to_element_id,
    )


# Contexts are heavyweight (schema items per object type), but requests can alternate
# between token triples — e.g. a request that still holds the previous model revision while
# a periodic refresh switched the cache. A single slot thrashed on that alternation, so the
# cache keeps a bounded LRU of slots. The most-recently-used slot is what stale serving and
# other readers use.
_OBJECT_TYPE_CONTEXT_CACHE_MAX_ENTRIES = 8
# Listing mapping is per-chunk so a large server cannot block the event loop for the whole build.
_OBJECT_TYPE_CONTEXT_BUILD_CHUNK_SIZE = 50


def _promote_object_type_slot(request: Any, slot: ObjectTypeContextSlot) -> None:
    """Mark a slot as most-recently-used."""
    cache = gateway_state(request).object_type_context_cache
    if cache is None:
        return
    cache.promote(slot)


def _cached_object_type_context(
    request: Any,
    model_token: str,
    namespace_token: tuple[Any, ...],
    object_types_token: str,
) -> _ObjectTypeContext | None:
    cache = gateway_state(request).object_type_context_cache
    if cache is None:
        return None
    slot = cache.find((model_token, namespace_token, object_types_token))
    if slot is None:
        return None
    context = slot.context
    if not isinstance(context, _ObjectTypeContext):
        return None
    _promote_object_type_slot(request, slot)
    return context


def _store_object_type_context(
    request: Any,
    model_token: str,
    namespace_token: tuple[Any, ...],
    object_types_token: str,
    context: _ObjectTypeContext,
) -> None:
    """Publish a freshly built context as the most-recently-used slot.

    Slots are bounded: contexts are heavyweight, so the least-recently-used slot is evicted
    once the cap is reached instead of accumulating every token triple ever requested.
    """
    state = gateway_state(request)
    cache = state.object_type_context_cache
    if cache is None:
        cache = ObjectTypeContextCache(max_entries=_OBJECT_TYPE_CONTEXT_CACHE_MAX_ENTRIES)
    cache.store(
        ObjectTypeContextSlot(
            model_token=model_token,
            namespace_token=namespace_token,
            object_types_token=object_types_token,
            context=context,
        )
    )
    state.object_type_context_cache = cache


def _stale_object_type_context(request: Any) -> _ObjectTypeContext | None:
    cache = gateway_state(request).object_type_context_cache
    if cache is None:
        return None
    for slot in reversed(cache.entries):
        if isinstance(slot.context, _ObjectTypeContext):
            return slot.context
    return None


def _schedule_object_type_context_refresh(app: Any, model: BuildResult, opcua_client: OpcUaClientProtocol) -> None:
    state = gateway_state(app)
    task = state.object_type_refresh_task
    if task is not None and not task.done():
        return
    state.object_type_refresh_task = asyncio.create_task(_refresh_object_type_context(app, model, opcua_client))


async def _refresh_object_type_context(app: Any, model: BuildResult, opcua_client: OpcUaClientProtocol) -> None:
    # Rebuild for the newest model: a request may still hold the model from before a refresh.
    target_model = gateway_state(app).model_cache or model
    try:
        await _get_object_type_context(SimpleNamespace(app=app), target_model, opcua_client, allow_stale=False)
    except Exception:
        logger.exception("Background object type context refresh failed; serving previous context")


async def _get_object_type_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None = None,
    *,
    allow_stale: bool = True,
) -> _ObjectTypeContext:
    context = await _resolve_object_type_context(request, model, opcua_client, namespace_infos, allow_stale=allow_stale)
    # Record the context's own revision: a stale context must not be tagged as current.
    record_revision(request, OBJECT_TYPES_PART, context.revision)
    return context


async def _resolve_object_type_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
    namespace_infos: list[OpcUaNamespaceInfo] | None,
    *,
    allow_stale: bool,
) -> _ObjectTypeContext:
    started = perf_counter()
    resolved_namespace_infos = (
        namespace_infos if namespace_infos is not None else await opcua_client.get_namespace_infos()
    )
    object_types = await opcua_client.get_object_types()
    namespace_token = tuple(
        (info.uri, info.display_name, info.namespace_version, info.server_uri) for info in resolved_namespace_infos
    )
    # Full definitions, not just node ids: a changed type (same node id) must rebuild the context.
    object_types_token = _memoized_object_types_token(request, object_types)
    # Content revision, hashed once per build: equivalent rebuilds still hit the cache.
    model_token = model_revision(model)

    cached_context = _cached_object_type_context(request, model_token, namespace_token, object_types_token)
    if cached_context is not None:
        return cached_context

    if allow_stale:
        stale_context = _stale_object_type_context(request)
        if stale_context is not None:
            logger.warning("Serving stale object type context while it is rebuilt in the background")
            _schedule_object_type_context_refresh(request.app, model, opcua_client)
            return stale_context

    async with gateway_state(request).object_type_lock:
        cached_context = _cached_object_type_context(request, model_token, namespace_token, object_types_token)
        if cached_context is not None:
            logger.debug(
                "Object type context cache hit model_nodes=%d object_types=%d duration_s=%.3f",
                len(model.nodes_by_id),
                len(object_types),
                perf_counter() - started,
            )
            return cached_context

        context = await _build_object_type_context(
            model=model,
            namespace_infos=resolved_namespace_infos,
            opcua_client=opcua_client,
            object_types=object_types,
        )
        # Hash the built output: it also covers live browse-name lookups that are not inputs.
        context.revision = _object_type_context_revision(context)
        _store_object_type_context(request, model_token, namespace_token, object_types_token, context)
        logger.info(
            "Object type context cache miss rebuilt model_nodes=%d object_types=%d items=%d duration_s=%.3f",
            len(model.nodes_by_id),
            len(object_types),
            len(context.items),
            perf_counter() - started,
        )
        return context


async def warm_object_type_context(
    app: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
) -> _ObjectTypeContext:
    """Populate the object type context cache outside of a request.

    Blocking startup awaits this; background startup and model refreshes also use it.
    """
    return await _get_object_type_context(SimpleNamespace(app=app), model, opcua_client, allow_stale=False)


async def _get_object_endpoint_context(
    request: Any,
    model: BuildResult,
    opcua_client: OpcUaClientProtocol,
) -> tuple[list[OpcUaNamespaceInfo], dict[str, str], dict[str, str]]:
    try:
        namespace_infos = await opcua_client.get_namespace_infos()
    except Exception as exc:
        raise i3x_http_error(
            502,
            "OpcUaNamespaceError",
            "Failed to read OPC UA namespaces",
            {"cause": str(exc)},
        ) from exc

    object_type_element_ids_by_node_id: dict[str, str] = {}
    object_type_element_ids_by_source_type: dict[str, str] = {}
    try:
        context = await _get_object_type_context(
            request,
            model,
            opcua_client,
            namespace_infos=namespace_infos,
        )
        object_type_element_ids_by_node_id = context.element_ids_by_node_id
        object_type_element_ids_by_source_type = context.source_type_to_element_id
    except Exception:
        object_type_element_ids_by_node_id = {}
        object_type_element_ids_by_source_type = {}
        mark_not_cacheable(request)

    record_revision(
        request,
        NAMESPACES_PART,
        content_revision(
            (info.uri, info.display_name, info.namespace_version, info.server_uri) for info in namespace_infos
        ),
    )
    return namespace_infos, object_type_element_ids_by_node_id, object_type_element_ids_by_source_type
