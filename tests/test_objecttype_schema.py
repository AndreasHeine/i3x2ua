from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field, make_dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua

from i3x_server.infrastructure.opcua.client import OpcUaNamespaceInfo, OpcUaObjectTypeInfo, OpcUaObjectTypeMemberInfo
from i3x_server.schemas import objecttype_schema
from i3x_server.schemas.objecttype_schema import build_object_type_schema


@dataclass(slots=True)
class FakeJobOrderState:
    State: ua.String | None = None


@dataclass(slots=True)
class FakeJobOrderAndState:
    JobOrderId: ua.String | None = None
    Quantity: ua.Double | None = None
    State: FakeJobOrderState = field(default_factory=FakeJobOrderState)


class Variant:
    def __init__(self, value: Any, variant_type_name: str, is_array: bool) -> None:
        self.Value = value
        self.VariantType = SimpleNamespace(name=variant_type_name)
        self.Dimensions = [0] if is_array else None


class DataValue:
    def __init__(self, variant: Variant) -> None:
        self.Value = variant


class ExtensionObject:
    def __init__(self, type_id: str, body: Any) -> None:
        self.TypeId = type_id
        self.Body = body


@dataclass(slots=True)
class RecursiveParameterDataType:
    ID: ua.String | None = None
    Value: Any | None = None
    Subparameters: list[RecursiveParameterDataType] = field(default_factory=list)


@dataclass(slots=True)
class RecursiveWorkMasterDataType:
    ID: ua.String | None = None
    Parameters: list[RecursiveParameterDataType] = field(default_factory=list)


@dataclass(slots=True)
class RecursiveJobOrderDataType:
    JobOrderID: ua.String | None = None
    Description: list[str] = field(default_factory=list)
    WorkMasterID: list[RecursiveWorkMasterDataType] = field(default_factory=list)
    JobOrderParameters: list[RecursiveParameterDataType] = field(default_factory=list)


@dataclass(slots=True)
class RecursiveJobOrderAndStateDataType:
    JobOrder: RecursiveJobOrderDataType | None = None
    State: list[str] = field(default_factory=list)


def _set_ua_attr(name: str, value: Any) -> None:
    setattr(ua, name, value)


def _assert_schema_type_contains(schema: dict[str, Any], expected: str) -> None:
    schema_type = schema["type"]
    if isinstance(schema_type, list):
        assert expected in schema_type
    else:
        assert schema_type == expected


def _extract_ref(schema: dict[str, Any]) -> str | None:
    if "$ref" in schema and isinstance(schema["$ref"], str):
        return schema["$ref"]
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and all_of and isinstance(all_of[0], dict):
        candidate = all_of[0].get("$ref")
        if isinstance(candidate, str):
            return candidate
    return None


def _def_key_from_ref(ref: str) -> str:
    return ref.split("#/$defs/", 1)[1]


def test_structured_array_schema_inferred_from_datatype_registry_when_value_empty() -> None:
    _set_ua_attr("extension_objects_by_datatype", {"ns=1;i=3015": FakeJobOrderAndState})

    member = OpcUaObjectTypeMemberInfo(
        node_id="ns=22;i=6033",
        browse_name="JobOrderList",
        display_name="JobOrderList",
        description="List of job orders",
        node_class="Variable",
        data_type="ns=1;i=3015",
        modelling_rule="Mandatory",
        value=[],
        schema_value=DataValue(Variant([], "ExtensionObject", True)),
        variant_type="ExtensionObject",
        is_array=True,
    )

    item = OpcUaObjectTypeInfo(
        node_id="ns=22;i=1002",
        parent_node_id=None,
        browse_name="ISA95JobOrderReceiverObjectType",
        display_name="ISA95JobOrderReceiverObjectType",
        properties={"JobOrderList": "ns=1;i=3015"},
        members=[member],
    )

    schema = build_object_type_schema(
        item=item,
        object_types_by_node_id={item.node_id: item},
        element_ids_by_node_id={item.node_id: "urn:opcua:objecttype:isa95joborderreceiver"},
        namespace_infos=[
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/", display_name="ISA95"),
        ],
    )

    job_order_list_schema = schema["properties"]["JobOrderList"]
    assert job_order_list_schema["type"] == "array"
    assert isinstance(job_order_list_schema["items"].get("$ref"), str)
    item_ref = job_order_list_schema["items"]["$ref"]
    assert item_ref.startswith("#/$defs/")
    assert "x-opcua-structureTypeId" not in job_order_list_schema
    item_def = schema["$defs"][_def_key_from_ref(item_ref)]
    assert item_def["x-opcua-structureDataType"] == ("nsu=http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/;i=3015")

    assert item_def["type"] == "object"
    _assert_schema_type_contains(item_def["properties"]["JobOrderId"], "string")
    _assert_schema_type_contains(item_def["properties"]["Quantity"], "number")
    assert isinstance(item_def["properties"]["State"].get("$ref"), str)
    state_def = schema["$defs"][_def_key_from_ref(item_def["properties"]["State"]["$ref"])]
    _assert_schema_type_contains(state_def["properties"]["State"], "string")


