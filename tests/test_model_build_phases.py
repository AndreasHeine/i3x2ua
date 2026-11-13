"""Unit tests for the ``ModelBuilder`` phase modules (PR-11 decomposition).

The phases degrade on OPC UA failures instead of poisoning the model: a failed namespace,
supertype or hierarchy lookup drops only what it could not resolve, and cancellation still
propagates. The graph and mapping phases are asserted on their own, so the relationship rules
stay testable without a full browse.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from i3x_server.domain.ports.opcua import (
    OpcUaClientProtocol,
    OpcUaNamespaceInfo,
    OpcUaNodeInfo,
    OpcUaReferenceInfo,
    OpcUaTypeDefinitionInfo,
)
from i3x_server.model import build_phases, node_mapping, reference_graph

_LOGGER = "i3x_server.model.build_phases"
_GRAPH_LOGGER = "i3x_server.model.reference_graph"


def _client(double: Any) -> OpcUaClientProtocol:
    """Hand a phase double to a phase that declares the full port as its collaborator.

    The phases probe their optional seams with ``getattr``, so a double implements only what the
    phase under test reads; the cast states that this seam subset is what the port sees.
    """
    return cast(OpcUaClientProtocol, double)


class _FatalFailure(BaseException):
    """A failure the phase must not degrade into a partially built model."""


class _NamespaceClient:
    """Client with the namespace seam; the outcome may be a failure to raise."""

    def __init__(self, infos: list[OpcUaNamespaceInfo] | Exception) -> None:
        self._infos = infos

    async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
        if isinstance(self._infos, Exception):
            raise self._infos
        return self._infos


class _NoSeamClient:
    """Test double without any optional metadata seam."""

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        return []


class _SupertypeClient:
    # Entries may carry non-string names on purpose: the phase must drop them rather than
    # crash the classifier, so the double is allowed to hand over malformed values.
    def __init__(self, outcomes: dict[str, list[Any] | BaseException]) -> None:
        self._outcomes = outcomes
        self.requested: list[str] = []

    async def resolve_reference_type_supertype_browse_names(self, reference_type_id: str) -> list[str]:
        self.requested.append(reference_type_id)
        outcome = self._outcomes.get(reference_type_id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome or []


class _HierarchyClient:
    """Seam client whose batch and individual resolutions can each be made to fail.

    ``batch=None`` removes the batch seam entirely, so the individual seam is exercised.
    """

    def __init__(
        self,
        batch: dict[str, list[Any]] | Exception | None = None,
        individual: dict[str, list[OpcUaTypeDefinitionInfo] | Exception] | None = None,
    ) -> None:
        self._batch = batch
        self._individual = individual or {}
        self.batch_requested: list[str] | None = None
        self.individual_requested: list[str] = []
        if batch is None:
            # The batch seam must be *absent*, not merely falsy: ``object.__setattr__`` installs
            # the None that ``getattr`` in the phase reads, which a typed attribute could not.
            object.__setattr__(self, "resolve_type_definition_hierarchies", None)

    async def resolve_type_definition_hierarchies(self, type_definition_ids: list[str]) -> dict[str, list[Any]]:
        self.batch_requested = type_definition_ids
        if isinstance(self._batch, Exception):
            raise self._batch
        assert self._batch is not None
        return self._batch

    async def resolve_type_definition_hierarchy(self, type_definition_id: str) -> list[OpcUaTypeDefinitionInfo]:
        self.individual_requested.append(type_definition_id)
        outcome = self._individual[type_definition_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _instance(type_id: str) -> OpcUaNodeInfo:
    return OpcUaNodeInfo(
        node_id="ns=2;s=Machine",
        parent_node_id=None,
        browse_name="Machine",
        display_name="Machine",
        node_class="Object",
        data_type=None,
        type_definition_id=type_id,
    )


def _node(node_id: str, *, class_name: str = "Object", parent: str | None = None) -> OpcUaNodeInfo:
    identifier = node_id.split(";", 1)[1]
    browse_name = identifier.split("=", 1)[1] if "=" in identifier else identifier
    return OpcUaNodeInfo(node_id, parent, browse_name, browse_name, class_name, None)


def _namespace_infos() -> list[OpcUaNamespaceInfo]:
    return [
        OpcUaNamespaceInfo(uri="urn:base", display_name="base"),
        OpcUaNamespaceInfo(uri="urn:x", display_name="x"),
        OpcUaNamespaceInfo(uri="urn:y", display_name="y"),
    ]


def _graph(nodes: list[OpcUaNodeInfo]) -> reference_graph.GraphResult:
    by_source = {node.node_id: node for node in nodes}
    _, active_profiles_by_source = build_phases.build_active_profiles(by_source, {}, {})
    return reference_graph.build_reference_graph(
        opc_nodes=nodes,
        by_source_node=by_source,
        namespace_infos=_namespace_infos(),
        reference_supertypes_by_id={},
        type_definition_hierarchy_by_id={},
        active_profiles_by_source=active_profiles_by_source,
    )


async def test_collect_namespace_infos_degrades_to_empty_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    infos, namespace_uri_by_index = await build_phases.collect_namespace_infos(
        _client(_NamespaceClient(RuntimeError("server rejected the namespace browse")))
    )

    assert infos == []
    assert namespace_uri_by_index == {}
    assert "Namespace info resolution failed" in caplog.text

    # Clients without the seam resolve nothing rather than raising.
    assert await build_phases.collect_namespace_infos(_client(_NoSeamClient())) == ([], {})


async def test_collect_namespace_infos_skips_blank_uris() -> None:
    infos, namespace_uri_by_index = await build_phases.collect_namespace_infos(
        _client(
            _NamespaceClient(
                [
                    OpcUaNamespaceInfo(uri="urn:cesmii", display_name="cesmii"),
                    OpcUaNamespaceInfo(uri="", display_name="blank"),
                ]
            )
        )
    )

    assert namespace_uri_by_index == {0: "urn:cesmii"}
    assert len(infos) == 2


async def test_resolve_reference_supertypes_resolves_each_id_once() -> None:
    nodes = [
        OpcUaNodeInfo(
            node_id="ns=0;i=1",
            parent_node_id=None,
            browse_name="A",
            display_name="A",
            node_class="Object",
            data_type=None,
            outgoing_references=[
                OpcUaReferenceInfo("ns=0;i=2", "ns=1;i=200", "Locations"),
                OpcUaReferenceInfo("ns=0;i=3", "ns=1;i=200", "Locations"),
            ],
        )
    ]
    client = _SupertypeClient({"ns=1;i=200": ["NonHierarchicalReferences", 7]})

    supertypes = await build_phases.resolve_reference_supertypes(_client(client), nodes)

    # Distinct ids only; non-string names are dropped rather than crashing the classifier.
    assert client.requested == ["ns=1;i=200"]
    assert supertypes == {"ns=1;i=200": ["NonHierarchicalReferences"]}
    assert await build_phases.resolve_reference_supertypes(_client(client), []) == {}
    assert await build_phases.resolve_reference_supertypes(_client(_NoSeamClient()), nodes) == {}


async def test_resolve_reference_supertypes_keeps_surviving_chains(caplog: pytest.LogCaptureFixture) -> None:
    nodes = [
        OpcUaNodeInfo(
            node_id="ns=0;i=1",
            parent_node_id=None,
            browse_name="A",
            display_name="A",
            node_class="Object",
            data_type=None,
            outgoing_references=[
                OpcUaReferenceInfo("ns=0;i=2", "ns=1;i=200", "Locations"),
                OpcUaReferenceInfo("ns=0;i=3", "ns=1;i=201", "Monitors"),
            ],
        )
    ]
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    client = _SupertypeClient({"ns=1;i=200": RuntimeError("browse failed"), "ns=1;i=201": ["ReferenceType"]})

    supertypes = await build_phases.resolve_reference_supertypes(_client(client), nodes)

    assert supertypes == {"ns=1;i=201": ["ReferenceType"]}
    assert "Reference supertype resolution failed for reference_type_id=ns=1;i=200" in caplog.text


async def test_resolve_reference_supertypes_propagates_base_exceptions() -> None:
    nodes = [
        OpcUaNodeInfo(
            node_id="ns=0;i=1",
            parent_node_id=None,
            browse_name="A",
            display_name="A",
            node_class="Object",
            data_type=None,
            outgoing_references=[OpcUaReferenceInfo("ns=0;i=2", "ns=1;i=200", "Locations")],
        )
    ]
    client = _SupertypeClient({"ns=1;i=200": _FatalFailure()})

    # Only ordinary ``Exception`` failures degrade the model; a BaseException from the
    # concurrent resolution must not be swallowed into a partially built model.
    with pytest.raises(_FatalFailure):
        await build_phases.resolve_reference_supertypes(_client(client), nodes)


async def test_resolve_type_hierarchies_falls_back_after_batch_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    client = _HierarchyClient(
        RuntimeError("batch browse failed"),
        {"ns=1;i=500": [OpcUaTypeDefinitionInfo(node_id="ns=1;i=499", browse_name="MachineBase")]},
    )

    hierarchies = await build_phases.resolve_type_hierarchies(
        _client(client),
        [_instance("ns=1;i=500")],
        {1: "urn:example"},
    )

    assert "Batch type definition hierarchy resolution failed; retrying individual types" in caplog.text
    assert client.individual_requested == ["ns=1;i=500"]
    # A missing namespace_uri is resolved from the namespace table instead of being invented.
    resolved = OpcUaTypeDefinitionInfo(node_id="ns=1;i=499", browse_name="MachineBase", namespace_uri="urn:example")
    assert hierarchies == {"ns=1;i=500": (resolved,)}


async def test_resolve_type_hierarchies_isolates_a_broken_chain(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    client = _HierarchyClient(
        None,
        {
            "ns=1;i=500": RuntimeError("chain browse failed"),
            "ns=1;i=501": [OpcUaTypeDefinitionInfo(node_id="ns=1;i=501", browse_name="Good", namespace_uri="urn:ex")],
        },
    )

    hierarchies = await build_phases.resolve_type_hierarchies(
        _client(client),
        [_instance("ns=1;i=500"), _instance("ns=1;i=501")],
        {},
    )

    assert list(hierarchies) == ["ns=1;i=501"]
    assert "Type definition hierarchy resolution failed for type_definition_id=ns=1;i=500" in caplog.text


async def test_resolve_type_hierarchies_batch_skips_missing_and_malformed_entries() -> None:
    client = _HierarchyClient({"ns=1;i=500": ["not a type info", OpcUaTypeDefinitionInfo("ns=1;i=500", "T")]})

    hierarchies = await build_phases.resolve_type_hierarchies(
        _client(client),
        [_instance("ns=1;i=500"), _instance("ns=1;i=501")],
        {},
    )

    assert client.batch_requested == ["ns=1;i=500", "ns=1;i=501"]
    assert client.individual_requested == []
    # The id the batch did not return is skipped; the malformed entry is dropped, so only the
    # well-formed type info of the returned chain survives (with no namespace table to resolve).
    assert hierarchies == {"ns=1;i=500": (OpcUaTypeDefinitionInfo(node_id="ns=1;i=500", browse_name="T"),)}


async def test_resolve_type_hierarchies_without_seams_returns_empty() -> None:
    assert await build_phases.resolve_type_hierarchies(_client(_NoSeamClient()), [_instance("ns=1;i=500")], {}) == {}


async def test_build_active_profiles_keys_every_source_node() -> None:
    nodes = {
        "ns=2;s=Machine": _instance("ns=1;i=500"),
        "ns=2;s=Temp": OpcUaNodeInfo("ns=2;s=Temp", "ns=2;s=Machine", "Temp", "Temp", "Variable", "Double"),
    }
    hierarchies = await build_phases.resolve_type_hierarchies(
        _client(_HierarchyClient({"ns=1;i=500": [OpcUaTypeDefinitionInfo("ns=1;i=500", "MachineType")]})),
        list(nodes.values()),
        {2: "urn:example"},
    )

    namespace_uri_by_source, active_profiles_by_source = build_phases.build_active_profiles(
        nodes,
        hierarchies,
        {2: "urn:example"},
    )

    assert namespace_uri_by_source == {"ns=2;s=Machine": "urn:example", "ns=2;s=Temp": "urn:example"}
    assert set(active_profiles_by_source) == set(nodes)


def test_reference_graph_turns_extra_hierarchy_parents_into_organized_by_edges() -> None:
    nodes = [_node("ns=0;i=1"), _node("ns=0;i=2"), _node("ns=0;i=3")]
    nodes[0].outgoing_references = [OpcUaReferenceInfo("ns=0;i=3", "i=35", "Organizes")]
    nodes[1].outgoing_references = [OpcUaReferenceInfo("ns=0;i=3", "i=35", "Organizes")]

    graph = _graph(nodes)
    child_id = graph.mapped_id_by_source["ns=0;i=3"]
    parent_id = graph.mapped_id_by_source["ns=0;i=1"]
    other_parent_id = graph.mapped_id_by_source["ns=0;i=2"]

    # One parent wins (deterministic tie-break); the loser stays reachable as a graph edge.
    assert graph.hierarchy_parent_by_id[child_id] == parent_id
    assert graph.graph_related_by_id[child_id] == [("OrganizedBy", other_parent_id)]
    assert graph.graph_relationship_inverse_by_id["OrganizedBy"] == "inverseOf_OrganizedBy"
    assert graph.relationships_by_id[child_id]["OrganizedBy"] == [other_parent_id]
    assert graph.relationships_by_id[other_parent_id]["inverseOf_OrganizedBy"] == [child_id]


def test_reference_graph_ignores_an_inverse_name_that_is_already_a_relationship(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_GRAPH_LOGGER)
    nodes = [_node("ns=0;i=1"), _node("ns=0;i=2"), _node("ns=0;i=3"), _node("ns=0;i=4")]
    # "Locations" publishes InverseName "Widget", but "Widget" is itself a registered
    # relationship (qualified because it exists in two namespaces) that declares its own
    # reverse, so the inverse cannot reuse it and must stay unique.
    nodes[0].outgoing_references = [
        OpcUaReferenceInfo("ns=0;i=2", "ns=1;i=200", "Locations", reference_inverse_name="Widget")
    ]
    nodes[2].outgoing_references = [OpcUaReferenceInfo("ns=0;i=3", "ns=1;i=201", "Widget")]
    nodes[3].outgoing_references = [OpcUaReferenceInfo("ns=0;i=4", "ns=2;i=302", "Widget")]

    graph = _graph(nodes)
    source_id = graph.mapped_id_by_source["ns=0;i=1"]
    target_id = graph.mapped_id_by_source["ns=0;i=2"]

    assert graph.graph_relationship_namespace_by_id["urn:x:Widget"] == "urn:x"
    assert graph.graph_relationship_inverse_by_id["Locations"] == "inverseOf_Locations"
    assert graph.relationships_by_id[source_id]["Locations"] == [target_id]
    # The reverse edge is stored on the target so both directions stay navigable.
    assert graph.relationships_by_id[target_id]["inverseOf_Locations"] == [source_id]
    assert "declares its own reverse" in caplog.text
    assert "already claimed" in caplog.text


def test_map_nodes_falls_back_to_the_source_parent_for_composition_only_children() -> None:
    nodes = [_node("ns=0;i=1"), _node("ns=0;i=2"), _node("ns=0;i=3", class_name="Variable", parent="ns=0;i=1")]
    nodes[0].outgoing_references = [
        # Hierarchy child: the i3X parentId comes from the hierarchy selection.
        OpcUaReferenceInfo("ns=0;i=2", "i=35", "Organizes"),
        # Composition child (Variable target): no hierarchy parent, so the OPC UA parent is
        # the fallback the API still navigates with.
        OpcUaReferenceInfo("ns=0;i=3", "i=47", "HasComponent"),
    ]
    graph = _graph(nodes)
    by_source = {node.node_id: node for node in nodes}

    mapping = node_mapping.map_nodes(
        by_source_node=by_source,
        graph=graph,
        type_definition_hierarchy_by_id={},
        namespace_uri_by_source={},
        active_profiles_by_source=build_phases.build_active_profiles(by_source, {}, {})[1],
    )

    hierarchy_child_id = graph.mapped_id_by_source["ns=0;i=2"]
    composition_child_id = graph.mapped_id_by_source["ns=0;i=3"]
    root_id = graph.mapped_id_by_source["ns=0;i=1"]

    assert mapping.root_ids == [root_id]
    assert mapping.parent_id_by_id[hierarchy_child_id] == root_id
    assert mapping.parent_id_by_id[composition_child_id] == root_id
    assert mapping.nodes_by_id[hierarchy_child_id].parent_id == root_id
    assert mapping.nodes_by_id[composition_child_id].children == []
    assert mapping.nodes_by_id[root_id].relationships["HasComponent"] == [composition_child_id]


def test_map_nodes_exposes_writable_and_type_metadata() -> None:
    variable = _node("ns=1;s=Temp", class_name="Variable")
    variable.user_access_level = 0x02
    variable.data_type = "Double"
    graph = _graph([variable])

    mapping = node_mapping.map_nodes(
        by_source_node={variable.node_id: variable},
        graph=graph,
        type_definition_hierarchy_by_id={},
        namespace_uri_by_source={},
        active_profiles_by_source={},
    )

    element_id = graph.mapped_id_by_source[variable.node_id]
    mapped = mapping.nodes_by_id[element_id]
    assert mapped.kind == "property"
    assert mapped.type == "Double"
    assert mapped.metadata["writable"] is True
    assert mapping.property_to_node == {element_id: variable.node_id}


def test_build_element_id_index_canonicalizes_namespace_uri_ids() -> None:
    variable = _node("ns=1;s=Temp", class_name="Variable")
    graph = _graph([variable])
    mapping = node_mapping.map_nodes(
        by_source_node={variable.node_id: variable},
        graph=graph,
        type_definition_hierarchy_by_id={},
        namespace_uri_by_source={"ns=1;s=Temp": "urn:x"},
        active_profiles_by_source={},
    )

    element_id = graph.mapped_id_by_source[variable.node_id]
    by_node_id, by_expanded_node_id = node_mapping.build_element_id_index(mapping.nodes_by_id, {1: "urn:x"})

    # The indexed spelling resolves through the raw index, the expanded one through the
    # expanded index; both canonical forms point at the same element.
    assert by_node_id["ns=1;s=Temp"] == element_id
    assert by_expanded_node_id["nsu=urn:x;s=Temp"] == element_id
    assert "nsu=urn:x;s=Temp" not in by_node_id
