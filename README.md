# i3x2ua

An asynchronous i3X Beta REST server with an OPC UA backend.

The server exposes the endpoints described in the provided `openapi.json` under `/v1` and uses an OPC UA server as its data source.

Documentation is split across three files:

- `README.md`: getting started, operations, configuration, API overview
- `I3X_CONFORMANCE.md`: API contract and conformance status
- `TODO.md`: roadmap and open work items

## Features

- Beta API under `/v1`
  - GET /v1/info
  - GET /v1/namespaces
  - GET /v1/objecttypes
  - POST /v1/objecttypes/query
  - GET /v1/relationshiptypes
  - POST /v1/relationshiptypes/query
  - GET /v1/objects
  - POST /v1/objects/list
  - POST /v1/objects/related
  - POST /v1/objects/value
  - POST /v1/objects/history
  - POST /v1/subscriptions
  - POST /v1/subscriptions/register
  - POST /v1/subscriptions/unregister
  - POST /v1/subscriptions/sync
  - POST /v1/subscriptions/list
  - POST /v1/subscriptions/delete
  - POST /v1/subscriptions/stream
- Non-implemented Beta endpoints are intentionally exposed as structured 501 responses.
- OPC UA browsing and mapping
  - Object -> Asset
  - Variable -> Property
  - Method -> Action
  - EventNotifier -> EventSource

## Status

Already implemented:

- Beta compatibility layer under `/v1`
- `GET /v1/info`, `GET /v1/namespaces`, `GET /v1/objecttypes`, `POST /v1/objecttypes/query`
- `GET /v1/relationshiptypes`, `POST /v1/relationshiptypes/query`
- `GET /v1/objects`, `POST /v1/objects/list`, `POST /v1/objects/related`, `POST /v1/objects/value`
- `POST /v1/objects/history`
- `POST /v1/subscriptions`, `POST /v1/subscriptions/register`, `POST /v1/subscriptions/unregister`
- `POST /v1/subscriptions/sync`, `POST /v1/subscriptions/list`, `POST /v1/subscriptions/delete`, `POST /v1/subscriptions/stream`
- Structured 501 responses for non-implemented Beta endpoints
- OPC UA metadata access for namespaces and object types

## Requirements

- Python 3.12
- uv
- Optional: running OPC UA server

## Installation

1. Change into the project directory.
2. Install dependencies:

   uv sync --extra dev

## Start the API

Default startup with OPC UA connection:

uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

OpenAPI and Swagger UI:

- http://127.0.0.1:8000/openapi.json
- http://127.0.0.1:8000/docs

### Start without an OPC UA server (local testing)

PowerShell:

$env:I3X_SKIP_OPCUA_CONNECT="1"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

cmd:

set I3X_SKIP_OPCUA_CONNECT=1
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

## Configuration

Configuration is handled via environment variables with the `I3X_` prefix.

Important variables:

- I3X_OPCUA_ENDPOINT (Default: opc.tcp://localhost:4840)
- I3X_OPCUA_SECURITY_MODE (Default: None)
- I3X_OPCUA_BROWSE_CONCURRENCY (Default: 16)
- I3X_OPCUA_METADATA_CACHE_TTL_SECONDS (Default: 300)
- I3X_MODEL_REFRESH_INTERVAL_SECONDS (Default: 60)
- I3X_MODEL_PRELOAD_ON_STARTUP (Default: true)
- I3X_MODEL_PRELOAD_BLOCKING (Default: false)
- I3X_FAIL_STARTUP_ON_MODEL_PRELOAD_ERROR (Default: false)
- I3X_SUBSCRIPTION_INTERVAL_SECONDS (Default: 5)
- I3X_LOG_LEVEL (Default: INFO)
- I3X_SKIP_OPCUA_CONNECT (for local tests only)

## API Overview

The active API surface is only available under `/v1`.

- GET /v1/info
- GET /v1/namespaces
- GET /v1/objecttypes
- POST /v1/objecttypes/query
- GET /v1/relationshiptypes
- POST /v1/relationshiptypes/query
- GET /v1/objects
- POST /v1/objects/list
- POST /v1/objects/related
- POST /v1/objects/value
- POST /v1/objects/history
- POST /v1/subscriptions
- POST /v1/subscriptions/register
- POST /v1/subscriptions/unregister
- POST /v1/subscriptions/sync
- POST /v1/subscriptions/list
- POST /v1/subscriptions/delete
- POST /v1/subscriptions/stream

Note: Exact semantics and current implementation status are documented in `I3X_CONFORMANCE.md`.

## Project Structure

i3x_server/
- main.py
- api/
  - beta.py
- opcua/
  - client.py
- model/
  - builder.py
  - mapper.py
- schemas/
- config/

tests/
- test_api.py
- test_mapper.py

## Development

Linting:

uv run ruff check .

Formatting:

uv run ruff format .

Type checks:

uv run mypy .

Tests:

uv run pytest -q

## Note

If you run this server in production, configure TLS, SecurityMode, authentication, and authorization according to your target environment.

## Related Documents

- Beta contract and deviations: `I3X_CONFORMANCE.md`
- Open items / roadmap: `TODO.md`
