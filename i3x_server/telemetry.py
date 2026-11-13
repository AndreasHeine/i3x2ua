"""Optional OpenTelemetry loading shared by the runtime's tracing seams.

``opentelemetry`` is an optional dependency, so every tracing site must keep working without
it. The tracing API is loaded here once; call sites keep their ``is not None`` guards and
degrade to no-op tracing when the loader reports nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
