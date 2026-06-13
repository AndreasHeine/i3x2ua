# The i3X API Gateway for OPC UA — REST, MCP & Real-Time Streaming

**FastAPI gateway implementing the i3X API over OPC UA — exposes OPC UA address spaces as standardized REST and MCP endpoints with JSON responses, OpenAPI docs, and Server-Sent Event subscriptions. Built for teams that do not have deep OPC UA expertise.**

[![Container Build](https://github.com/AndreasHeine/i3x2ua/actions/workflows/docker.yml/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/docker.yml) [![Quality Checks](https://github.com/AndreasHeine/i3x2ua/actions/workflows/quality.yml/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/quality.yml) [![Coverage](https://codecov.io/gh/AndreasHeine/i3x2ua/branch/master/graph/badge.svg)](https://codecov.io/gh/AndreasHeine/i3x2ua) [![Dependabot Updates](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/dependabot-updates/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/dependabot-updates) [![Dependency Graph](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/update-graph/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/update-graph)

![i3X Logo](img/i3X_logo.png)

## **Industrial Information Interoperability eXchange (i3X)**

> The Industrial Information Interoperability Exchange (i3X™) is an open, common API initiative proposed to address a growing interoperability challenge in modern manufacturing architectures: **manufacturing data silo proliferation and API chaos**. As manufacturers adopt heterogeneous software stacks from multiple vendors, the industry risks repeating past fragmentation seen with protocols, platforms, and namespaces - this time at the API layer.

## License

### Open Source License (AGPL-3.0-or-later)
This project is licensed under the GNU Affero General Public License v3.0 or later.
See the [LICENSE](LICENSE) file for the full legal text.

### Commercial Licensing
**Sponsors at USD 1/month or higher are granted a commercial license while sponsorship remains active, as defined in the sponsor terms.**
**This enables commercial use without AGPL copyleft obligations during active sponsorship.**

[![Sponsor @AndreasHeine on GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-181717?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/AndreasHeine)

See [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) for full grant scope, limits, grace period, and verification details.

### Third-Party and Upstream Components
This repository includes third-party and upstream materials with their own licenses, including the bundled `i3X/` subtree.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.

### Contributions
By contributing to this repository, you agree that your contributions are provided under AGPL-3.0-or-later for the open-source distribution and may be included in commercially licensed distributions of this project.
See [CONTRIBUTING.md](CONTRIBUTING.md) for sign-off requirements and contribution workflow.

## Architecture Overview

```mermaid
flowchart LR
	subgraph Consumer[Consumer]
		Client[i3X Client]
		McpConsumer[AI Agent]
	end
	Client <--> |i3X 1.0| Nginx[nginx Reverse Proxy]
	McpConsumer <--> |optional /mcp JSON-RPC| Nginx
	Nginx <--> |all app routes| API[FastAPI App i3x_server.main]

	subgraph App[Application Core]
		API --> Router[V1 Router /v1]
		API --> UaRouter[UA Router /ua]
		API -. optional /mcp .-> McpRouter[MCP Router /mcp]
		McpRouter -->|generated tools| McpTools[(MCP Tool Catalog)]
		McpRouter -->|tool calls| Router
		Router --> Deps[Dependency Layer]
		Deps --> ModelCache[(Model Cache)]
		Deps --> SubSvc[i3X Subscription Service]
		Deps --> OpcClient[OPC UA Client]
		API --> Ui[Landing + viewers / / /docs /view /mcp-tools-viewer]

		subgraph Model[Model Layer]
			Builder[Model Builder] --> Mapper[Node Mapper]
			Mapper --> BuildResult[BuildResult Indexes]
			BuildResult --> ModelCache
		end
	end

	OpcClient -->|browse tree + metadata| Builder
	OpcClient -->|read values/history| Router
	OpcClient -->|connection/state/limits/metrics| UaRouter
	OpcClient <-->|OPC UA binary protocol| UaServer[(External OPC UA Server)]
	UaServer -->|address space browse + read + history + events| OpcClient
	Router -.->|write endpoints currently not implemented| OpcClient
	Router -->|object/value/history responses| Nginx
	UaRouter -->|operational state and limits| Nginx
	Ui -->|HTML pages and docs links| Nginx
	Router -->|create/register/sync/list/delete/stream| SubSvc
	SubSvc -->|native subscription when limits allow| OpcClient
	SubSvc -->|polling fallback| OpcClient
	SubSvc -->|SSE updates| Router
	Router -->|text/event-stream| Nginx
```

```mermaid
sequenceDiagram
	participant C as Client
	participant R as /v1 Router
	participant D as Dependencies
	participant M as Model Cache/Builder
	participant O as OPC UA Client
	participant U as OPC UA Server
	participant S as i3X Subscription Service

	Note over C,R: Read current values
	C->>R: POST /v1/objects/value
	R->>D: get_or_build_model()
	D->>M: cache hit? else build()
	M-->>D: BuildResult
	R->>O: read_values(node_ids)
	O->>U: Read/Browse requests
	U-->>O: values + metadata
	O-->>R: values
	R-->>C: bulk success/error envelope

	Note over C,R: Streaming subscription
	C->>R: POST /v1/subscriptions
	R->>S: create_subscription()
	S-->>R: subscriptionId
	R-->>C: success

	C->>R: POST /v1/subscriptions/register
	R->>S: register_items(elementIds,maxDepth)
	S->>O: native subscribe OR polling loop
	O->>U: MonitoredItems or periodic reads
	U-->>O: data changes
	O-->>S: normalized updates
	C->>R: POST /v1/subscriptions/stream
	R->>S: wait_for_updates(afterSequence)
	S-->>R: sequence updates
	R-->>C: SSE data events + keepalive

	Note over C,R: Write API paths exist in spec but can return 501 when optional update operations are not implemented
```

## Quick Start

Requirements:

- Python 3.10 to 3.12
- uv
- Optional: running OPC UA server

Install dependencies:

```bash
uv sync --extra dev
```

Start API:

```bash
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

Start without OPC UA server (PowerShell):

```powershell
$env:I3X_SKIP_OPCUA_CONNECT="1"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

Enable MCP support explicitly when you want the `/mcp` endpoints and MCP tool catalog to be available:

```powershell
$env:I3X_ENABLE_MCP="1"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

If you do not set `I3X_ENABLE_MCP`, the app starts without MCP support and `/mcp` returns `404`.

Use encrypted OPC UA with the included sample client certificate (development/testing):

```bash
uv run python scripts/generate_opcua_client_cert.py
```

```powershell
$env:I3X_OPCUA_SECURITY_MODE="SignAndEncrypt"
$env:I3X_OPCUA_SECURITY_POLICY="Basic256Sha256"
$env:I3X_OPCUA_CLIENT_CERT_PATH="./certs/opcua-client-sample/client-cert.pem"
$env:I3X_OPCUA_CLIENT_KEY_PATH="./certs/opcua-client-sample/client-key.pem"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

The OPC UA server may require manually trusting the sample client certificate before the secure session can be established.

If you create your own OPC UA client certificates, ensure they include OPC UA-compatible SAN and key usage fields (application URI, DNS names, clientAuth EKU, and signing/encipherment key usages) and match the server's exposed security mode/policy.

OpenAPI/Swagger:

- http://127.0.0.1:8000/openapi.json
- http://127.0.0.1:8000/docs

## API Surface

Active endpoints are exposed under `/v1` for:

- info and metadata (`/info`, `/namespaces`, `/objecttypes`, `/relationshiptypes`)
- filtered type queries (`/objecttypes/query`, `/relationshiptypes/query`)
- object queries and values (`/objects`, `/objects/list`, `/objects/related`, `/objects/value`, `/objects/history`)
- subscriptions (`/subscriptions`, `/subscriptions/register`, `/subscriptions/unregister`, `/subscriptions/sync`, `/subscriptions/list`, `/subscriptions/delete`, `/subscriptions/stream`)

Current scope emphasis: this implementation currently prioritizes read/query/subscribe operations.

Optional MCP endpoints are exposed only when `I3X_ENABLE_MCP=1`:

- discovery and tool catalog (`/mcp`, `/mcp/tools`)
- JSON-RPC and tool call entry points (`/mcp`, `/mcp/call`)

MCP scope emphasis: the MCP bridge is currently focused on tool calling (`initialize`, `tools/list`, `tools/call`) for the current implementation scope.

## Current Limitations

- write APIs currently return `501 Not Implemented` (`PUT /v1/objects/{element_id}/value`, `PUT /v1/objects/{element_id}/history`)
- `GET /v1/objects/{element_id}/history` currently returns `501 Not Implemented`
- server capabilities report updates as not supported while read/history/streaming are supported

## Documentation

- i3X upstream specification materials: `i3X/spec/README.md`
- i3X conformance tests: `i3X/conformance-tests/README.md`
- OPC UA to i3X mapping profile: `docs/OPCUA_I3X_MAPPING_PROFILE.md`
- OPC UA client behavior and optimization guide: `docs/OPCUA_CLIENT_DOCUMENTATION.md`
- LM Studio / MCP bridge guide (including capability matrix): `docs/LM_STUDIO_MCP_GUIDE.md`
- deployment guide index: `docs/PRODUCTION_DEPLOYMENT_INDEX.md`
- quick ops reference: `docs/QUICK_REFERENCE.md`
- Python coding requirements: `python-coding-reguirements.md`
- API definition: `openapi.json`
- contribution guide: `CONTRIBUTING.md`
- release notes: `CHANGELOG.md`

## Docker

Quickstart (Docker image):

```bash
docker run -d --name i3x2ua-master -p 8080:8000 -e I3X_ENABLE_MCP=1 -e I3X_OPCUA_ENDPOINT=opc.tcp://opcua.umati.app:4843 ghcr.io/andreasheine/i3x2ua:master
```

Multiline (bash):

```bash
docker run -d \
	--name i3x2ua-master \
	-p 8080:8000 \
	-e I3X_ENABLE_MCP=1 \
	-e I3X_OPCUA_ENDPOINT=opc.tcp://opcua.umati.app:4843 \
	ghcr.io/andreasheine/i3x2ua:master
```

Run with compose:

```bash
docker compose up -d
```

Build your own image with an explicit server version for `/v1/info`:

```bash
docker build --build-arg BUILD_VERSION=1.1.0 -t i3x2ua:1.1.0 .
```

If `BUILD_VERSION` is not set, the API falls back to `master`.

The stack now starts the API behind an nginx reverse proxy. The app container stays internal, while nginx exposes HTTP and optional HTTPS.

The default compose setup also enables container hardening (`read_only`, `tmpfs`, dropped Linux capabilities, `no-new-privileges`).

Optional environment variables:

- `I3X_ENABLE_MCP=1` to enable MCP support; it is disabled by default
- `I3X_OPCUA_CERTS_DIR=./certs` to mount OPC UA client/server certificate files into the app container (`/app/certs`)
- `NGINX_HTTPS_ENABLED=1` to enable TLS termination
- `NGINX_SSL_CERTS_DIR=./certs` with `fullchain.pem` and `privkey.pem`
- `NGINX_BASIC_AUTH_ENABLED=1` with `NGINX_BASIC_AUTH_USER` and `NGINX_BASIC_AUTH_PASSWORD`
- `NGINX_SERVER_NAME` for the public host name

If you enable HTTPS, mount or place the certificate files in the configured cert directory before starting Compose.

If your OPC UA server runs on the Docker host machine, set `I3X_OPCUA_ENDPOINT` to `opc.tcp://host.docker.internal:<port>` instead of `127.0.0.1`.

For local HTTPS testing with the nginx reverse proxy, generate sample certificates:

```bash
uv run python scripts/generate_https_dev_cert.py
```

Then use these environment values:

- `NGINX_HTTPS_ENABLED=1`
- `NGINX_SSL_CERTS_DIR=./certs`
- `NGINX_SSL_CERTIFICATE=/etc/nginx/certs/https-sample/fullchain.pem`
- `NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/https-sample/privkey.pem`

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run pytest -q
uv run pytest -q --cov=i3x_server --cov-report=term-missing
```

## Production Deployment and i3X Strict Compliance

This application implements the i3X API specification and is designed to run **behind a reverse proxy** that is responsible for:

- **TLS termination** — the app itself serves plain HTTP; all HTTPS is handled by nginx.
- **Authentication and authorization** — the app has no built-in auth layer; token validation, basic auth, or mTLS are enforced at the proxy level.

### Required reverse proxy responsibilities for strict i3X compliance

| Requirement | Implementation |
|---|---|
| TLS (HTTPS) for all client-facing traffic | nginx `ssl_certificate` + `ssl_certificate_key` |
| Client authentication (API key / OAuth / mTLS) | nginx `auth_request` or `satisfy any` directives |
| Rate limiting | nginx `limit_req_zone` |
| Access logging | nginx `access_log` |

See the [NGINX configuration reference](docs/NGINX_CONFIGURATION_REFERENCE.md) and [HTTPS guide](docs/PRODUCTION_HTTPS_GUIDE.md) for details.

### OPC UA → i3X Relationship Mapping

Relationship mapping rules and depth semantics are documented centrally in:

- `docs/OPCUA_I3X_MAPPING_PROFILE.md`

