# i3x2ua Scaling Guide

This guide summarizes measured REST API scaling results and gives a starting
point for sizing Uvicorn workers and NGINX. Treat the figures as observations
from one test environment, not as capacity guarantees: OPC UA server behavior,
model size, request mix, container limits, and network latency can all change
the result.

## Test environment and workload

The measurements below were reported on a system with an AMD Ryzen 7 7800X3D,
8 physical cores, and 16 logical CPUs. NGINX used `worker_processes auto` and
could see all 16 logical CPUs. The API was tested with either 1 or 8 Uvicorn
workers.

The discovery workload repeatedly exercised these API operations:

1. `GET /info`
2. `GET /objects`
3. `POST /objects/list`
4. `POST /objects/value`
5. `POST /objecttypes/query`

Each discovery client repeated the workflow. The results demonstrate why
concurrency matters: a single client did not create enough simultaneous work
to reveal the full benefit of multiple application workers.

The original 1-versus-8-worker benchmark was run with HTTP API traffic and
OpenTelemetry exporting to Jaeger enabled. The additional 16-worker result
below used HTTPS through NGINX with Basic Authentication. Transport and proxy
differences are another reason not to treat the runs as a perfectly controlled
head-to-head comparison.

## Results

| Uvicorn workers | Discovery clients | Successful HTTP requests/s | Discovery workflows/s | Mean workflow latency | P95 workflow latency | P99 workflow latency |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 8 | 305 | 60 | 132 ms | 238 ms | — |
| 8 | 8 | 786 | 157 | 51 ms | 78 ms | 139 ms |
| 16 | 16 | 762 | 152 | 81 ms | 126 ms | 242 ms |

The 8-worker run increased throughput by about 2.6x (approximately 158%) and
reduced mean workflow latency by about 61%. It completed without errors in the
reported repeated runs.

In the 16-worker/16-client run, the reported successful HTTP throughput was
about 762 requests/s and successful discovery workflow throughput was about
152 workflows/s. The workflow mean was about 81 ms, with P95 of 126 ms and P99
of 242 ms. The run reported a 2.25% HTTP error rate, predominantly NGINX
`502 Bad Gateway` responses, including 498 failed `GET /info` requests. This
is a useful signal that this particular configuration/workload combination
was encountering errors; it is not evidence that NGINX alone was the root
cause. Review NGINX upstream and application logs, resource usage, and worker
health before attributing the failures.

The 16-worker run used twice as many discovery clients as the 8-worker run.
Although its throughput is in a similar range, the different concurrency,
HTTPS/proxy path, and reported errors mean that it should not be read as a
controlled comparison showing that 16 workers are slower than 8.

At a single discovery client, throughput was about 217 HTTP requests/s and 43
workflows/s, and increasing workers provided only a small improvement of
around 10%. That result is not representative of the higher-concurrency runs.

Reported endpoint mean latencies in the 8-worker test were approximately
8–13 ms:

| Endpoint | Mean latency |
|---|---:|
| `GET /info` | 8 ms |
| `GET /objects` | 13 ms |
| `POST /objects/list` | 10 ms |
| `POST /objects/value` | 9 ms |
| `POST /objecttypes/query` | 11 ms |

Another reported observation was P95 latency around 16–19 ms, P99 around
30–42 ms, and occasional maximums of 180–700 ms. These figures describe
different measurements from the workflow percentiles in the table above;
compare percentiles only when the workload, metric, and measurement window
match. Use throughput, P95, and P99 together rather than sizing from isolated
maximum latency spikes.

## Starting worker counts

Use these only as initial values to test:

| Available CPU capacity | Initial Uvicorn worker range |
|---|---:|
| 2–4 cores | 2–4 |
| 8 cores | 4–8 |
| 16+ logical CPUs | 8+ |

The reported 8-worker result was on a 16-logical-CPU system. More workers are
not automatically better: each worker is a separate process and consumes
memory and application resources. In particular, confirm the impact on OPC UA
connections and startup work for your deployment before increasing the count.

For NGINX, `worker_processes auto` is a reasonable starting point. In the
reported tests, increasing Uvicorn workers was the useful scaling change; no
NGINX tuning was needed. Revisit NGINX settings only when measurements point to
proxy saturation.

## Deployment options and process trade-offs

### One Uvicorn worker

For example, the application can be run with Uvicorn's default single worker,
or explicitly with `--workers 1`:

```sh
uvicorn i3x_server.main:app
uvicorn i3x_server.main:app --workers 1
```

This is still an ASGI application served by Uvicorn; it is not a serverless
plain Python process. A single worker is the simplest option and usually has
the lowest memory use. It can suit development, debugging, low request rates,
and small memory budgets. It also keeps process-local caches and state in one
worker. Its trade-offs are limited parallelism on multi-core systems and
throughput that can plateau under concurrent load; a slow or blocking
operation can affect requests handled by that worker.

### Multiple Uvicorn workers

For example:

```sh
uvicorn i3x_server.main:app --workers 8
```

