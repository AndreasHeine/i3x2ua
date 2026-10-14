from types import SimpleNamespace
from typing import Any

import pytest
from asyncua import ua
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.status_codes import StatusCodes

from i3x_server.infrastructure.opcua.client import OpcUaClient, OpcUaOperationalLimits


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected_calls", [(1, 4), (2, 3)])
async def test_type_hierarchies_batch_limits_shared_ancestors_and_warm_cache(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    expected_calls: int,
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    names = {
        "ns=2;i=1": "MachineToolType",
        "ns=2;i=2": "OtherMachineToolType",
        "ns=3;i=3": "FunctionalGroupType",
        "i=58": "BaseObjectType",
    }
    parents = {
        "ns=2;i=1": ["ns=3;i=3"],
        "ns=2;i=2": ["ns=3;i=3"],
        "ns=3;i=3": ["i=58"],
        "i=58": [],
    }
    read_batches: list[list[str]] = []
    browse_batches: list[list[str]] = []

    def get_node(type_id: str) -> SimpleNamespace:
        return SimpleNamespace(nodeid=ua.NodeId.from_string(type_id))

    async def read_attributes(nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
        assert attr == AttributeIds.BrowseName
        ids = [node.nodeid.to_string() for node in nodes]
        read_batches.append(ids)
        assert len(ids) <= limit
        return [ua.DataValue(ua.Variant(ua.QualifiedName(names[type_id], 0))) for type_id in ids]

    async def browse(params: ua.BrowseParameters, batch_size: int) -> list[ua.BrowseResult]:
        assert batch_size <= limit
        ids = [desc.NodeId.to_string() for desc in params.NodesToBrowse]
        browse_batches.append(ids)
        output = []
        for desc, type_id in zip(params.NodesToBrowse, ids, strict=True):
            assert desc.BrowseDirection == ua.BrowseDirection.Inverse
            assert desc.ReferenceTypeId == ua.NodeId(ua.Int32(ObjectIds.HasSubtype))
            assert not desc.IncludeSubtypes
            refs = [
                ua.ReferenceDescription(NodeId=ua.ExpandedNodeId.from_string(parent)) for parent in parents[type_id]
            ]
            output.append(ua.BrowseResult(References=refs))
        return output

    async def limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(max_nodes_per_read=limit, max_nodes_per_browse=limit)

    monkeypatch.setattr(client, "_client", SimpleNamespace(get_node=get_node, read_attributes=read_attributes))
    monkeypatch.setattr(client, "_browse_with_retry", browse)
    monkeypatch.setattr(client, "get_operational_limits", limits)

    requested = ["ns=2;i=1", "ns=2;i=2", "ns=2;i=1"]
    cold = await client.resolve_type_definition_hierarchies(requested)
    assert [info.browse_name for info in cold["ns=2;i=1"]] == [
        "MachineToolType",
        "FunctionalGroupType",
        "BaseObjectType",
    ]
    assert [info.browse_name for info in cold["ns=2;i=2"]] == [
        "OtherMachineToolType",
        "FunctionalGroupType",
        "BaseObjectType",
    ]
    assert len(read_batches) == len(browse_batches) == expected_calls
    assert [item for batch in read_batches for item in batch].count("ns=3;i=3") == 1

    warm = await client.resolve_type_definition_hierarchies(requested)
    assert warm == cold
    assert len(read_batches) == len(browse_batches) == expected_calls
    assert await client.resolve_type_definition_hierarchy("ns=2;i=1") == cold["ns=2;i=1"]

    # Returned lists must not allow callers to corrupt cached chains.
    warm["ns=2;i=1"].clear()
    assert await client.resolve_type_definition_hierarchy("ns=2;i=1") == cold["ns=2;i=1"]

    names["ns=2;i=4"] = "NewMachineToolType"
    parents["ns=2;i=4"] = ["ns=3;i=3"]
    new_chain = await client.resolve_type_definition_hierarchy("ns=2;i=4")
    assert [info.browse_name for info in new_chain] == ["NewMachineToolType", "FunctionalGroupType", "BaseObjectType"]
    assert len(read_batches) == len(browse_batches) == expected_calls + 1
    assert read_batches[-1] == ["ns=2;i=4"]

    for type_id, (_, chain) in client._type_definition_hierarchy_cache.items():
        client._type_definition_hierarchy_cache[type_id] = (float("-inf"), chain)
    refreshed = await client.resolve_type_definition_hierarchies(requested)
    assert refreshed == cold
    assert len(read_batches) == len(browse_batches) == expected_calls * 2 + 1


@pytest.mark.asyncio
async def test_type_hierarchy_batch_cycle_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    calls: list[str] = []

    async def read(nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
        return [ua.DataValue(ua.Variant(ua.QualifiedName("CyclicType", 2))) for _ in nodes]

    async def browse(
        nodes: list[Any],
        **kwargs: Any,
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        calls.extend(node.nodeid.to_string() for node in nodes)
        return [(node, [ua.ReferenceDescription(NodeId=ua.ExpandedNodeId.from_string("ns=2;i=1"))]) for node in nodes]

    async def limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(None, None)

    monkeypatch.setattr(
        client, "_client", SimpleNamespace(get_node=lambda value: SimpleNamespace(nodeid=ua.NodeId.from_string(value)))
    )
    monkeypatch.setattr(client, "_read_attribute_batch", read)
    monkeypatch.setattr(client, "_browse_references_descriptions", browse)
    monkeypatch.setattr(client, "get_operational_limits", limits)
    result = await client.resolve_type_definition_hierarchies(["ns=2;i=1"])
    assert calls == ["ns=2;i=1"]
    assert len(result["ns=2;i=1"]) == 1


@pytest.mark.asyncio
async def test_type_hierarchy_batch_read_failure_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")

    async def read(nodes: list[Any], attr: AttributeIds) -> list[ua.DataValue]:
        return [ua.DataValue(StatusCode=ua.StatusCode(ua.UInt32(StatusCodes.BadNodeIdUnknown))) for _ in nodes]

    async def limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(None, None)

    monkeypatch.setattr(
        client, "_client", SimpleNamespace(get_node=lambda value: SimpleNamespace(nodeid=ua.NodeId.from_string(value)))
    )
    monkeypatch.setattr(client, "_read_attribute_batch", read)
    monkeypatch.setattr(client, "get_operational_limits", limits)
    assert await client.resolve_type_definition_hierarchies([]) == {}
    with pytest.raises(ValueError, match="Cannot resolve type BrowseName"):
        await client.resolve_type_definition_hierarchies(["ns=2;i=1"])
    assert not client._type_definition_hierarchy_cache
