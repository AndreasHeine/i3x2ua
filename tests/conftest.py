"""
Shared test fixtures and utilities for feature test packages.

Provides:
  - TestClient fixtures (with and without MCP)
  - Fake OPC UA client
  - Test model configuration
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator, Iterator, Mapping
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from i3x_server.bootstrap.app_factory import create_app
from i3x_server.config.settings import get_settings
from i3x_server.infrastructure.subscriptions.service import SubscriptionService
from i3x_server.schemas.i3x import ModelNode, NodeKind
from i3x_server.schemas.state import BuildResult
from tests.opcua_fake import FakeOpcUaClient, data_value_dto

# Re-exported from ``tests.opcua_fake`` so ``from tests.conftest import ...`` keeps working.
__all__ = ["FakeOpcUaClient", "data_value_dto"]


class CountingNodeMap(dict[str, ModelNode]):
    """Node map that records every full pass over the nodes.

    Element lookups must resolve through an index: deriving the index records one pass, and
    resolving a query afterwards must record none.
    """

    def __init__(self, nodes: dict[str, ModelNode]) -> None:
        super().__init__(nodes)
        self.passes = 0

    def values(self) -> Any:
        self.passes += 1
        return super().values()

    def items(self) -> Any:
        self.passes += 1
        return super().items()


def fastapi_app(client: TestClient) -> FastAPI:
    """Extract FastAPI app from test client."""
    return cast(FastAPI, client.app)


def configure_test_app(app: FastAPI) -> None:
    """Configure test app with mock model and OPC UA client."""
    property_id = "property-abc"
    action_id = "action-def"
    root_id = "asset-root"

    app.state.model_cache = BuildResult(
        nodes_by_id={
            root_id: ModelNode(
                id=root_id,
                name="Machine",
                kind="asset",
                type=None,
                children=[property_id, action_id],
                source_node_id="ns=2;s=Machine",
                source_type_id="ns=1;i=1001",
            ),
            "sensor-root": ModelNode(
                id="sensor-root",
                name="Sensor",
                kind="asset",
                type=None,
                children=[],
                source_node_id="ns=2;s=Sensor",
                source_type_id="ns=1;i=1002",
            ),
            property_id: ModelNode(
                id=property_id,
                name="Temperature",
                kind="property",
                type="ns=1;i=11",
                children=[],
                source_node_id="ns=2;s=Temperature",
            ),
            action_id: ModelNode(
                id=action_id,
                name="Reset",
                kind="action",
                type=None,
                children=[],
                source_node_id="ns=2;s=Reset",
            ),
        },
        root_ids=[root_id, "sensor-root"],
        children_by_id={root_id: [property_id, action_id], "sensor-root": [], property_id: [], action_id: []},
        instances_by_type_id={"ns=1;i=1001": [root_id], "ns=1;i=1002": ["sensor-root"]},
        property_to_node={property_id: "ns=2;s=Temperature"},
        action_to_method={action_id: ("ns=2;s=Machine", "ns=2;s=Reset")},
    )
    app.state.opcua_client = FakeOpcUaClient()
    # Mirror the production wiring (bootstrap.app_factory): the admission caps come from the
    # environment, so API-level tests toggle ``I3X_SUBSCRIPTION_MAX_COUNT`` /
    # ``I3X_SUBSCRIPTION_MAX_PER_CLIENT`` / ``I3X_SUBSCRIPTION_SUBTREE_MAX_NODES`` exactly like a
    # deployed gateway does.
    runtime = get_settings()
    app.state.subscription_service = SubscriptionService(
        app.state.opcua_client,
        interval_seconds=1,
        max_subscriptions=runtime.subscription_max_count,
        max_subscriptions_per_client=runtime.subscription_max_per_client,
        subtree_max_nodes=runtime.subscription_subtree_max_nodes,
        native_max_bins=runtime.subscription_native_max_bins,
        native_max_monitored_items=runtime.subscription_native_max_monitored_items,
    )


def _append_unique(items: list[str], *values: str) -> None:
    for value in values:
        if value not in items:
            items.append(value)


def add_composition_depth_chain(app: FastAPI) -> dict[str, str]:
    """Attach a three-level composition chain plus a hierarchy-only branch to the test model.

    Composition plane (``HasComponent``): ``asset-root`` -> ``comp-prop`` (level 1),
    ``asset-root`` -> ``comp-child`` -> ``mid-prop`` (level 2), ``comp-child`` -> ``sub-comp``
    -> ``deep-prop`` (level 3). Hierarchy plane (``HasChildren``): ``asset-root`` ->
    ``hier-child`` -> ``hier-prop`` - those must never be reached by value/history recursion
    (i3X spec IMPLEMENTATION_GUIDE.md:1111).
    """
    model = app.state.model_cache
    chain: dict[str, tuple[str, NodeKind, str]] = {
        "comp-prop": ("CompositionProp", "property", "ns=2;s=CompositionProp"),
        "comp-child": ("CompChild", "asset", "ns=2;s=CompChild"),
        "mid-prop": ("MidProp", "property", "ns=2;s=MidProp"),
        "sub-comp": ("SubComp", "asset", "ns=2;s=SubComp"),
        "deep-prop": ("DeepProp", "property", "ns=2;s=DeepProp"),
        "hier-child": ("HierChild", "asset", "ns=2;s=HierChild"),
        "hier-prop": ("HierProp", "property", "ns=2;s=HierProp"),
    }
    for node_id, (name, kind, source_node_id) in chain.items():
        model.nodes_by_id[node_id] = ModelNode(
            id=node_id,
            name=name,
            kind=kind,
            type=None,
            children=[],
            source_node_id=source_node_id,
        )
        model.children_by_id[node_id] = []

    _append_unique(model.children_by_id.setdefault("asset-root", []), "comp-prop", "comp-child", "hier-child")
    model.children_by_id["comp-child"] = ["mid-prop", "sub-comp"]
    model.children_by_id["sub-comp"] = ["deep-prop"]
    model.children_by_id["hier-child"] = ["hier-prop"]

    composition = model.composition_children_by_id
    _append_unique(composition.setdefault("asset-root", []), "comp-prop", "comp-child")
    composition["comp-child"] = ["mid-prop", "sub-comp"]
    composition["sub-comp"] = ["deep-prop"]
    composition["hier-child"] = ["hier-prop"]

    hierarchy = model.hierarchy_children_by_id
    _append_unique(hierarchy.setdefault("asset-root", []), "comp-prop", "comp-child", "hier-child")
    hierarchy["hier-child"] = ["hier-prop"]

    client = app.state.opcua_client
    for node_id, (_name, kind, source_node_id) in chain.items():
        if kind != "property":
            continue
        model.property_to_node[node_id] = source_node_id
        client.values.setdefault(source_node_id, 10.0)
        client.history_values.setdefault(source_node_id, []).append(
            data_value_dto(10.0, "Good", datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc))
        )
    return {node_id: node_id for node_id in chain}


_MCP_ENABLED_ENV = {
    "I3X_ENABLE_MCP": "1",
    "I3X_SKIP_OPCUA_CONNECT": "1",
    "I3X_ENABLE_WRITES": "0",
    "I3X_MCP_INCLUDE_OPCUA_METADATA": "1",
}
# Explicitly disabled: unsetting would let a .env in the working directory enable MCP.
_MCP_DISABLED_ENV = {**_MCP_ENABLED_ENV, "I3X_ENABLE_MCP": "0"}


def _test_client_with_env(
    monkeypatch: pytest.MonkeyPatch,
    env: Mapping[str, str],
    configure: Callable[[FastAPI], None],
) -> Generator[TestClient, None, None]:
    """Build an app with the given environment.

    `monkeypatch` restores the variables even when `create_app()` raises, which manual
    `os.environ` mutation would leak into later tests (L12).
    """
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app = create_app()
    with TestClient(app) as test_client:
        configure(app)
        yield test_client


@contextlib.contextmanager
def _client_with_env(monkeypatch: pytest.MonkeyPatch, extra_env: Mapping[str, str]) -> Iterator[TestClient]:
    """Test client on the standard MCP env plus ``extra_env`` overrides.

    Use this for behaviour that is toggled by an ``I3X_*`` variable; the shared MCP env keeps
    the fixture's other defaults intact.
    """
    yield from _test_client_with_env(monkeypatch, {**_MCP_ENABLED_ENV, **extra_env}, configure_test_app)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP enabled."""
    yield from _test_client_with_env(monkeypatch, _MCP_ENABLED_ENV, configure_test_app)


@pytest.fixture
def client_without_mcp(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP disabled."""
    yield from _test_client_with_env(monkeypatch, _MCP_DISABLED_ENV, configure_test_app)


@pytest.fixture
def client_without_tool_overrides(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """Test client fixture with MCP tool overrides disabled."""
    monkeypatch.setattr(
        "i3x_server.mcp.tool_definitions.load_overrides",
        lambda *args, **kwargs: {"tools": {}, "prompts": {}, "excluded_tools": []},
    )
    yield from _test_client_with_env(monkeypatch, _MCP_ENABLED_ENV, configure_test_app)


def assert_documented_response(client: TestClient, method: str, path: str, status: int, payload: Any) -> None:
    """Validate a runtime payload against the response schema documented in ``/openapi.json``."""
    document = client.get("/openapi.json").json()
    schema = document["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
    Draft202012Validator(
        {**schema, "components": document["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    ).validate(payload)
