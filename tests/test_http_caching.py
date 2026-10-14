"""Tests for model revisions, ETags and conditional GET handling."""

from __future__ import annotations

import dataclasses
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from i3x_server.http_caching import (
    MODEL_REVISION_HEADER,
    content_revision,
    etag_matches,
    representation_etag,
)
from i3x_server.model.revision import compute_model_revision, model_revision
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _node(node_id: str, name: str = "Node", children: list[str] | None = None) -> ModelNode:
    return ModelNode(id=node_id, name=name, kind="asset", type=None, children=children or [], source_node_id=node_id)


def _model(**overrides: Any) -> BuildResult:
    values: dict[str, Any] = {
        "nodes_by_id": {"a": _node("a", children=["b"]), "b": _node("b")},
        "root_ids": ["a"],
        "children_by_id": {"a": ["b"], "b": []},
        "instances_by_type_id": {},
        "property_to_node": {},
        "action_to_method": {},
    }
    values.update(overrides)
    return BuildResult(**values)


def test_model_revision_is_stable_and_ignores_timings() -> None:
    first = _model(total_duration_s=1.0, build_completed_at_utc="2024-01-01T00:00:00Z")
    second = _model(total_duration_s=9.0, build_completed_at_utc="2025-01-01T00:00:00Z")

    assert compute_model_revision(first) == compute_model_revision(second)
    assert len(compute_model_revision(first)) == 32


def test_model_revision_ignores_node_insertion_order() -> None:
    forward = _model(nodes_by_id={"a": _node("a"), "b": _node("b")})
    backward = _model(nodes_by_id={"b": _node("b"), "a": _node("a")})

    assert compute_model_revision(forward) == compute_model_revision(backward)


def test_model_revision_changes_with_content() -> None:
    renamed = _model(nodes_by_id={"a": _node("a", name="Renamed", children=["b"]), "b": _node("b")})

    assert compute_model_revision(_model()) != compute_model_revision(renamed)


def test_model_revision_covers_fields_not_carried_by_nodes() -> None:
    base = compute_model_revision(_model())

    assert compute_model_revision(_model(root_ids=["b"])) != base
    assert compute_model_revision(_model(action_to_method={"a": ("p", "m")})) != base
    assert compute_model_revision(_model(graph_relationship_names={"OrganizedBy"})) != base
    assert compute_model_revision(_model(graph_relationship_display_name_by_id={"qualified": "ConnectedTo"})) != base
    assert compute_model_revision(
        _model(graph_relationship_namespace_by_id={"ConnectedTo": "urn:example:first"})
    ) != compute_model_revision(_model(graph_relationship_namespace_by_id={"ConnectedTo": "urn:example:second"}))


def test_model_revision_is_cached_on_model() -> None:
    model = _model()

    revision = model_revision(model)

    assert model.revision == revision
    model.revision = "preset"
    assert model_revision(model) == "preset"


def test_etag_matches_weak_lists_and_wildcard() -> None:
    etag = 'W/"abc"'

    assert etag_matches('W/"abc"', etag)
    assert etag_matches('"abc"', etag)
    assert etag_matches('"zzz", W/"abc"', etag)
    assert etag_matches("*", etag)
    assert not etag_matches('"zzz"', etag)
    assert not etag_matches(None, etag)
    assert not etag_matches("", etag)


def test_representation_etag_requires_opt_in_and_cacheable_parts() -> None:
    assert representation_etag({"model": "r1"}) is None
    assert representation_etag({"_etag": "1"}) is None
    assert representation_etag({"_etag": "1", "model": "r1", "_not_cacheable": "1"}) is None

    etag = representation_etag({"_etag": "1", "model": "r1", "namespaces": "n1"})
    assert etag is not None and etag.startswith('W/"')
    assert etag != representation_etag({"_etag": "1", "model": "r2", "namespaces": "n1"})


def test_content_revision_is_deterministic() -> None:
    assert content_revision(["a", 1]) == content_revision(["a", 1])
    assert content_revision(["a", 1]) != content_revision([1, "a"])


@pytest.mark.parametrize("path", ["/v1/objects", "/v1/objecttypes", "/v1/namespaces", "/v1/relationshiptypes"])
def test_get_returns_etag_and_revalidates_with_304(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    etag = response.headers["etag"]
    assert etag.startswith('W/"')
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers[MODEL_REVISION_HEADER]

    not_modified = client.get(path, headers={"If-None-Match": etag})

    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == etag
    assert "content-length" not in not_modified.headers or not_modified.headers["content-length"] == "0"


def test_mismatched_if_none_match_returns_full_body(client: TestClient) -> None:
    response = client.get("/v1/objects", headers={"If-None-Match": 'W/"outdated"'})

    assert response.status_code == 200
    assert response.json()


def test_model_change_produces_new_etag(client: TestClient) -> None:
    first = client.get("/v1/objects")
    app = cast(Any, client.app)
    model: BuildResult = app.state.model_cache
    nodes = dict(model.nodes_by_id)
    renamed_id = next(iter(nodes))
    nodes[renamed_id] = nodes[renamed_id].model_copy(update={"name": "Renamed"})
    app.state.model_cache = dataclasses.replace(model, nodes_by_id=nodes, revision="")

    second = client.get("/v1/objects", headers={"If-None-Match": first.headers["etag"]})

    assert second.status_code == 200
    assert second.headers["etag"] != first.headers["etag"]
    assert second.headers[MODEL_REVISION_HEADER] != first.headers[MODEL_REVISION_HEADER]


def test_post_gets_revision_header_without_etag(client: TestClient) -> None:
    response = client.post("/v1/objects/list", json={"elementIds": ["asset-root"]})

    assert response.status_code == 200
    assert response.headers[MODEL_REVISION_HEADER]
    assert "etag" not in response.headers


def test_metrics_endpoint_is_not_etagged(client: TestClient) -> None:
    response = client.get("/v1/model/metrics")

    assert "etag" not in response.headers
