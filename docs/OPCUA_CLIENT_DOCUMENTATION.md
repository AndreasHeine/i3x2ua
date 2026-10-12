# OPC UA Client Documentation (Non-Expert Guide)

This document explains what the OPC UA client does inside this application, how it is optimized, and how it behaves during connection loss and reconnect.

## What The OPC UA Client Does In This Application

Think of the OPC UA client as a translator and transport layer between:

- your i3X REST/MCP API consumers (HTTP clients), and
- the external OPC UA server (industrial data source).

At runtime, the OPC UA client is responsible for:

- opening and maintaining the OPC UA session,
- discovering the OPC UA address space (browse),
- reading live values and historical values,
- loading namespaces and object type metadata,
- supporting method calls,
- creating monitored-item subscriptions for real-time updates,
- recovering from dropped OPC UA connections.

In short: the API never talks directly to OPC UA. All OPC UA communication goes through this client.

## Where It Is Used In The App

- Startup: the app creates one shared OPC UA client instance and connects once.
- Model build/preload: the model builder uses the client to browse nodes and build i3X structures.
- Read endpoints: value/history endpoints call the client to fetch data.
- Subscription endpoints: the subscription service uses the client for native OPC UA monitored items or for polling fallback.

## Connection Behavior

## Connect

During startup, the client performs these phases:

1. Configure security (if enabled).
2. Connect session with asyncua auto-reconnect enabled.
3. Load additional OPC UA type definitions.
4. Read and cache operational limits from the server.

This keeps the connection setup explicit and gives the app server-aware limits before heavy browse/read operations start.

## Reconnect

asyncua's connection supervisor owns the connection; the gateway never tears down or rebuilds the session itself.

- The supervisor probes the server every `I3X_OPCUA_CONNECTION_MONITOR_INTERVAL_SECONDS` (asyncua watchdog interval, also the probe timeout). When the transport or the probe fails, it reconnects with exponential backoff up to `I3X_OPCUA_RECONNECT_MAX_DELAY_SECONDS` and never gives up. It first tries to re-activate the existing session and falls back to creating a new one.
- While a reconnect runs, new requests wait in asyncua's request gate for at most `I3X_OPCUA_RECONNECT_REQUEST_TIMEOUT_SECONDS` and then fail fast instead of piling up.
- Idempotent operations (read, browse, history) that fail because the connection or session was lost wait once for the supervisor to reach `CONNECTED` (same timeout) and are then retried once. Request timeouts are never retried. If the server reports a lost session (for example `BadSessionIdInvalid`) while the transport still looks healthy, the client hands this over to the supervisor immediately instead of waiting for the next probe.
- Writes and method calls are never retried: the server may already have applied them before the connection dropped.
- asyncua restores its subscriptions (TransferSubscriptions, or recreate with new server handles) before it reports the reconnect as finished.
- After a finished reconnect, operational limits and subscription capabilities are cleared and re-read lazily, with one shared OPC UA load for all concurrent callers. Namespace info and object types are marked stale: requests keep getting the previous values while one background load refreshes them, so a reconnect does not block API requests on a cold browse. Additional type definitions are reloaded only if the session had to be recreated.
- Registered reconnect listeners are then notified from a gateway task. The subscription service adopts the restored server handles of every native bin and re-admits only the monitored items asyncua could not restore; a bin whose OPC UA subscription is gone is dropped and its nodes are re-admitted.
- The connection state reported by the API mirrors asyncua's client state (`Connected`, `Reconnecting`, `Disconnected`).

Practical effect for users:

- Temporary OPC UA outages usually cause short delays, not full API restart requirements.
- Native subscriptions survive reconnects without being recreated by the gateway when the server still has them.
- Reconnect activity is logged by `asyncua.client.client` (`Supervisor detected connection issue`, `Reconnected successfully`) and by the gateway (`OPC UA connection lost`, `OPC UA reconnect finished`).

## Operational Limits And How They Are Used