def test_structured_array_schema_inferred_when_placeholder_extension_object_and_valuerank_array() -> None:
    _set_ua_attr("extension_objects_by_datatype", {"ns=22;i=3015": FakeJobOrderAndState})

    member = OpcUaObjectTypeMemberInfo(
        node_id="ns=22;i=6033",
        browse_name="JobOrderList",
        display_name="JobOrderList",
        description="List of job orders",
        node_class="Variable",
        data_type="ns=22;i=3015",
        modelling_rule="Mandatory",
        value={"TypeId": "i=0", "Body": None},
        schema_value=DataValue(Variant(ExtensionObject("i=0", None), "ExtensionObject", False)),
        variant_type="ExtensionObject",
        is_array=False,
        value_rank=1,
        array_dimensions=[1],
    )

    item = OpcUaObjectTypeInfo(
        node_id="ns=22;i=1002",
        parent_node_id=None,
        browse_name="ISA95JobOrderReceiverObjectType",
        display_name="ISA95JobOrderReceiverObjectType",
        properties={"JobOrderList": "ns=22;i=3015"},
        members=[member],
    )

    schema = build_object_type_schema(
        item=item,
        object_types_by_node_id={item.node_id: item},
        element_ids_by_node_id={item.node_id: "urn:opcua:objecttype:isa95joborderreceiver"},
        namespace_infos=[
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/", display_name="ISA95"),
        ],
    )

    job_order_list_schema = schema["properties"]["JobOrderList"]
    assert job_order_list_schema["type"] == "array"
    assert job_order_list_schema["x-opcua-dataTypeId"] in {
        "ns=22;i=3015",
        "nsu=http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/;i=3015",
    }
    assert job_order_list_schema["x-opcua-valueRank"] == 1
    assert job_order_list_schema["x-opcua-arrayDimensions"] == [1]
    assert isinstance(job_order_list_schema["items"].get("$ref"), str)
    assert job_order_list_schema["items"]["$ref"].startswith("#/$defs/")


