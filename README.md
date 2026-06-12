# i3X API Gateway for OPC UA — Browse, Read & Stream Industrial Data

**FastAPI gateway implementing the i3X REST API over OPC UA — exposes OPC UA address spaces as standardized JSON endpoints for browsing, value reads, history, and Server-Sent Event subscriptions.**

[![Container Build](https://github.com/AndreasHeine/i3x2ua/actions/workflows/docker.yml/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/docker.yml) [![Quality Checks](https://github.com/AndreasHeine/i3x2ua/actions/workflows/quality.yml/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/quality.yml) [![Coverage](https://codecov.io/gh/AndreasHeine/i3x2ua/branch/master/graph/badge.svg)](https://codecov.io/gh/AndreasHeine/i3x2ua) [![Dependabot Updates](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/dependabot-updates/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/dependabot-updates) [![Dependency Graph](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/update-graph/badge.svg)](https://github.com/AndreasHeine/i3x2ua/actions/workflows/dependabot/update-graph)

![i3X Logo](img/i3X_logo.png)

## **Industrial Information Interoperability eXchange (i3X)**

The Industrial Information Interoperability Exchange (i3X™) is an open, common API initiative proposed to address a growing interoperability challenge in modern manufacturing architectures: **manufacturing data silo proliferation and API chaos**. As manufacturers adopt heterogeneous software stacks from multiple vendors, the industry risks repeating past fragmentation seen with protocols, platforms, and namespaces - this time at the API layer.

## License

### Personal use only. 
All rights reserved. See the [LICENSE](LICENSE) file for details.

### Commercial Licensing
Commercial use, distribution, or modification of this code is strictly prohibited under the standard license. If you want to use this project for commercial purposes, please purchase a commercial license.

However, you can automatically acquire a commercial license by sponsoring this project on GitHub. Commercial use is permitted as long as you maintain an active sponsorship at the **[$1 a month]** level or higher.

Get your commercial license instantly here:
* **Become a Sponsor:** [Sponsor me on GitHub](https://github.com/sponsors/AndreasHeine)

Alternatively, for custom licensing agreements or one-time purchases, please contact me directly:
* **Email:** info@andreas-heine.net

## Architecture Overview

```mermaid
flowchart LR
	Client[i3X Client / Consumer] -->|HTTP JSON| Nginx[nginx Reverse Proxy]
	Nginx -->|/v1| API[FastAPI App i3x_server.main]

	subgraph App[Application Core]
		API --> Router[Beta Router /v1]
		API -. optional /mcp .-> McpRouter[MCP Router /mcp]
		McpRouter -->|generated tools| McpTools[(MCP Tool Catalog)]
		McpRouter -->|tool calls| Router
		Router --> Deps[Dependency Layer]
		Deps --> ModelCache[(Model Cache)]
		Deps --> SubSvc[Subscription Service]
		Deps --> OpcClient[OPC UA Client]
	end

	subgraph Model[Model Layer]
		Builder[Model Builder] --> Mapper[Node Mapper]
		Mapper --> BuildResult[BuildResult Indexes]
		BuildResult --> ModelCache
	end

	OpcClient -->|browse tree + metadata| Builder
	OpcClient -->|read values/history| Router
	OpcClient <-->|OPC UA binary protocol| UaServer[(External OPC UA Server)]
	UaServer -->|address space browse + read + history + events| OpcClient
	Router -.->|write endpoints currently not implemented| OpcClient
	Router -->|object/value/history responses| Client

	subgraph Subs[Subscribe Flow]
		Router -->|create/register/sync/list/delete/stream| SubSvc
		SubSvc -->|native subscription when limits allow| OpcClient
		SubSvc -->|polling fallback| OpcClient
		SubSvc -->|SSE updates| Router
		Router -->|text/event-stream| Client
	end

	subgraph Startup[Lifecycle]
		StartupCfg[Settings env I3X_* + I3X_ENABLE_MCP] --> API
		API -->|startup| OpcClient
		API -->|optional preload| Builder
		API -->|shutdown| SubSvc
		API -->|shutdown| OpcClient
	end
```

```mermaid
sequenceDiagram
	participant C as Client
	participant R as /v1 Router
	participant D as Dependencies
	participant M as Model Cache/Builder
	participant O as OPC UA Client
	participant U as OPC UA Server
	participant S as Subscription Service

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

	Note over C,R: Write API paths exist in spec but return 501 in this beta implementation
```

## Quick Start

Requirements:

- Python 3.12
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

OpenAPI/Swagger:

- http://127.0.0.1:8000/openapi.json
- http://127.0.0.1:8000/docs

## API Surface

Active endpoints are exposed under `/v1` for:

- info and metadata (`/info`, `/namespaces`, `/objecttypes`, `/relationshiptypes`)
- object queries and values (`/objects`, `/objects/list`, `/objects/related`, `/objects/value`, `/objects/history`)
- subscriptions (`/subscriptions`, `/subscriptions/register`, `/subscriptions/unregister`, `/subscriptions/sync`, `/subscriptions/list`, `/subscriptions/delete`, `/subscriptions/stream`)

Optional MCP endpoints are exposed only when `I3X_ENABLE_MCP=1`:

- discovery and tool catalog (`/mcp`, `/mcp/tools`)
- JSON-RPC and tool call entry points (`/mcp`, `/mcp/call`)

## Documentation

- Beta contract and conformance: `docs/I3X_CONFORMANCE.md`
- OPC UA to i3X mapping profile: `docs/OPCUA_I3X_MAPPING_PROFILE.md`
- LM Studio / MCP bridge guide: `docs/LM_STUDIO_MCP_GUIDE.md`
- Roadmap and open items: `docs/TODO.md`
- Python coding requirements: `docs/python-coding-reguirements.md`
- API definition: `openapi.json`

## Docker

Run with compose:

```bash
docker compose up -d
```

The stack now starts the API behind an nginx reverse proxy. The app container stays internal, while nginx exposes HTTP and optional HTTPS.

Optional environment variables:

- `I3X_ENABLE_MCP=1` to enable MCP support; it is disabled by default
- `NGINX_HTTPS_ENABLED=1` to enable TLS termination
- `NGINX_SSL_CERTS_DIR=./certs` with `fullchain.pem` and `privkey.pem`
- `NGINX_BASIC_AUTH_ENABLED=1` with `NGINX_BASIC_AUTH_USER` and `NGINX_BASIC_AUTH_PASSWORD`
- `NGINX_SERVER_NAME` for the public host name

If you enable HTTPS, mount or place the certificate files in the configured cert directory before starting Compose.

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run pytest -q
uv run pytest -q --cov=i3x_server --cov-report=term-missing
```
