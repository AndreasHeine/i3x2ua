"""Guards for the typed app-state container (ADR 0001, PR-10)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from i3x_server.application.state import (
    GatewayState,
    ObjectTypeContextCache,
    ObjectTypeContextSlot,
    gateway_state,
    install_gateway_state,
)
from i3x_server.bootstrap.app_factory import create_app


def test_create_app_installs_typed_container() -> None:
    app = create_app()
    assert isinstance(app.state, GatewayState)
    # MCP-owned fields exist even when MCP is disabled, so readers never need defaults.
    assert app.state.mcp_tools == {}
    assert app.state.mcp_transport_sessions == {}
    assert app.state.mcp_prompts is None
    # The allowlist is built from the current environment, so only its shape is asserted.
    assert isinstance(app.state.mcp_allowed_origins, list)


def test_uninstalled_app_state_reads_fail_loudly() -> None:
    """A bare Starlette ``State`` bag has no declared fields: reads raise, no silent fallback."""
    app = FastAPI()
    request = Request({"type": "http", "app": app})
    with pytest.raises(AttributeError) as excinfo:
        _ = gateway_state(request).model_cache
    assert "model_cache" in str(excinfo.value)


def test_gateway_state_typo_raises_attribute_error() -> None:
    state = GatewayState()
    # Reading a misspelled field must surface the miss instead of returning None. The name is
    # looked up dynamically, so the misspelling stays a runtime probe rather than a static one.
    misspelled_field = "model_cach"
    with pytest.raises(AttributeError) as excinfo:
        _ = getattr(state, misspelled_field)
    assert "model_cach" in str(excinfo.value)


def test_install_gateway_state_wires_container_for_app_and_request() -> None:
    app = FastAPI()
    state = install_gateway_state(app, GatewayState())

    request = Request({"type": "http", "app": app})
    # Requests resolve through ``request.app.state``; the per-request ``Request.state``
    # bag must never be mistaken for the container.
    assert gateway_state(request) is state
    assert gateway_state(app) is state


def test_object_type_context_cache_bounds_and_orders_slots() -> None:
    cache = ObjectTypeContextCache(max_entries=2)
    slots = [
        ObjectTypeContextSlot(model_token=f"t{index}", namespace_token=(), object_types_token="", context=f"c{index}")
        for index in range(3)
    ]
    for slot in slots:
        cache.store(slot)

    # Least-recently-used first; the oldest slot is evicted at the cap.
    assert cache.entries == slots[1:]
    assert cache.current_slot() is slots[2]
    assert cache.find(("t0", (), "")) is None

    assert cache.find(("t1", (), "")) is slots[1]
    cache.promote(slots[1])
    assert cache.current_slot() is slots[1]
    assert cache.entries == [slots[2], slots[1]]


def test_object_type_context_cache_store_replaces_matching_token_triple() -> None:
    cache = ObjectTypeContextCache(max_entries=4)
    first = ObjectTypeContextSlot(model_token="t", namespace_token=(), object_types_token="", context="old")
    other = ObjectTypeContextSlot(model_token="u", namespace_token=(), object_types_token="", context="other")
    rebuilt = ObjectTypeContextSlot(model_token="t", namespace_token=(), object_types_token="", context="new")

    cache.store(first)
    cache.store(other)
    cache.store(rebuilt)

    # A rebuild for the same token triple replaces the slot instead of duplicating it.
    assert cache.entries == [other, rebuilt]
    assert cache.current_slot() is rebuilt
    assert cache.find(("t", (), "")) is rebuilt
    assert cache.current_slot() is not None
    assert cache.find(("missing", (), "")) is None
