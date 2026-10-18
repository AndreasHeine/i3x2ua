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
- Cloud OPC UA server

## Minimal Configuration

Enabled:

- Direct HTTP access to Uvicorn
- Cloud OPC UA server

Disabled:

- NGINX
- HTTPS
- Basic Authentication
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
438 HTTP requests/s
88 discovery workflows/s
```

using:

```text
1 Uvicorn worker
64 concurrent discovery clients
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

## Full Production-Style Deployment

The following measurements were taken using:

- NGINX
- HTTPS
- Basic Authentication
- OpenTelemetry
- Cloud-hosted OPC UA server

| Uvicorn Workers | Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Mean Workflow Latency |
|---:|---:|---:|---:|---:|
| 1 | 8 | 314 | 62 | 128 ms |
| 4 | 8 | 623 | 124 | 50 ms |
| 8 | 8 | 786 | 157 | 51 ms |

### Observations

The best observed result was:

```text
786 HTTP requests/s
157 discovery workflows/s
```

using:

```text
8 Uvicorn workers
8 concurrent discovery clients
```

This represents approximately:

```text
2.5x higher throughput
```

than the equivalent single-worker production-style deployment.

However, increasing worker count also increases:

- Memory consumption
- Cache duplication
- OPC UA sessions and connections
- Startup overhead

For many deployments the additional throughput is unnecessary.

---

# Interpretation of the Results

The benchmark workload is intentionally demanding because it continuously performs complete information-model discovery operations.

Most industrial applications do not perform continuous discovery.

Typical industrial workloads consist of:

- HMI access
- Dashboard refreshes
- MES queries
- Engineering diagnostics
- Asset lookup
- Subscription-based updates

rather than repeated full information-model discovery.

For that reason, benchmark throughput numbers should be viewed as stress-test results rather than representative day-to-day operating conditions.

---

# Recommended Worker Counts

The benchmark results suggest starting conservatively and scaling only when necessary.

## Suggested Starting Points

| Deployment Type | Initial Worker Count |
|---|---:|
| Single Machine | 1 |
| Production Cell | 1 |
| Production Line | 1-2 |
| Factory Gateway | 2-4 |
| Enterprise Aggregation Gateway | 4-8 |

A single-worker deployment sustained approximately:

```text
388-443 REST requests/s
77-88 discovery workflows/s
```

during testing against a cloud-hosted OPC UA server.

For many machine-level and production-line deployments this level of performance is already sufficient.

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

# NGINX Configuration

The benchmark results indicate that NGINX was not the primary bottleneck.

A reasonable production starting point is:

```nginx
worker_processes auto;
```

No additional NGINX tuning was required to achieve the reported throughput numbers.

Only revisit NGINX parameters when measurements specifically indicate reverse-proxy saturation.

---

# Memory Planning

A useful approximation is:

```text
Total RAM ≈ master/supervisor process
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
- Several concurrent client applications

Recommended configuration:

```text
2-4 workers
```

---

## Enterprise Gateway

Typical examples:

- Multiple OPC UA servers
- Centralized asset access
- Digital twin platforms

Recommended configuration:

```text
4-8 workers
```

Benchmark before increasing beyond eight workers.

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
```

For realistic comparisons:

- Keep the OPC UA server identical.
- Keep the information model identical.
- Change only one parameter at a time.
- Repeat measurements.
- Monitor CPU, memory, latency, and errors.
- Validate using the real production proxy and TLS path when applicable.

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

5. Check whether increased worker counts create excessive sessions or backend load.

6. Repeat measurements to distinguish transient failures from reproducible capacity limits.

---

# Summary

The benchmark results demonstrate that i3x2ua can support the workload of many industrial applications without requiring complex deployment topologies.

A single-worker deployment communicating with a cloud-hosted OPC UA server achieved approximately:

```text
440 HTTP requests/s
88 discovery workflows/s
```

with zero observed errors.

This level of performance is sufficient for many machine-level, production-cell, and production-line deployments.

Additional workers provide substantial gains for highly concurrent environments, reaching approximately:

```text
786 HTTP requests/s
157 discovery workflows/s
```

in the tested environment.

For most deployments, start with:

```text
1 worker
```

measure actual latency, memory consumption, and CPU usage, and scale incrementally only when required.

This approach minimizes memory consumption, reduces OPC UA backend resource usage, and reflects the scaling characteristics observed during benchmarking.