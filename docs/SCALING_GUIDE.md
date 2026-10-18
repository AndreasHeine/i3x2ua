# i3x2ua Scaling Guide

This guide summarizes measured REST API scaling results and provides practical guidance for sizing Uvicorn workers and NGINX deployments.

The reported figures should be treated as observations from a specific test environment, not as capacity guarantees. Actual performance depends on factors including OPC UA server behavior, information model size, request patterns, network latency, available hardware, and deployment configuration.

---

# Test Environment and Workload

The measurements below were reported on:

- AMD Ryzen 7 7800X3D
- 8 physical CPU cores
- 16 logical CPUs
- 32 GB DDR5 RAM @ 6000 MT/s

The OPC UA server was:

```text
opc.tcp://opcua.umati.app:4843
```

which is a cloud-hosted OPC UA server accessed over the Internet. As a result, the measured performance includes both OPC UA server behavior and network latency.

The discovery workload repeatedly executed the following API operations:

1. `GET /info`
2. `GET /objects`
3. `POST /objects/list`
4. `POST /objects/value`
5. `POST /objecttypes/query`

Each discovery client continuously repeated this workflow for the configured benchmark duration.

---

# Benchmark Configurations

Two deployment configurations were tested.

## Full Production-Style Configuration

Enabled:

- NGINX reverse proxy
- HTTPS
- HTTP Basic Authentication
- OpenTelemetry tracing
- Cloud-hosted OPC UA server

## Minimal Configuration

Enabled:

- Direct HTTP access to Uvicorn
- Cloud-hosted OPC UA server

Disabled:

- NGINX
- HTTPS
- HTTP Basic Authentication
- OpenTelemetry

The minimal configuration removes most infrastructure overhead and better isolates application performance.

---

# Results

## Single Worker - Direct HTTP (No NGINX, No HTTPS, No Telemetry)

The following measurements were taken using:

- 1 Uvicorn worker
- Direct HTTP access
- No NGINX reverse proxy
- No HTTPS
- No Authentication
- No OpenTelemetry
- Cloud-hosted OPC UA server

| Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Mean Workflow Latency |
|---:|---:|---:|---:|
| 1 | 388 | 77 | 13 ms |
| 8 | 443 | 88 | 91 ms |
| 16 | 407 | 81 | 197 ms |
| 32 | 429 | 85 | 373 ms |
| 64 | 438 | 85 | 730 ms |

### Observations

The highest observed throughput was approximately:

```text
443 HTTP requests/s
88 discovery workflows/s
```

using:

```text
1 Uvicorn worker
8 concurrent discovery clients
```

Throughput remains remarkably stable between 8 and 64 concurrent discovery clients:

```text
407-443 HTTP requests/s
81-88 discovery workflows/s
```

while latency increases as requests queue.

Most importantly:

```text
0% error rate
```

was observed for all measurements.

This demonstrates that even a single-worker deployment can provide stable operation under substantial discovery traffic.

---

## Production Deployment (NGINX, HTTPS, Authentication, OpenTelemetry)

The following measurements were taken using:

- NGINX reverse proxy
- HTTPS
- HTTP Basic Authentication
- OpenTelemetry tracing
- Cloud-hosted OPC UA server

### Historical Results

| Uvicorn Workers | Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Mean Workflow Latency |
|---:|---:|---:|---:|---:|
| 1 | 8 | 314 | 62 | 128 ms |
| 4 | 8 | 623 | 124 | 50 ms |
| 8 | 8 | 786 | 157 | 51 ms |

### Optimized Production Deployment

After enabling upstream connection pooling and keepalive reuse:

| Uvicorn Workers | Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Mean Workflow Latency |
|---:|---:|---:|---:|---:|
| 4 | 64 | 1074 | 213 | 298 ms |
| 4 | 128 | 1132 | 223 | 564 ms |
| 4 | 256 | 1037 | 200 | 1225 ms |
| 4 | 512 | 1003 | 187 | 2496 ms |
| 8 | 64 | 896 | 177 | 355 ms |
| 8 | 128 | **1587** | **314** | **402 ms** |
| 8 | 256 | 1580 | 309 | 805 ms |
| 8 | 512 | 1469 | 280 | 1704 ms |

### Worker Scaling Comparison

| Workers | Peak HTTP Requests/s | Peak Discovery Workflows/s |
|---:|---:|---:|
| 1 | 443 | 88 |
| 4 | 1132 | 223 |
| 8 | 1587 | 314 |

Compared to a single-worker deployment:

- 4 workers increased throughput by approximately 2.6×.
- 8 workers increased throughput by approximately 3.6×.
- All optimized configurations completed with 0% observed HTTP errors.

### Observations

The highest observed throughput was:

```text
1587 HTTP requests/s
314 discovery workflows/s
```

using:

```text
8 Uvicorn workers
128 concurrent discovery clients
HTTPS
Basic Authentication
OpenTelemetry
NGINX upstream keepalive pooling
```

