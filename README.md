# i3x2ua

Ein asynchroner i3X-Beta-REST-Server mit OPC-UA-Backend.

Der Server stellt die in der gelieferten `openapi.json` beschriebenen Endpunkte unter `/v1` bereit und nutzt einen OPC-UA-Server als Datenquelle.

Die Dokumentation ist auf drei Dateien verteilt:

- `README.md`: Einstieg, Betrieb, Konfiguration, API-Ueberblick
- `I3X_CONFORMANCE.md`: API-Vertrag und Conformance-Einordnung
- `TODO.md`: Roadmap und offene Arbeitspakete

## Features

- Beta-API unter `/v1`
  - GET /v1/info
  - GET /v1/namespaces
  - GET /v1/objecttypes
  - POST /v1/objecttypes/query
  - GET /v1/objects
  - POST /v1/objects/list
  - POST /v1/objects/related
  - POST /v1/objects/value
- Nicht implementierte Beta-Endpunkte werden als strukturierte 501-Antworten sichtbar gemacht.
- OPC-UA Browsing und Mapping
  - Object -> Asset
  - Variable -> Property
  - Method -> Action
  - EventNotifier -> EventSource

## Status

Bereits umgesetzt:

- Beta-Kompatibilitaetsschicht unter `/v1`
- `GET /v1/info`, `GET /v1/namespaces`, `GET /v1/objecttypes`, `POST /v1/objecttypes/query`
- `GET /v1/objects`, `POST /v1/objects/list`, `POST /v1/objects/related`, `POST /v1/objects/value`
- Strukturierte 501-Antworten fuer nicht implementierte Beta-Endpunkte
- OPC-UA-Metadatenzugriff fuer Namespaces und ObjectTypes

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

Die aktive API liegt ausschliesslich unter `/v1`.

- GET /v1/info
- GET /v1/namespaces
- GET /v1/objecttypes
- POST /v1/objecttypes/query
- GET /v1/objects
- POST /v1/objects/list
- POST /v1/objects/related
- POST /v1/objects/value

Hinweis: Die exakte Semantik und der aktuelle Implementierungsstand stehen in `I3X_CONFORMANCE.md`.

## Projektstruktur

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

- Beta-Kontrakt und Abweichungen: `I3X_CONFORMANCE.md`
- OpenAPI-Abweichungen und Migrationsplan: `OPENAPI_MIGRATION.md`
- Offene Punkte / Roadmap: `TODO.md`
