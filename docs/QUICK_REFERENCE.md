# Production Deployment Quick Reference

## Files Included

| File | Purpose |
|------|---------|
| `PRODUCTION_HTTPS_GUIDE.md` | Comprehensive production deployment guide (START HERE) |
| `.env.production.example` | Example environment configuration |
| `docker-compose.multi-instance.yml` | Multi-instance HA setup with load balancing |
| `cert-manager.sh` | Certificate management utility script |
| `deploy.sh` | Automated deployment helper script |
| `QUICK_REFERENCE.md` | This quick reference guide |

---

## Quick Start (5 minutes)

### 1. Prepare Certificates
```bash
# Option A: Generate self-signed (development)
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/privkey.pem \
  -out certs/fullchain.pem -days 365 -nodes -subj "/CN=your-domain.com"

# Option B: Use Let's Encrypt
certbot certonly --standalone -d your-domain.com
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/
```

### 2. Configure Environment
```bash
# Copy example and edit
cp docs/.env.production.example .env

# Edit .env with your values:
# - NGINX_SERVER_NAME=your-domain.com
# - I3X_OPCUA_ENDPOINT=opc.tcp://your-server:4843
# - I3X_OPCUA_USERNAME=your-username
# - I3X_OPCUA_PASSWORD=your-password
```

### 3. Start Services
```bash
# Using provided helper script
bash docs/deploy.sh full

# Or manually
docker-compose up -d
docker-compose ps
curl https://your-domain.com/v1/info
```

---

## Common Tasks

### Check Certificate Status
```bash
bash docs/cert-manager.sh check --cert-dir ./certs
bash docs/cert-manager.sh info
bash docs/cert-manager.sh renewal
```

### Enable BasicAuth
```env
NGINX_BASIC_AUTH_ENABLED=1
NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=secure-password
```

### Multi-Instance Setup
```bash
# Use multi-instance compose file
docker-compose -f docs/docker-compose.multi-instance.yml up -d

# Update .env with per-instance OPC-UA settings
I3X_OPCUA_ENDPOINT_1=opc.tcp://server-1:4843
I3X_OPCUA_ENDPOINT_2=opc.tcp://server-2:4843
I3X_OPCUA_ENDPOINT_3=opc.tcp://server-3:4843
```

### Access Multiple Instances
```bash
# Instance 1
curl https://your-domain.com/i3x/instance1/v1/info

# Instance 2
curl https://your-domain.com/i3x/instance2/v1/info

# Instance 3
curl https://your-domain.com/i3x/instance3/v1/info
```

### View Logs
```bash
docker-compose logs -f nginx
docker-compose logs -f i3x2ua
docker-compose logs -f --tail=50  # Last 50 lines
```

### Monitor Services
```bash
docker-compose ps
docker stats
docker-compose top i3x2ua
```

### Restart Services
```bash
docker-compose restart                  # All services
docker-compose restart nginx            # Specific service
docker-compose restart i3x2ua-1         # Specific instance
```

### Backup Data
```bash
bash docs/deploy.sh backup
```

---

## Environment Variables Quick Reference

```env
# HTTPS/SSL
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.example.com
NGINX_SSL_CERTIFICATE=/etc/nginx/certs/fullchain.pem
NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/privkey.pem

# BasicAuth (optional)
NGINX_BASIC_AUTH_ENABLED=0
NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=password

# OPC-UA
I3X_OPCUA_ENDPOINT=opc.tcp://server:4843
I3X_OPCUA_USERNAME=user
I3X_OPCUA_PASSWORD=pass

# MCP Support (optional)
I3X_ENABLE_MCP=0

# Logging
I3X_LOG_LEVEL=INFO
```

---

## MCP Support (Model Context Protocol)

MCP is an optional feature that exposes the i3X API as tools for AI models and tool-calling clients.

### Enable MCP

To enable MCP tool support, set:

```env
I3X_ENABLE_MCP=1
```

If running without Docker, pass it to the startup command:

```bash
I3X_ENABLE_MCP=1 uv run uvicorn i3x_server.main:app --host 0.0.0.0 --port 8000
```

