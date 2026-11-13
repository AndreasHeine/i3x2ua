"""OPC UA namespace and datatype identity tables plus node-id predicates.

Pure lookup logic over expanded node IDs and the standard UA namespace; no I/O, so it is
shared by schema resolution, type classification, and serving without pulling in the client.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from asyncua import ua

_UA_BUILTIN_DATATYPE_NAMES: dict[int, str] = {
    1: "Boolean",
    2: "SByte",
    3: "Byte",
    4: "Int16",
    5: "UInt16",
    6: "Int32",
    7: "UInt32",
    8: "Int64",
    9: "UInt64",
    10: "Float",
    11: "Double",
    12: "String",
    13: "DateTime",
    14: "Guid",
    15: "ByteString",
    16: "XmlElement",
    17: "NodeId",
    18: "ExpandedNodeId",
    19: "StatusCode",
    20: "QualifiedName",
    21: "LocalizedText",
    22: "ExtensionObject",
    23: "DataValue",
    24: "Variant",
    25: "DiagnosticInfo",
}

_UA_STANDARD_NON_DATATYPE_TYPE_NAMES: set[str] = {
    "BaseObjectType",
    "FolderType",
    "BaseVariableType",
    "BaseDataVariableType",
    "PropertyType",
    "DataTypeDescriptionType",
    "DataTypeDictionaryType",
    "DataTypeSystemType",
    "DataTypeEncodingType",
    "ModellingRuleType",
    "NamingRuleType",
}

_UA_STANDARD_INTEGER_OPTIONSET_TYPE_NAMES: set[str] = {
    "PermissionType",
    "AccessRestrictionType",
}

_UA_STANDARD_OBJECT_FALLBACK_TYPE_NAMES: set[str] = {
    "Range",
    "EUInformation",
    "Annotation",
    "RolePermissionType",
}

# Built-in datatypes that only constrain a subtype by kind, never by a concrete scalar.
_ABSTRACT_BUILTIN_DATATYPE_NAMES = {"BaseDataType", "Number", "Integer", "UInteger", "Enumeration"}

# Concrete scalar ranges per built-in id, so a subtype inherits a bounded integer schema.
_INTEGER_RANGES_BY_BUILTIN_ID: dict[int, tuple[int, int]] = {
    2: (-(2**7), 2**7 - 1),
    3: (0, 2**8 - 1),
    4: (-(2**15), 2**15 - 1),
    5: (0, 2**16 - 1),
    6: (-(2**31), 2**31 - 1),
    7: (0, 2**32 - 1),
    8: (-(2**63), 2**63 - 1),
    9: (0, 2**64 - 1),
}


def _is_builtin_ua_datatype_node_id(element_id: str) -> bool:
    match = re.match(r"^nsu=([^;]+);i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return False

    namespace_uri = match.group(1).rstrip("/").lower()
    if namespace_uri != "http://opcfoundation.org/ua":
        return False

    identifier = int(match.group(2))
    return 1 <= identifier <= 25


def _is_standard_ua_namespace_node_id(element_id: str) -> bool:
    expanded_match = re.match(r"^nsu=([^;]+);", element_id, flags=re.IGNORECASE)
    if expanded_match is not None:
        namespace_uri = expanded_match.group(1).rstrip("/").lower()
        return namespace_uri == "http://opcfoundation.org/ua"

    indexed_match = re.match(r"^ns=(\d+);", element_id, flags=re.IGNORECASE)
    if indexed_match is None:
        return False
    return int(indexed_match.group(1)) == 0


def _standard_ua_type_name(element_id: str) -> str | None:
    match = re.match(r"^nsu=[^;]+;i=(\d+)$", element_id, flags=re.IGNORECASE)
    if match is None:
        return None
    identifier = int(match.group(1))
    object_id_names = getattr(ua, "ObjectIdNames", None)
    if not isinstance(object_id_names, Mapping):
        return None
    candidate = object_id_names.get(identifier)
    return candidate if isinstance(candidate, str) and candidate else None
