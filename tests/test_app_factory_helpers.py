from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from i3x_server.api.v1 import monolithic
from i3x_server.bootstrap.app_factory import (
    _extract_inline_script_bodies,
    _frontend_inline_script_hashes,
    _prepare_otel_environment,
    _readable_operation_id,
    _run_model_preload,
    _run_periodic_model_refresh,
    _status_title,
    _to_lower_camel_case,
    create_app,
)
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def test_status_title_and_text_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _status_title(404) == "Not Found"
    assert _status_title(999) == "Error"

    assert _to_lower_camel_case("Get Objects") == "getObjects"
    assert _to_lower_camel_case("___") == "operation"


def test_readable_operation_id_prefers_route_name_and_fallback() -> None:
    named_route = SimpleNamespace(name="Get Objects", methods={"GET"}, path_format="/v1/objects")
    assert _readable_operation_id(cast(APIRoute, named_route)) == "getObjects"

    unnamed_route = SimpleNamespace(name="", methods={"POST"}, path_format="/v1/subscriptions/{id}")
    assert _readable_operation_id(cast(APIRoute, unnamed_route)) == "postV1SubscriptionsId"


def test_extract_inline_script_bodies_matches_closing_tag_whitespace() -> None:
    html = "<script>window.__x=1;</script   >"
    assert _extract_inline_script_bodies(html) == ["window.__x=1;"]


def test_extract_inline_script_bodies_matches_closing_tag_with_trailing_text() -> None:
    html = "<script>window.__y=2;</script\t\n bar>"
    assert _extract_inline_script_bodies(html) == ["window.__y=2;"]


def test_frontend_inline_script_hashes_supports_script_tag_variants(tmp_path: Any) -> None:
    html = (
        "<html><body>"
        "<script>window.a=1;</script>"
        '<script type="module">window.b=2;</script   >'
        "<script>window.d=4;</script\t\n bar>"
        "<SCRIPT>window.c=3;</SCRIPT >"
        "</body></html>"
    )
    (tmp_path / "index.html").write_text(html, encoding="utf-8")

    hashes = _frontend_inline_script_hashes(tmp_path)
    assert len(hashes) == 4
    assert all(item.startswith("'sha256-") and item.endswith("'") for item in hashes)


@pytest.mark.asyncio
async def test_run_model_preload_success_sets_cache() -> None:
    model = BuildResult(
        nodes_by_id={},
        root_ids=[],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )

    class _ModelBuilder:
        async def build(self) -> BuildResult:
            return model

    class _OpcUaClient:
        def reset_runtime_metrics(self) -> None:
            return None

        def snapshot_runtime_metrics(self) -> Any:
            return SimpleNamespace(
                browse_calls=0,
                browse_next_calls=0,
                read_calls=0,
                history_read_calls=0,
                method_calls=0,
                browse_nodes=0,
                browse_initial_references=0,
                browse_next_references=0,
                read_nodes=0,
                history_read_nodes=0,
                browse_tree_calls=0,
                browse_tree_nodes_last=0,
                namespace_reads=0,
                namespace_count_last=0,
                namespace_info_builds=0,
                namespace_info_count_last=0,
                object_type_reads=0,
                object_type_count_last=0,
            )

    app = FastAPI()
    app.state.opcua_client = _OpcUaClient()
    app.state.model_builder = _ModelBuilder()
    app.state.model_cache = None
    await _run_model_preload(app)
    assert app.state.model_cache is model


@pytest.mark.asyncio
async def test_run_model_preload_failure_without_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ModelBuilder:
        async def build(self) -> BuildResult:
            raise RuntimeError("build failed")

    class _OpcUaClient:
        def reset_runtime_metrics(self) -> None:
            return None

        def snapshot_runtime_metrics(self) -> Any:
            return SimpleNamespace()

    app = FastAPI()
    app.state.opcua_client = _OpcUaClient()
    app.state.model_builder = _ModelBuilder()
    app.state.model_cache = None
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.fail_startup_on_model_preload_error", False)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.model_preload_blocking", False)
    await _run_model_preload(app)


