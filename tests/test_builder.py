from __future__ import annotations

from typing import cast

import pytest

from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo, OpcUaTypeDefinitionInfo
from i3x_server.infrastructure.opcua.client import OpcUaClientProtocol, OpcUaNodeInfo, OpcUaReferenceInfo
from i3x_server.model.builder import ModelBuilder, _kind_for_node
from i3x_server.model.mapper import infer_kind, map_type, stable_i3x_id
from i3x_server.schemas.state import BuildResult


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
async def test_model_builder_shares_validated_relationship_containers() -> None:
    for client in (FakeOpcuaForBuilder(), FakeOpcuaWithOutgoingReferences(), FakeOpcuaWithGraphReference()):
        model = await ModelBuilder(cast(OpcUaClientProtocol, client)).build()
        for node_id, node in model.nodes_by_id.items():
            assert model.relationships_by_id[node_id] is node.relationships
            for name, targets in node.relationships.items():
                assert model.relationships_by_id[node_id][name] is targets

    isolated = await ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithNamespaceInfo("i=85", []))).build()
    node = next(iter(isolated.nodes_by_id.values()))
    assert node.relationships == {}
    assert isolated.relationships_by_id[node.id] is node.relationships


@pytest.mark.asyncio
async def test_model_builder_shares_validated_child_lists() -> None:
    for client in (FakeOpcuaForBuilder(), FakeOpcuaWithOutgoingReferences(), FakeOpcuaWithGraphReference()):
        model = await ModelBuilder(cast(OpcUaClientProtocol, client)).build()
        assert model.children_by_id.keys() == model.nodes_by_id.keys()
        for node_id, node in model.nodes_by_id.items():
            assert model.children_by_id[node_id] is node.children
            assert model.hierarchy_children_by_id[node_id] is not node.children
            assert model.composition_children_by_id[node_id] is not node.children

    isolated = await ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithNamespaceInfo("i=85", []))).build()
    node = next(iter(isolated.nodes_by_id.values()))
    assert node.children == []
    assert isolated.children_by_id[node.id] is node.children


@pytest.mark.asyncio
async def test_model_builder_shares_applied_profile_lists() -> None:
    model = await ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaForBuilder())).build()
    for node_id, node in model.nodes_by_id.items():
        assert node.metadata["appliedProfileIds"] == ["generic"]
        assert model.applied_profile_ids_by_id[node_id] is node.metadata["appliedProfileIds"]

    profile_lists = list(model.applied_profile_ids_by_id.values())
    assert len({id(profile_ids) for profile_ids in profile_lists}) == len(profile_lists)


@pytest.mark.asyncio
async def test_model_builder_maps_opcua_description_to_metadata() -> None:
    class FakeOpcuaWithDescription:
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            return [
                OpcUaNodeInfo(
                    node_id="ns=2;s=Machine",
                    parent_node_id=None,
                    browse_name="Machine",
                    display_name="Machine",
                    node_class="Object",
                    data_type=None,
                    description="Machine description",
                )
            ]

    model = await ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithDescription())).build()

    node = next(iter(model.nodes_by_id.values()))
    assert node.metadata["description"] == "Machine description"


@pytest.mark.asyncio
async def test_model_builder_shares_hierarchy_parent_index() -> None:
    for client in (FakeOpcuaForBuilder(), FakeOpcuaWithOutgoingReferences(), FakeOpcuaWithGraphReference()):
        model = await ModelBuilder(cast(OpcUaClientProtocol, client)).build()
        assert model.parent_by_id is model.hierarchy_parent_by_id
        assert model.composition_parent_by_id is not model.parent_by_id
        assert model.parent_id_by_id is not model.parent_by_id


def test_manually_constructed_model_preserves_independent_parent_indexes() -> None:
    parents = {"child": "fallback-parent"}
    hierarchy_parents = {"child": "hierarchy-parent"}
    model = BuildResult(
        nodes_by_id={},
        root_ids=[],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
        parent_by_id=parents,
        hierarchy_parent_by_id=hierarchy_parents,
    )
    assert model.parent_by_id is parents
    assert model.hierarchy_parent_by_id is hierarchy_parents
    assert model.parent_by_id is not model.hierarchy_parent_by_id