### MCP Scope and Capabilities

| Capability | Status | Notes |
|------------|--------|-------|
| Tool discovery and listing | ✅ Supported | All read/query/subscribe operations exposed. |
| Tool calling | ✅ Supported | Dispatches to REST API endpoints. |
| Prompt discovery and execution | ✅ Supported | `prompts/list`, `prompts/get`, `prompts/execute` via JSON-RPC and REST. |
| Resource listing and reading | ✅ Supported | `resources/list`, `resources/read` via JSON-RPC and REST. |
| Plant root listing | Custom REST | `/mcp/roots`; use object discovery for plant roots, not MCP filesystem `roots/list`. |
| JSON-RPC batch requests | Not supported | Protocol revision `2025-06-18` accepts one message per POST. |
| Update/write operations | REST-only optional | `PUT` routes are intentionally excluded from MCP tools. Use REST writes with `I3X_ENABLE_WRITES=1`. |
| Server-Sent Event streaming | ✅ Available | Via REST `/v1/subscriptions/stream` endpoint. |

### MCP Endpoints

- **Transport**: `POST /mcp` uses Streamable HTTP with JSON responses and revision `2025-06-18`. `GET /mcp` returns `405`; legacy SSE discovery is not supported. List-changed notifications are not supported; prompt overrides load at startup.
- **Tool listing**: `GET /mcp/tools` — Returns tool catalog in REST format.
- **Tool calling**: `POST /mcp/call` — REST-style tool invocation.
- **Prompt listing**: `GET /mcp/prompts` — Returns prompt metadata.
- **Prompt definition**: `GET /mcp/prompts/{name}` — Returns a full prompt definition.
- **Prompt execution**: `POST /mcp/prompts/execute` — Renders and executes prompt templates.
- **Resource listing**: `GET /mcp/resources` — Returns MCP resources.
- **Resource reading**: `POST /mcp/resources/read` — Reads a resource by URI.
- **Plant root listing**: `GET /mcp/roots` — Custom helper for plant roots, not MCP filesystem roots.
- **JSON-RPC**: `POST /mcp` — Standard JSON-RPC 2.0 interface.

Standard `prompts/list` advertises arguments; `prompts/get` accepts string
`arguments` and returns rendered MCP messages. REST prompt definitions and the
custom `prompts/execute` operation retain their previous formats.

Tools advertise annotations and an output schema for
`structuredContent: {"statusCode": ..., "body": ...}`. JSON text content remains
available. `isError` flags HTTP failures and per-item failures, preserving all
item results. Argument errors use `-32602`; missing resources use `-32002`.
Notifications return HTTP `202` without a body. Unsupported initialization
versions negotiate to `2025-06-18`; unsupported `MCP-Protocol-Version` headers
return HTTP `400`. Origin-bearing requests require a same-origin loopback URL or an
explicit `I3X_CORS_ALLOWED_ORIGINS` entry; `*` does not authorize MCP origins.

### Composite MCP Tools

These ordinary MCP tools complement, rather than replace, the atomic i3X tools.
They perform deterministic server-side workflows without internal LLM calls.
MCP arguments are **flat**: do not wrap them in `body`.

| Tool | Purpose | Required arguments |
|------|---------|--------------------|
| `discoverAssetContext` | Bounded graph, canonical metadata, deduplicated type schemas, optional VQT | `elementIds` |
| `findAssets` | Explicit candidates filtered by name substring, exact `typeElementId`, exact `namespaceUri`, optional scope | None |
| `getAssetSnapshot` | Selected signal VQT, identity/types/provenance, collection interval; never an atomic read or health verdict | `elementIds` |
| `startAssetMonitoring` | Resolve signals, create subscription, register, roll back incomplete registration | `elementIds`, `clientId` |
| `summarizeSignalHistory` | Bounded history pages with deterministic numeric statistics and sample quality coverage | `elementIds`, `startTime`, `endTime` |
| `diagnoseDataAvailability` | UA status/connection/capabilities, model/schema/read failures, quality and timestamp findings | `elementIds` |