def test_recursive_datatype_annotations_resolve_nested_subschemas() -> None:
    _set_ua_attr("RecursiveParameterDataType", RecursiveParameterDataType)
    _set_ua_attr("RecursiveWorkMasterDataType", RecursiveWorkMasterDataType)
    _set_ua_attr("RecursiveJobOrderDataType", RecursiveJobOrderDataType)
    _set_ua_attr("RecursiveJobOrderAndStateDataType", RecursiveJobOrderAndStateDataType)
    _set_ua_attr(
        "extension_objects_by_datatype",
        {
            "ns=22;i=3015": RecursiveJobOrderAndStateDataType,
        },
    )

    member = OpcUaObjectTypeMemberInfo(
        node_id="ns=22;i=6033",
        browse_name="JobOrderList",
        display_name="JobOrderList",
        description="List of job orders",
        node_class="Variable",
        data_type="ns=22;i=3015",
        modelling_rule="Mandatory",
        value=[],
        schema_value=DataValue(Variant([], "ExtensionObject", True)),
        variant_type="ExtensionObject",
        is_array=True,
        value_rank=1,
    )

    item = OpcUaObjectTypeInfo(
        node_id="ns=22;i=1002",
        parent_node_id=None,
        browse_name="ISA95JobOrderReceiverObjectType",
        display_name="ISA95JobOrderReceiverObjectType",
        properties={"JobOrderList": "ns=22;i=3015"},
        members=[member],
    )

    schema = build_object_type_schema(
        item=item,
        object_types_by_node_id={item.node_id: item},
        element_ids_by_node_id={item.node_id: "urn:opcua:objecttype:isa95joborderreceiver"},
        namespace_infos=[
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA"),
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/", display_name="ISA95"),
        ],
    )

    items_schema = schema["properties"]["JobOrderList"]["items"]
    assert isinstance(items_schema.get("$ref"), str)
    items_ref = items_schema["$ref"]
    assert items_ref.startswith("#/$defs/")
    items_def = schema["$defs"][_def_key_from_ref(items_ref)]
    job_order_schema = items_def["properties"]["JobOrder"]
    assert isinstance(job_order_schema.get("$ref"), str)
    assert job_order_schema["$ref"].startswith("#/$defs/")
    job_order_def = schema["$defs"][_def_key_from_ref(job_order_schema["$ref"])]
    _assert_schema_type_contains(job_order_def["properties"]["JobOrderID"], "string")
    assert job_order_def["properties"]["Description"]["type"] == "array"
    assert isinstance(job_order_def["properties"]["WorkMasterID"]["items"].get("$ref"), str)
    assert job_order_def["properties"]["WorkMasterID"]["items"]["$ref"].startswith("#/$defs/")
    parameter_schema = job_order_def["properties"]["JobOrderParameters"]["items"]
    assert isinstance(parameter_schema.get("$ref"), str)
    assert parameter_schema["$ref"].startswith("#/$defs/")
    parameter_def = schema["$defs"][_def_key_from_ref(parameter_schema["$ref"])]
    _assert_schema_type_contains(parameter_def["properties"]["ID"], "string")
    assert isinstance(parameter_def["properties"]["Subparameters"]["items"].get("$ref"), str)
    assert parameter_def["properties"]["Subparameters"]["items"]["$ref"].startswith("#/$defs/")


def test_unresolved_datatype_annotation_falls_back_to_object() -> None:
    schema = objecttype_schema._schema_for_annotation_string(
        "ua.UnknownCustomDataType", objecttype_schema._SchemaRegistry()
    )
    assert schema["type"] == "object"
    assert schema["title"] == "UnknownCustomDataType"


def test_forwardref_annotation_is_resolved_as_structure() -> None:
    class _Nested:
        __annotations__ = {"ID": "ua.String"}

    _set_ua_attr("ForwardRefNested", _Nested)

    class _ForwardRef:
        __forward_arg__ = "ua.ForwardRefNested"

    schema = objecttype_schema._schema_for_annotation(_ForwardRef(), objecttype_schema._SchemaRegistry())
    assert schema == {"$ref": "#/$defs/Nested"}


def test_non_dataclass_structured_class_with_annotations_is_resolved() -> None:
    class _AnnotatedOnlyType:
        __annotations__ = {"JobOrderID": "ua.String", "Priority": "ua.Int16"}

    _set_ua_attr("AnnotatedOnlyType", _AnnotatedOnlyType)
    schema = objecttype_schema._schema_for_annotation_string(
        "ua.AnnotatedOnlyType", objecttype_schema._SchemaRegistry()
    )
    assert schema == {"$ref": "#/$defs/AnnotatedOnlyType"}


def test_dataclass_docstring_vartype_override_applies_for_ambiguous_annotations() -> None:
    class _DocNested:
        __annotations__ = {"ID": "ua.String"}

    @dataclass(slots=True)
    class _DocCarrier:
        """
        :vartype JobOrder: ua.DocNested
        """

        JobOrder: ua.String | None = None

    _set_ua_attr("DocNested", _DocNested)

    registry = objecttype_schema._SchemaRegistry()
    schema = objecttype_schema._structure_schema_for_type(_DocCarrier, registry)
    assert schema["properties"]["JobOrder"] == {"$ref": "#/$defs/DocNested"}


def test_dataclass_ua_types_override_applies_for_ambiguous_annotations() -> None:
    class _UaNested:
        __annotations__ = {"ID": "ua.String"}

    @dataclass(slots=True)
    class _UaCarrier:
        JobOrder: ua.String | None = None

    cast(Any, _UaCarrier).ua_types = [("JobOrder", "ua.UaNested")]
    _set_ua_attr("UaNested", _UaNested)

    registry = objecttype_schema._SchemaRegistry()
    schema = objecttype_schema._structure_schema_for_type(_UaCarrier, registry)
    assert schema["properties"]["JobOrder"] == {"$ref": "#/$defs/UaNested"}