class FakeOpcuaWithNamespaceInfo:
    def __init__(self, node_id: str, namespace_infos: list[OpcUaNamespaceInfo]) -> None:
        self._node_id = node_id
        self._namespace_infos = namespace_infos

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        return [
            OpcUaNodeInfo(
                node_id=self._node_id,
                parent_node_id=None,
                browse_name="Machine",
                display_name="Machine",
                node_class="Object",
                data_type=None,
                type_definition_id="ns=0;i=58",
                event_notifier=False,
            )
        ]

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return self._namespace_infos


class FakeOpcuaWithOutgoingReferences:
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
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=Temperature",
                        reference_type_id="ns=0;i=46",
                        reference_browse_name="HasProperty",
                    ),
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=Reset",
                        reference_type_id="ns=0;i=35",
                        reference_browse_name="Organizes",
                    ),
                ],
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


class FakeOpcuaWithGraphReference:
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
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=Sensor",
                        reference_type_id="ns=0;i=32",
                        reference_browse_name="Locations",
                    ),
                ],
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=Sensor",
                parent_node_id=None,
                browse_name="Sensor",
                display_name="Sensor",
                node_class="Object",
                data_type=None,
                type_definition_id="ns=1;i=1002",
                event_notifier=False,
            ),
        ]


class FakeOpcuaWithHasEventSourceAndHasComponent:
    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        return [
            OpcUaNodeInfo(
                node_id="ns=2;s=Server",
                parent_node_id=None,
                browse_name="Server",
                display_name="Server",
                node_class="Object",
                data_type=None,
                type_definition_id=None,
                event_notifier=False,
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=JobOrderResults",
                        reference_type_id="ns=0;i=36",
                        reference_browse_name="HasEventSource",
                    ),
                ],
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=JobOrderControl",
                parent_node_id=None,
                browse_name="JobOrderControl",
                display_name="JobOrderControl",
                node_class="Object",
                data_type=None,
                type_definition_id=None,
                event_notifier=False,
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=JobOrderResults",
                        reference_type_id="ns=0;i=47",
                        reference_browse_name="HasComponent",
                    ),
                ],
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=JobOrderResults",
                parent_node_id="ns=2;s=JobOrderControl",
                browse_name="JobOrderResults",
                display_name="JobOrderResults",
                node_class="Object",
                data_type=None,
                type_definition_id=None,
                event_notifier=False,
            ),
        ]


class FakeOpcuaWithReferenceSubtypeResolution:
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
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=Addon",
                        reference_type_id="ns=2;i=5001",
                        reference_browse_name="VendorRelationship",
                    ),
                ],
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=Addon",
                parent_node_id="ns=2;s=Machine",
                browse_name="Addon",
                display_name="Addon",
                node_class="Object",
                data_type=None,
                type_definition_id="ns=1;i=1002",
                event_notifier=False,
            ),
        ]

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        if reference_type_id == "ns=2;i=5001":
            return ["HasComponent", "Aggregates", "HierarchicalReferences"]
        return []


class FakeOpcuaWithAmbiguousReferenceSubtypeResolution:
    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        return [
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="OPC UA"),
            OpcUaNamespaceInfo(uri="urn:example:server", display_name="Server"),
            OpcUaNamespaceInfo(uri="urn:example:vendor", display_name="Vendor"),
        ]

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
                outgoing_references=[
                    OpcUaReferenceInfo(
                        target_node_id="ns=2;s=Addon",
                        reference_type_id="ns=2;i=6001",
                        reference_browse_name="VendorHasComponentLike",
                    ),
                ],
            ),
            OpcUaNodeInfo(
                node_id="ns=2;s=Addon",
                parent_node_id="ns=2;s=Machine",
                browse_name="Addon",
                display_name="Addon",
                node_class="Object",
                data_type=None,
                type_definition_id="ns=1;i=1002",
                event_notifier=False,
            ),
        ]

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        if reference_type_id == "ns=2;i=6001":
            return ["HasComponent", "HierarchicalReferences", "NonHierarchicalReferences"]
        return []


