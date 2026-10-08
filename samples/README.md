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
python .\samples\client.py --base-url http://127.0.0.1:8000
```

Limit discovery on large address spaces:

```powershell
python .\samples\client.py --max-nodes 100 --max-depth 3
```

Fetch instances of a known type without walking the tree:

```powershell
python .\samples\client.py --type-element-id "ns=2;i=1001"
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
python .\samples\subscription_client.py --mode poll --element-ids "property-dd1a9a05d251425f" "property-0123456789abcdef"
```

Change the polling interval, for example to half a second:

```powershell
python .\samples\subscription_client.py --mode poll --poll-interval 0.5 --element-ids "property-dd1a9a05d251425f" "property-0123456789abcdef"
```

Polling acknowledges only updates already printed and reports queue-overflow
warnings to stderr.

### SSE streaming

Receive updates continuously over Server-Sent Events:

```powershell
python .\samples\subscription_client.py --mode sse --element-ids "property-dd1a9a05d251425f" "property-0123456789abcdef"
```

SSE ignores connection/keepalive comments and stops on the server's close event.

### Notifications and lifecycle

Use the i3X `elementId` values returned by your server, such as
`property-dd1a9a05d251425f`, rather than the underlying OPC UA NodeIds.
Replace the example IDs with your actual IDs; `property-0123456789abcdef` is
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
| `--base-url URL` | i3X server base URL | `http://127.0.0.1:8000` |
| `--username USER` | HTTP Basic Auth username | Not set |
| `--password PASSWORD` | HTTP Basic Auth password | Not set |
| `--insecure` | Disable TLS certificate verification for development certificates | Verification enabled |

For HTTPS behind a Basic Auth proxy:

```powershell
python .\samples\client.py --base-url https://127.0.0.1:8443 --username admin --password pw1 --insecure
python .\samples\subscription_client.py --base-url https://127.0.0.1:8443 --username admin --password pw1 --insecure --mode sse --element-ids "property-dd1a9a05d251425f"
```

Use your actual proxy credentials. Omit `--insecure` when the server certificate
is trusted; do not disable certificate verification in production.

Display all command-line options:

```powershell
python .\samples\client.py --help
python .\samples\subscription_client.py --help
```
