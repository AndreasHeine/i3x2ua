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

Build:

```bash
docker build -t i3x2ua:prod .
```

Run with compose:

```bash
docker compose -f docker-compose.prod.yml up -d
```

## Development

```bash
uv run ruff check .
uv run ruff format .
uv run mypy .
uv run pytest -q
```
