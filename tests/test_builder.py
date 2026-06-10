from __future__ import annotations

import pytest

from i3x_server.model.builder import ModelBuilder, _kind_for_node
from i3x_server.model.mapper import infer_kind, map_type, stable_i3x_id
from i3x_server.opcua.client import OpcUaNodeInfo


class FakeOpcuaForBuilder:
    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        return [
            OpcUaNodeInfo(
                node_id="ns=2;s=Machine",
                parent_node_id=None,
                browse_name="Machine",
                display_name="Machine",
                node_class="Object",
                data_type=None,
                type_definition_id="ns=1;i=1001",
                event_notifier=False,
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=Temperature",
                parent_node_id="ns=2;s=Machine",
                browse_name="Temperature",
                display_name="Temperature",
                node_class="Variable",
                data_type="ns=1;i=11",
                type_definition_id=None,
                event_notifier=False,
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=Reset",
                parent_node_id="ns=2;s=Machine",
                browse_name="Reset",
                display_name="Reset",
                node_class="Method",
                data_type=None,
                type_definition_id=None,
                event_notifier=False,
            ),
        ]


@pytest.mark.asyncio
async def test_model_builder_build_maps_nodes_children_properties_and_actions() -> None:
    builder = ModelBuilder(FakeOpcuaForBuilder())
    result = await builder.build()

    assert len(result.nodes_by_id) == 3
    assert len(result.root_ids) == 1
    assert len(result.property_to_node) == 1
    assert len(result.action_to_method) == 1

    property_id = next(iter(result.property_to_node.keys()))
    action_id = next(iter(result.action_to_method.keys()))
    root_id = result.root_ids[0]
    assert property_id in result.children_by_id[root_id]
    assert action_id in result.children_by_id[root_id]
    assert result.property_to_node[property_id] == "ns=2;s=Temperature"
    assert result.action_to_method[action_id] == ("ns=2;s=Machine", "ns=2;s=Reset")


def test_kind_for_node_branches() -> None:
    event_source = OpcUaNodeInfo("n1", None, "A", "A", "Object", None, event_notifier=True)
    variable = OpcUaNodeInfo("n2", None, "B", "B", "Variable", "Double", event_notifier=False)
    method = OpcUaNodeInfo("n3", None, "C", "C", "Method", None, event_notifier=False)
    other = OpcUaNodeInfo("n4", None, "D", "D", "ObjectType", None, event_notifier=False)

    assert _kind_for_node(event_source) == "eventSource"
    assert _kind_for_node(variable) == "property"
    assert _kind_for_node(method) == "action"
    assert _kind_for_node(other) == "asset"


def test_mapper_helper_functions() -> None:
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Temperature",
        parent_node_id="ns=2;s=Machine",
        browse_name="Temperature",
        display_name="Temperature",
        node_class="Variable",
        data_type="Double",
        event_notifier=False,
    )
    assert stable_i3x_id(node.node_id, "property").startswith("property-")
    assert infer_kind(node) == "property"
    assert map_type(node, "property") == "Double"
    assert map_type(node, "asset") is None