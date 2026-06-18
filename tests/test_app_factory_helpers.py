from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from i3x_server.bootstrap.app_factory import (
    _configure_otel,
    _env_flag,
    _readable_operation_id,
    _run_model_preload,
    _status_title,
    _to_lower_camel_case,
)
from i3x_server.schemas.state import BuildResult


def test_status_title_and_text_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _status_title(404) == "Not Found"
    assert _status_title(999) == "Error"

    monkeypatch.setenv("I3X_TEST_FLAG", "true")
    assert _env_flag("I3X_TEST_FLAG") is True
    monkeypatch.setenv("I3X_TEST_FLAG", "0")
    assert _env_flag("I3X_TEST_FLAG") is False

    assert _to_lower_camel_case("Get Objects") == "getObjects"
    assert _to_lower_camel_case("___") == "operation"


def test_readable_operation_id_prefers_route_name_and_fallback() -> None:
    named_route = SimpleNamespace(name="Get Objects", methods={"GET"}, path_format="/v1/objects")
    assert _readable_operation_id(named_route) == "getObjects"

    unnamed_route = SimpleNamespace(name="", methods={"POST"}, path_format="/v1/subscriptions/{id}")
    assert _readable_operation_id(unnamed_route) == "postV1SubscriptionsId"


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

    app = SimpleNamespace(
        state=SimpleNamespace(
            opcua_client=_OpcUaClient(),
            model_builder=_ModelBuilder(),
            model_cache=None,
        )
    )
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

    app = SimpleNamespace(
        state=SimpleNamespace(
            opcua_client=_OpcUaClient(),
            model_builder=_ModelBuilder(),
            model_cache=None,
        )
    )
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.fail_startup_on_model_preload_error", False)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.model_preload_blocking", False)
    await _run_model_preload(app)


def test_configure_otel_disabled_and_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_enabled", False)
    _configure_otel(app)

    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_enabled", True)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_otlp_endpoint", None)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_service_name", "svc")

    import builtins

    original_import = builtins.__import__

    def _import_with_forced_failure(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError("missing")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import_with_forced_failure)
    _configure_otel(app)


def test_configure_otel_with_stubbed_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_enabled", True)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_otlp_endpoint", None)
    monkeypatch.setattr("i3x_server.bootstrap.app_factory.settings.otel_service_name", "svc")

    class _Resource:
        @staticmethod
        def create(data: dict[str, str]) -> dict[str, str]:
            return data

    class _TracerProvider:
        def __init__(self, resource: Any) -> None:
            self.resource = resource
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    class _BatchSpanProcessor:
        def __init__(self, exporter: Any) -> None:
            self.exporter = exporter

    trace_module = SimpleNamespace(set_tracer_provider=lambda provider: provider)

    class _FastAPIInstrumentor:
        @staticmethod
        def instrument_app(target_app: FastAPI) -> None:
            target_app.state.otel_instrumented = True

    class _MeterProvider:
        def __init__(self, resource: Any, metric_readers: list[Any]) -> None:
            self.resource = resource
            self.metric_readers = metric_readers

    class _PeriodicExportingMetricReader:
        def __init__(self, exporter: Any) -> None:
            self.exporter = exporter

    class _Meter:
        def create_counter(self, name: str, description: str) -> Any:
            del name, description
            return SimpleNamespace(add=lambda value, attrs=None: (value, attrs))

        def create_histogram(self, name: str, description: str, unit: str) -> Any:
            del name, description, unit
            return SimpleNamespace(record=lambda value, attrs=None: (value, attrs))

    metrics_module = SimpleNamespace(
        set_meter_provider=lambda provider: provider,
        get_meter=lambda name: _Meter(),
    )

    monkeypatch.setitem(
        sys.modules,
        "opentelemetry",
        SimpleNamespace(metrics=metrics_module, trace=trace_module),
    )
    monkeypatch.setitem(sys.modules, "opentelemetry.metrics", metrics_module)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_module)
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.resources",
        SimpleNamespace(SERVICE_NAME="service.name", Resource=_Resource),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace",
        SimpleNamespace(TracerProvider=_TracerProvider),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace.export",
        SimpleNamespace(BatchSpanProcessor=_BatchSpanProcessor),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.instrumentation.fastapi",
        SimpleNamespace(FastAPIInstrumentor=_FastAPIInstrumentor),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.metrics",
        SimpleNamespace(MeterProvider=_MeterProvider),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.metrics.export",
        SimpleNamespace(PeriodicExportingMetricReader=_PeriodicExportingMetricReader),
    )

    _configure_otel(app)
    assert getattr(app.state, "otel_instrumented", False) is True
