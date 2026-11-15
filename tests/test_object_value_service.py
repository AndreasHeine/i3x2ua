from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua
from fastapi import HTTPException

from i3x_server.application.services.object_value import ObjectValueService
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult
from tests.conftest import data_value_dto

_TIMESTAMP = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_argument_serialization_uses_null_dimensions() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, object()), _model())
    argument = ua.Argument(ValueRank=-1, ArrayDimensions=[])
    value = service._to_json_safe_value([argument])
    assert value[0]["ArrayDimensions"] is None
    assert argument.ArrayDimensions == []


def _model() -> BuildResult:
    root = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=["prop-a", "asset-child"],
        source_node_id="ns=2;s=Root",
    )
    child_asset = ModelNode(
        id="asset-child",
        name="Child",
        kind="asset",
        type=None,
        children=["prop-b"],
        source_node_id="ns=2;s=Child",
    )
    prop_a = ModelNode(
        id="prop-a",
        name="Temperature",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Temperature",
    )
    prop_b = ModelNode(
        id="prop-b",
        name="Pressure",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Pressure",
    )
    return BuildResult(
        nodes_by_id={
            root.id: root,
            child_asset.id: child_asset,
            prop_a.id: prop_a,
            prop_b.id: prop_b,
        },
        root_ids=[root.id],
        children_by_id={
            root.id: [prop_a.id, child_asset.id],
            child_asset.id: [prop_b.id],
            prop_a.id: [],
            prop_b.id: [],
        },
        # Relationship planes per i3X spec: prop-a is a composition child of the root,
        # child_asset is a hierarchical child (independent, never read via the parent).
        hierarchy_children_by_id={root.id: [child_asset.id]},
        composition_children_by_id={root.id: [prop_a.id], child_asset.id: [prop_b.id]},
        instances_by_type_id={},
        property_to_node={prop_a.id: prop_a.source_node_id, prop_b.id: prop_b.source_node_id},
        action_to_method={},
    )


class _RecordingClient:
    """Answers batched reads with one value per node id and records every request."""

    def __init__(self, values: dict[str, Any] | None = None, history: dict[str, list[Any]] | None = None) -> None:
        self.value_requests: list[list[str]] = []
        self.history_requests: list[tuple[list[str], datetime, datetime]] = []
        self._values = values or {}
        self._history = history or {}

    async def read_data_values(self, node_ids: list[str]) -> list[Any]:
        self.value_requests.append(list(node_ids))
        return [data_value_dto(self._values.get(node_id, node_id), "Good", _TIMESTAMP) for node_id in node_ids]

    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, list[Any]]:
        self.history_requests.append((list(node_ids), start_time, end_time))
        return {
            node_id: [data_value_dto(entry, "Good", _TIMESTAMP) for entry in self._history.get(node_id, [node_id])]
            for node_id in node_ids
        }


class _EmptyHistoryClient(_RecordingClient):
    async def read_history_values(
        self,
        node_ids: list[str],
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, list[Any]]:
        del node_ids, start_time, end_time
        return {}


class _FailingClient:
    def __init__(self, cause: Exception) -> None:
        self._cause = cause

    async def read_data_values(self, node_ids: list[str]) -> Any:
        del node_ids
        raise self._cause

    async def read_history_values(self, node_ids: list[str], start_time: datetime, end_time: datetime) -> Any:
        del node_ids, start_time, end_time
        raise self._cause


class _ShortReadClient:
    async def read_data_values(self, node_ids: list[str]) -> list[Any]:
        del node_ids
        return []


@pytest.mark.asyncio
async def test_get_current_value_for_missing_node_raises_not_found() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_current_value("missing")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_current_value_wraps_unexpected_error() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _FailingClient(RuntimeError("read failed"))), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_current_value("prop-a")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_current_value_reports_bad_null_read_failure_as_bad_not_missing() -> None:
    """A server-level read failure (Bad-null data value) must not read as a missing value."""

    class _BadNullClient(_RecordingClient):
        async def read_data_values(self, node_ids: list[str]) -> list[Any]:
            self.value_requests.append(list(node_ids))
            return [data_value_dto(None, "Bad", _TIMESTAMP) for _ in node_ids]

    service = ObjectValueService(cast(OpcUaClientProtocol, _BadNullClient()), _model())
    result = await service.get_current_value("prop-a")
    assert result["quality"] == "Bad"
    assert result["value"] is None