@pytest.mark.asyncio
async def test_model_builder_build_maps_nodes_children_properties_and_actions() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaForBuilder()))
    result = await builder.build()

    assert len(result.nodes_by_id) == 3
    assert len(result.root_ids) == 1
    assert len(result.property_to_node) == 1
    assert len(result.action_to_method) == 1
    assert result.instances_by_type_id == {"ns=1;i=1001": [result.root_ids[0]]}

    property_id = next(iter(result.property_to_node.keys()))
    action_id = next(iter(result.action_to_method.keys()))
    root_id = result.root_ids[0]
    assert property_id in result.children_by_id[root_id]
    assert action_id in result.children_by_id[root_id]
    assert result.parent_by_id[action_id] == root_id
    assert property_id not in result.parent_by_id
    assert result.property_to_node[property_id] == "ns=2;s=Temperature"
    assert result.action_to_method[action_id] == ("ns=2;s=Machine", "ns=2;s=Reset")
    assert result.node_id_by_name["Machine"] == root_id
    assert result.node_id_by_type["ns=1;i=11"] == property_id

    assert result.hierarchy_children_by_id[root_id] == [action_id]
    assert result.composition_children_by_id[root_id] == [property_id]
    assert result.hierarchy_parent_by_id[action_id] == root_id
    assert result.composition_parent_by_id[property_id] == root_id

    assert result.relationships_by_id[root_id]["HasChildren"] == [action_id]
    assert result.relationships_by_id[root_id]["HasComponent"] == [property_id]
    assert result.relationships_by_id[action_id]["HasParent"] == [root_id]
    assert result.relationships_by_id[property_id]["ComponentOf"] == [root_id]


@pytest.mark.asyncio
async def test_model_builder_prefers_outgoing_reference_metadata_when_present() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithOutgoingReferences()))
    result = await builder.build()

    root_id = result.root_ids[0]
    property_id = next(iter(result.property_to_node.keys()))
    action_id = next(iter(result.action_to_method.keys()))

    assert result.composition_children_by_id[root_id] == [property_id]
    assert result.hierarchy_children_by_id[root_id] == [action_id]
    assert result.relationships_by_id[root_id]["HasComponent"] == [property_id]
    assert result.relationships_by_id[root_id]["HasChildren"] == [action_id]


@pytest.mark.asyncio
async def test_model_builder_element_ids_ignore_namespace_index_reordering() -> None:
    standard = OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="OPC UA")
    process_values = OpcUaNamespaceInfo(
        uri="http://opcfoundation.org/UA/Machinery/ProcessValues/",
        display_name="Process Values",
    )
    other = OpcUaNamespaceInfo(uri="urn:example:other", display_name="Other")

    first_builder = ModelBuilder(
        cast(
            OpcUaClientProtocol,
            FakeOpcuaWithNamespaceInfo("ns=1;s=Machine", [standard, process_values]),
        )
    )
    reordered_builder = ModelBuilder(
        cast(
            OpcUaClientProtocol,
            FakeOpcuaWithNamespaceInfo("ns=2;s=Machine", [standard, other, process_values]),
        )
    )

    first_result = await first_builder.build()
    reordered_result = await reordered_builder.build()

    expected_id = stable_i3x_id(
        "nsu=http://opcfoundation.org/UA/Machinery/ProcessValues/;s=Machine",
        "asset",
    )
    assert first_result.root_ids == [expected_id]
    assert reordered_result.root_ids == [expected_id]
    assert first_result.nodes_by_id[expected_id].source_node_id == "ns=1;s=Machine"
    assert reordered_result.nodes_by_id[expected_id].source_node_id == "ns=2;s=Machine"


@pytest.mark.asyncio
async def test_model_builder_stores_graph_relationships_bidirectionally() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithGraphReference()))
    result = await builder.build()

    machine_id = result.node_id_by_name["Machine"]
    sensor_id = result.node_id_by_name["Sensor"]

    assert result.relationships_by_id[machine_id]["Locations"] == [sensor_id]
    assert result.relationships_by_id[sensor_id]["inverseOf_Locations"] == [machine_id]
    assert "Locations" in result.graph_relationship_names
    assert result.graph_relationship_namespace_by_id == {"Locations": "http://opcfoundation.org/UA/"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference_type_id", "expected_namespace"),
    [
        ("i=32", "http://opcfoundation.org/UA/"),
        ("ns=0;i=32", "http://opcfoundation.org/UA/"),
        ("ns=1;i=5001", "urn:example:references"),
        ("nsu=urn:example:expanded;i=5001", "urn:example:expanded"),
    ],
)
async def test_graph_relationship_namespace_flows_from_reference_type_to_query(
    reference_type_id: str, expected_namespace: str
) -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            nodes[0].outgoing_references[0].reference_type_id = reference_type_id
            return nodes

        async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
            return [
                OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="OPC UA"),
                OpcUaNamespaceInfo(uri="urn:example:references", display_name="Reference types"),
                OpcUaNamespaceInfo(uri="urn:example:instances", display_name="Instances"),
            ]

    client = cast(OpcUaClientProtocol, Client())
    result = await ModelBuilder(client).build()
    assert result.graph_relationship_namespace_by_id == {"Locations": expected_namespace}
    service = ModelQueryService(client, result)
    items = await service.get_relationship_types(expected_namespace)
    assert {item["elementId"] for item in items} == {"Locations", "inverseOf_Locations"}
    assert all(item["namespaceUri"] == expected_namespace for item in items)
    assert await service.get_relationship_types("https://opcfoundation.org/UA/") == []