Workers are independent Python processes, not threads in one shared process.
They can use multiple cores and isolate a worker crash, but each process loads
the application and maintains its own process-local state. This can duplicate
memory and caches, and may create additional OPC UA connections, sessions, or
subscriptions. Confirm the actual behavior and resource impact with the
configured OPC UA server and workload.

The benchmark's 1-worker and 8-worker results illustrate the throughput and
latency benefits possible under concurrent load. They should be balanced
against the additional memory and backend connection costs for the deployment.

## Memory planning

A useful first approximation is:

```text
Total RAM ≈ master/supervisor process + (worker count × worker memory)
```

As a rule of thumb, measure a worker's resident set size (RSS) after startup
and a representative workload, then estimate:

```text
Estimated deployment RAM ≈ worker RSS × worker count × 1.2
```

The extra 20% is a rough allowance for the operating system, NGINX, and runtime
overhead, not a guaranteed upper bound. The estimate depends on cache size,
OPC UA model and object count, loaded libraries, and how much memory is shared
between processes. RSS can count shared pages in each process, so summing
process RSS may overestimate physical memory; use container/cgroup memory for
the actual container budget.

On Linux, inspect the Python worker processes and their RSS with:

```sh
ps -o pid,rss,command -C python
```

RSS is reported in kilobytes; for example, `180000` is approximately 180 MB.
For Docker, observe the actual container memory while the workload is running:

```sh
docker stats
```

An example `i3x2ua 1.4GiB` reading is total container usage at that moment, not
the memory of one worker.

## Example starting profiles

These hardware and memory profiles are starting points only; validate them
with representative measurements:

| Deployment size | Example hardware | Initial worker setting |
|---|---|---:|
| Small | 2–4 cores, 4 GB RAM | 2 |
| Medium | 8 cores, 8 GB RAM | 4; compare with 8 |
| Large | 16+ logical CPUs, 16+ GB RAM | 8; benchmark before increasing |

For most production deployments, a reasonable initial combination is
`worker_processes auto` in NGINX and 4 Uvicorn workers, then compare with 8
using the benchmark procedure below. Use one worker for development, debugging,
very low request rates, or very limited RAM. Start near the physical core count
only as a hypothesis to test, not as a rule that overrides measured memory,
latency, errors, or OPC UA capacity.

## Configure Docker Compose

The root [`docker-compose.yml`](../docker-compose.yml) currently starts the
application with one Uvicorn worker. Change the value following `--workers`
under the `i3x2ua` service to the number you intend to test. For example:

```yaml
      - "--workers"
      - "4"
```

Apply the change by recreating the service:

```sh
docker compose up -d --build i3x2ua
```

Check the service logs and health after changing the worker count:

```sh
docker compose logs --tail=100 i3x2ua
docker compose ps
```

## Benchmark your deployment

Use enough concurrent discovery clients to exercise the workers. Start with 8,
then compare 16 and 32; include 1 as a low-concurrency baseline:

```sh
uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 1 --duration 60
uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 8 --duration 60
uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 16 --duration 60
uv run python run_benchmark.py --base-url http://127.0.0.1:8000/v1 --discovery 32 --duration 60
```

`run_benchmark.py` defaults to a 5-second warm-up and reports successful
requests per second, errors, and mean/P50/P95/P99/maximum latency. For
repeatable comparisons:

- Keep the API data, OPC UA server, benchmark client, request limits, and
  duration the same between runs.
- Change only the worker count or only the client concurrency at a time.
- Run each configuration more than once and compare consistent results.
- Watch CPU and memory use as well as API latency and error rate.
- Include the real proxy and TLS path in a separate test if those are part of
  production traffic.
- Increase concurrency gradually and stop if errors or resource exhaustion
  appear.

The project benchmark supports `--output results.json` to save a machine-
readable report. See `uv run python run_benchmark.py --help` for all workload
options. The benchmark's discovery workload and any custom end-to-end workflow
should not be assumed to be identical; use the workload that best represents
your clients.

## Troubleshooting during scaling tests

If errors appear or latency rises sharply:

1. Check container resource use with `docker stats`.
2. Review application logs with `docker compose logs i3x2ua` and NGINX logs
   with `docker compose logs nginx`.
3. Check whether the error is repeatable with the same worker count and
   concurrency before treating a transient 502 as a capacity limit.
4. Compare CPU, memory, OPC UA behavior, and NGINX upstream errors; do not
   assume the reverse proxy is the bottleneck.
5. Reduce the worker count if memory or OPC UA resources are exhausted, then
   retest.

## Summary

The reported workload scaled substantially from 1 to 8 Uvicorn workers when
discovery concurrency was increased to 8. The 16-worker/16-client HTTPS run
reached similar throughput but had higher workflow latency and a 2.25% error
rate; investigate these errors before increasing workers further. For
production, start with a worker-count range appropriate to available CPU and
memory, keep NGINX at `worker_processes auto`, and benchmark at representative
concurrency (8, 16, and 32 clients) to find the deployment's actual saturation
point.