@pytest.mark.asyncio
async def test_get_current_value_batches_root_and_all_component_levels_into_one_read() -> None:
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _chain_model())

    result = await service.get_current_value("asset-root", max_depth=3)

    assert len(client.value_requests) == 1
    assert client.value_requests[0] == ["ns=2;s=CompProp", "ns=2;s=MidProp", "ns=2;s=DeepProp"]
    assert result["quality"] == "GoodNoData"
    assert set(result["components"]) == {"comp-prop", "mid-prop", "deep-prop"}
    assert result["components"]["deep-prop"]["value"] == "ns=2;s=DeepProp"


@pytest.mark.asyncio
async def test_get_current_value_honors_depth_one_without_reading_components() -> None:
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _chain_model())

    result = await service.get_current_value("asset-root", max_depth=1)

    assert "components" not in result
    assert client.value_requests == []

    property_result = await service.get_current_value("comp-prop", max_depth=1)
    assert property_result["value"] == "ns=2;s=CompProp"
    assert client.value_requests == [["ns=2;s=CompProp"]]


@pytest.mark.asyncio
async def test_get_current_value_reads_property_nodes_only_once_per_shared_source_node() -> None:
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _shared_source_model())

    result = await service.get_current_value("asset-root", max_depth=2)

    assert client.value_requests == [["ns=2;s=Shared"]]
    assert result["components"]["prop-a"]["value"] == "ns=2;s=Shared"
    assert result["components"]["prop-b"]["value"] == "ns=2;s=Shared"


@pytest.mark.asyncio
async def test_get_current_value_uses_opcua_data_value_for_property() -> None:
    client = _RecordingClient(values={"ns=2;s=Temperature": 42.5})
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _model())

    result = await service.get_current_value("prop-a")

    assert result["value"] == 42.5
    assert result["quality"] == "Good"
    assert result["timestamp"].endswith("Z")


@pytest.mark.asyncio
async def test_get_current_value_non_property_returns_good_no_data_without_a_read() -> None:
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _model())

    result = await service.get_current_value("asset-root")

    assert result["value"] is None
    assert result["quality"] == "GoodNoData"
    assert client.value_requests == []


@pytest.mark.asyncio
async def test_get_current_value_reports_short_read_result_as_upstream_failure() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _ShortReadClient()), _chain_model())

    with pytest.raises(HTTPException) as exc_info:
        await service.get_current_value("asset-root", max_depth=3)

    assert exc_info.value.status_code == 502
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Failed to read current value for asset-root"


@pytest.mark.asyncio
async def test_get_history_validates_time_range() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_history_rejects_invalid_iso_timestamps() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "not-a-date", "2026-01-01T00:00:00Z")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_history_batches_all_levels_into_one_read() -> None:
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _chain_model())

    result = await service.get_history(
        "asset-root",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        max_depth=3,
    )

    assert len(client.history_requests) == 1
    assert client.history_requests[0][0] == ["ns=2;s=CompProp", "ns=2;s=MidProp", "ns=2;s=DeepProp"]
    assert result["values"] == []
    assert set(result["components"]) == {"comp-prop", "mid-prop", "deep-prop"}
    assert result["components"]["deep-prop"][0]["value"] == "ns=2;s=DeepProp"

    # Composition only; hierarchical children are queried independently.
    child_client = _RecordingClient()
    child_service = ObjectValueService(cast(OpcUaClientProtocol, child_client), _model())
    child_result = await child_service.get_history(
        "asset-child",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        max_depth=2,
    )
    assert set(child_result["components"]) == {"prop-b"}


@pytest.mark.asyncio
async def test_get_history_reads_a_property_root_once() -> None:
    """The root is also the component list for a property root: one read, not two."""
    client = _RecordingClient()
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _model())

    result = await service.get_history(
        "prop-a",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        max_depth=2,
    )

    assert client.history_requests == [(["ns=2;s=Temperature"], _start(), _end())]
    assert result["values"][0]["value"] == "ns=2;s=Temperature"


@pytest.mark.asyncio
async def test_get_history_maps_data_values_and_binary() -> None:
    client = _RecordingClient(history={"ns=2;s=Temperature": [b"\xff\x00"]})
    service = ObjectValueService(cast(OpcUaClientProtocol, client), _model())

    result = await service.get_history(
        "prop-a",
        "2026-01-01T09:00:00Z",
        "2026-01-01T11:00:00Z",
    )

    assert len(result["values"]) == 1
    assert result["values"][0]["quality"] == "Good"
    assert result["values"][0]["value"] == "/wA="