@pytest.mark.asyncio
async def test_graph_relationship_with_unresolved_namespace_fails_explicitly() -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            nodes[0].outgoing_references[0].reference_type_id = "ns=9;i=5001"
            return nodes

    with pytest.raises(ValueError, match="Cannot resolve namespace for reference type"):
        await ModelBuilder(cast(OpcUaClientProtocol, Client())).build()


@pytest.mark.asyncio
async def test_graph_relationship_name_collision_qualifies_ids_and_edges() -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            nodes[0].outgoing_references.append(
                OpcUaReferenceInfo(
                    target_node_id="ns=2;s=Sensor",
                    reference_type_id="nsu=urn:example:custom;i=5001",
                    reference_browse_name="Locations",
                )
            )
            return nodes

    client = cast(OpcUaClientProtocol, Client())
    result = await ModelBuilder(client).build()
    expected = {
        "http://opcfoundation.org/UA/:Locations": "http://opcfoundation.org/UA/",
        "urn:example:custom:Locations": "urn:example:custom",
    }
    assert result.graph_relationship_namespace_by_id == expected
    machine_id = result.node_id_by_name["Machine"]
    sensor_id = result.node_id_by_name["Sensor"]
    service = ModelQueryService(client, result)
    for relationship_id, namespace_uri in expected.items():
        assert result.relationships_by_id[machine_id][relationship_id] == [sensor_id]
        assert result.relationships_by_id[sensor_id][f"inverseOf_{relationship_id}"] == [machine_id]
        assert (relationship_id, sensor_id) in result.graph_related_by_id[machine_id]
        items = await service.get_relationship_types(namespace_uri)
        assert {item["elementId"] for item in items} == {relationship_id, f"inverseOf_{relationship_id}"}
        assert [item["displayName"] for item in items] == ["Locations", "inverseOf_Locations"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["HasParent", "HasComponent", "inverseOf_Locations", "urn:reserved", "Has:Space %"])
async def test_graph_relationship_reserved_name_keeps_unique_ids(name: str) -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
            return ["NonHierarchicalReferences"]

        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            nodes[0].outgoing_references[0].reference_browse_name = name
            return nodes

    client = cast(OpcUaClientProtocol, Client())
    result = await ModelBuilder(client).build()
    items = await ModelQueryService(client, result).get_relationship_types()
    ids = [item["elementId"] for item in items]
    assert len(ids) == len(set(ids)) == 6
    graph_id = next(iter(result.graph_relationship_names))
    assert graph_id == f"http://opcfoundation.org/UA/:{name}"
    assert result.graph_relationship_display_name_by_id[graph_id] == name
    machine_id = result.node_id_by_name["Machine"]
    sensor_id = result.node_id_by_name["Sensor"]
    assert result.relationships_by_id[machine_id][graph_id] == [sensor_id]


@pytest.mark.asyncio
async def test_ambiguous_expanded_qualified_relationship_ids_fail_explicitly() -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            nodes[0].outgoing_references = [
                OpcUaReferenceInfo(
                    target_node_id="ns=2;s=Sensor",
                    reference_type_id=f"nsu={namespace_uri};i={index}",
                    reference_browse_name=name,
                )
                for index, (namespace_uri, name) in enumerate(
                    [
                        ("urn:example", "part:Locations"),
                        ("urn:example:part", "Locations"),
                        ("urn:other", "Locations"),
                    ],
                    start=5001,
                )
            ]
            return nodes

    with pytest.raises(ValueError, match="Expanded qualified relationship ID .* is ambiguous"):
        await ModelBuilder(cast(OpcUaClientProtocol, Client())).build()


