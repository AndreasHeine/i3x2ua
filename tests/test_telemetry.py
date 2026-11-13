"""The optional OpenTelemetry loader must degrade to no-op rather than raise."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from i3x_server.telemetry import OtelTracing, load_otel_tracing


def _fake_api() -> SimpleNamespace:
    """A stand-in for the ``opentelemetry`` API surface the loader reads."""
    return SimpleNamespace(
        trace=SimpleNamespace(
            get_tracer=lambda name: SimpleNamespace(tracer_name=name),
        ),
    )


def _fake_trace_module() -> SimpleNamespace:
    return SimpleNamespace(Status=object(), StatusCode=object(), get_current_span=lambda: None)


def test_load_otel_tracing_degrades_when_dependency_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing package leaves every member ``None`` so tracing sites stay no-op."""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)

    bundle = load_otel_tracing("i3x_server.prompts")

    assert bundle == OtelTracing()
    assert bundle.trace is None
    assert bundle.status is None
    assert bundle.status_code is None
    assert bundle.get_current_span is None
    assert bundle.tracer is None


def test_load_otel_tracing_builds_a_tracer_for_the_given_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", _fake_api())
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", _fake_trace_module())

    bundle = load_otel_tracing("i3x_server.mcp")

    assert bundle.tracer is not None
    assert bundle.tracer.tracer_name == "i3x_server.mcp"
    assert bundle.status is not None
    assert bundle.status_code is not None
    assert bundle.get_current_span is not None


def test_load_otel_tracing_without_a_tracer_name_leaves_the_tracer_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sites that create their tracer at call time must not get a proxy from the loader."""
    monkeypatch.setitem(sys.modules, "opentelemetry", _fake_api())
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", _fake_trace_module())

    bundle = load_otel_tracing()

    assert bundle.tracer is None
    assert bundle.trace is not None