@pytest.mark.asyncio
async def test_get_history_reports_no_values_for_nodes_the_server_omits() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _EmptyHistoryClient()), _chain_model())

    result = await service.get_history(
        "asset-root",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        max_depth=3,
    )

    assert result["components"]["deep-prop"] == []


@pytest.mark.asyncio
async def test_get_history_wraps_unexpected_error() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _FailingClient(RuntimeError("history failed"))), _model())

    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", max_depth=3)
    assert exc_info.value.status_code == 502


def test_collect_composition_components_honors_depth_1() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    root = service.model.nodes_by_id["asset-root"]
    assert service._collect_composition_components(root, max_depth=1) == []


def test_collect_history_source_nodes_includes_root_property() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    prop = service.model.nodes_by_id["prop-a"]
    assert [node.id for node in service._collect_history_source_nodes(prop, max_depth=0)] == ["prop-a"]


def _chain_model() -> BuildResult:
    """Composition chain across three levels plus a hierarchy-only branch.

    ``asset-root`` -> ``comp-prop`` (level 1); ``asset-root`` -> ``comp-child`` -> ``mid-prop``
    (level 2) -> ``sub-comp`` -> ``deep-prop`` (level 3); ``asset-root`` -> ``hier-child`` ->
    ``hier-prop`` sits on the hierarchy plane and must never be traversed (spec :1111).
    """
    nodes = {
        "asset-root": ("Root", "asset"),
        "comp-prop": ("CompProp", "property"),
        "comp-child": ("CompChild", "asset"),
        "mid-prop": ("MidProp", "property"),
        "sub-comp": ("SubComp", "asset"),
        "deep-prop": ("DeepProp", "property"),
        "hier-child": ("HierChild", "asset"),
        "hier-prop": ("HierProp", "property"),
    }
    nodes_by_id = {
        node_id: ModelNode(
            id=node_id,
            name=name,
            kind=cast(Any, kind),
            type=None,
            children=[],
            source_node_id=f"ns=2;s={name}",
        )
        for node_id, (name, kind) in nodes.items()
    }
    children_by_id = {
        "asset-root": ["comp-prop", "comp-child", "hier-child"],
        "comp-child": ["mid-prop", "sub-comp"],
        "sub-comp": ["deep-prop"],
        "hier-child": ["hier-prop"],
        "comp-prop": [],
        "mid-prop": [],
        "deep-prop": [],
        "hier-prop": [],
    }
    return BuildResult(
        nodes_by_id=nodes_by_id,
        root_ids=["asset-root"],
        children_by_id=children_by_id,
        hierarchy_children_by_id={"asset-root": ["comp-prop", "comp-child", "hier-child"], "hier-child": ["hier-prop"]},
        composition_children_by_id={
            "asset-root": ["comp-prop", "comp-child"],
            "comp-child": ["mid-prop", "sub-comp"],
            "sub-comp": ["deep-prop"],
            "hier-child": ["hier-prop"],
        },
        instances_by_type_id={},
        property_to_node={
            "comp-prop": "ns=2;s=CompProp",
            "mid-prop": "ns=2;s=MidProp",
            "deep-prop": "ns=2;s=DeepProp",
            "hier-prop": "ns=2;s=HierProp",
        },
        action_to_method={},
    )


def _shared_source_model() -> BuildResult:
    """Two element ids that resolve to the same OPC UA node."""
    prop_a = ModelNode(
        id="prop-a",
        name="Shared",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Shared",
    )
    prop_b = ModelNode(
        id="prop-b",
        name="SharedAlias",
        kind="property",
        type="Double",
        children=[],
        source_node_id="ns=2;s=Other",
    )
    root = ModelNode(
        id="asset-root",
        name="Root",
        kind="asset",
        type=None,
        children=["prop-a", "prop-b"],
        source_node_id="ns=2;s=Root",
    )
    return BuildResult(
        nodes_by_id={root.id: root, prop_a.id: prop_a, prop_b.id: prop_b},
        root_ids=[root.id],
        children_by_id={root.id: [prop_a.id, prop_b.id], prop_a.id: [], prop_b.id: []},
        hierarchy_children_by_id={root.id: [prop_a.id, prop_b.id]},
        composition_children_by_id={root.id: [prop_a.id, prop_b.id]},
        instances_by_type_id={},
        # ``prop-b`` is served by the same node as ``prop-a`` through the property mapping.
        property_to_node={"prop-a": "ns=2;s=Shared", "prop-b": "ns=2;s=Shared"},
        action_to_method={},
    )