def test_module_path_resolution_inlines_structured_annotation() -> None:
    schema = objecttype_schema._schema_for_annotation_string(
        "asyncua.common.structures104.isa95joborderdatatype",
        objecttype_schema._SchemaRegistry(),
    )
    assert schema["type"] == "object"
    assert schema["title"] == "isa95joborderdatatype"


def test_loaded_structures_module_name_resolution_supports_recursive_arrays() -> None:
    module = types.ModuleType("asyncua.common.structures999")
    module_any = cast(Any, module)

    @dataclass(slots=True)
    class _Parameter:
        Subparameters: list[Any] = field(default_factory=list)

    module_any.ISA95ParameterDataType = _Parameter
    sys.modules[module.__name__] = module
    try:
        schema = objecttype_schema._schema_for_annotation_string(
            "ua.ISA95ParameterDataType",
            objecttype_schema._SchemaRegistry(),
        )
        assert schema == {"$ref": "#/$defs/Parameter"}
    finally:
        sys.modules.pop(module.__name__, None)


def test_localizedtext_deduplicates_across_distinct_runtime_types() -> None:
    module = types.ModuleType("asyncua.common.structures902")
    module_any = cast(Any, module)

    localized_text_runtime = make_dataclass(
        "LocalizedText",
        [("Locale", str | None, None), ("Text", str | None, None)],
        slots=True,
    )
    localized_text_runtime.__module__ = module.__name__
    module_any.LocalizedText = localized_text_runtime
    sys.modules[module.__name__] = module

    localized_text_ua = make_dataclass(
        "LocalizedText",
        [("Locale", str | None, None), ("Text", str | None, None)],
        slots=True,
    )
    _set_ua_attr("LocalizedText", localized_text_ua)

    try:
        registry = objecttype_schema._SchemaRegistry()
        from_ua = objecttype_schema._schema_for_annotation_string("ua.LocalizedText", registry)
        from_module = objecttype_schema._schema_for_annotation_string(
            "asyncua.common.structures902.LocalizedText",
            registry,
        )

        assert from_ua == {"$ref": "#/$defs/LocalizedText"}
        assert from_module == {"$ref": "#/$defs/LocalizedText"}
        assert list(registry.defs.keys()) == ["LocalizedText"]
    finally:
        sys.modules.pop(module.__name__, None)


def test_localizedtext_deduplicates_between_value_and_annotation_paths() -> None:
    @dataclass(slots=True)
    class LocalizedText:
        Encoding: int = 0
        Locale: str | None = None
        Text: str | None = None

    _set_ua_attr("LocalizedText", LocalizedText)
    registry = objecttype_schema._SchemaRegistry()

    value_ref = objecttype_schema._reference_or_register_structure(
        "py-structure:test.localizedtextvalue",
        LocalizedText(),
        registry,
        {},
    )
    annotation_ref = objecttype_schema._schema_for_annotation_string("ua.LocalizedText", registry)

    assert value_ref == {"$ref": "#/$defs/LocalizedText"}
    assert annotation_ref == {"$ref": "#/$defs/LocalizedText"}
    assert list(registry.defs.keys()) == ["LocalizedText"]
    localized_text_def = registry.defs["LocalizedText"]
    assert "Encoding" not in localized_text_def["properties"]
    assert set(localized_text_def["properties"].keys()) == {"Locale", "Text"}