@pytest.mark.asyncio
async def test_monolithic_object_type_context_cache_uses_content_tokens() -> None:
    model_node = ModelNode(id="n1", name="n1", kind="asset", source_node_id="n1")
    model_a = BuildResult(
        nodes_by_id={"n1": model_node},
        root_ids=["n1"],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
        build_completed_at_utc="2026-01-01T00:00:00Z",
    )
    model_b = BuildResult(
        nodes_by_id={"n1": model_node},
        root_ids=["n1"],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
        build_completed_at_utc="2026-02-01T00:00:00Z",
    )
    cached_context = monolithic._ObjectTypeContext(
        namespace_infos=[],
        object_types=[],
        element_ids_by_node_id={},
        items=[],
        source_type_to_element_id={},
    )
    app = FastAPI()
    app.state.object_type_context_cache = {
        "model_token": (
            len(model_a.nodes_by_id),
            len(model_a.root_ids),
            len(model_a.property_to_node),
            len(model_a.action_to_method),
            tuple(sorted(model_a.nodes_by_id)),
            tuple(sorted(model_a.root_ids)),
        ),
        "namespace_token": tuple(),
        "object_types_token": tuple(),
        "context": cached_context,
    }

    class _OpcUaClient:
        async def get_namespace_infos(self) -> list[object]:
            return []

        async def get_object_types(self) -> list[object]:
            return []

    called = False

    async def _raise_if_called(*args: Any, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("should use cached model context and not rebuild")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(monolithic, "_build_object_type_context", _raise_if_called)
    try:
        result = await monolithic._get_object_type_context(
            cast(Any, SimpleNamespace(app=app)),
            model_b,
            cast(Any, _OpcUaClient()),
            namespace_infos=[],
        )
    finally:
        monkeypatch.undo()

    assert result is cached_context
    assert called is False


@pytest.mark.asyncio
async def test_run_periodic_model_refresh_rebuilds_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls = 0
    model = BuildResult(
        nodes_by_id={},
        root_ids=[],
        children_by_id={},
        instances_by_type_id={},
        property_to_node={},
        action_to_method={},
    )

    class _ModelBuilder:
        async def build(self) -> BuildResult:
            nonlocal build_calls
            build_calls += 1
            return model

    class _OpcUaClient:
        def reset_runtime_metrics(self) -> None:
            return None

        async def get_namespace_infos(self) -> list[object]:
            return []

        async def get_object_types(self) -> list[object]:
            return []

    sleep_calls = 0

    async def _fake_sleep(_: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError()

    app = FastAPI()
    app.state.model_builder = _ModelBuilder()
    app.state.opcua_client = _OpcUaClient()
    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    app.state.model_cache = None
    app.state.object_type_context_cache = object()

    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.model_refresh_interval_seconds", 1)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await _run_periodic_model_refresh(app)

    assert build_calls == 1
    assert app.state.model_cache is model
    assert isinstance(app.state.object_type_context_cache, dict)


@pytest.mark.asyncio
async def test_run_periodic_model_refresh_disabled_when_interval_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class _ModelBuilder:
        async def build(self) -> BuildResult:
            raise AssertionError("build should not run when refresh interval is zero")

    class _OpcUaClient:
        def reset_runtime_metrics(self) -> None:
            return None

    app = FastAPI()
    app.state.model_builder = _ModelBuilder()
    app.state.opcua_client = _OpcUaClient()
    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    app.state.model_cache = None
    app.state.object_type_context_cache = object()

    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.model_refresh_interval_seconds", 0)
    await _run_periodic_model_refresh(app)


@pytest.mark.parametrize("enabled", [False, True])
def test_create_app_configures_native_telemetry(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_enabled", enabled)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-service")
    app = create_app()

    assert app._telemetry == {
        "tracer_provider": None,
        "meter_provider": None,
        "logger_provider": None,
        "tracing": enabled,
        "metrics": enabled,
        "logs": False,
        "operation_spans": enabled,
        "auto_configure": enabled,
        "exclude": None,
    }


def test_prepare_otel_environment_preserves_standard_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_enabled", True)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_service_name", "legacy-service")
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_otlp_endpoint", "http://legacy:4318")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "standard-service")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    _prepare_otel_environment()

    assert os.environ["OTEL_SERVICE_NAME"] == "standard-service"
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://legacy:4318"