Example discovery:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "discoverAssetContext",
    "arguments": {
      "elementIds": ["<discovered-element-id>"],
      "maxDepth": 2,
      "maxNodes": 100,
      "includeSchemas": true,
      "includeValues": false
    }
  }
}
```

Common budgets: `maxNodes` defaults to 100 (maximum 500), `maxDepth` to 2
(maximum 10), `maxEdges` to 500 (maximum 2000), and `timeoutSeconds` to 10
(maximum 60). Explicit seed IDs are deduplicated and must fit `maxNodes`.
Depth is **graph hops**: zero selects only the seeds, not unlimited recursion.
Composition is followed outward by default; `includeHierarchy` defaults to true,
and optional `includeGraph` defaults to false. Reverse edges and shared/multiple
parents are preserved in output, but are not automatically traversed. Cycles
are visited once. Edges may reference nodes outside the selected/truncated set.
No upstream value recursion is used: selected property nodes are explicitly
batched (50 IDs per read). Type schemas are resolved once per distinct type.

Results include `objects` keyed by `elementId`, `edges` with source, target,
relationship type and plane, `types` keyed by `typeElementId`, optional `values`,
`errors`, `coverage`, model revision and collection timestamps. Units and OPC UA
provenance are included only where canonical metadata/schemas provide them;
missing signal context is reported, not invented. A latest-known value may be
old without the connection being broken.
Current-value VQT preserves canonical i3X timestamps, whose source/server/fallback
origin is not exposed by that API; this limitation is listed in `missingInformation`.
Historical first/last samples additionally identify `timestampKind` as `source`,
`server`, or `unavailable` (with a null timestamp rather than an invented one).

**Partial results:** successful read-only collection can be incomplete.
Traversal, search, and history-page limits preserve `success: true` and MCP
`isError: false`, while reporting `coverage.truncated: true`; success does not
imply complete coverage. Per-item errors, timeouts, and output-budget failures
still set `success: false` and MCP `isError: true`. Monitoring rejects incomplete
target expansion before creating a subscription. Inspect
`coverage.limitReasons`; graph continuation is not supported, so narrow the
scope or increase a budget explicitly. A 1 MiB serialized-output ceiling returns
an explicit output-budget error rather than silently dropping fields. This ceiling
limits returned content; it does not limit the size of individual upstream values
or the initial model/type-catalog build. The deadline includes model resolution;
timed-out operations return errors rather than complete-looking results.

**Search:** `name` is a case-insensitive display-name substring; names are never
assumed unique. Search covers mapped objects, including signals. `maxScan`
defaults to 1000 (maximum 10000). Global searches return `coverage.nextOffset`
and a model revision; pass them as `offset` and `modelRevision` to continue.
Changed revisions reject continuation. `scopeElementIds` restricts search to a
bounded hierarchy/composition subgraph; scoped continuation is not supported.

**Monitoring:** defaults to composition-only expansion
(`includeHierarchy: false`); graph expansion is rejected. Truncated, missing,
or empty signal targets are rejected **before** creating a subscription.
Expanded properties are registered with i3X `maxDepth: 1`, preventing hidden
unlimited expansion. Any incomplete/failed registration rolls back the newly
created subscription. Inspect `registration`, `cleanup`, `errors`, and
`monitoringActive`: `null` means the handle may still be active after an uncertain
failure. The actual `subscriptionId` remains available for manual cleanup.
Cancellation/timeouts allow up to five seconds to recover an in-flight creation
handle and up to five seconds for compensating deletion; creation can still have
an explicitly unknown outcome if its result never arrives. `clientId` scopes
subscriptions but is **not authentication**.

```json
{
  "tool": "startAssetMonitoring",
  "arguments": {
    "elementIds": ["<discovered-asset-id>"],
    "clientId": "maintenance-assistant",
    "maxDepth": 2,
    "maxNodes": 100
  }
}
```

Send that example to `POST /mcp/call`; all composites are also callable through
standard `tools/call`. Direct helper POSTs under `/mcp/composites/` accept the same
flat JSON argument objects. Existing sync/remove/delete tools accept the returned
subscription handle. Composites never acknowledge/discard updates, start an
infinite polling loop, or turn REST SSE into MCP notifications.

**History:** times require explicit timezones, an ordered interval of at most
24 hours, and `maxPointsPerSignal` (default 100, maximum 1000).
`maxNodes * maxPointsPerSignal` must not exceed 10000. The OPC UA adapter requests
one bounded raw-history page per signal and releases continuation points. A page
or point cap is reported as truncated; statistics describe returned samples only.
Adapters without bounded-history support return an explicit unsupported error,
never an unbounded fallback. Empty history proves neither downtime nor historian
support. `qualityCounts` and `goodSampleFraction` describe sample coverage, not
time coverage. Numeric min/max/arithmetic mean use only Good, finite numeric
samples (excluding booleans), without interpolation or time weighting.

**Availability:** `staleAfterSeconds` defaults to 300; this is a caller-selected
age threshold, not a universal freshness guarantee. Findings distinguish poor
quality, null values, old/future timestamps and invalid timestamps. Raw UA evidence
and per-stage errors remain available. No failure-risk score, causal conclusion,
unit conversion or process-control action is inferred.

### With Authentication (BasicAuth + HTTPS)

When `NGINX_BASIC_AUTH_ENABLED=1`, MCP clients must include credentials in requests:

```bash
curl -u admin:password https://your-domain.com/mcp/tools
```

For JSON-RPC clients (e.g., LM Studio), configure the MCP server URL as:

```
https://admin:password@your-domain.com/mcp
```

---

## Troubleshooting Quick Fixes

### HTTPS Connection Refused
```bash
# Check if NGINX is running
docker-compose ps nginx