def test_isa95_workmaster_deduplicates_between_annotation_and_datatype_paths() -> None:
    @dataclass(slots=True)
    class ISA95WorkMasterDataType:
        ID: str | None = None

    registry = objecttype_schema._SchemaRegistry()

    from_annotation = objecttype_schema._reference_or_register_structure_from_type(
        "py-annotation:test.isa95workmasterdatatype",
        ISA95WorkMasterDataType,
        registry,
        {},
    )
    from_datatype = objecttype_schema._reference_or_register_structure_from_type(
        "opcua-datatype:nsu=http://opcfoundation.org/ua/isa95-jobcontrol_v2/;i=3007",
        ISA95WorkMasterDataType,
        registry,
        {"x-opcua-structureDataType": "nsu=http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/;i=3007"},
    )

    assert from_annotation == {"$ref": "#/$defs/ISA95WorkMasterDataType"}
    assert from_datatype == {"$ref": "#/$defs/ISA95WorkMasterDataType"}
    assert "ISA95WorkMasterDataType_2" not in registry.defs
    assert registry.defs["ISA95WorkMasterDataType"]["x-opcua-structureDataType"] == (
        "nsu=http://opcfoundation.org/UA/ISA95-JOBCONTROL_V2/;i=3007"
    )


def test_expand_schema_refs_inlines_nested_refs() -> None:
    defs = {
        "nested": {"type": "object", "properties": {"x": {"type": "string"}}},
        "outer": {"type": "object", "properties": {"inner": {"$ref": "#/$defs/nested"}}},
    }
    schema = {"$ref": "#/$defs/outer"}
    expanded = objecttype_schema._expand_schema_refs(schema, defs)
    assert expanded["properties"]["inner"]["properties"]["x"]["type"] == "string"


def test_expand_schema_refs_preserves_recursive_boundary_ref() -> None:
    defs = {
        "recursive": {
            "type": "object",
            "properties": {
                "children": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/recursive"},
                }
            },
        }
    }
    schema = {"$ref": "#/$defs/recursive"}
    expanded = objecttype_schema._expand_schema_refs(schema, defs)
    assert expanded["type"] == "object"
    assert expanded["properties"]["children"]["type"] == "array"
    assert expanded["properties"]["children"]["items"]["$ref"] == "#/$defs/recursive"


def test_generated_dep_annotation_is_evaluated_in_owner_module_context() -> None:
    module = types.ModuleType("generated.owner")
    module_any = cast(Any, module)

    @dataclass(slots=True)
    class _Nested:
        ID: ua.String | None = None

    module_any._dep_0 = _Nested
    sys.modules[module.__name__] = module
    try:
        carrier_cls = type("Carrier", (), {"__module__": module.__name__, "__annotations__": {"Child": "_dep_0"}})
        _Carrier: type[Any] = dataclass(carrier_cls)
        registry = objecttype_schema._SchemaRegistry()
        schema = objecttype_schema._structure_schema_for_type(_Carrier, registry)
        child_ref = _extract_ref(schema["properties"]["Child"])
        assert child_ref == "#/$defs/Nested"
    finally:
        sys.modules.pop(module.__name__, None)


def test_uint32_annotation_maps_to_integer() -> None:
    schema = objecttype_schema._schema_for_annotation(ua.UInt32, objecttype_schema._SchemaRegistry())
    assert schema["type"] == "integer"


def test_enum_annotation_string_maps_to_integer_enum() -> None:
    schema = objecttype_schema._schema_for_annotation_string("ua.ServerState", objecttype_schema._SchemaRegistry())
    assert schema["type"] == "integer"
    assert isinstance(schema.get("enum"), list)
    assert schema.get("x-opcua-enumNames")


def test_server_status_datatype_state_schema_maps_to_integer() -> None:
    schema = objecttype_schema.build_data_type_schema(
        "nsu=http://opcfoundation.org/UA/;i=862",
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
    )
    assert isinstance(schema, dict)
    state_schema = schema["properties"]["State"]
    assert state_schema["type"] == "integer"
    assert isinstance(state_schema.get("enum"), list)


