"""Configurable semantic profiles loaded from overrides/semantic_overrides.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo, OpcUaNodeInfo, OpcUaTypeDefinitionInfo
from i3x_server.infrastructure.opcua.client import OpcUaClientProtocol, OpcUaReferenceInfo
from i3x_server.model.builder import ModelBuilder
from i3x_server.model.semantic_overrides import (
    DEFAULT_SEMANTIC_OVERRIDES_PATH,
    SemanticOverridesError,
    load_semantic_profiles,
)
from i3x_server.model.semantic_profiles import (
    SemanticProfile,
    active_profiles_for_node,
    load_default_profiles,
    resolve_semantic_role,
)

# Tests validate against the schema shipped in the repository, so they exercise the real contract.
REPO_SCHEMA_PATH = "overrides/semantic_overrides.schema.json"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load(tmp_path: Path, payload: dict[str, Any]) -> tuple[SemanticProfile, ...]:
    overrides_path = tmp_path / "semantic_overrides.json"
    _write_json(overrides_path, payload)
    return load_semantic_profiles(path=overrides_path, schema_path=REPO_SCHEMA_PATH)


def test_defaults_when_file_absent(tmp_path: Path) -> None:
    profiles = load_semantic_profiles(path=tmp_path / "semantic_overrides.json", schema_path=REPO_SCHEMA_PATH)

    assert profiles == load_default_profiles()
    assert tuple(profile.profile_id for profile in profiles) == ("machinery", "generic")


def test_empty_profiles_array_preserves_defaults(tmp_path: Path) -> None:
    profiles = _load(tmp_path, {"profiles": []})

    assert profiles == load_default_profiles()


def test_new_custom_profile_extends_builtins(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {
                    "profile_id": "vendor_asset",
                    "priority": 150,
                    "namespace_uri": "urn:example:vendor",
                    "known_types": ["VendorMachineType"],
                    "hierarchy_references": ["Organizes"],
                    "composition_references": ["HasProperty"],
                    "graph_references": ["FlowsTo"],
                    "overrides": [
                        {
                            "type_name": "VendorIdentificationType",
                            "namespace_uri": "urn:example:vendor",
                            "semantic_role": "asset",
                            "force_hierarchy": True,
                        }
                    ],
                }
            ]
        },
    )

    assert tuple(profile.profile_id for profile in profiles) == ("vendor_asset", "machinery", "generic")
    custom = profiles[0]
    assert custom.priority == 150
    assert custom.namespace_uri == "urn:example:vendor"
    assert custom.known_types == ("VendorMachineType",)
    assert custom.hierarchy_references == ("Organizes",)
    assert custom.composition_references == ("HasProperty",)
    assert custom.graph_references == ("FlowsTo",)
    assert custom.overrides[0].type_name == "VendorIdentificationType"
    assert custom.overrides[0].semantic_role == "asset"
    assert custom.overrides[0].force_hierarchy is True
    assert custom.overrides[0].force_composition is False


def test_custom_profile_with_existing_id_replaces_builtin_entirely(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {
                    "profile_id": "machinery",
                    "priority": 200,
                    "namespace_uri": "urn:example:plant",
                    "known_types": ["PlantMachineType"],
                    "hierarchy_references": ["Organizes"],
                    "composition_references": [],
                    "graph_references": [],
                    "overrides": [],
                }
            ]
        },
    )

    assert tuple(profile.profile_id for profile in profiles) == ("machinery", "generic")
    replaced = profiles[0]
    # The replacement is the custom definition, not the built-in: the canonical Machinery
    # namespace and its identity overrides are gone.
    assert replaced.namespace_uri == "urn:example:plant"
    assert replaced.known_types == ("PlantMachineType",)
    assert replaced.overrides == ()


def test_duplicate_custom_profile_ids_rejected(tmp_path: Path) -> None:
    with pytest.raises(SemanticOverridesError) as raised:
        _load(
            tmp_path,
            {
                "profiles": [
                    {"profile_id": "dup", "priority": 5, "namespace_uri": ""},
                    {"profile_id": "dup", "priority": 6, "namespace_uri": ""},
                ]
            },
        )

    assert "duplicate" in str(raised.value).lower()
    assert "dup" in str(raised.value)


def test_equal_priority_custom_profiles_precede_builtin_and_keep_file_order(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {"profile_id": "alpha", "priority": 100, "namespace_uri": ""},
                {"profile_id": "beta", "priority": 100, "namespace_uri": ""},
            ]
        },
    )

    assert tuple(profile.profile_id for profile in profiles) == ("alpha", "beta", "machinery", "generic")


def test_priority_ordering_is_descending_across_custom_and_builtin(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {"profile_id": "mid", "priority": 50, "namespace_uri": ""},
                {"profile_id": "top", "priority": 150, "namespace_uri": ""},
            ]
        },
    )

    assert tuple(profile.profile_id for profile in profiles) == ("top", "machinery", "mid", "generic")


def test_invalid_json_fails_startup(tmp_path: Path) -> None:
    overrides_path = tmp_path / "semantic_overrides.json"
    overrides_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SemanticOverridesError) as raised:
        load_semantic_profiles(path=overrides_path, schema_path=REPO_SCHEMA_PATH)

    assert "Invalid JSON" in str(raised.value)


def test_schema_violation_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(SemanticOverridesError) as raised:
        # ``priority`` is required by the shipped schema.
        _load(tmp_path, {"profiles": [{"profile_id": "x", "namespace_uri": ""}]})

    assert "validation" in str(raised.value).lower()


def test_override_confidence_is_not_exposed(tmp_path: Path) -> None:
    # Confidence has no runtime behavior yet, so the shipped schema rejects it.
    with pytest.raises(SemanticOverridesError) as raised:
        _load(
            tmp_path,
            {
                "profiles": [
                    {
                        "profile_id": "x",
                        "priority": 5,
                        "namespace_uri": "",
                        "overrides": [{"type_name": "WidgetType", "confidence": "high"}],
                    }
                ]
            },
        )

    assert "confidence" in str(raised.value)


def test_conflicting_forced_relationship_flags_rejected(tmp_path: Path) -> None:
    with pytest.raises(SemanticOverridesError) as raised:
        _load(
            tmp_path,
            {
                "profiles": [
                    {
                        "profile_id": "x",
                        "priority": 5,
                        "namespace_uri": "",
                        "overrides": [{"type_name": "WidgetType", "force_hierarchy": True, "force_composition": True}],
                    }
                ]
            },
        )

    assert "force_hierarchy" in str(raised.value)
    assert "force_composition" in str(raised.value)


def test_unreadable_file_fails_startup(tmp_path: Path) -> None:
    overrides_path = tmp_path / "semantic_overrides.json"
    overrides_path.write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(SemanticOverridesError) as raised:
        load_semantic_profiles(path=overrides_path, schema_path=REPO_SCHEMA_PATH)

    assert "could not be read" in str(raised.value)


def test_non_object_document_fails_startup(tmp_path: Path) -> None:
    overrides_path = tmp_path / "semantic_overrides.json"
    overrides_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SemanticOverridesError) as raised:
        load_semantic_profiles(path=overrides_path, schema_path=REPO_SCHEMA_PATH)

    assert "JSON object" in str(raised.value)


def test_invalid_schema_file_fails_startup(tmp_path: Path) -> None:
    overrides_path = tmp_path / "semantic_overrides.json"
    _write_json(overrides_path, {"profiles": []})
    schema_path = tmp_path / "semantic_overrides.schema.json"
    _write_json(schema_path, {"type": "not-a-valid-type"})

    with pytest.raises(SemanticOverridesError) as raised:
        load_semantic_profiles(path=overrides_path, schema_path=schema_path)

    assert "schema" in str(raised.value).lower()


def test_missing_schema_file_with_present_overrides_fails_startup(tmp_path: Path) -> None:
    overrides_path = tmp_path / "semantic_overrides.json"
    _write_json(overrides_path, {"profiles": []})

    with pytest.raises(SemanticOverridesError) as raised:
        load_semantic_profiles(path=overrides_path, schema_path=tmp_path / "missing.schema.json")

    assert "schema" in str(raised.value)


def test_custom_namespace_scoped_override_does_not_collide_with_other_namespace(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {
                    "profile_id": "vendor_widget",
                    "priority": 120,
                    "namespace_uri": "urn:example:vendor",
                    "overrides": [
                        {
                            "type_name": "WidgetType",
                            "namespace_uri": "urn:example:vendor",
                            "semantic_role": "component",
                        }
                    ],
                }
            ]
        },
    )

    scoped_node = OpcUaNodeInfo(
        node_id="ns=2;s=Widget",
        parent_node_id=None,
        browse_name="Widget",
        display_name="Widget",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=900",
    )
    vendor_chain = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=3;i=900",
            browse_name="WidgetType",
            namespace_uri="urn:example:vendor/",
        ),
    )
    other_chain = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=4;i=900",
            browse_name="WidgetType",
            namespace_uri="urn:other:vendor/",
        ),
    )

    vendor_active = active_profiles_for_node(scoped_node, vendor_chain, profiles)
    assert resolve_semantic_role(scoped_node, vendor_active, type_definition_hierarchy=vendor_chain) == "component"
    # A same-named type in an unrelated namespace carries no override (ancestry semantics preserved).
    other_active = active_profiles_for_node(scoped_node, other_chain, profiles)
    assert resolve_semantic_role(scoped_node, other_active, type_definition_hierarchy=other_chain) == "asset"
    assert vendor_active.profile_ids == ("vendor_widget", "generic")
    assert other_active.profile_ids == ("generic",)


def test_canonical_namespace_match_ignores_trailing_slash(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {"profile_id": "vendor", "priority": 120, "namespace_uri": "urn:example:vendor/"},
            ]
        },
    )
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Machine",
        parent_node_id=None,
        browse_name="Machine",
        display_name="Machine",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=900",
    )
    chain = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=3;i=900",
            browse_name="VendorMachineType",
            namespace_uri="urn:example:vendor",
        ),
    )

    assert active_profiles_for_node(node, chain, profiles).profile_ids == ("vendor", "generic")


def test_sub_namespace_does_not_match_canonical_machinery_namespace(tmp_path: Path) -> None:
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {
                    "profile_id": "custom_machinery",
                    "priority": 200,
                    "namespace_uri": "http://opcfoundation.org/UA/Machinery/",
                }
            ]
        },
    )
    node = OpcUaNodeInfo(
        node_id="ns=2;s=Signal",
        parent_node_id=None,
        browse_name="Signal",
        display_name="Signal",
        node_class="Object",
        data_type=None,
        type_definition_id="ns=2;i=900",
    )
    process_values_chain = (
        OpcUaTypeDefinitionInfo(
            node_id="ns=3;i=900",
            browse_name="ProcessValueType",
            namespace_uri="http://opcfoundation.org/UA/Machinery/ProcessValues/",
        ),
    )

    # Never a substring match: ProcessValues is a distinct namespace and must not activate
    # a profile scoped to the canonical Machinery namespace.
    assert active_profiles_for_node(node, process_values_chain, profiles).profile_ids == ("generic",)


@pytest.mark.asyncio
async def test_model_builder_applies_loaded_custom_profiles(tmp_path: Path) -> None:
    """End-to-end: custom profiles loaded from disk drive roles and forced hierarchy in a real build."""
    profiles = _load(
        tmp_path,
        {
            "profiles": [
                {
                    "profile_id": "vendor_asset",
                    "priority": 150,
                    "namespace_uri": "urn:example:vendor",
                    "hierarchy_references": ["Organizes"],
                    "composition_references": ["HasProperty"],
                    "graph_references": [],
                    "overrides": [
                        {
                            "type_name": "VendorIdentificationType",
                            "namespace_uri": "urn:example:vendor",
                            "semantic_role": "asset",
                            "force_hierarchy": True,
                        }
                    ],
                }
            ]
        },
    )

    class FakeOpcuaVendor:
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
                            target_node_id="ns=1;s=Identification",
                            reference_type_id="ns=0;i=46",
                            reference_browse_name="HasProperty",
                        ),
                        OpcUaReferenceInfo(
                            target_node_id="ns=1;s=Temp",
                            reference_type_id="ns=0;i=46",
                            reference_browse_name="HasProperty",
                        ),
                    ],
                ),
                OpcUaNodeInfo(
                    node_id="ns=1;s=Identification",
                    parent_node_id="ns=1;s=Root",
                    browse_name="Identification",
                    display_name="Identification",
                    node_class="Object",
                    data_type=None,
                    type_definition_id="ns=2;i=27",
                ),
                OpcUaNodeInfo(
                    node_id="ns=1;s=Temp",
                    parent_node_id="ns=1;s=Root",
                    browse_name="Temp",
                    display_name="Temp",
                    node_class="Variable",
                    data_type="Double",
                ),
            ]

        async def get_namespace_infos(self) -> list[OpcUaNamespaceInfo]:
            return [
                OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
                OpcUaNamespaceInfo(uri="urn:example:showcase", display_name="Showcase"),
                OpcUaNamespaceInfo(uri="urn:example:vendor/", display_name="Vendor"),
            ]

        async def resolve_type_definition_hierarchy(self, type_definition_id: str) -> list[OpcUaTypeDefinitionInfo]:
            assert type_definition_id == "ns=2;i=27"
            return [
                OpcUaTypeDefinitionInfo(node_id="ns=2;i=27", browse_name="VendorIdentificationType"),
            ]

    result = await ModelBuilder(
        cast(OpcUaClientProtocol, FakeOpcuaVendor()),
        semantic_profiles=profiles,
    ).build()

    root_id = result.node_id_by_name["Root"]
    identification_id = result.node_id_by_name["Identification"]
    temp_id = result.node_id_by_name["Temp"]

    assert result.applied_profile_ids_by_id[identification_id] == ["vendor_asset", "generic"]
    assert result.semantic_role_by_id[identification_id] == "asset"
    # The forced hierarchy moves the HasProperty child onto the hierarchy plane.
    assert result.hierarchy_parent_by_id[identification_id] == root_id
    assert identification_id in result.hierarchy_children_by_id[root_id]
    # A plain Variable child keeps its generic composition classification.
    assert result.composition_parent_by_id[temp_id] == root_id
    assert result.relationships_by_id[root_id]["HasChildren"] == [identification_id]
    assert result.relationships_by_id[root_id]["HasComponent"] == [temp_id]


def test_shipped_semantic_overrides_validate_and_extend_defaults() -> None:
    """The repository ships a documented example that loads through the shipped schema."""
    profiles = load_semantic_profiles()

    assert tuple(profile.profile_id for profile in profiles) == ("vendor_asset", "machinery", "generic")
    assert DEFAULT_SEMANTIC_OVERRIDES_PATH.exists()
    assert (REPO_ROOT / "overrides" / "semantic_overrides.schema.json").exists()