# Check certificate files
docker-compose exec nginx ls -la /etc/nginx/certs/

# Verify NGINX config
docker-compose exec nginx nginx -t

# Check if port is open
sudo netstat -tuln | grep 443
```

### Slow Responses
```bash
# Check OPC-UA connection
docker-compose logs i3x2ua | grep -i "connection\|timeout"

# Monitor container resources
docker stats

# Check load across instances (if multi-instance)
docker-compose logs i3x2ua-1 | wc -l
docker-compose logs i3x2ua-2 | wc -l
```

---

## Security Checklist

- [ ] Use strong passwords (16+ characters)
- [ ] Enable HTTPS in production
- [ ] Set read-only filesystem on containers
- [ ] Drop all Linux capabilities except needed ones
- [ ] Enable firewall rules (UFW/iptables)
- [ ] Restrict SSH access
- [ ] Set up certificate auto-renewal
- [ ] Monitor certificate expiration
- [ ] Enable rate limiting on NGINX
- [ ] Use BasicAuth or OAuth for API access
- [ ] Regular backups
- [ ] Monitor access logs
- [ ] Keep containers updated

---

## Monitoring & Alerts

### Check NGINX Health
```bash
curl -I https://your-domain.com/v1/info
```

### Container Health
```bash
docker-compose exec i3x2ua python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/info')"
```

### Disk Usage
```bash
docker system df
```

### Certificate Expiration
```bash
openssl x509 -in certs/fullchain.pem -noout -enddate
```

---

## Performance Tuning

### Increase Workers (Per Instance)
In `docker-compose.yml`:
```yaml
command:
  - --workers
  - "8"  # Increase from 4
```

### Enable Caching Headers
Add to NGINX config in `entrypoint.sh`:
```nginx
add_header Cache-Control "public, max-age=3600" always;
```

### Load Balancing Algorithm
In `.env`:
```env
NGINX_LOAD_BALANCING=least_conn  # Change from round_robin
```

---

## Reference Links

- Full Guide: [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)
- NGINX Docs: https://nginx.org/en/docs/
- Let's Encrypt: https://letsencrypt.org/docs/
- Docker Compose: https://docs.docker.com/compose/
- OAuth2-Proxy: https://github.com/oauth2-proxy/oauth2-proxy

---

## Getting Help

1. Check [Troubleshooting](#troubleshooting-quick-fixes) section
2. Review logs: `docker-compose logs service-name`
3. See full guide: [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)
4. Check Docker health: `docker-compose ps`

---

**Last Updated**: 2024
**Status**: Production Ready