@pytest.mark.parametrize(
    ("data_type_id", "field_name"),
    [
        ("nsu=http://opcfoundation.org/UA/;i=853", "ServerState"),
        ("nsu=http://opcfoundation.org/UA/;i=862", "State"),
        ("nsu=http://opcfoundation.org/UA/;i=12079", "AxisScaleType"),
        ("nsu=http://opcfoundation.org/UA/;i=19316", "Status"),
    ],
)
def test_enum_like_fields_in_standard_datatypes_do_not_degrade_to_object(
    data_type_id: str,
    field_name: str,
) -> None:
    schema = objecttype_schema.build_data_type_schema(
        data_type_id,
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
    )
    if not isinstance(schema, dict):
        pytest.skip(f"Datatype schema unavailable in this asyncua build: {data_type_id}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or field_name not in properties:
        pytest.skip(f"Field unavailable in this asyncua build: {data_type_id}.{field_name}")

    field_schema = properties[field_name]
    assert isinstance(field_schema, dict)
    assert field_schema.get("type") == "integer"


def test_guess_generated_dependency_type_prefers_parameter_for_joborderparameters() -> None:
    module = types.ModuleType("asyncua.common.structures777")
    module_any = cast(Any, module)

    @dataclass(slots=True)
    class ISA95JobOrderDataType:
        pass

    @dataclass(slots=True)
    class ISA95ParameterDataType:
        pass

    ISA95JobOrderDataType.__module__ = module.__name__
    ISA95ParameterDataType.__module__ = module.__name__

    module_any.ISA95JobOrderDataType = ISA95JobOrderDataType
    module_any.ISA95ParameterDataType = ISA95ParameterDataType
    sys.modules[module.__name__] = module
    try:
        guessed = objecttype_schema._guess_generated_dependency_type("JobOrderParameters", ISA95JobOrderDataType)
        assert guessed is ISA95ParameterDataType
    finally:
        sys.modules.pop(module.__name__, None)


def test_guess_generated_dependency_type_prefers_workmaster_for_workmasterid() -> None:
    module = types.ModuleType("asyncua.common.structures778")
    module_any = cast(Any, module)

    @dataclass(slots=True)
    class ISA95WorkMasterDataType:
        pass

    @dataclass(slots=True)
    class ISA95ParameterDataType:
        pass

    ISA95WorkMasterDataType.__module__ = module.__name__
    ISA95ParameterDataType.__module__ = module.__name__

    module_any.ISA95WorkMasterDataType = ISA95WorkMasterDataType
    module_any.ISA95ParameterDataType = ISA95ParameterDataType
    sys.modules[module.__name__] = module
    try:
        guessed = objecttype_schema._guess_generated_dependency_type("WorkMasterID", ISA95WorkMasterDataType)
        assert guessed is ISA95WorkMasterDataType
    finally:
        sys.modules.pop(module.__name__, None)


def test_guess_generated_dependency_type_prefers_personnel_for_personnelrequirements() -> None:
    module = types.ModuleType("asyncua.common.structures779")
    module_any = cast(Any, module)

    @dataclass(slots=True)
    class ISA95PersonnelDataType:
        pass

    @dataclass(slots=True)
    class ISA95ParameterDataType:
        pass

    ISA95PersonnelDataType.__module__ = module.__name__
    ISA95ParameterDataType.__module__ = module.__name__

    module_any.ISA95PersonnelDataType = ISA95PersonnelDataType
    module_any.ISA95ParameterDataType = ISA95ParameterDataType
    sys.modules[module.__name__] = module
    try:
        guessed = objecttype_schema._guess_generated_dependency_type("PersonnelRequirements", ISA95PersonnelDataType)
        assert guessed is ISA95PersonnelDataType
    finally:
        sys.modules.pop(module.__name__, None)


def test_build_data_type_schema_resolves_expanded_node_id_via_namespace_lookup() -> None:
    @dataclass(slots=True)
    class _StandardStruct:
        Name: ua.String | None = None

    _set_ua_attr("extension_objects_by_datatype", {"ns=0;i=96": _StandardStruct})
    schema = objecttype_schema.build_data_type_schema(
        "nsu=http://opcfoundation.org/UA/;i=96",
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
    )

    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["title"] == "RolePermissionType"
    assert schema["x-opcua-structureDataType"] == "nsu=http://opcfoundation.org/UA/;i=96"


def test_nodeid_datatype_schema_uses_discriminated_identifier_branches() -> None:
    schema = objecttype_schema.build_data_type_schema(
        "nsu=http://opcfoundation.org/UA/;i=17",
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
    )

    assert isinstance(schema, dict)
    assert schema.get("title") == "NodeId"
    assert isinstance(schema.get("oneOf"), list)

    branches = schema["oneOf"]
    branch_by_const = {
        branch["properties"]["NodeIdType"]["const"]: branch
        for branch in branches
        if isinstance(branch, dict)
        and isinstance(branch.get("properties"), dict)
        and isinstance(branch["properties"].get("NodeIdType"), dict)
        and "const" in branch["properties"]["NodeIdType"]
    }

    assert branch_by_const[2]["properties"]["Identifier"]["type"] == "integer"
    assert branch_by_const[3]["properties"]["Identifier"]["type"] == "string"
    assert branch_by_const[4]["properties"]["Identifier"]["type"] == "string"
    assert branch_by_const[4]["properties"]["Identifier"]["format"] == "uuid"
    assert branch_by_const[5]["properties"]["Identifier"]["type"] == "string"
    assert branch_by_const[5]["properties"]["Identifier"]["contentEncoding"] == "base64"


def test_nodeid_annotation_schema_keeps_nodeidtype_identifier_dependency() -> None:
    registry = objecttype_schema._SchemaRegistry()
    ref = objecttype_schema._schema_for_annotation_string("ua.NodeId", registry)
    assert ref == {"$ref": "#/$defs/NodeId"}

    nodeid_def = registry.defs["NodeId"]
    assert isinstance(nodeid_def.get("oneOf"), list)

    branches = nodeid_def["oneOf"]
    branch_by_const = {
        branch["properties"]["NodeIdType"]["const"]: branch
        for branch in branches
        if isinstance(branch, dict)
        and isinstance(branch.get("properties"), dict)
        and isinstance(branch["properties"].get("NodeIdType"), dict)
        and "const" in branch["properties"]["NodeIdType"]
    }
    assert branch_by_const[3]["properties"]["Identifier"] == {"type": "string"}
    assert branch_by_const[4]["properties"]["Identifier"]["format"] == "uuid"
    assert branch_by_const[5]["properties"]["Identifier"]["contentEncoding"] == "base64"


def test_nodeid_annotation_and_datatype_use_single_canonical_definition() -> None:
    registry = objecttype_schema._SchemaRegistry()

    annotation_ref = objecttype_schema._schema_for_annotation_string("ua.NodeId", registry)
    datatype_ref = objecttype_schema._schema_from_data_type(
        "nsu=http://opcfoundation.org/UA/;i=17",
        [OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="UA")],
        registry,
    )

    assert annotation_ref == {"$ref": "#/$defs/NodeId"}
    assert datatype_ref == {"$ref": "#/$defs/NodeId"}
    assert "NodeId" in registry.defs
    assert "NodeId_2" not in registry.defs


