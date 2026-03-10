# i3x2ua

Ein asynchroner i3X-REST-Server mit OPC-UA-Backend.

Der Server stellt i3X-konforme Endpunkte bereit und nutzt einen OPC-UA-Server als Datenquelle.

## Features

- i3X Model API
  - GET /model
  - GET /model/{id}
  - GET /model/{id}/children
- i3X Data API
  - GET /data/{propertyId}
  - POST /data/query
- i3X Action API
  - POST /action/{actionId}/invoke
- OPC-UA Browsing und Mapping
  - Object -> Asset
  - Variable -> Property
  - Method -> Action
  - EventNotifier -> EventSource

## Voraussetzungen

- Python 3.10+
- uv
- Optional: laufender OPC-UA-Server

## Installation

1. In den Projektordner wechseln.
2. Abhaengigkeiten installieren:

   uv sync --extra dev

## Anwendung starten

Standardstart mit OPC-UA-Verbindung:

uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

OpenAPI und Swagger UI:

- http://127.0.0.1:8000/openapi.json
- http://127.0.0.1:8000/docs

### Start ohne OPC-UA-Server (lokaler Test)

PowerShell:

$env:I3X_SKIP_OPCUA_CONNECT="1"
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

cmd:

set I3X_SKIP_OPCUA_CONNECT=1
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000

## Konfiguration

Konfiguration erfolgt ueber Umgebungsvariablen mit Prefix I3X_.

Wichtige Variablen:

- I3X_OPCUA_ENDPOINT (Default: opc.tcp://localhost:4840)
- I3X_OPCUA_SECURITY_MODE (Default: None)
- I3X_MODEL_REFRESH_INTERVAL_SECONDS (Default: 60)
- I3X_LOG_LEVEL (Default: INFO)
- I3X_SKIP_OPCUA_CONNECT (nur fuer lokale Tests)

## Projektstruktur

i3x_server/
- main.py
- api/
  - model.py
  - data.py
  - action.py
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

## Entwicklung

Linting:

uv run ruff check .

Formatierung:

uv run ruff format .

Type-Checks:

uv run mypy .

Tests:

uv run pytest -q

## Hinweis

Wenn du den Server in Produktion betreibst, sollten TLS, SecurityMode, Authentifizierung und Rollenmodell gemaess Zielumgebung konfiguriert werden.
