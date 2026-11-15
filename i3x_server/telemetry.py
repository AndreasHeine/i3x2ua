"""Optional OpenTelemetry loading shared by the runtime's tracing seams.

``opentelemetry`` is an optional dependency, so every tracing site must keep working without
it. The tracing API is loaded here once; call sites keep their ``is not None`` guards and
degrade to no-op tracing when the loader reports nothing.

:func:`missing_otel_export_modules` answers the separate question of whether automatic OTLP
*export* can run at all, which is what startup diagnostics and the FastAPI telemetry switches
need to know.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any

# Modules FastAPI imports when it configures OTel providers from the environment
# (``fastapi/telemetry/_runtime.py``). Their absence is what makes tracing advertised but
# impossible, so the gateway detects them by name instead of relying on FastAPI's warning,
# which the framework's lifespan guard swallows.
_OTEL_EXPORT_MODULES = (
    "opentelemetry.sdk.trace",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
)


@dataclass(frozen=True, slots=True)
class OtelTracing:
    """Tracing members loaded from the optional ``opentelemetry`` package.

    Every member is ``None`` when the dependency is not installed, so call sites never raise
    at import time.
    """

    trace: Any = None
    status: Any = None
    status_code: Any = None
    get_current_span: Any = None
    tracer: Any = None


def load_otel_tracing(tracer_name: str | None = None) -> OtelTracing:
    """Load the OpenTelemetry tracing API, optionally creating a tracer for ``tracer_name``.

    A tracer is a proxy that forwards to the global ``TracerProvider`` configured during
    bootstrap, so building it before startup is safe.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode, get_current_span
    except ImportError:
        return OtelTracing()

    tracer = trace.get_tracer(tracer_name) if tracer_name is not None else None
    return OtelTracing(
        trace=trace,
        status=Status,
        status_code=StatusCode,
        get_current_span=get_current_span,
        tracer=tracer,
    )


def missing_otel_export_modules() -> tuple[str, ...]:
    """Names of the optional OTel modules automatic export needs; empty when export can run.

    ``find_spec`` answers without importing the module itself, but it does resolve the parent
    packages, so callers only probe when tracing is actually enabled. A dotted module whose
    parent package cannot be imported raises rather than returning ``None``; both answers mean
    the module is unavailable.
    """
    missing: list[str] = []
    for module in _OTEL_EXPORT_MODULES:
        try:
            spec = find_spec(module)
        except (ImportError, ValueError):
            missing.append(module)
            continue
        if spec is None:
            missing.append(module)
    return tuple(missing)