@pytest.mark.asyncio
async def test_synthetic_secondary_parent_relationship_uses_i3x_namespace() -> None:
    class Client(FakeOpcuaWithGraphReference):
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            nodes = await super().browse_tree()
            ref = nodes[0].outgoing_references[0]
            ref.reference_type_id = "i=35"
            ref.reference_browse_name = "Organizes"
            nodes.append(
                OpcUaNodeInfo(
                    node_id="ns=2;s=Other",
                    parent_node_id=None,
                    browse_name="Other",
                    display_name="Other",
                    node_class="Object",
                    data_type=None,
                    outgoing_references=[
                        OpcUaReferenceInfo(
                            target_node_id="ns=2;s=Sensor",
                            reference_type_id="i=35",
                            reference_browse_name="Organizes",
                        )
                    ],
                )
            )
            return nodes

    client = cast(OpcUaClientProtocol, Client())
    result = await ModelBuilder(client).build()
    assert result.graph_relationship_namespace_by_id == {"OrganizedBy": "https://cesmii.org/i3x"}
    items = await ModelQueryService(client, result).get_relationship_types("https://cesmii.org/i3x")
    assert {item["elementId"] for item in items} == {
        "HasParent",
        "HasChildren",
        "HasComponent",
        "ComponentOf",
        "OrganizedBy",
        "inverseOf_OrganizedBy",
    }


@pytest.mark.asyncio
async def test_model_builder_keeps_has_event_source_as_graph_not_secondary_parent() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithHasEventSourceAndHasComponent()))
    result = await builder.build()

    server_id = result.node_id_by_name["Server"]
    control_id = result.node_id_by_name["JobOrderControl"]
    results_id = result.node_id_by_name["JobOrderResults"]

    assert result.hierarchy_parent_by_id[results_id] == control_id
    assert result.relationships_by_id[control_id]["HasChildren"] == [results_id]
    assert result.relationships_by_id[results_id]["HasParent"] == [control_id]
    assert result.relationships_by_id[server_id]["HasEventSource"] == [results_id]
    assert result.relationships_by_id[results_id]["inverseOf_HasEventSource"] == [server_id]
    assert "HasEventSource" in result.graph_relationship_names
    assert "inverseOf_OrganizedBy" not in result.relationships_by_id[results_id]


@pytest.mark.asyncio
async def test_model_builder_uses_reference_supertypes_for_subtype_classification() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithReferenceSubtypeResolution()))
    result = await builder.build()

    machine_id = result.node_id_by_name["Machine"]
    addon_id = result.node_id_by_name["Addon"]

    assert result.relationships_by_id[machine_id]["HasChildren"] == [addon_id]
    assert result.relationships_by_id[addon_id]["HasParent"] == [machine_id]


@pytest.mark.asyncio
async def test_model_builder_nonhierarchical_supertype_takes_precedence() -> None:
    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithAmbiguousReferenceSubtypeResolution()))
    result = await builder.build()

    machine_id = result.node_id_by_name["Machine"]
    addon_id = result.node_id_by_name["Addon"]

    assert result.relationships_by_id[machine_id]["VendorHasComponentLike"] == [addon_id]
    assert result.relationships_by_id[addon_id]["inverseOf_VendorHasComponentLike"] == [machine_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_fails", [False, True])
async def test_model_builder_applies_profile_from_type_definition_ancestor(batch_fails: bool) -> None:
    class FakeOpcuaWithTypeDefinitionHierarchy:
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            return [
                OpcUaNodeInfo(
                    node_id="ns=1;s=Machine",
                    parent_node_id=None,
                    browse_name="Machine",
                    display_name="Machine",
                    node_class="Object",
                    data_type=None,
                    type_definition_id="ns=2;i=26",
                )
            ]

        async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
            return [
                OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
                OpcUaNamespaceInfo(uri="urn:example:showcase", display_name="Showcase"),
                OpcUaNamespaceInfo(
                    uri="http://opcfoundation.org/UA/MachineTool/",
                    display_name="MachineTool",
                ),
                OpcUaNamespaceInfo(
                    uri="http://opcfoundation.org/UA/Machinery/",
                    display_name="Machinery",
                ),
            ]

        async def resolve_type_definition_hierarchy(
            self,
            type_definition_id: str,
        ) -> list[OpcUaTypeDefinitionInfo]:
            assert type_definition_id == "ns=2;i=26"
            return [
                OpcUaTypeDefinitionInfo(
                    node_id="ns=2;i=26",
                    browse_name="MachineToolType",
                ),
                OpcUaTypeDefinitionInfo(
                    node_id="ns=3;i=1001",
                    browse_name="FunctionalGroupType",
                ),
            ]

        async def resolve_type_definition_hierarchies(
            self,
            type_definition_ids: list[str],
        ) -> dict[str, list[OpcUaTypeDefinitionInfo]]:
            assert type_definition_ids == ["ns=2;i=26"]
            if batch_fails:
                raise RuntimeError("Batch request failed")
            return {"ns=2;i=26": await self.resolve_type_definition_hierarchy("ns=2;i=26")}

    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithTypeDefinitionHierarchy()))
    result = await builder.build()

    machine_id = result.node_id_by_name["Machine"]
    assert result.applied_profile_ids_by_id[machine_id] == ["machinery", "generic"]
    assert result.semantic_role_by_id[machine_id] == "group"


