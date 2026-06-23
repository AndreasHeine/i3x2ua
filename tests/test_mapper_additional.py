from __future__ import annotations

from i3x_server.domain.ports.opcua import OpcUaNodeInfo
from i3x_server.model.mapper import _normalize_token, classify_opcua_reference, map_node


def test_normalize_token_empty_and_separator_cases() -> None:
    assert _normalize_token(None) == ""
    assert _normalize_token("") == ""
    assert _normalize_token("  ") == ""
    assert _normalize_token("ns=0;i=35") == "i35"
    assert _normalize_token("0:Organizes") == "organizes"


def test_classify_opcua_reference_additional_fallbacks() -> None:
    assert classify_opcua_reference("ns=0;i=31", "References") == "graph"
    assert classify_opcua_reference("ns=0;i=33", "HierarchicalReferences", target_node_class="Variable") == "hierarchy"
    assert (
        classify_opcua_reference(
            "ns=1;i=123",
            "VendorReference",
            supertype_browse_names=["HasProperty"],
            target_node_class="Variable",
        )
        == "composition"
    )


def test_map_node_uses_browse_name_when_display_name_empty() -> None:
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Motor",
        parent_node_id=None,
        browse_name="Motor_Browse",
        display_name="",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=0;i=58",
        event_notifier=False,
    )
    mapped = map_node(node, [])
    assert mapped.name == "Motor_Browse"
