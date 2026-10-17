# i3X sample clients

These clients use only the Python standard library; no additional client packages
are required. Start an i3X server first (see the [project README](../README.md)).
The default server URL is `http://127.0.0.1:8000`.

Run the commands below from the repository root. If your terminal is already in
this folder, omit the `.\samples\` prefix.

## Discovery client

[client.py](client.py) discovers objects, resolves their type schemas, prints the
object tree, and reads values for composition roots.

```powershell
python .\samples\client.py --base-url http://127.0.0.1:8000/v1
```

Limit discovery on large address spaces:

```powershell
python .\samples\client.py --max-nodes 100 --max-depth 3
```

Fetch instances of a known type without walking the tree:

```powershell
python .\samples\client.py --type-element-id "urn:sampleserver:objecttype:http-opcfoundation-org-ua:v-1-05-03:servertype:i-2004"
```

Replace the example type ID with a `typeElementId` from your server.

| Option | Description | Default |
|---|---|---|
| `--max-nodes N` | Maximum number of discovered objects | `200` |
| `--max-depth N` | Maximum breadth-first discovery levels from the roots | Unlimited |
| `--type-element-id ID` | Fetch instances of this type instead of walking the tree | Not set |
| `--no-values` | Skip reading and printing current values | Values enabled |

## Subscription client

[subscription_client.py](subscription_client.py) creates a subscription, registers
the supplied element IDs, and prints incoming notifications.

### Polling

Poll pending updates once per second:

```powershell
python .\samples\subscription_client.py --mode poll --element-ids "property-dd1a9a05d251425f" "property-7fe339112a5b01e4"
```

Change the polling interval, for example to half a second:

```powershell
python .\samples\subscription_client.py --mode poll --poll-interval 0.5 --element-ids "property-dd1a9a05d251425f" "property-7fe339112a5b01e4"
```

Polling acknowledges only updates already printed and reports queue-overflow
warnings to stderr.

### SSE streaming

Receive updates continuously over Server-Sent Events:

```powershell
python .\samples\subscription_client.py --mode sse --element-ids "property-dd1a9a05d251425f" "property-7fe339112a5b01e4"
```

SSE ignores connection/keepalive comments and stops on the server's close event.

### Notifications and lifecycle

Use the i3X `elementId` values returned by your server, such as
`property-dd1a9a05d251425f`, rather than the underlying OPC UA NodeIds.
Replace the example IDs with your actual IDs; `property-7fe339112a5b01e4` is
only a placeholder for a second property. Pass each ID as a separate argument.
The client sends these IDs unchanged and registers duplicate IDs only once.

Each notification is printed immediately as one JSON line:

```json
{"sequenceNumber":7,"elementId":"property-dd1a9a05d251425f","value":21.5,"quality":"Good","timestamp":"2026-10-08T14:00:00Z"}
```

Status messages and errors go to stderr, so stdout can be redirected to a file:

```powershell
python .\samples\subscription_client.py --mode sse --element-ids "property-dd1a9a05d251425f" > notifications.jsonl
```

Press **Ctrl+C** to stop. The client deletes its subscription on exit, including
after registration or receive errors. Connection failures are reported without
automatic reconnection; receive and cleanup failures produce a nonzero exit code.
Abrupt process termination cannot perform cleanup.

| Option | Description | Default |
|---|---|---|
| `--element-ids ID [ID ...]` | List of element IDs to monitor | Required |
| `--mode poll\|sse` | Notification receive mode | `poll` |
| `--client-id ID` | Subscription owner ID | Generated unique ID |
| `--poll-interval SECONDS` | Delay between sync calls; positive, finite number | `1` |
| `--max-depth N` | Maximum composition depth to monitor; nonnegative integer | All descendant properties |
| `--timeout SECONDS` | HTTP/socket read timeout, including SSE reads; positive, finite number | `30` |

## Connection options for both clients

| Option | Description | Default |
|---|---|---|
| `--base-url URL` | i3X server base URL | `http://127.0.0.1:8000/v1` |
| `--username USER` | HTTP Basic Auth username | Not set |
| `--password PASSWORD` | HTTP Basic Auth password | Not set |
| `--insecure` | Disable TLS certificate verification for development certificates | Verification enabled |

For HTTPS behind a Basic Auth proxy:

```powershell
python .\samples\client.py --base-url https://127.0.0.1:8443/v1 --username admin --password pw1 --insecure
python .\samples\subscription_client.py --base-url https://127.0.0.1:8443/v1 --username admin --password pw1 --insecure --mode sse --element-ids "property-dd1a9a05d251425f"
```

Use your actual proxy credentials. Omit `--insecure` when the server certificate
is trusted; do not disable certificate verification in production.

Display all command-line options:

```powershell
python .\samples\client.py --help
python .\samples\subscription_client.py --help
```

## Performance benchmark

Run [run_benchmark.py](../run_benchmark.py) from the repository root to measure
the sample clients rather than repeatedly launching their command-line programs:

```powershell
python .\run_benchmark.py --duration 30 --warmup 5 --output benchmark.json
python .\run_benchmark.py --discovery 4 --subscriptions 4 --element-ids "property-dd1a9a05d251425f" --duration 60 --output benchmark-mixed.json
```

Use IDs discovered on your own server for subscriptions. The default workload is
one discovery client and no subscriptions, plus an independent `GET /info` probe.
Discovery clients repeatedly run the sample's breadth-first discovery, type/schema
resolution, and composition-root value reads, without tree/value printing. Object
and type caches are cleared between discovery cycles; each worker retains its own
HTTP keep-alive connection. Polling clients reuse the subscription sample's create,
register, and delete methods, acknowledging received updates without printing them.
Subscriptions are created before warm-up and deleted after all workers stop.

The console and optional JSON report show successful requests/second, failure
counts, and successful-request latency mean, p50, p95, p99, and maximum in
milliseconds, grouped by workload and endpoint. Discovery workflow latency is
reported separately and is **not** counted again in HTTP totals. Polling also
reports notification counts and notifications/second. These are delivered update
counts, not source sampling rates or end-to-end source-to-client latency.

Measurements use a monotonic high-resolution clock and include HTTP transport,
response reads, JSON decoding, and response validation. Only operations that both
start and finish inside the fixed measurement window are counted. Warm-up,
subscription setup/cleanup, and requests draining after the deadline are excluded.
Ctrl+C shortens the measurement window and still attempts subscription cleanup.
Shutdown can take longer than the measured duration while active workflows drain;
`--request-timeout` bounds each request, not an entire discovery workflow.
Percentiles use nearest rank over **all successful measured samples**, not a rolling
window; memory use grows with measured request count.

This is a closed-loop benchmark: each worker waits for its response before issuing
the next request. It measures client-observed performance at the selected
concurrency, not an open-loop arrival rate or server-only execution time. Run it
on a separate machine from the gateway for less client/server resource contention.
Keep workload limits and polling/probe intervals identical when comparing runs.

| Option | Description | Default |
|---|---|---|
| `--duration SECONDS` | Fixed measurement duration after warm-up | `30` |
| `--warmup SECONDS` | Unmeasured warm-up under the same workload; `0` disables it | `5` |
| `--discovery N` | Parallel discovery workers; `0` disables discovery | `1` |
| `--subscriptions N` | Parallel polling workers; requires `--element-ids` | `0` |
| `--max-nodes N`, `--max-depth N` | Discovery bounds, using the sample client's traversal semantics | `200`, `1` |
| `--no-values` | Skip composition-root value reads | Values enabled |
| `--value-max-depth N` | Composition depth for value reads; `0` is unbounded | `1` |
| `--subscription-max-depth N` | Composition depth for monitored items | All descendant properties |
| `--poll-interval SECONDS` | Delay after each sync; `0` requests maximum polling throughput | `1` |
| `--probe-interval SECONDS` | Delay after each independent `/info` probe | `0.5` |
| `--request-timeout SECONDS` | Per-request socket timeout | `10` |
| `--failure-delay SECONDS` | Backoff after a failed discovery cycle | `0.2` |
| `--output PATH` | Save machine-readable results (overwrites an existing file) | Not set |

The benchmark accepts the same `--base-url`, `--username`, `--password`, and
`--insecure` connection options shown above. Credentials are omitted from report
configuration. Partial bulk failures, malformed responses, and subscription
overflow warnings are failures, not successful samples. Errors during warm-up,
setup, draining, or cleanup are reported separately and also cause a nonzero exit.
Exit codes: `0` for an error-free run with measured requests, `1` for failures or no
measured requests, `2` for invalid arguments, and `130` for interruption.
