from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.sdk") is None,
    reason="OpenTelemetry SDK extra not installed",
)
def test_native_telemetry_exports_http_spans_and_mcp_metrics() -> None:
    script = dedent(
        """
        import os

        os.environ["I3X_OTEL_ENABLED"] = "true"
        os.environ["I3X_SKIP_OPCUA_CONNECT"] = "true"
        os.environ["I3X_MODEL_PRELOAD_ON_STARTUP"] = "false"

        from fastapi.testclient import TestClient
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        import i3x_server.mcp as mcp
        from i3x_server.bootstrap.app_factory import create_app

        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        metric_reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

        with TestClient(create_app()) as client:
            response = client.get("/openapi.json")
            assert response.status_code == 200
            assert mcp._mcp_tool_calls is not None
            assert mcp._mcp_tool_duration is not None
            mcp._mcp_tool_calls.add(1, {"mcp.tool.name": "smoke"})
            mcp._mcp_tool_duration.record(0.001, {"mcp.tool.name": "smoke"})

        span_names = {span.name for span in span_exporter.get_finished_spans()}
        metric_data = metric_reader.get_metrics_data()
        metric_names = {
            metric.name
            for resource_metrics in metric_data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        }

        assert "GET /openapi.json" in span_names
        assert {
            "http.server.request.duration",
            "mcp.tool_calls",
            "mcp.tool_duration_seconds",
        } <= metric_names
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
