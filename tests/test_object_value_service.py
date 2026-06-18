from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from i3x_server.application.services.object_value import VQT, ObjectValueService
from i3x_server.domain.ports.opcua import OpcUaClientProtocol
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


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
        instances_by_type_id={},
        property_to_node={prop_a.id: prop_a.source_node_id, prop_b.id: prop_b.source_node_id},
        action_to_method={},
    )


class _Service(ObjectValueService):
    async def _read_current_value(self, node: ModelNode) -> VQT:
        return VQT(value=node.id, quality="Good", timestamp="2026-01-01T00:00:00Z")

    async def _read_history_values(
        self,
        node: ModelNode,
        start_time: datetime,
        end_time: datetime,
    ) -> list[VQT]:
        del start_time, end_time
        return [VQT(value=node.id, quality="Good", timestamp="2026-01-01T00:00:00Z")]


@pytest.mark.asyncio
async def test_get_current_value_for_missing_node_raises_not_found() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_current_value("missing")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_current_value_wraps_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())

    async def _boom(node: ModelNode) -> VQT:
        del node
        raise RuntimeError("read failed")

    monkeypatch.setattr(service, "_read_current_value", _boom)

    with pytest.raises(HTTPException) as exc_info:
        await service.get_current_value("asset-root")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_get_current_value_includes_components_when_depth_allows() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    result = await service.get_current_value("asset-root", max_depth=3)
    assert result["value"] == "asset-root"
    assert set(result["components"].keys()) == {"prop-a", "prop-b"}


@pytest.mark.asyncio
async def test_get_history_validates_time_range() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_history_rejects_invalid_iso_timestamps() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "not-a-date", "2026-01-01T00:00:00Z")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_get_history_collects_components_for_composition_nodes() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    result = await service.get_history(
        "asset-root",
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        max_depth=3,
    )
    assert result["values"][0]["value"] == "asset-root"
    assert set(result["components"].keys()) == {"prop-a", "prop-b"}


@pytest.mark.asyncio
async def test_get_history_wraps_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())

    async def _boom(node: ModelNode, start_time: datetime, end_time: datetime) -> list[VQT]:
        del node, start_time, end_time
        raise RuntimeError("history failed")

    monkeypatch.setattr(service, "_read_history_values", _boom)

    with pytest.raises(HTTPException) as exc_info:
        await service.get_history("asset-root", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert exc_info.value.status_code == 502


def test_collect_composition_components_honors_depth_1() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    root = service.model.nodes_by_id["asset-root"]
    assert service._collect_composition_components(root, max_depth=1) == []


def test_collect_history_source_nodes_includes_root_property() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    prop = service.model.nodes_by_id["prop-a"]
    assert [node.id for node in service._collect_history_source_nodes(prop, max_depth=0)] == ["prop-a"]


def test_parse_iso_datetime_normalizes_z_and_naive() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    parsed_z = service._parse_iso_datetime("2026-01-01T00:00:00Z", "startTime")
    parsed_naive = service._parse_iso_datetime("2026-01-01T00:00:00", "startTime")
    assert parsed_z.isoformat().endswith("+00:00")
    assert parsed_naive.isoformat().endswith("+00:00")


def test_parse_iso_datetime_rejects_invalid_values() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    with pytest.raises(HTTPException) as exc_info:
        service._parse_iso_datetime("invalid", "startTime")
    assert exc_info.value.status_code == 400


def test_now_iso_has_utc_suffix() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model())
    assert service._now_iso().endswith("Z")


@pytest.mark.asyncio
async def test_get_related_objects_not_implemented() -> None:
    service = _Service(cast(OpcUaClientProtocol, object()), _model(), request=cast(Any, SimpleNamespace()))
    with pytest.raises(NotImplementedError):
        await service.get_related_objects("asset-root")
