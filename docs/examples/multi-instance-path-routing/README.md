# Multi-instance deployment with path-based routing

This example runs three independent i3x2ua instances. NGINX maps each public
path prefix to its corresponding instance and removes the prefix before
forwarding the request:

| Public path | Backend |
| --- | --- |
| `/i3x/instance1/...` | `i3x2ua-1` |
| `/i3x/instance2/...` | `i3x2ua-2` |
| `/i3x/instance3/...` | `i3x2ua-3` |

Each instance has its own OPC UA connection and its own in-memory application
state. Requests for an instance should continue to use that instance's path.

## Files

- `docker-compose.yml` starts the three API instances and NGINX.
- `.env.example` lists the environment variables used to configure the example.
- `nginx/Dockerfile` and `nginx/nginx.conf` provide the example-specific proxy
  configuration.

Monitoring services are not included; the old Compose example referred to
Prometheus and Grafana configuration files that are not present in the
repository.

## Configure

From the repository root, copy the example environment file and edit the OPC UA
endpoints and credentials:

```powershell
Copy-Item docs/examples/multi-instance-path-routing/.env.example docs/examples/multi-instance-path-routing/.env
```

The endpoints in `.env` are examples only. Replace them with addresses
reachable from the Compose network. If your OPC UA servers require security,
set the appropriate per-instance `I3X_OPCUA_SECURITY_MODE_N` and
`I3X_OPCUA_SECURITY_POLICY_N` values. Configure certificates and keys using
the application's supported environment variables and mount them into each
service as needed.

The NGINX configuration expects these certificate files in the mounted
certificate directory:

- `https-sample/fullchain.pem`
- `https-sample/privkey.pem`

For local use, the repository's `certs` directory is mounted by default. Replace
the sample certificates for any real deployment. The example redirects HTTP to
HTTPS.

## Start and stop

Run these commands from the repository root:

```powershell
docker compose --env-file docs/examples/multi-instance-path-routing/.env -f docs/examples/multi-instance-path-routing/docker-compose.yml up --build -d
docker compose --env-file docs/examples/multi-instance-path-routing/.env -f docs/examples/multi-instance-path-routing/docker-compose.yml ps
docker compose --env-file docs/examples/multi-instance-path-routing/.env -f docs/examples/multi-instance-path-routing/docker-compose.yml logs -f nginx
docker compose --env-file docs/examples/multi-instance-path-routing/.env -f docs/examples/multi-instance-path-routing/docker-compose.yml down
```

Check the instance-specific info routes:

```powershell
curl.exe -k https://localhost/i3x/instance1/v1/info
curl.exe -k https://localhost/i3x/instance2/v1/info
curl.exe -k https://localhost/i3x/instance3/v1/info
```

`-k` is appropriate only for testing with the local sample certificate. Use a
trusted certificate and normal certificate validation in production.

If ports 80 or 443 are already in use, set `NGINX_HTTP_PORT` and
`NGINX_HTTPS_PORT` in `.env`. The NGINX HTTP-to-HTTPS redirect assumes HTTPS is
reachable on the standard external port 443; if you change the HTTPS host port,
update the redirect in `nginx/nginx.conf` as well.

## Worker and subscription state

Each API instance uses one Uvicorn worker by default. Subscription state is
process-local, so keep one worker per instance when using subscription REST
operations. The three URL prefixes route to separate instances; subscriptions
created through one prefix are not visible through another.

This is a path-routing example, not a shared-state or high-availability
subscription manager. Each instance opens a separate OPC UA connection.
