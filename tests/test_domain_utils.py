from __future__ import annotations

import json
from pathlib import Path

from i3x_server.domain.ports.opcua import OpcUaNamespaceInfo
from i3x_server.domain.utils import (
    canonical_namespace_uri,
    display_name_for_uri,
    expanded_node_id,
    is_null_opcua_type_node_id,
    namespace_infos_by_uri,
    namespace_uri_for_node_id,
    namespace_uri_from_expanded_node_id,
    normalize_namespace_uri,
    server_name_from_openapi,
)


def _infos() -> list[OpcUaNamespaceInfo]:
    return [
        OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="OPC UA"),
        OpcUaNamespaceInfo(uri="http://example.com/Custom/", display_name="Custom"),
        OpcUaNamespaceInfo(uri="", display_name="Empty"),
    ]


def test_normalize_namespace_uri_trims_slashes_and_case() -> None:
    assert normalize_namespace_uri("  HTTP://Example.com/Path/  ") == "http://example.com/path"


def test_canonical_namespace_uri_returns_matching_original() -> None:
    infos = _infos()
    assert canonical_namespace_uri("http://example.com/custom", infos) == "http://example.com/Custom/"
    assert canonical_namespace_uri("http://does-not-exist", infos) == "http://does-not-exist"


def test_namespace_uri_for_node_id_handles_missing_and_out_of_range_indices() -> None:
    infos = _infos()
    assert namespace_uri_for_node_id("ns=1;i=42", infos) == "http://example.com/Custom/"
    assert namespace_uri_for_node_id("i=42", infos) == "http://opcfoundation.org/UA/"
    assert namespace_uri_for_node_id("ns=99;i=42", infos) == ""


def test_expanded_node_id_covers_supported_variants() -> None:
    infos = _infos()
    assert expanded_node_id("nsu=http://example.com/Custom/;i=42", infos) == "nsu=http://example.com/Custom/;i=42"
    assert expanded_node_id("asset-root", infos) == "asset-root"
    assert expanded_node_id("i=42", infos) == "nsu=http://opcfoundation.org/UA/;i=42"
    assert expanded_node_id("ns=1;i=42", infos) == "nsu=http://example.com/Custom/;i=42"
    assert expanded_node_id("ns=2;i=42", infos) == "ns=2;i=42"
    assert expanded_node_id("ns=99;i=42", infos) == "ns=99;i=42"


def test_namespace_uri_from_expanded_node_id_parses_and_rejects_invalid() -> None:
    assert namespace_uri_from_expanded_node_id("nsu=http://example.com/custom;i=42") == "http://example.com/custom"
    assert namespace_uri_from_expanded_node_id("ns=1;i=42") is None


def test_is_null_opcua_type_node_id_for_all_supported_forms() -> None:
    assert is_null_opcua_type_node_id("i=0") is True
    assert is_null_opcua_type_node_id("NS=3;I=0") is True
    assert is_null_opcua_type_node_id("nsu=http://example.com/Custom/;i=0") is True
    assert is_null_opcua_type_node_id("i=1") is False


def test_display_name_for_uri_prefers_tail_then_host_fallback() -> None:
    assert display_name_for_uri("http://example.com/custom-equipment") == "Custom Equipment"
    assert display_name_for_uri("http://example.com/prod_v2") == "PROD V2"
    assert display_name_for_uri("http://gateway.example.com") == "Gateway.Example.Com"


def test_namespace_infos_by_uri_indexes_by_exact_uri() -> None:
    infos = _infos()
    indexed = namespace_infos_by_uri(infos)
    assert set(indexed.keys()) == {"http://opcfoundation.org/UA/", "http://example.com/Custom/", ""}
    assert indexed["http://example.com/Custom/"].display_name == "Custom"


def test_server_name_from_openapi_reads_title_and_falls_back() -> None:
    openapi_path = Path(__file__).resolve().parents[1] / "openapi.json"
    backup_text: str | None = openapi_path.read_text(encoding="utf-8") if openapi_path.exists() else None
    try:
        openapi_path.write_text(json.dumps({"info": {"title": "Custom Gateway"}}), encoding="utf-8")
        server_name_from_openapi.cache_clear()
        assert server_name_from_openapi("fallback") == "Custom Gateway"

        openapi_path.write_text(json.dumps({"info": {"title": "   "}}), encoding="utf-8")
        server_name_from_openapi.cache_clear()
        assert server_name_from_openapi("fallback") == "fallback"
    finally:
        if backup_text is None:
            if openapi_path.exists():
                openapi_path.unlink()
        else:
            openapi_path.write_text(backup_text, encoding="utf-8")
        server_name_from_openapi.cache_clear()