The optimized configuration completed all benchmark runs with:

```text
0% error rate
```

Throughput increased substantially as concurrency increased from 64 to 128 concurrent clients.

Beyond approximately:

```text
128 concurrent discovery clients
```

throughput plateaued while latency continued to increase.

This behavior indicates that the deployment reached a saturation point where additional concurrency primarily increased queueing delay rather than useful work.

The observed practical throughput limit for the tested environment appears to be approximately:

```text
1600 REST requests/s
300+ discovery workflows/s
```

with stable operation and no observed HTTP errors.

---

# NGINX Connection Reuse and Scaling

Benchmarking identified NGINX upstream connection management as a major factor affecting both throughput and reliability.

During early testing, the reverse proxy occasionally produced:

```text
502 Bad Gateway
connect() failed (99: Address not available)
```

errors under heavy load.

The root cause was excessive upstream connection churn between NGINX and Uvicorn. Without an upstream keepalive pool, NGINX can establish and close backend TCP connections far more frequently than necessary.

Enabling upstream connection reuse:

- Eliminated the observed 502 errors
- Increased throughput substantially
- Reduced backend connection churn
- Improved scalability under high concurrency

A recommended starting configuration is:

```nginx
upstream i3x2ua_backend {
    server i3x2ua:8000;

    keepalive 32;
    keepalive_requests 1000;
    keepalive_timeout 60s;
}

location / {
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    proxy_connect_timeout 5s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;

    proxy_pass http://i3x2ua_backend;
}
```

Additional recommended settings:

```nginx
worker_processes auto;

events {
    worker_connections 4096;
}
```

For Docker deployments, increasing the file descriptor limit is also recommended:

```yaml
ulimits:
  nofile:
    soft: 65535
    hard: 65535
```

For production deployments, upstream keepalive pooling should be considered a standard configuration rather than an optional optimization.

---

# Saturation Characteristics

The benchmark results indicate that throughput scales significantly with concurrency up to approximately:

```text
128 concurrent discovery clients
```

At that point the tested environment achieved approximately:

```text
1587 HTTP requests/s
314 discovery workflows/s
```

Beyond this level, additional concurrency did not significantly increase throughput:

| Discovery Clients | HTTP Requests/s |
|---:|---:|
| 128 | 1587 |
| 256 | 1580 |
| 512 | 1469 |

Instead, latency increased while throughput remained relatively constant.

This behavior is consistent with a saturated system and suggests that either:

- the OPC UA backend,
- network latency,
- application processing,
- or hardware resources

became the dominant limiting factors rather than the HTTP stack itself.

---

# Interpretation of the Results

The benchmark workload is intentionally demanding because it continuously performs complete information-model discovery operations.

Most industrial applications do not continuously execute discovery workflows.

Typical industrial workloads consist of:

- HMI access
- Dashboard refreshes
- MES queries
- Engineering diagnostics
- Asset lookups
- Subscription-based updates

rather than repeated full information-model discovery.

For this reason, benchmark throughput numbers should be viewed as stress-test results rather than representative day-to-day operating conditions.

The benchmark also demonstrates that infrastructure configuration can significantly influence throughput. In this test environment, NGINX connection reuse was responsible for a substantial throughput increase without requiring changes to the application itself.

---

# Recommended Worker Counts

The benchmark results suggest starting conservatively and scaling only when necessary.

## Suggested Starting Points

| Deployment Type | Initial Worker Count |
|---|---:|
| Single Machine | 1 |
| Production Cell | 1 |
| Production Line | 1-2 |
| Factory Gateway | 4 |
| Enterprise Aggregation Gateway | 8 |

A single-worker deployment sustained approximately:

```text
388-443 REST requests/s
77-88 discovery workflows/s
```

during testing against a cloud-hosted OPC UA server.

For many machine-level and production-line deployments, this level of performance is already sufficient.

---

# Why Start With One Worker?

A single worker offers several advantages:

- Lowest memory footprint
- Lowest OPC UA session count
- Simplest deployment
- Shared cache state
- No worker coordination overhead
- Excellent performance for most machine-level deployments

The benchmark results show that one worker is not merely a development configuration. It is a viable production configuration for many industrial use cases.

---

# Multiple Uvicorn Workers

For example:

```bash
uvicorn i3x_server.main:app --workers 4
```

or:

```bash
uvicorn i3x_server.main:app --workers 8
```

Each worker is an independent Python process.

Advantages:

- Better concurrency
- Higher throughput
- Lower latency under heavy load
- Better CPU utilization

Trade-offs:

- Higher memory consumption
- Duplicated caches
- Additional OPC UA connections or sessions
- Longer startup time

Additional workers should therefore be added only when measurements demonstrate a need for additional concurrency.

---

# Memory Planning

A useful approximation is:

```text
Total RAM ≈ supervisor process
          + (worker count × worker memory)
```

Estimate deployment memory with:

