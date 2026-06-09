# Asynchronous i3X Beta REST-API for OPC UA.

![i3X Logo](img/i3X_logo.png)

## **Industrial Information Interoperability eXchange (i3X)**

The Industrial Information Interoperability Exchange (i3X™) is an open, common API initiative proposed to address a growing interoperability challenge in modern manufacturing architectures: **manufacturing data silo proliferation and API chaos**. As manufacturers adopt heterogeneous software stacks from multiple vendors, the industry risks repeating past fragmentation seen with protocols, platforms, and namespaces - this time at the API layer.

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

OpenAPI/Swagger:

- http://127.0.0.1:8000/openapi.json
- http://127.0.0.1:8000/docs

## API Surface

Active endpoints are exposed under `/v1` for:

- info and metadata (`/info`, `/namespaces`, `/objecttypes`, `/relationshiptypes`)
- object queries and values (`/objects`, `/objects/list`, `/objects/related`, `/objects/value`, `/objects/history`)
- subscriptions (`/subscriptions`, `/subscriptions/register`, `/subscriptions/unregister`, `/subscriptions/sync`, `/subscriptions/list`, `/subscriptions/delete`, `/subscriptions/stream`)

## Documentation

- Beta contract and conformance: `docs/I3X_CONFORMANCE.md`
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
```
