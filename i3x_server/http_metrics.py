"""Prometheus instrumentation for the gateway's own request handling.

The metrics here are a gateway concern, not part of the i3X contract: ``/metrics`` is served
outside ``/v1`` and excluded from the OpenAPI document (so it never becomes an MCP tool). The
request middleware records the per-request instruments, which is where the duration is measured
anyway, so no extra middleware layer is needed:

- ``i3x_http_requests_total{method,path,status}`` - one per served request, including the
  ``499`` the middleware reports for a client-aborted request.
- ``i3x_http_request_duration_seconds{method,path}`` - time until the response *started*
  (time to first byte), not the full drain of a streamed body.
- ``i3x_http_stream_duration_seconds{method,path}`` - time until a response written in chunks
  finished. A stream that stays open for minutes is invisible in the TTFB histogram, which is why
  this one exists separately and with minute-scale buckets.
- ``i3x_http_requests_in_flight`` - requests being served right now, which is what makes event-loop
  contention on a single uvicorn worker visible.
- ``i3x_http_requests_shed_total{reason}`` - requests admission control refused before a handler
  ran. A refusal is not a handler failure, so counting it as a status code alone would mix it in
  with the responses a handler produced.
- ``i3x_http_rate_limit_clients`` - client keys the limiter tracks, i.e. how close it is to the
  bounded key set that evicts idle clients.

``path`` is the matched route template (``/v1/objects/{element_id}``), never the concrete URL,
so the label cardinality stays bounded by the number of endpoints. A route reached through a
router prefix or a mount keeps that prefix in the label (``/mcp/prompts/{name}``), because the
dashboard should show the path a client actually requested.
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import Counter, Gauge, Histogram

# Buckets wide enough to separate a model-derived lookup from a bulk write round-trip without
# exposing a per-request histogram of thousands of distinct values.
_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
# A stream is open until its last chunk, so the useful range reaches minutes rather than seconds.
_STREAM_DURATION_BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0)

UNMATCHED_PATH = "unmatched"

# Created once per process at import: repeated ``create_app()`` calls (tests, reload) reuse the
# same instruments instead of raising on re-registration into the default registry.
requests_total = Counter(
    "i3x_http_requests_total",
    "HTTP requests served by the gateway",
    ["method", "path", "status"],
)
request_duration_seconds = Histogram(
    "i3x_http_request_duration_seconds",
    "Seconds until the response started (time to first byte)",
    ["method", "path"],
    buckets=_DURATION_BUCKETS,
)
stream_duration_seconds = Histogram(
    "i3x_http_stream_duration_seconds",
    "Seconds until a streamed response finished writing its body",
    ["method", "path"],
    buckets=_STREAM_DURATION_BUCKETS,
)
requests_in_flight = Gauge(
    "i3x_http_requests_in_flight",
    "Requests the gateway is serving right now",
)
requests_shed_total = Counter(
    "i3x_http_requests_shed_total",
    "Requests refused by admission control before a handler ran",
    ["reason"],
)
rate_limit_clients = Gauge(
    "i3x_http_rate_limit_clients",
    "Client keys the request limiter currently tracks",
)


def _concrete_suffix(path_format: object, params: object) -> str | None:
    """Fill a route's ``path_format`` with the matched path parameters.

    Returns ``None`` when either side is missing or the parameters do not cover the template, in
    which case the prefix cannot be recovered and the bare template is used instead.
    """
    if not isinstance(path_format, str) or not isinstance(params, dict):
        return None
    try:
        return path_format.format(**params)
    except (KeyError, IndexError):
        return None


def _matched_prefix(scope: Mapping[str, object], route: object) -> str:
    """Router prefix or mount path the matched template was reached under.

    ``Route.path`` is only the route's own template, so a route included under a prefix
    (``APIRouter(prefix="/mcp")``) reports ``/prompts/{name}`` for a request to
    ``/mcp/prompts/<name>``. Filling ``path_format`` with the matched ``path_params`` reproduces
    the concrete suffix that matched, and whatever precedes it in the request path is the prefix.
    Both the prefix and the template are fixed per endpoint, so the label stays bounded.
    """
    suffix = _concrete_suffix(getattr(route, "path_format", None), scope.get("path_params"))
    full_path = scope.get("path")
    if isinstance(suffix, str) and isinstance(full_path, str) and full_path.endswith(suffix):
        return full_path[: len(full_path) - len(suffix)]
    return ""


def route_label(scope: Mapping[str, object]) -> str:
    """Route template for a request scope, or ``unmatched`` when no route matched.

    Starlette/FastAPI attach the matched route to the scope; its ``path`` is the template, which
    keeps ``{element_id}``-style paths from turning into one label per resource.
    """
    route = scope.get("route")
    template = getattr(route, "path", None) if route is not None else None
    if not isinstance(template, str) or not template:
        return UNMATCHED_PATH
    return _matched_prefix(scope, route) + template


def observe_request(method: str, path: str, status: int, duration_seconds: float) -> None:
    """Record one served request: the counter and the duration histogram."""
    requests_total.labels(method=method, path=path, status=str(status)).inc()
    request_duration_seconds.labels(method=method, path=path).observe(duration_seconds)


def observe_stream(method: str, path: str, duration_seconds: float) -> None:
    """Record how long a response written in chunks took to finish writing its body."""
    stream_duration_seconds.labels(method=method, path=path).observe(duration_seconds)


def count_shed(reason: str) -> None:
    """Count one request refused by admission control, naming which surface refused it."""
    requests_shed_total.labels(reason=reason).inc()


def track_rate_limit_clients(count: int) -> None:
    """Report how many client keys the limiter is holding, bounded by its eviction policy."""
    rate_limit_clients.set(count)