def test_nodeid_structure_value_and_annotation_share_single_canonical_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NodeId:
        __annotations__ = {
            "Identifier": int,
            "NamespaceIndex": int,
            "NodeIdType": int,
        }

        def __init__(self) -> None:
            self.Identifier = 42
            self.NamespaceIndex = 2
            self.NodeIdType = 2

    monkeypatch.setattr(ua, "NodeId", NodeId)

    registry = objecttype_schema._SchemaRegistry()
    value_ref = objecttype_schema._reference_or_register_structure(
        "py-structure:test.nodeid",
        NodeId(),
        registry,
        {},
    )
    annotation_ref = objecttype_schema._schema_for_annotation_string("ua.NodeId", registry)

    assert value_ref == {"$ref": "#/$defs/NodeId"}
    assert annotation_ref == {"$ref": "#/$defs/NodeId"}
    assert "NodeId_2" not in registry.defs
    assert isinstance(registry.defs["NodeId"].get("oneOf"), list)


def test_dedupe_defs_rewrites_local_refs_to_first_definition() -> None:
    schema: dict[str, Any] = {
        "$defs": {
            "Alpha": {
                "type": "object",
                "properties": {
                    "v": {"type": "integer"},
                },
            },
            "Alpha_2": {
                "type": "object",
                "properties": {
                    "v": {"type": "integer"},
                },
            },
            "Wrapper": {
                "type": "object",
                "properties": {
                    "nested": {"$ref": "#/$defs/Alpha_2"},
                },
            },
        },
        "allOf": [{"$ref": "#/$defs/Alpha_2"}],
    }

    objecttype_schema._dedupe_defs_and_rewrite_local_refs(schema)

    assert set(schema["$defs"].keys()) == {"Alpha", "Wrapper"}
    assert schema["allOf"][0]["$ref"] == "#/$defs/Alpha"
    assert schema["$defs"]["Wrapper"]["properties"]["nested"]["$ref"] == "#/$defs/Alpha"
