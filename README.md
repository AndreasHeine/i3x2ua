# i3x2ua

Ein asynchroner i3X-REST-Server mit OPC-UA-Backend.

Der Server stellt i3X-konforme Endpunkte bereit und nutzt einen OPC-UA-Server als Datenquelle.

Die Dokumentation ist auf drei Dateien verteilt:

- `README.md`: Einstieg, Betrieb, Konfiguration, API-Ueberblick
- `I3X_CONFORMANCE.md`: API-Vertrag und Conformance-Einordnung
- `TODO.md`: Roadmap und offene Arbeitspakete

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
- Diagnose-/Kompatibilitaets-APIs
  - GET /namespaces
  - GET /objecttypes
  - GET /objects?includeMetadata=false
- OPC-UA Browsing und Mapping
  - Object -> Asset
  - Variable -> Property
  - Method -> Action
  - EventNotifier -> EventSource

## Status

Bereits umgesetzt:

- Kern-Endpoints fuer Model, Data und Action
- Startup-Preload fuer Model-Cache
- Batch-Reads in `/data/query` mit `MaxNodesPerRead`
- Batch-Browse mit `MaxNodesPerBrowse` fuer `browse_tree()` und `objecttypes`
- Namespace-Metadaten ueber OPC UA Namespaces-Object (`i=11715`)
- Erweiterte Browse-/Read-Logs mit Item-Counts

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
- I3X_OPCUA_BROWSE_CONCURRENCY (Default: 16)
- I3X_OPCUA_METADATA_CACHE_TTL_SECONDS (Default: 300)
- I3X_MODEL_REFRESH_INTERVAL_SECONDS (Default: 60)
- I3X_MODEL_PRELOAD_ON_STARTUP (Default: true)
- I3X_FAIL_STARTUP_ON_MODEL_PRELOAD_ERROR (Default: false)
- I3X_LOG_LEVEL (Default: INFO)
- I3X_SKIP_OPCUA_CONNECT (nur fuer lokale Tests)

## API Ueberblick

Kern:

- GET /model
- GET /model/{id}
- GET /model/{id}/children
- GET /data/{propertyId}
- POST /data/query
- POST /action/{actionId}/invoke

Diagnose/Kompatibilitaet:

- GET /namespaces
- GET /objecttypes
- GET /objects?includeMetadata=false

Hinweis: Die exakte Semantik und Conformance-Einordnung steht in `I3X_CONFORMANCE.md`.

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

## Weiterfuehrende Dokumente

- Conformance und API-Vertrag: `I3X_CONFORMANCE.md`
- Offene Punkte / Roadmap: `TODO.md`
