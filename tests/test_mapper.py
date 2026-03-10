from i3x_server.model.mapper import map_node
from i3x_server.opcua.client import OpcUaNodeInfo


def test_map_variable_to_property() -> None:
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Temperature",
        parent_node_id="ns=2;s=Machine",
        browse_name="Temperature",
        display_name="Temperature",
        node_class="Variable",
        data_type="Double",
        event_notifier=False,
    )

    mapped = map_node(node, [])

    assert mapped.kind == "property"
    assert mapped.type == "Double"
    assert mapped.source_node_id == "ns=2;s=Temperature"