The client reads server-side limits from OPC UA `ServerCapabilities.OperationLimits` and applies them in request planning.

Main limits used:

- `MaxNodesPerBrowse`
- `MaxNodesPerRead`

How this helps:

- Browse/read requests are split into batches that respect server capabilities.
- This reduces `BadTooManyOperations` errors and avoids oversized single requests.

If limits cannot be read, safe defaults are used and warnings are logged.

## Concurrency Model

Concurrency is intentionally controlled, not unlimited.

Key controls:

- `I3X_OPCUA_BROWSE_CONCURRENCY` (default 128 in settings) controls parallel worker count for fallback browse/history/member reads.
- For many operations, the client prefers batched reads first, then falls back to per-node workers behind a semaphore when required.

Why this matters:

- Too little concurrency increases latency.
- Too much concurrency can overload smaller OPC UA servers.
- The combination of batching + semaphore provides stable throughput across different server sizes.

## Subscription Optimization

The subscription service first attempts native OPC UA subscriptions, then falls back to polling if server limits would be exceeded.

Capability checks include:

- maximum monitored items per call/subscription,
- maximum subscriptions,
- maximum monitored items total,
- maximum subscriptions per session.

Decision logic:

- If limits allow: use native monitored items (push from OPC UA server).
- If limits do not allow (or native setup fails): use polling mode at configured interval.

This protects reliability in constrained environments while still enabling real-time behavior where possible.

## Metadata Caching

To reduce repeated expensive metadata calls:

- namespace info and object type information are cached with TTL,
- operational limits and subscription capabilities are cached,
- caches are invalidated on reconnect (namespace info and object types are served stale until the background refresh completes, even with a TTL of 0).

Relevant setting:

- `I3X_OPCUA_METADATA_CACHE_TTL_SECONDS` (default 300).

## Security And Authentication

The client supports:

- anonymous session,
- username/password session,
- OPC UA secure channel with certificates.

When security mode is not `None`, policy + certificate + key must be configured, otherwise startup fails fast with a clear configuration error.

## Sample Client Certificate (Encrypted Connection)

This repository includes a sample OPC UA client certificate/key pair for local encrypted connection tests:

- `certs/opcua-client-sample/client-cert.pem`
- `certs/opcua-client-sample/client-key.pem`

To generate or refresh these files with an OPC UA-compatible profile:

```bash
uv run python scripts/generate_opcua_client_cert.py
```

Use it with these environment variables:

```powershell
$env:I3X_OPCUA_SECURITY_MODE="SignAndEncrypt"
$env:I3X_OPCUA_SECURITY_POLICY="Basic256Sha256"
$env:I3X_OPCUA_CLIENT_CERT_PATH="./certs/opcua-client-sample/client-cert.pem"
$env:I3X_OPCUA_CLIENT_KEY_PATH="./certs/opcua-client-sample/client-key.pem"
# Optional but recommended when your server requires pinning/trust by certificate file
# $env:I3X_OPCUA_SERVER_CERT_PATH="./certs/opcua-server/server-cert.pem"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

Important:

- The sample certificate is for development/testing only.
- Many OPC UA servers require trusting the client certificate in their trust list before connection succeeds.
- For production, generate and manage your own certificates and private keys.

When generating your own OPC UA client certificate, include at least:

- SAN application URI compatible with your client application identity (for `asyncua` default, `urn:example.org:FreeOpcUa:opcua-asyncio`)
- SAN DNS entries for `localhost` and host name(s) used by deployment
- Key Usage: `digitalSignature`, `nonRepudiation/contentCommitment`, `keyEncipherment`, `dataEncipherment`
- EKU: `clientAuth` (and optionally `serverAuth` for broader compatibility)

If these fields are missing or too restrictive, servers may reject the secure channel with errors like `BadCertificateUseNotAllowed`.

## Sequence Diagram: OPC UA Client And Server Communication

```mermaid
sequenceDiagram
    autonumber
    participant API as i3X API (FastAPI)
    participant OPC as OPC UA Client
    participant UAS as OPC UA Server
    participant SUB as Subscription Service

    rect rgb(240, 248, 255)
        Note over API,OPC: Startup and initial connect
        API->>OPC: connect()
        OPC->>OPC: configure security/auth
        OPC->>UAS: OpenSecureChannel + CreateSession + ActivateSession
        UAS-->>OPC: Session active
        OPC->>UAS: Load type definitions
        OPC->>UAS: Read OperationLimits
        OPC-->>API: ready
    end

    rect rgb(245, 255, 245)
        Note over API,UAS: Read request path
        API->>OPC: read_values(node_ids)
        OPC->>OPC: batch by MaxNodesPerRead
        OPC->>UAS: Read (batched)
        UAS-->>OPC: DataValues
        OPC-->>API: values
    end

    rect rgb(255, 250, 240)
        Note over SUB,UAS: Subscription path
        SUB->>OPC: get_subscription_capabilities()
        OPC->>UAS: Read capability nodes
        UAS-->>OPC: limits
        alt Native subscription allowed
            SUB->>OPC: create_datachange_subscription()
            OPC->>UAS: CreateSubscription + CreateMonitoredItems
            UAS-->>OPC: DataChange notifications
            OPC-->>SUB: updates
        else Limits exceeded / native fails
            loop every interval
                SUB->>OPC: read_values(monitored_nodes)
                OPC->>UAS: Read
                UAS-->>OPC: current values
                OPC-->>SUB: normalized updates
            end
        end
    end

    rect rgb(255, 245, 245)
        Note over OPC,UAS: Connection drop and recovery (asyncua supervisor)
        UAS-xOPC: connection lost / health probe fails
        OPC->>OPC: state RECONNECTING, requests wait in request gate
        API->>OPC: read/browse fails with connection error
        OPC->>OPC: wait for CONNECTED (bounded by reconnect request timeout)
        OPC->>UAS: supervisor re-activates or recreates session (backoff)
        UAS-->>OPC: session restored
        OPC->>UAS: restore subscriptions (transfer or recreate)
        OPC->>OPC: mark metadata stale, clear limits caches
        OPC-->>SUB: reconnect listener callback
        SUB->>OPC: adopt restored handles, re-admit lost items
        OPC-->>API: retried read/browse result
    end
```

## Key Environment Variables

- `I3X_OPCUA_ENDPOINT`
- `I3X_OPCUA_USERNAME`
- `I3X_OPCUA_PASSWORD`
- `I3X_OPCUA_SECURITY_MODE`
- `I3X_OPCUA_SECURITY_POLICY`
- `I3X_OPCUA_CLIENT_CERT_PATH`
- `I3X_OPCUA_CLIENT_KEY_PATH`
- `I3X_OPCUA_CLIENT_KEY_PASSWORD`
- `I3X_OPCUA_SERVER_CERT_PATH`
- `I3X_OPCUA_BROWSE_CONCURRENCY`
- `I3X_OPCUA_METADATA_CACHE_TTL_SECONDS`
- `I3X_OPCUA_CONNECTION_MONITOR_INTERVAL_SECONDS`
- `I3X_OPCUA_RECONNECT_REQUEST_TIMEOUT_SECONDS`
- `I3X_OPCUA_RECONNECT_MAX_DELAY_SECONDS`
- `I3X_SUBSCRIPTION_INTERVAL_SECONDS`

## Operational Guidance (Simple)

- Start with default concurrency and observe latency/error logs.
- If OPC UA server is small/embedded: reduce browse concurrency.
- asyncua allows at most 100 concurrent requests per client, and the supervisor's health probe queues behind them. With a browse concurrency close to or above 100, heavy load can delay the probe beyond its timeout and cause unneeded reconnects; prefer 64 or less in that case.
- If metadata calls are frequent: increase metadata cache TTL.
- If native subscriptions are frequently downgraded to polling: check server subscription capability limits.