def _start() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)


def _end() -> datetime:
    return datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)


def test_service_history_sources_count_composition_levels() -> None:
    """`maxDepth = N` means N levels; `0` is unlimited, hierarchy edges are never followed."""
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _chain_model())
    root = service.model.nodes_by_id["asset-root"]

    assert service._collect_history_source_nodes(root, max_depth=1) == []
    assert [node.id for node in service._collect_history_source_nodes(root, max_depth=2)] == ["comp-prop", "mid-prop"]
    assert [node.id for node in service._collect_history_source_nodes(root, max_depth=3)] == [
        "comp-prop",
        "mid-prop",
        "deep-prop",
    ]
    assert [node.id for node in service._collect_history_source_nodes(root, max_depth=0)] == [
        "comp-prop",
        "mid-prop",
        "deep-prop",
    ]


def test_service_value_components_count_composition_levels() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _chain_model())
    root = service.model.nodes_by_id["asset-root"]

    assert service._collect_composition_components(root, max_depth=1) == []
    assert [node.id for node in service._collect_composition_components(root, max_depth=2)] == ["comp-prop", "mid-prop"]
    assert [node.id for node in service._collect_composition_components(root, max_depth=0)] == [
        "comp-prop",
        "mid-prop",
        "deep-prop",
    ]


@pytest.mark.asyncio
async def test_service_history_unlimited_depth_collects_all_levels() -> None:
    """`maxDepth = 0` must stay unlimited (the old `max_depth or -1` guard stopped at level 1)."""
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _chain_model())

    limited = await service.get_history("asset-root", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", max_depth=2)
    assert set(limited["components"]) == {"comp-prop", "mid-prop"}

    unlimited = await service.get_history("asset-root", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", max_depth=0)
    assert set(unlimited["components"]) == {"comp-prop", "mid-prop", "deep-prop"}
    assert "hier-prop" not in unlimited["components"]


@pytest.mark.asyncio
async def test_service_value_unlimited_depth_collects_all_levels() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _chain_model())

    unlimited = await service.get_current_value("asset-root", max_depth=0)
    assert set(unlimited["components"]) == {"comp-prop", "mid-prop", "deep-prop"}
    assert "hier-prop" not in unlimited["components"]


def test_parse_iso_datetime_normalizes_z_and_naive() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    parsed_z = service._parse_iso_datetime("2026-01-01T00:00:00Z", "startTime")
    parsed_naive = service._parse_iso_datetime("2026-01-01T00:00:00", "startTime")
    assert parsed_z.isoformat().endswith("+00:00")
    assert parsed_naive.isoformat().endswith("+00:00")


def test_parse_iso_datetime_rejects_invalid_values() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    with pytest.raises(HTTPException) as exc_info:
        service._parse_iso_datetime("invalid", "startTime")
    assert exc_info.value.status_code == 400


def test_now_iso_has_utc_suffix() -> None:
    service = ObjectValueService(cast(OpcUaClientProtocol, _RecordingClient()), _model())
    assert service._now_iso().endswith("Z")


@pytest.mark.asyncio
async def test_get_related_objects_returns_children_and_supports_filter() -> None:
    service = ObjectValueService(
        cast(OpcUaClientProtocol, _RecordingClient()), _model(), request=cast(Any, SimpleNamespace())
    )
    root = service.model.nodes_by_id["asset-root"]
    root.relationships = {"ConnectedTo": ["asset-child"]}

    related = await service.get_related_objects("asset-root")
    assert {item["sourceRelationship"] for item in related} == {"HasChildren", "ConnectedTo"}

    filtered = await service.get_related_objects("asset-root", relationship_type="ConnectedTo", include_metadata=True)
    assert len(filtered) == 1
    assert filtered[0]["sourceRelationship"] == "ConnectedTo"
    assert isinstance(filtered[0]["object"]["metadata"], dict)


@pytest.mark.asyncio
async def test_get_related_objects_missing_node_raises_not_found() -> None:
    service = ObjectValueService(
        cast(OpcUaClientProtocol, _RecordingClient()), _model(), request=cast(Any, SimpleNamespace())
    )
    with pytest.raises(HTTPException) as exc_info:
        await service.get_related_objects("missing")
    assert exc_info.value.status_code == 404
