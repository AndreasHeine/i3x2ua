# OpenTelemetry Configuration for MCP

This guide explains how to enable distributed tracing and metrics for the MCP
(Model Context Protocol) layer.

---

## Prerequisites

Install the optional OpenTelemetry extras into the project environment:

```bash
uv pip install "i3x2ua[otel]"
```

This pulls in:

| Package | Purpose |
|---|---|
| `opentelemetry-api` | API surface (tracer, meter, span) |
| `opentelemetry-sdk` | SDK with TracerProvider / MeterProvider |
| `opentelemetry-exporter-otlp-proto-http` | OTLP/HTTP exporter for traces and metrics |
| `opentelemetry-instrumentation-fastapi` | Auto-instrumentation of every HTTP route |

All packages are **optional** — the server starts and runs normally without them.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `I3X_OTEL_ENABLED` | `false` | Set to `true` / `1` to enable OpenTelemetry |
| `I3X_OTEL_SERVICE_NAME` | `i3x2ua` | Value of the `service.name` resource attribute |
| `I3X_OTEL_OTLP_ENDPOINT` | *(unset)* | Base URL of your OTLP collector (no trailing slash) |

### Minimal local setup

```env
I3X_OTEL_ENABLED=true
I3X_OTEL_OTLP_ENDPOINT=http://localhost:4318
```

### Production `.env` example

```env
I3X_OTEL_ENABLED=true
I3X_OTEL_SERVICE_NAME=i3x2ua-prod
I3X_OTEL_OTLP_ENDPOINT=https://otel-collector.example.com
```

> **Note**: `I3X_OTEL_ENABLED=true` without setting `I3X_OTEL_OTLP_ENDPOINT`
> activates the SDK and FastAPI auto-instrumentation but does **not** export
> data anywhere. This is useful for local debugging with a custom exporter or
> when you want spans only in logs.

---

## What Gets Instrumented

### 1. HTTP routes (FastAPI auto-instrumentation)

Every request handled by FastAPI automatically creates a span:

```
GET /v1/namespaces  →  span: GET /v1/namespaces
POST /mcp           →  span: POST /mcp
```

Attributes follow [OpenTelemetry HTTP semantic conventions](https://opentelemetry.io/docs/specs/semconv/http/).

### 2. MCP tool calls (`mcp.tool_call` span)

Every call to `invoke_mcp_tool` creates a child span named `mcp.tool_call`
with the following attributes:

| Attribute | Example |
|---|---|
| `mcp.tool.name` | `getNamespaces` |
| `mcp.tool.method` | `GET` |
| `mcp.tool.path` | `/v1/namespaces` |
| `mcp.tool.arg_count` | `0` |
| `http.response.status_code` | `200` |
| `mcp.tool.duration_s` | `0.042` |

On failure the span is marked with `StatusCode.ERROR` and the exception is
recorded via `span.record_exception()`.

### 3. Prompt execution (`prompt.execute` span)

Every call to `execute_prompt` creates a child span named `prompt.execute`:

| Attribute | Example |
|---|---|
| `prompt.name` | `analyze_machine_state` |
| `prompt.inputs` | `telemetry` |
| `render.success` | `true` / `false` |
| `execution.time` | `0.001` |

Failures (missing inputs, bad template variables) set `StatusCode.ERROR` with
a description.

---

## Metrics

Two instruments are recorded per tool invocation and exported via OTLP:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `mcp.tool_calls` | Counter | `mcp.tool.name`, `http.response.status_code` | Total invocations |
| `mcp.tool_duration_seconds` | Histogram | `mcp.tool.name` | Wall-clock duration in seconds |

On error the counter is labelled with `error=true` instead of a status code.

---

## Running a Local Collector (Jaeger / Grafana Stack)

### Option A — Jaeger all-in-one

```bash
docker run --rm -p 4318:4318 -p 16686:16686 \
  jaegertracing/all-in-one:latest
```

Open `http://localhost:16686` to browse traces.
Set `I3X_OTEL_OTLP_ENDPOINT=http://localhost:4318`.

### Option B — OpenTelemetry Collector + Jaeger (docker-compose)

```yaml
# docker-compose.otel.yml
services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:latest
    command: ["--config=/etc/otel/config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otel/config.yaml
    ports:
      - "4318:4318"   # OTLP/HTTP receiver
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"
```

`otel-collector-config.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  jaeger:
    endpoint: jaeger:14250
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [jaeger]
```

### Option C — Grafana LGTM stack

```bash
docker run --rm -p 4318:4318 -p 3000:3000 \
  grafana/otel-lgtm:latest
```

Open `http://localhost:3000` (admin / admin) and use the **Tempo** data source
for traces and **Mimir** for metrics.

---

## Docker / Docker Compose integration

Pass the variables via environment in your `docker-compose.yml`:

```yaml
services:
  i3x2ua:
    environment:
      I3X_OTEL_ENABLED: "true"
      I3X_OTEL_SERVICE_NAME: "i3x2ua"
      I3X_OTEL_OTLP_ENDPOINT: "http://otel-collector:4318"
```

---

## Verifying the integration

1. Start the server with `I3X_OTEL_ENABLED=true`.
2. Look for these log lines at startup:

   ```
   INFO  i3x_server.bootstrap.app_factory OpenTelemetry OTLP trace exporter configured endpoint=http://localhost:4318
   INFO  i3x_server.bootstrap.app_factory FastAPI OpenTelemetry instrumentation enabled
   INFO  i3x_server.bootstrap.app_factory OpenTelemetry OTLP metric exporter configured endpoint=http://localhost:4318
   INFO  i3x_server.bootstrap.app_factory OpenTelemetry configured service=i3x2ua
   ```

3. Call any MCP tool (e.g. `GET /mcp/tools`, then `POST /mcp` with
   `method: tools/call`).
4. Open your tracing backend and search for service name `i3x2ua` — you should
   see a `POST /mcp` root span with a `mcp.tool_call` child span.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No log lines about OTel at startup | `I3X_OTEL_ENABLED` is not set or is `false` | Set `I3X_OTEL_ENABLED=true` |
| `opentelemetry-sdk not installed` warning | Extras not installed | Run `uv pip install "i3x2ua[otel]"` |
| `OTLP trace exporter skipped` warning | Exporter package missing | Run `uv pip install "i3x2ua[otel]"` |
| Spans visible but no metrics | OTLP metrics exporter or SDK metrics missing | Same extras install |
| Collector unreachable errors | Wrong endpoint or collector not running | Check `I3X_OTEL_OTLP_ENDPOINT` and collector status |