@pytest.mark.asyncio
async def test_model_builder_profile_relationship_flags_override_reference_classification() -> None:
    class FakeOpcuaWithForcedRelationshipOverrides:
        async def browse_tree(self) -> list[OpcUaNodeInfo]:
            return [
                OpcUaNodeInfo(
                    node_id="ns=1;s=Root",
                    parent_node_id=None,
                    browse_name="Root",
                    display_name="Root",
                    node_class="Object",
                    data_type=None,
                    outgoing_references=[
                        OpcUaReferenceInfo(
                            target_node_id="ns=1;s=Group",
                            reference_type_id="ns=0;i=46",
                            reference_browse_name="HasProperty",
                        ),
                        OpcUaReferenceInfo(
                            target_node_id="ns=1;s=DataItem",
                            reference_type_id="ns=0;i=35",
                            reference_browse_name="Organizes",
                        ),
                    ],
                ),
                OpcUaNodeInfo(
                    node_id="ns=1;s=Group",
                    parent_node_id="ns=1;s=Root",
                    browse_name="Group",
                    display_name="Group",
                    node_class="Object",
                    data_type=None,
                    type_definition_id="ns=2;i=26",
                ),
                OpcUaNodeInfo(
                    node_id="ns=1;s=DataItem",
                    parent_node_id="ns=1;s=Root",
                    browse_name="DataItem",
                    display_name="DataItem",
                    node_class="Object",
                    data_type=None,
                    type_definition_id="ns=2;i=27",
                ),
            ]

        async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
            return [
                OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
                OpcUaNamespaceInfo(uri="urn:example:showcase", display_name="Showcase"),
                OpcUaNamespaceInfo(
                    uri="http://opcfoundation.org/UA/MachineTool/",
                    display_name="MachineTool",
                ),
                OpcUaNamespaceInfo(
                    uri="http://opcfoundation.org/UA/Machinery/",
                    display_name="Machinery",
                ),
            ]

        async def resolve_type_definition_hierarchy(
            self,
            type_definition_id: str,
        ) -> list[OpcUaTypeDefinitionInfo]:
            type_info_by_id = {
                "ns=2;i=26": OpcUaTypeDefinitionInfo(
                    node_id="ns=3;i=1001",
                    browse_name="FunctionalGroupType",
                ),
                "ns=2;i=27": OpcUaTypeDefinitionInfo(
                    node_id="ns=3;i=1002",
                    browse_name="DataItemType",
                ),
            }
            return [type_info_by_id[type_definition_id]]

    builder = ModelBuilder(cast(OpcUaClientProtocol, FakeOpcuaWithForcedRelationshipOverrides()))
    result = await builder.build()

    root_id = result.node_id_by_name["Root"]
    group_id = result.node_id_by_name["Group"]
    data_item_id = result.node_id_by_name["DataItem"]

    assert result.hierarchy_children_by_id[root_id] == [group_id]
    assert result.composition_children_by_id[root_id] == [data_item_id]
    assert result.relationships_by_id[root_id]["HasChildren"] == [group_id]
    assert result.relationships_by_id[root_id]["HasComponent"] == [data_item_id]


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
