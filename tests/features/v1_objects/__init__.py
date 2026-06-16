"""Objects endpoints tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from i3x_server.schemas.i3x import ModelNode
from tests.conftest import fastapi_app


def test_v1_objects_list(client: TestClient) -> None:
    object_types = client.get("/v1/objecttypes").json()["result"]
    expected_type_element_id = object_types[0]["elementId"]

    response = client.post("/v1/objects/list", json={"elementIds": ["asset-root", "missing"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][1]["success"] is False
    first = payload["results"][0]["result"]
    assert first["typeElementId"] == expected_type_element_id


def test_v1_objects_root_uses_hierarchy_only(client: TestClient) -> None:
    response = client.get("/v1/objects", params={"root": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    element_ids = {item["elementId"] for item in payload["result"]}
    assert element_ids == {"asset-root", "sensor-root"}


def test_v1_objects_root_excludes_composition_only_children(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.hierarchy_parent_by_id = {}
    app.state.model_cache.composition_parent_by_id = {
        "sensor-root": "asset-root",
    }

    response = client.get("/v1/objects", params={"root": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    element_ids = {item["elementId"] for item in payload["result"]}
    assert element_ids == {"asset-root"}


def test_v1_objects_related_uses_relationships_map_including_graph(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.relationships_by_id = {
        "asset-root": {
            "HasChildren": ["action-def"],
            "HasComponent": ["property-abc"],
            "Monitors": ["sensor-root"],
        },
        "action-def": {"HasParent": ["asset-root"]},
        "property-abc": {"ComponentOf": ["asset-root"]},
        "sensor-root": {},
    }

    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root"], "includeMetadata": False},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    related = payload["results"][0]["result"]
    by_relationship = {item["sourceRelationship"]: item["object"]["elementId"] for item in related}
    assert by_relationship["HasChildren"] == "action-def"
    assert by_relationship["HasComponent"] == "property-abc"
    assert by_relationship["Monitors"] == "sensor-root"


def test_v1_objects_related_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root", "does-not-exist"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"
    assert isinstance(failing["responseDetail"]["detail"], str)


def test_v1_objects_list_missing_element_item_includes_response_detail(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["asset-root", "does-not-exist"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is False
    failing = next(item for item in payload["results"] if not item["success"])
    assert failing["elementId"] == "does-not-exist"
    assert failing["error"]["code"] == 404
    assert failing["responseDetail"]["status"] == 404
    assert failing["responseDetail"]["title"] == "Not Found"


def test_v1_objects_related_graph_relationships_appear_in_result(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.relationships_by_id = {
        "asset-root": {
            "HasChildren": ["action-def"],
            "HasComponent": ["property-abc"],
            "Monitors": ["sensor-root"],
            "ConnectedTo": ["sensor-root"],
        },
        "action-def": {"HasParent": ["asset-root"]},
        "property-abc": {"ComponentOf": ["asset-root"]},
        "sensor-root": {},
    }

    response = client.post(
        "/v1/objects/related",
        json={"elementIds": ["asset-root"]},
    )
    assert response.status_code == 200
    payload = response.json()
    related = payload["results"][0]["result"]
    relationship_types = {item["sourceRelationship"] for item in related}
    assert "Monitors" in relationship_types
    assert "ConnectedTo" in relationship_types


def test_v1_objects_list_include_metadata_uses_expanded_source_type_id(client: TestClient) -> None:
    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["property-abc"], "includeMetadata": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    result = payload["results"][0]["result"]
    assert result["typeElementId"].startswith("urn:opcua:objecttype:")
    metadata = payload["results"][0]["result"]["metadata"]
    assert metadata["typeNamespaceUri"] == "http://example.com/i3x"
    assert metadata["sourceTypeId"] == "nsu=http://example.com/runtime;s=Temperature"


def test_v1_objects_list_include_metadata_exposes_composition_parent_id(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.composition_parent_by_id = {
        "sensor-root": "asset-root",
        "property-abc": "asset-root",
    }

    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["sensor-root"], "includeMetadata": True},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    metadata = payload["results"][0]["result"]["metadata"]
    assert metadata["compositionParentId"] == "asset-root"


def test_v1_objects_list_property_null_type_resolves_to_unknown_type(client: TestClient) -> None:
    app = fastapi_app(client)
    app.state.model_cache.nodes_by_id["property-null-type"] = ModelNode(
        id="property-null-type",
        name="UnknownProperty",
        kind="property",
        type="ns=0;i=0",
        children=[],
        source_node_id="ns=2;s=UnknownProperty",
    )
    app.state.model_cache.children_by_id.setdefault("asset-root", []).append("property-null-type")
    app.state.model_cache.children_by_id["property-null-type"] = []
    app.state.model_cache.property_to_node["property-null-type"] = "ns=2;s=UnknownProperty"

    response = client.post(
        "/v1/objects/list",
        json={"elementIds": ["property-null-type"]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    result = payload["results"][0]["result"]
    assert result["typeElementId"].startswith("urn:opcua:objecttype:")
    assert result["typeElementId"] != "nsu=http://opcfoundation.org/UA/;i=0"

    object_types = client.get("/v1/objecttypes").json()["result"]
    known_type_ids = {item["elementId"] for item in object_types}
    assert result["typeElementId"] in known_type_ids