```text
Estimated RAM ≈ worker RSS × worker count × 1.2
```

The additional 20% accounts for:

- Operating system overhead
- Container runtime overhead
- NGINX
- Background activity

Measure worker memory using:

```bash
ps -o pid,rss,command -C python
```

For Docker deployments:

```bash
docker stats
```

Use measured container memory whenever possible.

---

# Example Deployment Profiles

## Single Machine

Typical examples:

- CNC machine
- Robot cell
- Packaging machine
- Test station

Recommended configuration:

```text
1 worker
```

---

## Production Cell

Typical examples:

- Multiple machines
- Local HMI
- Local dashboards

Recommended configuration:

```text
1 worker
```

Increase to:

```text
2 workers
```

only if measurements justify it.

---

## Production Line

Typical examples:

- Multiple production stations
- MES connectivity
- Several dashboard users

Recommended configuration:

```text
1-2 workers
```

---

## Factory Gateway

Typical examples:

- Multiple machine connections
- Multiple HMIs
- MES integration
- Dashboarding platforms

Recommended configuration:

```text
4 workers
```

The benchmark demonstrated approximately:

```text
1100 HTTP requests/s
220 discovery workflows/s
```

using four workers, making this configuration suitable for many centralized factory gateway deployments.

---

## Enterprise Gateway

Typical examples:

- Multiple OPC UA servers
- Centralized asset access
- Analytics systems
- Digital twin platforms

Recommended configuration:

```text
8 workers
```

The benchmark demonstrated approximately:

```text
1587 HTTP requests/s
314 discovery workflows/s
0% error rate
```

using eight workers.

For deployments serving many concurrent clients or aggregating multiple OPC UA servers, eight workers can provide substantially higher throughput than four workers.

---

# Configure Docker Compose

The default deployment starts with a single worker.

Example:

```yaml
command:
  - python
  - -m
  - uvicorn
  - i3x_server.main:app
  - --workers
  - "1"
```

Increase only when measurements demonstrate a need for additional concurrency.

Apply changes with:

```bash
docker compose up -d --build i3x2ua
```

Verify deployment health afterwards:

```bash
docker compose ps

docker compose logs --tail=100 i3x2ua
```

---

# Benchmark Your Deployment

Suggested benchmark progression:

```bash
uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 1 --duration 60

uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 8 --duration 60

uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 16 --duration 60

uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 32 --duration 60

uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 64 --duration 60

uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 128 --duration 60
```

For realistic comparisons:

- Keep the OPC UA server identical.
- Keep the information model identical.
- Change only one parameter at a time.
- Repeat measurements.
- Monitor CPU, memory, latency, and errors.
- Validate using the real production proxy and TLS path whenever applicable.

---

# Troubleshooting Scaling Issues

If latency increases or errors appear:

1. Check resource usage:

```bash
docker stats
```

2. Review application logs:

```bash
docker compose logs i3x2ua
```

3. Review NGINX logs:

```bash
docker compose logs nginx
```

4. Verify OPC UA server responsiveness.

5. Check whether increased worker counts create excessive OPC UA sessions or backend load.

6. Verify NGINX upstream connection reuse and keepalive configuration.

7. Repeat measurements to distinguish transient failures from reproducible capacity limits.

---

# Summary

The benchmark results demonstrate that i3x2ua can support substantial industrial REST workloads ranging from individual machines to centralized factory gateways and enterprise deployments.

A single-worker deployment achieved approximately:

```text
440 HTTP requests/s
88 discovery workflows/s
0% error rate
```

A four-worker deployment achieved approximately:

```text
1130 HTTP requests/s
223 discovery workflows/s
0% error rate
```

A production-style deployment using:

- NGINX
- HTTPS
- HTTP Basic Authentication
- OpenTelemetry
- Optimized upstream connection pooling
- 8 Uvicorn workers

achieved approximately:

```text
1587 HTTP requests/s
314 discovery workflows/s
0% error rate
```

on the tested hardware.

These benchmark results demonstrate three practical deployment tiers:

| Deployment Tier | Workers | Typical Throughput |
|---|---:|---:|
| Machine / Cell | 1 | ~440 req/s |
| Factory Gateway | 4 | ~1130 req/s |
| Enterprise Gateway | 8 | ~1587 req/s |

Throughput increased with concurrency up to approximately:

```text
128 concurrent discovery clients
```

Beyond that point, throughput remained largely constant while latency increased, indicating a practical saturation point for the tested environment.

The benchmark also demonstrated that NGINX upstream connection reuse is an important part of a high-performance deployment. Enabling keepalive pooling eliminated observed reverse-proxy-related 502 errors and significantly increased throughput.

For most deployments:

```text
Start with 1 worker
Measure
Scale incrementally
```

For centralized factory gateways and enterprise deployments:

```text
4-8 workers
```

combined with NGINX upstream keepalive pooling provides excellent scalability while maintaining stable operation.