# Separate discovery and subscription services

This example keeps subscription state in one Uvicorn process while allowing the
discovery API to use multiple workers. NGINX routes all direct REST subscription
requests to the dedicated single-worker service; all other API paths go to the
discovery service.

This is a deployment workaround for process-local subscription state. It is not
a shared-state subscription manager or a highly available design.

## Layout

- `docker-compose.yml` starts the discovery service, subscription service,
  Jaeger, and NGINX.
- `nginx/entrypoint.sh` generates the NGINX configuration with separate
  discovery and subscription upstreams.
- `nginx/nginx.conf` and `nginx/Dockerfile` build the example NGINX image.

The app image is built from the repository root (`../../..` from this example
directory). Certificate and override mounts also refer to the repository root.

## Configure before starting

Edit `I3X_OPCUA_ENDPOINT` and, if needed, the OPC UA security settings and
credentials in `docker-compose.yml`. The checked-in example values mirror the
working local setup this workaround was extracted from; replace them with the
settings for your environment.

Provide the HTTPS certificate files expected at:

- `certs/https-sample/fullchain.pem`
- `certs/https-sample/privkey.pem`

The example enables NGINX Basic Authentication. Change
`NGINX_BASIC_AUTH_PASSWORD` to a strong local value before use. Do not expose
the sample credentials to an untrusted network.

## Start and stop

From the repository root:

```powershell
docker compose -f docs/examples/discovery-subscription-split/docker-compose.yml up --build -d
docker compose -f docs/examples/discovery-subscription-split/docker-compose.yml ps
docker compose -f docs/examples/discovery-subscription-split/docker-compose.yml down
```

The API is available at `https://127.0.0.1:8443/v1`. The default discovery
worker count is two. Override it without changing the file:

```powershell
$env:I3X2UA_DISCOVERY_WORKERS = "4"
docker compose -f docs/examples/discovery-subscription-split/docker-compose.yml up --build -d
```

The subscription service is intentionally configured with one Uvicorn worker.
Do not scale it horizontally while subscription state remains process-local.

## Routing

The NGINX configuration sends these paths to `i3x2ua-subscriptions`:

- `/v1/subscriptions`
- `/v1/subscriptions/` and all subpaths, including create, register,
  unregister, sync, list, delete, and SSE stream requests

`/v1/info` and every other path are sent to `i3x2ua-discovery`.

Use the direct REST API for subscription operations with this routing split.
MCP requests are processed inside the application serving the MCP endpoint;
NGINX cannot route individual MCP tools by their underlying REST path. MCP
subscription tools sent to discovery will use discovery's local state, not the
dedicated subscription service.

## Important limitation

Both services are full i3x2ua application instances and each creates its own
OPC UA client. This separates the in-memory subscription lifecycle from
discovery worker processes, but it does not share OPC UA connections or
subscription state across services. It increases OPC UA client connections and
does not provide subscription recovery after the dedicated service restarts.

Monitor OPC UA server capacity and compare load before
increasing discovery workers.
