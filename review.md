# Senior Technical Review — i3x2ua (i3X API Gateway for OPC UA)

Review vom 2026-11-12 auf Commit `c9d9525`. Methode: evidenzbasierte Read-only-Analyse der
gesamten Codebasis (88 Quelldateien ≈ 20.700 Zeilen `i3x_server`, 56 Testdateien ≈ 20.700 Zeilen
`tests`, CI/CD, Docker/nginx, Docs, Git-Historie). Alle Befunde sind **FACT** (verifiziert) oder
**ASSUMPTION** (abgeleitet, nicht gemessen) markiert.

Scope-Entscheidungen des Maintainers (gelten für den Fix-Plan `fix_plan.md`):
- `/objects` wird **nicht** paginiert (aus dem Roadmap-Scope ausgeschlossen).
- Default-Credentials (`docker-compose.yml` `admin`/`pw1`, committed Sample-Certs/Keys) bleiben
  zunächst (**deferred**).
- Keine Verletzung der i3X-Spec; die Conformance-Suite muss jeden PR mit „Full 1.0 Compliance"
  passieren.

Baseline-Gate-Zustand (FACT):

| Gate | Ergebnis |
|---|---|
| `pytest -q` | 1139 passed, 1 skipped (≈81–99 s) |
| Line-Coverage | 87,19 % (9514/10912 Statements) |
| Branch-Coverage | 0 % — nicht instrumentiert (`coverage.xml` hat `branches-valid="0"` überall) |
| `ruff check i3x_server tests` | All checks passed! (0 findings) |
| `ruff format --check` | 157 files already formatted |
| `mypy i3x_server` | Success: no issues found in 88 source files (strict) |
| import-linter | 5 Verträge, in CI geprüft |
| i3X-Conformance | verdict `Full 1.0 Compliance`, pass=56 fail=0 warn=2 skip=2 |

---

## 1. Executive Summary

| Area | Score | Assessment |
|---|---|---|
| Architecture | 7 | Echte hexagonale Absicht + in CI erzwungene import-linter-Verträge; aber leaky Ports (`Any`), `app.state` als untypisiertem Global-Bag, MCP-Paket außerhalb der Layer-Model, Encapsulation-Breach im Infra-Cluster |
| Code Quality | 7 | `ruff check` 0 findings, `ruff format` 157 files clean, `mypy --strict` Success (88 files), keine TODO/FIXME-Marker; aber God-Functions, 6 duplizierte Helper-Cluster, 540 `Any`, Docstring-Lücken (35/88 Dateien ohne Modul-Docstring) |
| Maintainability | 6 | 3 Module über der eigenen Regel „>900 lines → refactor required" (`objecttype_helpers.py` 1263, `subscriptions/service.py` 1057, `opcua/client.py` 999); Bus-Factor 1 (655 Commits ein Mensch, 126 Bots); Docs↔Code-Drift |
| Performance | 5 | Batched Reads gut designed; sequenzielle Bulk-Writes mit Pflicht-Read-Back, O(N)-Scans pro SSE-Wakeup, ein globaler Lock auf dem Fan-out-Pfad, keine Parallelität, kein Serverseitiges Paging auf `/objects` |
| Security | 4 | Kein App-Auth (by design, ehrlich dokumentiert), aber das ausgelieferte Compose enthält hardcodierte Credentials (`admin`/`pw1`), committed private keys, kein Rate-Limiting, unbegrenzte Subscriptionen, ein `eval()` auf Server-gestützten Daten, spoofbarer `base_url` |
| Testing | 7 | 87,19 % Line-Coverage (9514/10912), 1139 passed / 1 skipped, OpenAPI↔Runtime-Contract-Tests; aber Branch-Coverage = 0 % (nicht instrumentiert), riskantestes Modul (`client.py`, 999 Zeilen) aus Coverage ausgeschlossen, Conformance-Suite nicht in CI |
| Operations | 6 | Gehärtetes Image (non-root uid 10001, `read_only`, `cap_drop ALL`, tini, HEALTHCHECK, SBOM+Provenance), opt-in OTel+Jaeger; aber kein Prometheus, keine strukturierten Logs, keine liveness/readiness-Trennung, „push before scan" im Release, keine k8s-Artefakte |
| Technical Debt | 6 | 10 = minimal. Moderate Debt: strukturell (Encapsulation, `Any`-Ports), funktionale Perf-P0s, Ops-Gaps, dokumentierte vs. realisierte Sicherheit |

**Management Summary**

*Was gut ist:* Ein bewusst designedes hexagonales System, dessen Schichtgrenzen maschinell
erzwungen werden (`.importlinter`, 5 Verträge, in CI geprüft — `quality.yml:38-60`). Strikte
Typprüfung und Lint sind sauber durchgeladen, kein „TODO-Schmutz". Test-Infra ist contract-driven:
Runtime-Payloads werden gegen `/openapi.json` verifiziert (`tests/conftest.py:543-550`,
`tests/test_api.py:963,1058,1085`). Die Deployment-Härtung des Containers ist ungewöhnlich gut für
ein Projekt dieser Größe. Die Spezifikations-Compliance ist real gemessen: 56 pass / 0 fail /
2 warn / 2 skip (`i3x-conformance-report.json`).

*Größte Risiken:* (1) Vertrauensgrenzen liegen komplett auf einem optionalen Proxy — die App
selbst hat keinen Auth/Zulassungskontrollmechanismus; (2) der Gateway startet hart, wenn der
OPC-UA-Server down ist (`app_factory.py:342-343` ohne Try/Except), und `/health` ist statisch
(`api/health.py:11-22`), also ist „healthy" ≠ „OPC UA verbunden"; (3) Write-/Subscription-Pfade
sind RTT-gebunden und serialisiert — throughput skaliert nicht; (4) der asyncua-Adapter ist das
größte, risikoreichste Modul und wird von Coverage nicht gemessen; (5) Bus-Factor 1.

*Empfohlene nächste Aktionen:* Branch-Coverage aktivieren + CI-Gate auf Branch; Auth/Zulassung
(Rate-Limit, Subscription-Cap) als harte App-Schicht; Start degraded-mode oder
Supervisor-before-connect; Bulk-Writes parallelisieren; `deque` + Per-Stream-Cursor im
Subscription-Fan-out; docs-drift bereinigen (ADR 0001, architecture.md, SECURITY.md,
BENCHMARK_RESULTS.md).

---

## 2. Architecture Review

### Current State Assessment

Die Schichten sind real vorhanden und weitgehend in der richtigen Richtung gebaut. **FACT:**
`infrastructure/*` importiert nur `domain`, `schemas`, `model`, `config` plus
TYPE_CHECKING-Referenzen auf `client.py`; `domain` importiert kein `api`/`infrastructure`;
`application` importiert kein `api`/`bootstrap`. Die Verträge sind in CI gesperrt
(`quality.yml:38-60`).

Aber die Architektur ist **hexagonal nur an der Oberfläche**: die Ports sind die schwächsten
Stellen im System.

### Key Findings

- **A. Die Domain-Ports importieren die Concrete-Library (FACT).** `domain/ports/opcua.py:14` →
  `from asyncua import ua`; Port-Signaturen nutzen `ua.DataValue` (`:188`, `:207`, `:214`).
  `docs/architecture.md:142-143` verbietet explizit „domain importing … protocol route modules /
  direct network/runtime primitives". Das ist ein Selbstverstoß gegen die eigene
  Architektur-Regel; die Abstraktion kann die asyncua-Implementierung nie ersetzen.
- **B. Port-Typen sind `Any` (FACT).** `domain/ports/opcua.py:182,186,216,218,220,222,224,226,228`
  — `read_value(...) -> Any`, `create_datachange_subscription(..., handler: Any) -> Any`,
  `subscription: Any`. Damit ist die Grenze, an der Typsicherheit zählen sollte, die am
  wenigsten typisierte. `api/mcp/jsonrpc.py:252`:
  `McpMethodHandler = Callable[[Request, Any, Any], Awaitable[dict[str, Any]]]` — JSON-RPC
  `id`/`params` untypisiert in allen Handlers.
- **C. Encapsulation-Breach im infra-Cluster (FACT).** 335 Zugriffe auf `client._private…` in 6
  Sibling-Modulen, inkl. Mutation fremder Zustände: `values.py:85-86`
  (`client._runtime_metrics.read_calls += 1`), `values.py:429,432`
  (`client._history_update_supported = False/True`), Cache-Writes in `metadata.py:83,124` /
  `object_types.py:354,402`, und direkter asyncua-Bypass: `browse.py:298,311,335`
  (`client._client.uaclient.browse(params)`), `node_reading.py:446,455`, `values.py:611,620`. Das
  ist ein 10-Modul-Cluster mit geteiltem mutable Internal-Bag, nicht ein Adapter mit Seam.
  `client.py:368` reacht selbst in asyncua:
  `self._client.uaclient.pre_request_hook = self._client._wait_until_ready`.
- **D. `app.state` ist ein untypisierter Global-Bag (FACT).** 20 verschiedene Attribute, 49
  direkte Zugriffe + 6 string-keyed `getattr(app.state, "...")`-Fallbacks
  (`app_factory.py:259,273,289,467,472,478`, `api/mcp/jsonrpc.py:77,91`,
  `api/v1/objecttype_helpers.py:1129,1137`). mypy kann nichts davon prüfen. ADR 0001
  (`docs/adr/0001-app-state-strategy.md:37`) fordert „typed state access wrappers" — nie
  umgesetzt. ADR 0001:18 zitiert zudem `i3x_server/dependencies.py`, das nicht existiert.
- **E. Application-Layer hängt an FastAPI (FACT).** `application/dependencies.py:13` importiert
  `Depends, Request`; Services erhalten `request`-Objekte (`:65`, `:111`). Der Guard
  `model_query_no_fastapi` (`.importlinter:37-43`) deckt nur ein Modul — der Rest der
  Application-Schicht ist framework-gekoppelt.
- **F. Presentation → Composition Root (FACT).** Sieben Routen importieren `bootstrap.dependencies`
  (`subscription_routes.py:42`, `object_value_routes.py:56`, `object_routes.py:26`,
  `objecttype_routes.py:19`, `api/ua.py:13`, `api/dependencies.py:10`, `app_factory.py:38`). Der
  Layer-Vertrag `.importlinter:5-12` kennt `bootstrap` nicht, also ist das nicht verboten — aber
  Presentation kennt die Wiring-Schicht.
- **G. Das `mcp`-Paket liegt außerhalb des Schichtmodells (FACT).** `mcp/model_access.py:14` und
  `mcp/invocation.py:231` importieren `api.v1.objecttype_helpers._ObjectTypeContext` —
  Application-Level-Logik hängt an Presentation-Internals (`docs/architecture.md:131-137` erlaubt
  das nicht). `docs/architecture.md:40` diagrammt sogar `MCP --> REST` als normales Pattern:
  MCP-Tools gehen durch einen synthetischen ASGI-Self-Call (`mcp/invocation.py:696-760`) statt
  durch Service-Funktionen. Das ist ein bewusster Design-Entscheid, aber er kopiert
  Middleware-Kosten und macht die „application services"-Schicht für MCP faktisch wirkungslos.
- **H. Facade ohne Mehrwert (FACT).** `application/ports/opcua.py` ist ein reiner Re-Export von
  `domain/ports/opcua.py` (`:7-19`) — Zeremonie, die eine dritte Port-Schicht erzeugt, ohne eine
  neue Abstraktion.
- **I. Zirkulare Kopplung nur per TYPE_CHECKING gebrochen (FACT).** `browse.py:28`,
  `metadata.py:28`, `node_reading.py:26`, `object_types.py:36`, `values.py:33`, `recovery.py:25`,
  `monitoring.py:27` importieren `client` nur unter `TYPE_CHECKING`, weil `client.py:49`
  umgekehrt alle Module importiert. Runtime-zirkular ist das vermieden, aber die Kopplung ist
  bidirektional — genau das Muster, das Refactoring blockiert.
- **J. Legacy-Shims leben (FACT).** `api/v1/monolithic.py:102-107` ist ein Test-only-Shim mit
  `-> Any`; Docstring `:8-11` dokumentiert, dass die alte duplizierte Context-Maschinery
  „cache-poisoning hazard" war. Im `__pycache__` liegen .pyc-Relikte von gelöschten Modulen
  (`api/beta.py`, `api/data.py`, `api/model.py`, `api/namespaces.py`, `api/objects.py`,
  `api/objecttypes.py`, `api/action.py`, `api/mcp/composites.py`, `api/mcp/fastmcp_server.py`,
  `application/services/asset_context.py`, `mcp/analysis_resources.py`, `mcp_agent_guide.py`) —
  refactor churn ist nicht vollständig nachgezogen (Docs zitieren noch `api/mcp/monolithic.py`,
  `docs/architecture.md:66`).

### Risks

- Ports mit `Any` + asyncua-Typen machen einen zweiten Transport-Adapter (z. B. OPC UA 2.0 Stack,
  Test-Double mit echter Semantik) praktisch unmöglich ohne einen cross-layer Rewrite.
- Der infra-Cluster teilt mutable Internals: jeder Fix an `client.py` kann 6 Consumer still
  brechen; Tests greifen dieselben Privates (`tests/test_opcua_client_helpers.py:1115,1222`).
- `app.state` + getattr-Fallbacks: Name-Typos sind zur Laufzeit unsichtbar und werden zu stillen
  Degradationen.
- MCP→REST-In-Prozess-Dispatch: Änderungen an Route-Signaturen brechen Tools implizit; keine
  Typ-Prüfung durch `Any` (`invocation.py:147,390,409,426`).

### Growth-Answer

| Wachstum | Was schwierig wird |
|---|---|
| 2x (Namespaces/Knoten) | Model-Rebuild bleibt O(N) und sequenziell gebrowst (`browse.py:86-131`); Refresh-Spitzen verdoppeln Speicher (`model_cache.py:41-66`, Swap `app_factory.py:275-276`) |
| 5x | `/objects` ohne Server-Paging (`object_routes.py:103`) wird pro Request O(N)-Serialisierung + Pydantic-Validierung; Object-Type-Context-LRU ist 8 Slots (`objecttype_helpers.py:1025`) → Rebuild-Thrash |
| 10x | Ein Event-Loop + ein Read-Semaphore 64 (`client.py:150`) + ein globaler Subscription-Lock (`service.py:74`) saturieren; Monitored-Item-Anlage ist 1 Node/RPC bei 4-facher Admission (`monitoring.py:270,355`); kein horizontales Scaling möglich, weil Subscriptions/MCP-Sessions process-lokal sind (`docs/SCALING_GUIDE.md:719`) |

### Recommended Improvements

| Improvement | Priority | Effort |
|---|---|---|
| Echte Wert-Typen im Port: `OpcUaValue`/`DataValueDTO` statt `Any`, kein asyncua-Import im Domain-Port | Critical | L |
| Öffentliche, typisierte Seams im Adapter (`record_metric()`, `metadata_cache`, `browse_raw()`), Private-Zugriffe eliminieren | Critical | L |
| Typed State Container (Pydantic/dataclass) statt `app.state`, ohne string-getattr | High | M |
| `bootstrap`-Importe aus Routen durch Application-Dependencies ersetzen; `bootstrap` in den Layer-Vertrag aufnehmen | High | M |
| MCP-Tools auf Service-Funktionen umstellen, ASGI-Self-Call nur für Rest-Routen | High | L |
| Enum für Quality/Mode/Media-Type; magische Zahlen (500/50/128/1000) in Settings | Medium | S |
| Facade `application/ports/opcua.py` auflösen oder als echten Application-Port definieren | Low | S |

---

## 3. Python Engineering Assessment

### Current State Assessment

Tooling ist exzellent: **FACT** — `ruff check i3x_server tests` → `All checks passed!`
(0 findings), `ruff format --check` → `157 files already formatted`, `mypy i3x_server` →
`Success: no issues found in 88 source files` (strict, `pyproject.toml:76-82`). Nur 2
`type: ignore` und 3 `# noqa` in der gesamten Tree. Das ist ein außergewöhnlich sauberer
Gate-Zustand.

Die Semantic-Qualität ist jedoch deutlich schwächer als die Gate-Qualität.

### Key Findings

- **Typisierung (FACT).** 540 `Any`-Zeilen in `i3x_server`; 185 `dict[str, Any]`; 56 `-> Any`;
  17 `request: Any`/`app: Any` (`http_caching.py:36,44,50`, `recovery.py:65,86`,
  `objecttype_helpers.py:252,1050,1068,1087,1119,1128,1135,1146,1160,1223,1235`,
  `invocation.py:697`). Schwächste Module: `client.py` 42, `objecttype_schema.py` 41,
  `objecttype_annotations.py` 39 (16× `type[Any]`), `objecttype_helpers.py` 36,
  `invocation.py` 26. Ein bare `type: ignore` ohne Error-Code: `api/v1/common_helpers.py:89` —
  versteckt künftige Fehler auf dieser Zeile.
- **PEP 604 konsequent** (240 `str | None`, nur 1 `Optional[` und das nur in einem Kommentar).
  `slots=True` Dataclasses 32×, Pydantic dominiert die API (47 Models in `contracts.py`).
- **Fehlende moderne Idiome (FACT).** Keine `Enum`-Klassen (`Enum` nur für `isinstance` in
  `objecttype_schema.py:16,575`), kein `Final`, kein `typing.Self`, kein `match`, kein
  `asyncio.TaskGroup` (13× `asyncio.gather`), nur ein `asyncio.timeout`
  (`api/mcp/router.py:88`). Quality-Strings `"Good"/"Bad"/"GoodNoData"/"Uncertain"` erscheinen
  ~40× über 7 Dateien — ohne Enum sind Tippfehler nicht erfassbar.
- **Exceptions (FACT).** 117 `except Exception`, nur 18 `raise … from`, 3× `except BaseException`
  (`model_cache.py:52`, `browse.py:360`, `values.py:273` — fangen auch `CancelledError`),
  13 `contextlib.suppress`, 0 bare `except:`. Silent Swallows ohne Logging:
  `domain/utils.py:240-241` (`except Exception: pass`), `schemas/opcua_value.py:82-85`
  (`except Exception: continue` → liefert `"Bad"` für jeden Fehler: ein Read-Fehler wird als
  Datenwert berichtet), `api/v1/common_helpers.py:59-62` (`except Exception: return []` →
  Namespaces werden leer), `client.py:948-949,955-956`. 12 Re-Raises ohne `from exc` in
  `values.py:111,198,249,257,277,367,387,431,483,550,578,634,682`. `api/mcp/router.py:227-231`
  macht jeden Fehler zu JSON-RPC `-32603` ohne Error-Taxonomie.
- **Komplexität (FACT).** God-Functions: `model/builder.py:36-567` (531 Zeilen, 5
  Nesting-Ebenen), `app_factory.py:493-807` (315), `app_factory.py:296-492` (`lifespan`, 197),
  `browse.py:155-339` (184), `invocation.py:511-693` (182), `objecttype_helpers.py:863-1017`
  (155), `metadata.py:175-332` (157), `object_value_helpers.py:190-322` (132),
  `subscription_routes.py:167-293` (~127), `service.py:633-757` (~124). Zwei nahezu identische
  Classification-Loops: `objecttype_helpers.py:921-939` vs `:957-990`.
- **Duplizierung (FACT).** `_runtime_settings()` ×4 (`app_factory.py:105`, `monolithic.py:68`,
  `objecttype_helpers.py:179`, `object_value_helpers.py:56`); `_stream_debug_enabled()` ×2
  (`monolithic.py:72`, `state.py:39`); `_load_otel_tracing()` ×2 mit unterschiedlichen
  Return-Typen (`invocation.py:54` → `tuple[Any,Any,Any,Any]`, `prompts/api.py:13` →
  `tuple[Any,Any,Any]`); `_to_namespace()` ×2, `_relationship_type_items()` ×2
  (`model_query.py:112,149` vs `object_helpers.py:58,165`);
  `_normalize_quality`/`_vqt_from_data_value`/`_normalize_timestamp` ×2
  (`object_value.py:370,437,450,396` vs `object_value_helpers.py:530,439,563,557`);
  `_to_json_safe_value()` drei Varianten (`object_value.py:403`, `common_helpers.py:169`,
  `ua.py:72`) + `_to_json_compatible` (`object_types.py:57`);
  `_extract_attribute_value`/`_coerce_node_class` dupliziert (`client.py:652,655` vs
  `node_reading.py:72,81`); `ua.BrowseDescription` Blocks dupliziert (`browse.py:168-177` vs
  `:231-240`); `Namespace`/`ServerCapabilities`/`ServerInfo` als Pydantic-Models **und** als
  handgeschriebene `model_dump()`-Twins (`contracts.py:248,169,175` vs `model_query.py:61,72,88`).
- **Dead Code / API-Leaks (FACT).** `infrastructure/opcua/__init__.py:3` und
  `subscriptions/__init__.py:3` sind `import *`-Re-Exports, die kein Consumer nutzt (alle
  importieren Submodule). `api/v1/object_helpers.py:17-47` re-exportiert sechs `_`-private
  Domain-Helpers über `__all__` → Privates werden Cross-Layer-API. `domain/utils.py:208`
  (`namespace_infos_by_uri`) hat nur einen Test-Caller. `ObjectValueService` wird von keiner
  Route benutzt (`application/dependencies.py:50-65`, nur Tests).
- **Docstrings (FACT).** 53/88 Dateien mit Modul-Docstring, 35 ohne; 9 Dateien öffnen mit leerer
  Summary-Zeile (`"""` allein auf Zeile 1) — PEP-257-Verstoß (`domain/__init__.py`,
  `domain/utils.py`, `application/__init__.py`, `application/dependencies.py`,
  `application/services/__init__.py`, `model_query.py`, `object_value.py`, `subscription.py`,
  `api/mcp/__init__.py`). Function-Coverage ist dünn: `object_helpers.py` 28 defs / 1 Docstring
  (~4 %), `objecttype_helpers.py` 41 defs / 4 (~10 %).
- **Konflikt mit eigenen Coding-Regeln (FACT).** `python-coding-requirements.md:11` verlangt Type
  Annotations überall — erfüllt formal, aber 56 `-> Any` machen die Regel inhaltlich zahnlos.
  Zeile 12 sagt „Do not add comments" — der Code hat hunderte erklärende Comments (was gut ist;
  die Regel ist unrealistisch).

### Risks

`Any`-Ports + `app.state` bedeuten: mypy strict ist ein **starker Gate für 88 Dateien**, aber
schützt die Systemgrenzen nicht — die gefährlichsten Stellen sind die, die der Checker nicht prüft.

### Recommended Improvements

| Issue | Priority | Effort |
|---|---|---|
| Port-Werttypen statt `Any`; `Request`/`FastAPI` typisieren (17 Stellen) | Critical | M |
| Silent Swallows entfernen (`domain/utils.py:240`, `opcua_value.py:82`, `common_helpers.py:59`, `client.py:948,955`); Cause-Chaining in `values.py` | Critical | M |
| Quality/Mode/Media-Type als Enums; `"127.0.0.1"`/500/50/128/1000 in Settings | High | S |
| Duplizierte Helper zu einem Modul konsolidieren (6 Cluster) | High | M |
| `builder.build` (531 Zeilen) und `lifespan` (197) in Phasen zerlegen | High | L |
| Private Re-Exports entfernen; `import *` löschen; Docstring-Baseline für public APIs | Medium | M |
| Bare `type: ignore` (`common_helpers.py:89`) mit Error-Code ersetzen | Low | S |

---

## 4. Maintainability Review

### Current State Assessment

Das Projekt hat eine eigene File-Size-Guidance (`docs/architecture.md:181-191`: preferred ≤400,
warning >600, „Refactor required >900 unless justified"). **FACT:** Die Realität verletzt sie:
>900 Zeilen — `objecttype_helpers.py` 1263, `subscriptions/service.py` 1057, `opcua/client.py`
999; >600 — `objecttype_annotations.py` 815, `objecttype_schema.py` 805, `app_factory.py` 802,
`object_types.py` 772, `invocation.py` 768, `builder.py` 711, `contracts.py` 701, `values.py`
690, `object_value_helpers.py` 672. Der geforderte „follow-up issue or refactor note" existiert
nicht — `fixme.md` dokumentiert nur ein anderes Thema (InverseName) und sagt in Zeile 1 selbst
„nicht committen", ist aber tracked.

### Key Findings (mit Module-Scores, 10 = gut)

| Module | Zeilen | Maintainability | Grund |
|---|---|---|---|
| `api/v1/objecttype_helpers.py` | 1263 | 4 | 41 defs, 36 `Any`, God-Funktion 155 Zeilen, 8-Slot-Cache, 10× `request: Any`, zwei Duplikat-Loops |
| `infrastructure/subscriptions/service.py` | 1057 | 4 | Ein globaler Lock, 12+ Responsibilities in einer Klasse, `updates` als list mit `pop(0)`, TTL-Sweep, Fan-out, Bin-Admission |
| `infrastructure/opcua/client.py` | 999 | 3 | 42 `Any`, 999 Zeilen aus Coverage ausgeschlossen, 6 Sibling-Module greifen Privates |
| `bootstrap/app_factory.py` | 802 | 5 | `create_app` 315 + `lifespan` 197 Zeilen, 20 `app.state`-Assignments, 8 nested defs, gemischte Settings-Quellen |
| `schemas/objecttype_annotations.py` | 815 | 5 | 16× `type[Any]`, **ein `eval()`** (`:288`) auf Server-gestützten Annotationen |
| `schemas/objecttype_schema.py` | 805 | 6 | 41 `Any`, lange Schema-Literals |
| `mcp/invocation.py` | 768 | 5 | 182-Zeilen-Funktion, synthetischer ASGI-Scope, Module-Globals `_mcp_tool_calls`/`_mcp_tool_duration` (`:76-77`), Paginatoren `-> Any` |
| `model/builder.py` | 711 | 5 | 531-Zeilen-Methode, 5 Nesting-Ebenen |
| `api/v1/object_value_helpers.py` | 672 | 5 | 3× `zip(strict=False)` (`:239,251,260`), duplizierte Value-Helper |
| `infrastructure/opcua/values.py` | 690 | 4 | 17 `except Exception`, 12 Re-Raises ohne `from`, ~99 Private-Zugriffe |
| `infrastructure/opcua/node_reading.py` | 595 | 5 | 67 Private-Zugriffe, asyncua-Bypass |
| `api/v1/contracts.py` | 701 | 7 | 47 Pydantic-Models, konsistent; `le`-Caps fehlen bei `maxDepth` (`:351-360,380-384,583-590`) |
| `application/services/*` | 1745 | 6 | `SubscriptionAppService` 475 / `ObjectValueService` 451 Zeilen; Service wird nicht von Routen genutzt |
| `domain/*` | 491 | 8 | Klein, klar, aber Port importiert asyncua |
| `config/settings.py` | 136 | 7 | Gute Validatoren; `get_settings()` scannt Env + 2 Syscalls pro Call (`:122-128`) |
| `api/mcp/*` | 1440 | 7 | Klar gesplittet; Origin-Guard nur auf Transport-Routen |
| `mcp/model_tools.py` + `model_access.py` | 1093 | 6 | Schwere per-Revision-Indizes (`model_access.py:96-124`) |
| `prompts/*` | 197 | 8 | Klein, 98 % Coverage |

**Bus-Factor (FACT):** `git shortlog -sn HEAD` → 655 Commits „Andreas Heine", 110 renovate[bot],
14 dependabot[bot], 2 copilot-swe-agent[bot]. Ein menschlicher Maintainer. Onboarding-Difficulty
hoch: die Semantik sitzt in langen Funktionen und in Docs, nicht in typisierten Grenzen.

**Versteckte Refactor-Brittelei (FACT).** Tests greifen Implementations-Internals:
`app.state.opcua_client._browse_roots` (`tests/test_app_factory_helpers.py:264`),
`service._bins`/`_node_monitors` (`tests/test_subscription_service.py:769,1414,1478`),
`model._model_access_index_cache` (`tests/test_mcp_model_access.py:379,531`),
`cast(Any, client)._limits_cache` (`tests/test_opcua_client_helpers.py:1115,1222`). Jeder Umbau
bricht Tests, die nichts öffentliches prüfen.

**Repo-Hygiene (FACT, `git ls-files`).** Getrackt, obwohl nicht Quellcode:
`tests/features/.tmp_pytest_out.txt`, `tests/features/pytest_report.xml`, `fixme.md` (Working
Notes), `certs/https-sample/privkey.pem`, `certs/opcua-client-sample/client-key.pem`, duplizierte
Assets `img/` und `static/`. `REPORT.md` (neuere Conformance-Aussage vom 2026-11-08) ist nicht
committet; das getrackte `i3x-conformance-report.json` stammt vom 2026-10-21.

### Risks

- Jede Änderung an `client.py` hat 6 unbekannte Consumers.
- Zwei Settings-Quellen gemischt im selben Modul: `app_factory.py` benutzt `settings.`
  (`:150,197,245,343,443`) **und** `get_settings()` (`:300`) — Env-Änderungen nach Import wirken
  nur teilweise.
- Docs↔Code drift: ADR 0001:18 (`dependencies.py` existiert nicht), architecture.md:66
  (`api/mcp/monolithic.py` existiert nicht), BENCHMARK_RESULTS.md:331-344 behauptet
  `sampling_interval=50.0`, real `client.py:848-850` hat keinen Sampling-Parameter.

### Recommended Improvements

| Issue | Priority | Effort |
|---|---|---|
| Die drei >900-Zeilen-Module entlang eigener Guidance splitten | High | L |
| Public Seams statt Private-Test-Zugriffe | High | M |
| Settings-Zugriff auf eine Quelle vereinheitlichen | Medium | S |
| Docs drift bereinigen; `fixme.md`/Test-Artefakte aus Tracking nehmen | Medium | S |
| Enum + Konstanten für wiederholte Literals (~40 Quality-Strings) | Medium | S |

---

## 5. Performance Review

### Current State Assessment

**FACT:** Single process, single event loop, single OPC UA session, keine Thread/Process-Parallelität
im Server (0 `ThreadPoolExecutor`/`multiprocessing`/`trio`); einziger Thread-Offload ist
`model/builder.py:558` (`asyncio.to_thread`). Alle Handlers sind `async def`, also laufen
Pydantic-Serialisierung, sha256, jsonschema-Validierung und GZip auf dem Loop. Batched Read-Pfade
sind gut designed (`values.py:151-161`, `object_value_routes.py:259-266,344-352`, Polling-Tick
`monitoring.py:479`).

### Issue Table

| Issue | Impact | Priority | Recommendation |
|---|---|---|---|
| Bulk-Writes sequenziell + Pflicht-Read-Back (`object_value_helpers.py:298-305`, `values.py:479,486`) | 500 Items ≈ 1000 serialisierte RTTs; Write-Throughput ist RTT-gebunden | P0 | Semaphore-parallele Writes; Read-Back optional machen oder batched Write |
| `wait_for_updates` scannt die ganze Update-Liste pro Wake (`service.py:651,740,756`) | Bis 10k Iterationen pro Stream pro Wake | P0 | Per-Stream-Cursor + Slice ab Index |
| `updates.pop(0)` auf einer list (`service.py:993-994`) | O(n) Memmove pro Update nach Sättigung + 10k gehaltene Objekte/Subscription | P0 | `collections.deque(maxlen=…)` |
| Monitored Items 1 Node/RPC bei 4-facher Admission (`monitoring.py:270,355`; `settings.py:43`) | Register-Latenz linear in Node-Zahl | P0 | Batched `CreateMonitoredItems` per Bin (asyncua nimmt Listen) |
| Kein Cap auf `maxDepth`/Subtree-Monitoring (`contracts.py:583-590`, `mapping.py:61-91` kein Node-Cap) | Anonym möglicher Upstream-DoS | P0 | `le` + `composition_max_nodes` in `mapping.py` |
| `/objects` liefert das ganze Modell ohne Paging (`object_routes.py:103`) | O(N) Objekte + `response_model`-Validierung pro Request | P0 | Server-Paging/Cursor — **vom Maintainer aus dem Scope ausgeschlossen** |
| Ein globaler Lock auf Fan-out/register/sync/TTL (`service.py:74,143,644,821,224`) | Serialisiert alle Publish-Ticks | P1 | Per-Subscription-Locks |
| GZip `minimum_size=1`, inline auf dem Loop (`app_factory.py:559`) | CPU pro Response (Level 9; erst ≥128 KiB in Thread) | P1 | `minimum_size` erhöhen, Level 5 |
| 2 synchrone Log-Zeilen/Request im App-Middleware (`app_factory.py:746-752`), Compose hat `--no-access-log` (`docker-compose.yml:27`), `run.py:54-73` nicht | Blocking stderr I/O pro Request | P1 | `--no-access-log` auch in `run.py`, Queue-Handler |
| `get_settings()` Env-Scan + `os.stat` + `getcwd` pro Call (`settings.py:122-128`), aufgerufen pro SSE-Event (`subscription_routes.py:212,221,233,267`) | Syscalls pro Event | P1 | Toggles einmal beim Startup auflösen |
| Write-Validator: sha256(repr(schema)) pro Item, FIFO 64 (`write_validation.py:20-38`); Type-Linearscan pro Item (`:87`) | O(types) pro Write-Item, Validator-Thrash | P1 | Index pro Context, Key = type_id + Model-Revision |
| `_reverse_parent_index_cache` Fingerprint = nur zwei dict-Längen (`object_helpers.py:233-236`) | **Stale Parent-Index nach Rebuild gleicher Größe → falsche Hierarchien** | P1 (Korrectheit) | `BuildResult.revision` als Fingerprint |
| Model-Rebuild hält alte + neue `BuildResult` gleichzeitig (`model_cache.py:41-66`) | 2× Model-Speak während jedem Refresh | P1 | Dokumentieren/Intervall erhöhen oder inkrementeller Refresh |
| `BuildResult` mit 23 Node-Keyed Maps (`schemas/state.py:39-82`), `parent_by_id`/`hierarchy_parent_by_id`/`parent_id_by_id` überlappen | Per-Node Memory groß | P1 | Redundante Maps trimmen |
| History-Reads ohne `NumValuesPerNode` (`values.py:521`, Continuation-Drain `:535-575`) | Breite Zeitbereiche → Millionen Values im RAM | P1 | `maxValues`/413 |
| MCP-Tools durch synthetischen ASGI-Self-Call (`invocation.py:696-760`, `readContainerValues` = 5 sequenzielle interne Calls `:306-314`) | Encode → Middleware → Decode → Re-Pagination pro Tool-Call | P1 | Mehr Cached-Tools, echte Service-Calls |
| SSE-Payload 3× gemappt (`subscription_routes.py:251-255,256-265,266,280`) | 3 Python-Passes pro Event | P1 | Einmal bauen, direkt `json.dumps` |
| `response_model=` auf 21 Routen | Pydantic-Validierung + Serialisierung doublet | P1 | Weglassen wenn Handler typisiert zurückgibt |
| 304-Check erst nach Discovery (`object_routes.py:69,70`; `objecttype_routes.py:51,54`) | Conditional GET mit cold Cache zahlt volle Discovery | P1 | Revision aus Cache Dependency vor Discovery |
| Nur Load-Shedding = uvicorn `--limit-concurrency 200`; SSE/MCP-Streams zählen mit (`run.py:69-71`) | Viele Streams shedden normale Requests mit 503 | P1 | Separater Stream-Admission-Cap |
| Bin-Caps kommen aus Server-Capability-Vars, Fallback 1000 nur wenn non-None (`metadata.py:117-121`, `monitoring.py:150-155,511`) | Server ohne diese Vars → unbegrenzte Bins | P1 | Process-lokale Caps als Floor |
| `zip(strict=False)` bei Bulk-Mapping (`object_value_helpers.py:239,251,260`) | Stille Fehlpaarung statt Error | P2 | `strict=True` |
| `_dropped_node_datachanges` nur beim Fan-out gelöscht (`service.py:92,146`) | Leak pro Node ohne Subscriber | P2 | Counter beim Monitor-Release löschen |
| MCP-Sessions nur bei Registration gepunt (`jsonrpc.py:109-128`) | Idle-Sessions bleiben | P2 | Cleanup-Loop |

### Async/Concurrency Quality

Positiv (FACT): Queue ist bounded (`service.py:87-88`), Overflows werden als Sequence-Gap
markiert (`:111-125`) statt still verworfen; Reconnect-Listener symmetrisch entfernt
(`client.py:525-530` / `service.py:306-311`); Shutdown-Reihenfolge korrekt
(`app_factory.py:467-490`); Signal-Handler chained und deregistriert (`:362-404, 459-466`);
`ModelRevisionMiddleware` ist pure ASGI, SSE ungebuffert (`app_factory.py:554-555`,
`http_caching.py:111-153`). Negativ: ein einziger Lock für alles (`service.py:74`), kein
`TaskGroup`, kein Semaphore um Writes, keine Thread-Isolation für CPU-Arbeit.

### Determinism/Flake in der Test-Perf (FACT)

Fixe Sleeps: `test_subscription_service.py:936` (`2.2 s`), `:611`, `:1003`;
`tests/features/subscriptions/test_subscriptions.py:48` (`time.sleep(1.2)`); Zeit-Assertion
`assert time.monotonic() - started < 0.5` (`test_opcua_client_helpers.py:1931`) — der
wahrscheinlichste Flake auf geladenen Runnern. Kein `pytest.mark.timeout`, keine Clock-Freeze-Lib.

---

## 6. Scalability Assessment

| Ziel | Urteil | Begründung |
|---|---|---|
| 10x mehr API-Clients | Ja bis ~200 concurrent, dann Shedding | `--limit-concurrency 200`; Read-Semaphore 64 (`client.py:150`) ist der eigentliche Engpass |
| 100x mehr Daten (Nodes) | Nein ohne Paging | Build/Refresh O(N) und sequenziell gebrowst (`browse.py:86-131`); `/objects` O(N) (`object_routes.py:103`); 2× Memory-Peak pro Refresh |
| 10x mehr Devices/Subscriptions | Nein | Monitored-Item-Anlage 1/RPC bei 4 Admission; Fan-out unter einem Lock; kein Subscription-Count-Cap (`service.py:220-239`) |
| Distributed Deployment | Nein, strukturell | Kein Redis/etcd/Queue im Repo (grep 0 Treffer); Subscriptions, Bins, Model-Cache, MCP-Sessions sind process-lokal (`docs/SCALING_GUIDE.md:719`); Compose erzwingt `--workers 1` (`docker-compose.yml:23-24` mit Begründung) |

**Statelessness (FACT):** Nicht stateless. SSE muss alle `/v1/subscriptions*`-Pfade zum owning
Worker routen (`docs/SCALING_GUIDE.md:336-342`); das ausgelieferte nginx partitioniert nicht
(`nginx/entrypoint.sh:95-114`), nur das Beispiel `docs/examples/discovery-subscription-split/` tut
es.

**Event-driven Opportunity:** Die interne Queue (`service.py:87`) und Fan-out sind bereits
event-driven; ein externer Bus (Redis/NATS) würde Fan-out und Sessions entkoppeln — größte
Skalierungshebel, Aufwand XL.

---

## 7. Security Review

### Current State Assessment

Das Modell ist offen dokumentiert: die App hat keinen Auth, TLS und Auth sind Deployment-Sache
(`README.md:72-73,289-291,626`; `app_factory.py:505-508`). Das ist ehrlich. Die ausgelieferte
Default-Konfiguration ist jedoch unsicher, und es gibt keine App-seitige Zulassungskontrolle.

### Findings (OWASP-orientiert)

| Finding | Evidence | OWASP | Severity |
|---|---|---|---|
| Keine App-AuthN/AuthZ auf allen REST/MCP/OpenAPI-Routes; nur `Depends(get_opcua_client/get_or_build_model)`; kein `Depends(security)` im gesamten Paket | grep über `i3x_server`; `api/health.py:15`, `api/ua.py`, `api/mcp/router.py:63,114,176` | A01 Broken Access Control | Critical (Deployment-abhängig) |
| Hardcodierte Credentials im Compose: `NGINX_BASIC_AUTH_ENABLED: 1`, `USER: "admin"`, `PASSWORD: "pw1"` | `docker-compose.yml:127-130` | A07 / A01 | Critical — **deferred (Maintainer-Entscheid)** |
| Committed private keys: `certs/https-sample/privkey.pem`, `certs/opcua-client-sample/client-key.pem` (unverschlüsselt, im Git-History) | `git ls-files`; SECURITY.md:96-107 kennt es als Dev-Material | A02 | High — **deferred (Maintainer-Entscheid)** |
| Kein Rate-Limiting in App oder nginx (`nginx/nginx.conf` kein `limit_req`; `client_max_body_size 8m` Zeile 38); einzige Bremme ist `--limit-concurrency 200` | `nginx/nginx.conf`, `run.py:69-71` | A05 DoS | High |
| Unbegrenzte Subscriptionen + unbegrenzte Subtree-Monitoring (`contracts.py:583-590` `maxDepth ge=0` ohne `le`; `mapping.py:61-91` kein Node-Cap) | siehe §5 | A05 | High |
| `eval()` auf Server-gestützten Annotationen | `schemas/objecttype_annotations.py:288` | A03 Injection / A08 | High — einziger Code-Ausführungsvektor; Ausnutzbarkeit hängt von vertrauenswürdigem OPC-Server ab (**ASSUMPTION**) |
| MCP-Origin-Guard trivial umgangen: `Origin` absent → `True` (`jsonrpc.py:131-141`); Helper-Routen ohne Origin-Check (`tools.py:211,228`, `resources.py:433,438`, `prompts.py:76-86`, `roots.py:34`); nginx matcht nur exakt `location = /mcp` | FACT | A01 | Medium |
| Spoofbarer `base_url`: `--proxy-headers` + `--forwarded-allow-ips "*"` (`docker-compose.yml:20-22`) + Origin-Vergleich gegen `request.base_url` → Host-Spoofing kann den Guard umgehen | FACT (Config), **ASSUMPTION** (Exploit-Pfad, von nginx/uvicorn Verhalten abhängig) | A01 | Medium/High |
| Audit-Feld vom Angreifer gesetzt: `principal = request.headers.get("x-principal") or "anonymous"` | `api/v1/object_value_routes.py:458`, Audit `:510-519` | A09 | Medium (Integrität) |
| Rekursive Value-Dekodierung ohne Tiefenbegrenzung | `api/v1/common_helpers.py:169-199` | A05 | Medium (**ASSUMPTION**: asyncua-Limits unbekannt) |
| OPC UA Plaintext Default: `opcua_security_mode = "None"` (`settings.py:18`); `opcua_server_cert_path` default None | FACT | A06 | Medium; MITM gegen Upstream ist **ASSUMPTION** (asyncua-Verhalten) |
| Docs versprechen HSTS/X-Frame-Options/`limit_req`, nirgends implementiert | `docs/NGINX_CONFIGURATION_REFERENCE.md:275,290,315-342`, `PRODUCTION_HTTPS_GUIDE.md:149-155`, `PRODUCTION_DEPLOYMENT_INDEX.md:170-188` | A06 | Medium (Doc↔Realität) |
| Release: push before scan — `push: ${{ github.event_name != 'pull_request' }}` (`docker.yml:99`) läuft **vor** Trivy (`:111-135`); Trivy nur auf non-PR, `ignore-unfixed: true` (`:119,134`); keine CodeQL/Secret-Scanning/Dependency-Review | FACT | A06 Supply Chain | High |
| SECURITY.md behauptet Dependabot (`:38-40`); kein `dependabot.yml` getrackt und nie in der Historie gewesen (`git log --all -- dependabot.yml` → leer), obwohl 14 dependabot[bot]-Commits existieren | FACT + **ASSUMPTION** (evtl. über Repo-Settings aktiv) | A06 | Medium |
| nginx loggt nur non-200 (`nginx/nginx.conf:22-27` `map $status $log_non_200`) | FACT | A09 | Medium |

**Gut (FACT):** Keine Injection-Pfade im FS/Shell (kein `subprocess` mit shell, kein
`pickle`/`yaml.load`); MCP-Pfade gehärtet (`_safe_api_prefix` `invocation.py:450-468`,
`_safe_request_path` `:471-486`, `_safe_path_parameter_value` `:489-503` lehnt `..`/scheme/netloc
ab); keine SSRF (synthetischer Host hardcoded `mcp.local`, `:705-726`); ElementIds werden nie
rohe NodeIds (Resolution über Model-Cache, `validate_write_element_id` `contracts.py:22-28`,
`validate_client_id` `:31-46`); Writes schema-validiert mit `Draft202012Validator` +
`FormatChecker`, externe `$ref` verworfen (`write_validation.py:21,48-57`); MCP schließt PUT-Tools
aus (`tool_definitions.py:339-340`); MCP-Metrics/Logs leaken keine Credentials (201 `logger.*`,
keine mit Secrets/Bodies); Container non-root uid 10001 (`Dockerfile:65-78`), `cap_drop ALL`,
`read_only`, certs nur zur Laufzeit gemountet.

### Prioritized Security Actions

| Action | Priority | Effort |
|---|---|---|
| Compose ohne hardcodierte Credentials; Credentials nur aus Env/Secret-Store | Critical | S — **deferred** |
| App-seitige Zulassung: Rate-Limit pro Client, Subscription-Count-Cap, `maxDepth le`, Subtree-Node-Budget | Critical | M |
| Scan vor Push in `docker.yml`; Trivy auch auf PRs; `ignore-unfixed` aus; CodeQL + Secret Scanning + pip-audit | Critical | M |
| Private Keys aus Repo/Git-History entfernen, nur Generierungs-Skripte shippen | High | M — **deferred** |
| `eval()` ersetzen durch typisierte Dekodierung | High | M |
| `dependabot.yml` konfigurieren oder SECURITY.md korrigieren | High | S |
| Origin-Guard auf allen MCP-Routen; Host-Header auf Allowlist statt `base_url` | High | S |
| HSTS/X-Frame-Options/`limit_req` im ausgelieferten nginx; Audit-Principal aus Auth, nicht aus Header | Medium | S |

---

## 8. Testing Assessment

### Current State Assessment

**FACT:** 1140 collected items, 756 `def test_`, 88 `parametrize`, ~2000 `assert`; volles Run:
**1139 passed / 1 skipped in 98.8 s**; Line-Coverage **87,19 %** (9514/10912). CI-Gate
`--cov-fail-under=80` (`quality.yml:158`), lokal `TARGET_COVERAGE_PERCENT = 85.0`
(`run_tests.py:19-58`).

**Branch-Coverage ist 0 %** — nicht instrumentiert (`coverage.xml` hat `branches-valid="0"` auf
allen Paketen; `pyproject.toml:49-62` kein `branch = true`). Die 87 % sind damit für die
error-heavy Modules schmeichelhaft; realistisch ~65-75 % Branch (**ASSUMPTION**).

**Größte Coverage-Lücke (FACT):** `infrastructure/opcua/client.py` (999 Zeilen, echter
asyncua-Transport/Browse/Subscription/Reconnect) ist mit Begründung ausgeschlossen
(`pyproject.toml:52-54`). Damit ist das risikoreichste Modul ungemessen. Lowest-covered:
`values.py` 0.611 (159 missed), `node_reading.py` 0.688 (104), `mapping.py` 0.714,
`object_types.py` 0.728 (123), `object_value_helpers.py` 0.768 (86, davon ein ganzer unbeprüfter
Tail ~Zeilen 583-672 laut `docs/adr/0004-v1-api-ownership-model.md:68-83`), `browse.py` 0.786.
Pattern: alles an der asyncua-Grenze ist dünn, alles rein Logische (`domain` 0.9145, ports 1.0,
`contracts`, `prompts` 0.9843) ist dicht.

### Strengths (FACT)

- Contract-driven: `assert_documented_response` prüft Runtime-Payloads gegen `/openapi.json`
  (`tests/conftest.py:543-550`); `test_openapi_errors_match_runtime:963`,
  `test_openapi_conditional_get_matches_runtime:1058`,
  `test_openapi_examples_match_schemas:1085`,
  `test_openapi_advertises_the_bulk_batch_limit:1467` — Docs und Verhalten können nicht
  divergieren.
- Architektur-Guardrails in CI (5 import-linter-Verträge, `quality.yml:38-60`).
- Keine `assert True`, kein `skip`, kein `xfail`; nur ein `skipif`
  (`tests/test_native_telemetry.py:14`, OTel fehlt lokal; CI installiert `--extra otel`,
  `quality.yml:153`).
- Echte Server-Tests existieren: `tests/test_opcua_browse_roots.py:26-29` (asyncua `Server` auf
  `unused_tcp_port`), `tests/test_conformance_fixture_history.py:38-45`,
  `test_history_update_adapter.py:119` assertiert echte Server-Ablehnung statt Fake.
- Security Verhalten getestet: MCP-Origin → 403 (`tests/test_mcp_protocol.py:478`).
- Negative Path-Matrizen: `test_write_conformance.py:212-292`, `:328` (Control-Char/Whitespace
  ElementIds), malformed Inputs (`test_sample_subscription_client.py:281`, `"client\nnewline"`).

### Weaknesses (FACT)

- **Determinismus:** fixe Sleeps (`test_subscription_service.py:936,611,1003`,
  `features/subscriptions/test_subscriptions.py:48`), Wanduhr-Fenster
  (`test_write_conformance.py:47,49,104`), Timing-Assertion `< 0.5`
  (`test_opcua_client_helpers.py:1931`). Kein Time-Freezing.
- **Fake-Semantik divergiert vom Wire:** `FakeOpcUaClient.read_values` mutiert Werte nach
  `self._reads`-Counter (`tests/conftest.py:335-346`) → Update-Ordering/Coalescing-Bugs wären
  ohne Live-Server unsichtbar.
- **Implementation-Kopplung** an Privates (siehe §4).
- **Thin Smoke-Files:** `features/v1_namespaces/test_v1_namespaces.py` = 1 Test/3 Asserts
  (13 Zeilen), `features/v1_info/test_v1_info.py` = 1 Test/6 Asserts (16 Zeilen); prüfen
  Status+Envelope, nicht Semantik.
- **Conformance nicht in CI:** `run_conformancetest.py` fährt Fixture+uvicorn+
  `node bin/i3x-test.js` — ist ein interaktives UI, kein Gate; kein Workflow ruft es. Das
  getrackte Report-Datum (2026-10-21) ist älter als die `REPORT.md`-Aussage (2026-11-08), deren
  Report nur in `%TEMP%` lag.
- **Keine Frontend-Tests:** `frontend/package.json` hat nur dev/build/preview, kein `test`, kein
  vitest. CI baut das SPA zweimal (`quality.yml:31-36,134-140`).
- **Lokale vs CI-Gates widersprechen sich:** `run_tests.py` erzwingt 85 %, CI 80 %, und
  `run_tests.py` schreibt Dateien via `ruff format`.
- **mypy pinnt `python_version = "3.14"`** (`pyproject.toml:77`), CI läuft 3.15 und das Image
  shippt 3.15.2 (`Dockerfile:43`) → typgeprüft auf einer Version, die nicht ausgeliefert wird.
- **Kritische ungetestete Logik:** asyncua-Reconnect/Retry-Paths (`recovery.py`, `client.py`),
  Bulk-Write-Error-Klassifikation am Tail von `object_value_helpers.py`, `mapping.py`
  Subtree-Traversierung ohne Cap, MCP-Helper-Routen ohne Origin-Check.

### Recommended Improvements

| Improvement | Priority | Effort |
|---|---|---|
| `branch = true` + Branch-Threshold-Gate; Codecov-Branch-Report | Critical | S |
| Integration-Tier gegen echte asyncua-Fixture (`conf-test-server/server.py`) für `client.py`/`browse.py`/`node_reading.py`/`values.py` in CI | Critical | L |
| Conformance-Suite headless in CI, JSON-Report committet | High | M |
| Sleeps → Event/Queue-Signalisation oder Fake-Clock; `< 0.5`-Assertion entfernen | High | M |
| Fake mit scripted Value-Sequences statt `_reads`-Counter | High | M |
| Private-Attribute-Assertions durch Public Seams | High | M |
| Frontend-Tests (vitest) oder explizit „untested SPA" dokumentieren | Medium | M |
| Coverage-Gate lokal/CI vereinheitlichen; mypy-Python-Version auf Shipping-Version | Low | S |

---

## 9. Dependency Analysis

| Dependency | Version | Zweck | Notwendigkeit | Maintenance | Security | Alternative |
|---|---|---|---|---|---|---|
| `asyncua` | 2.1.0 (pinned) | OPC UA Client/Transport | Ja, Kern | Aktiv entwickelt | Transitive `cryptography` (Rust-Wheel) ist die größte Supply-Chain-Fläche; Parse-DoS-Historie ist **ASSUMPTION** | Kein praktikabler Ersatz; opcua-asyncio forks prüfen |
| `fastapi` | 0.162.5 (pinned) | HTTP/MCP/REST | Ja | Aktiv | OK | — |
| `pydantic` | 2.14.3 (pinned) | Contracts | Ja | Aktiv | OK | — |
| `pydantic-settings` | >=2.7.0 (unpinned) | Config | Ja | Aktiv | OK | Lock macht deterministisch |
| `jsonschema[format]` | >=4.23.0 | Write/MCP-Validierung | Ja | Aktiv | `FormatChecker` aktiv (`write_validation.py:21`) | — |
| `httpx` | >=0.27.0 | **Eine** Stelle: `mcp/invocation.py:703` (`QueryParams`) | Marginal | Aktiv | Nur Query-Encoding | stdlib `urllib.parse.urlencode` → Dependency entfernbbar |
| `trio` | >=0.27.0 | **0 Imports** | Nein | Aktiv | Unused | entfernen |
| `httptools` | >=0.6.0 | **0 Imports** | Nur als uvicorn-Perf-Option | Aktiv | Unused als Direkt-Dependency | in `uvicorn[standard]` aufnehmen oder entfernen |
| `wsproto` | >=1.2.0 | **0 Imports** | Nein | Aktiv | Unused | entfernen |
| `uvicorn` | >=0.54,<0.55 | Server | Ja | Aktiv | OK | — |
| dev: pytest 9.0.3, pytest-asyncio, pytest-cov, ruff, mypy, import-linter | — | Gates | Ja | Aktiv | OK | — |
| optional `otel`: `fastapi[opentelemetry]` | — | Traces/Metrics | Nur bei `I3X_OTEL_ENABLED` | Aktiv | OK | — |
| Frontend: svelte 5.16, kit 3.1, vite 8.3.4 (`overrides.cookie ^2.0.1`) | — | SPA | Ja | Aktiv | `overrides` zeigt bekannten Cookie-Patch; kein Test/Lint/Typecheck | — |

**Bewertung:** Core-Deps gepinnt, Devs/Rand-Deps unpinned, aber `uv.lock` löst alle mit Hashes
(`uv.lock:127-142` für asyncua), Docker nutzt `uv sync --frozen`. **FACT:** 3 deklarierte Deps sind
ungenutzt (`trio`, `httptools`, `wsproto`), `httpx` nur einmal benutzt. Kein
pip-audit/Dependency-Review in CI. Image-Bases tag-pinned, nicht digest-pinned; `node:24-alpine`
unpinned; App-Image kommentiert mit mutablem Tag `:master` (`docker-compose.yml:8`, README:517).

---

## 10. DevOps & Production Readiness

### Container (FACT)

3 Stages (`python:3.15.2-alpine`, `node:24-alpine`, `python:3.15.2-alpine`), `apk upgrade`, pip
entfernt, `USER app` uid 10001, tini, HEALTHCHECK auf `/health`, CMD single worker +
`--limit-concurrency 200`. SBOM + Provenance `mode=max` (`docker.yml:106-107`), multi-platform
amd64/arm64 (`:27`). Gaps: kein digest-pin; CMD ohne `--proxy-headers` (`Dockerfile:91`) während
Compose sie setzt → plain `docker run` hinter nginx hat falsche Client-IPs; `.dockerignore`
schließt `certs/`, `.env`, `i3X/`, `run_*.py`, `coverage.xml` nicht aus (Build-Kontext-Blähung);
`docker.yml` ohne `timeout-minutes`, auf `ubuntu-latest` statt gepinntem `ubuntu-26.04`, ohne
concurrency group.

### Compose/nginx (FACT)

Gut gehärtet: `read_only`, tmpfs, `cap_drop ALL`, `no-new-privileges`, `pids_limit 256`, internal
`expose: 8000`, Jaeger-UI an 127.0.0.1, nginx `depends_on: service_healthy`. Schwächen:
hardcodierte Basic-Auth-Credentials (`:127-130`); **kein nginx-Healthcheck**; `/health` ist
statisch (`api/health.py:11-22` — Docstring sagt explizit, Probes dürfen OPC UA nicht berühren),
also ist `healthy` ≠ OPC-UA-readiness, während das Docs-Beispiel `/v1/info` probe
(`docs/examples/multi-instance-path-routing/docker-compose.yml:35`); kein Rate-Limit/HSTS/Frame-Blocking
im ausgelieferten Edge; nginx loggt nur non-200 (`nginx/nginx.conf:22-27`), contradict
`SECURITY.md:93` („Enable audit logging"); TLS-Block sane (TLSv1.2/1.3, `server_tokens off`,
HTTP→HTTPS, `client_max_body_size 8m` passend zu Bulk-Limit 500).

### Startup/Shutdown Robustheit (FACT)

**Kritisch:** `await opcua_client.connect()` (`app_factory.py:342-343`) ohne try/except — wenn der
OPC-Server beim Start nicht erreichbar ist, **serve die App nie**, und der Reconnect-Supervisor
wird erst nach `_connect_sequence` angelegt, also rescues ihn nicht → Restart-Loop in Compose/k8s
bis der Upstream hochkommt. Es gibt `skip_opcua_connect` (`settings.py:52`, Empty-Model-Fallback
`app_factory.py:432-440`) als Degraded-Mode, aber default False. Model-Preload-Failure ist
toleriert außer `fail_startup_on_model_preload_error` + blocking (`:243-247`); Object-Type-Warmup
wird geschluckt (`:169-180`). Shutdown-Reihenfolge korrekt (`:467-490`). Recovery post-startup ist
gut (`recovery.py`, `_should_retry_after_disconnect` retryt Timeouts bewusst nicht).

### Observability (FACT)

Plain-text `logging.basicConfig`, Level aus `I3X_LOG_LEVEL` (`app_factory.py:149-158`);
asyncua-Rauschen unterdrückt, Lifecycle-Logs behalten (`:156-158`); INFO-Request-Log mit 499 bei
Client-Disconnect (`:728-753`). **Keine strukturierten/JSON-Logs, kein structlog, keine
Request-ID/Correlation-Middleware.** OTel opt-in (`:161-166`, MCP-Metrics `:299`, Spans
`mcp/invocation.py:54-72,101-114,524`, HTTP-Spans via `fastapi[opentelemetry]`), Compose mit
Jaeger 1.76.0. **Kein Prometheus-Endpoint** — Diagnostics sind JSON (`/ua/metrics` `api/ua.py:206`,
`/model/metrics` `api/v1/model_routes.py:22`, `/ua/status` `:121`, `/ua/connection` `:151`), also
in einem Prometheus-Stack nichts scrapebares. Kein Alerting, keine Error-Aggregation (Sentry etc.).

### Kubernetes/Cloud/Edge (FACT)

Keine k8s-Manifests, kein Helm, kein Kustomization (Suche `**/*.yaml|**/*.yml` außer
`.github`/`docs/examples` → nur Compose). Image ist k8s-freundlich (non-root, read-only fs, tini,
`I3X_*` Env), aber: `overrides/mcp_overrides.json` + `schema.json` werden cwd-relativ geladen
(`mcp/tool_definitions.py:15-16`, baked via `Dockerfile:74`) → ohne ConfigMap-Mount fallen
MCP-Overrides still fallen (`:77-138`, Flags `app.state.mcp_overrides_applied`,
`app_factory.py:421`); kein liveness/readiness-Trennung; process-lokale Subscriptions machen
Autoscaling unsicher ohne Sticky-Routing. Edge-Deployment funktioniert (single worker, kleine RSS
~126 MB als Doc-Claim).

### Deploy/Release (FACT)

Kein Deploy-Workflow; Deployment manuell via `docs/deploy.sh` (Smoke-Test auf `/v1/info`,
`:208-250`). `dco.yml` skippt jeden Commit dessen Author/Committer mit `[bot]` endet (`:83`) —
breiter als die begründete Allowlist (`:37-40`).

---

## 11. Technical Debt Report

| Priority | Area | Description | Business Impact | Effort |
|---|---|---|---|---|
| Critical | Security | App hat keine Auth/Zulassung; Compose shippt `admin`/`pw1` (`docker-compose.yml:127-130`); committed private keys im Git | Risiko bei Default-Bring-up in Produktion | M — Credentials/Keys **deferred**, Zulassung im Fix-Plan |
| Critical | Reliability | Startup hard-failt bei unreachable OPC UA (`app_factory.py:342-343`); `/health` statisch → Orchestrierung restart-loopt | Availability in k8s/Compose | M |
| Critical | Performance | Sequential Bulk-Writes + Pflicht-Read-Back (~1000 RTTs/500 Items) | Write-Latenz skaliert nicht | M |
| Critical | Performance | SSE Fan-out: globaler Lock + O(N)-Scans + `list.pop(0)` (`service.py:74,651,740,756,993`) | CPU/Throughput bei vielen Streams | M |
| Critical | Testing | Branch-Coverage nicht instrumentiert; asyncua-Adapter aus Coverage ausgeschlossen | Unsichtbare Error-Branches im Kernpfad | S |
| High | Security | Kein Rate-Limit; unbegrenzte Subscriptionen; `maxDepth` ohne `le` | Upstream-DoS | M |
| High | Security | `eval()` auf Server-Daten (`objecttype_annotations.py:288`) | Code-Ausführung bei kompromittiertem Upstream | M |
| High | Architecture | Port-Signaturen mit `Any` + asyncua-Typen im Domain-Layer | Kein zweiter Adapter, keine echte Test-Doubles | L |
| High | Architecture | 335 `client._private`-Zugriffe + asyncua-Bypass in 6 Modulen | Refactor-Risiko im gesamten I/O-Layer | L |
| High | Ops | Push-before-scan (`docker.yml:99` vs `:111-135`), Trivy nur non-PR, `ignore-unfixed`, keine CodeQL/Secret-Scan | CVE-Publication | M |
| High | Ops | Keine strukturierten Logs, kein Prometheus, keine Request-ID | Incident-Analyse | M |
| High | Testing | Conformance nicht in CI; getrackter Report älter als Behauptung | Compliance-Nachweis | M |
| High | Code Quality | God-Functions (531/315/197/184 Zeilen) und 6 Duplikat-Cluster | Bug-Reproduktion, Review-Kosten | L |
| High | Performance | `_reverse_parent_index_cache` Fingerprint aus dict-Längen (`object_helpers.py:233-236`) | Falsche Hierarchien nach Refresh | S |
| High | Maintainability | Bus-Factor 1 (655 Commits ein Mensch) | Knowledge-Risk | L |
| Medium | Design | `app.state` untypisiert, 6 string-getattr-Fallbacks | Stille Degradationen | M |
| Medium | Design | MCP läuft durch synthetischen ASGI-Self-Call statt Services | Perf-Tax + implizite Kopplung | L |
| Medium | Performance | `/objects` ohne Paging (**aus Scope ausgeschlossen**); 2× Memory-Peak pro Refresh; History ohne Value-Cap | Growth-Grenze | M |
| Medium | Quality | ~40 Quality-Strings ohne Enum; `zip(strict=False)` ×3 | Typo-/Paarungsfehler | S |
| Medium | Docs | ADR 0001 zitiert nonexistent Modul; architecture.md zitiert `api/mcp/monolithic.py`; BENCHMARK claimt Sampling 50 ms; SECURITY.md claimt Dependabot | Vertrauens-/Compliance-Drift | S |
| Medium | Ops | `I3X_HOST`/`I3X_PORT` in `.env-example` haben im Image keine Wirkung (CMD hardcoded 8000) | Stille Fehlkonfiguration | S |
| Low | Debt | 3 ungenutzte Declared-Deps (`trio`, `httptools`, `wsproto`); `httpx` nur 1 Nutzung | Cleanup | S |
| Low | Debt | Getrackte Artefakte: `fixme.md`, `tests/features/.tmp_pytest_out.txt`, `tests/features/pytest_report.xml`, `img/`+`static/` Duplikate | Repo-Hygiene | S |
| Low | Quality | 35/88 Dateien ohne Modul-Docstring; 9 leere Summary-Zeilen; bare `type: ignore` (`common_helpers.py:89`) | Onboarding | M |
| Low | Ops | mypy pinnt 3.14, shipped 3.15.2; lokale 85 % vs CI 80 % Gates | Gate-Drift | S |

Klassifizierung: Architektonisch (Ports, Encapsulation, app.state, MCP-Dispatch) · Design
(Enum/Constants, Facade) · Code-Quality (God-Functions, Duplikation, Docstrings) · Testing
(Branch, Integration, Conformance, Determinismus) · Performance (Writes, Fan-out, Paging, Memory)
· Security (Auth/Zulassung, Keys, eval, Supply Chain) · Operations (Observability, Startup,
nginx-Härtung, k8s).

---

## 12. Module-by-Module Analysis

| Package | Responsibility | Quality | Complexity | Dependency Assessment | Maintainability | Recommendations |
|---|---|---|---|---|---|---|
| `api/v1` (5093 LOC) | i3X REST-Presentation, Contracts, Helpers | 6 | High: 1263-LOC Modul, 155-Zeilen-Funktion | Richtung ok, aber `bootstrap.dependencies` + `model.revision` + `config` direkt | 4-6 | Helpers zu Services verlagern; Duplikate zusammenlegen; Paging ausgeschlossen |
| `api/mcp` (1440) | JSON-RPC-Transport + Helper-Routen | 7 | Medium | clean; Origin-Guard nur auf Transport-Routen | 7 | Guard auf alle Routen; typisierte `id`/params (`jsonrpc.py:252`) |
| `api` root (332) | health, ua-Diagnose, openapi-Subclass | 7 | Low | ua-Routen lesen `app.state` direkt (`ua.py:147`) | 7 | Diagnose über Services/DI |
| `application/services` (1745) | Use-Case-Orchestration | 6 | Medium-High (475/451/423 LOC) | hängt an FastAPI (`dependencies.py:13`); Services bekommen `request` | 6 | Framework raus; `ObjectValueService` (nicht von Routen genutzt) entfernen oder batched machen |
| `application/ports` (169) | Subscription-Port + opcua-Facade | 7 | Low | Facade ohne Mehrwert | 6 | Facade auflösen |
| `application/dependencies.py` (112) | DI-Zugriff auf `app.state` | 5 | Low | cast + getattr, stringly | 5 | Typed State Container |
| `domain` (491) | Utils + OPC-UA-Ports | 7 | Low | Port importiert asyncua (`ports/opcua.py:14`) | 7 | Port-Werttypen; Utils haben nur Test-Caller (`:208`) |
| `infrastructure/opcua` (3178) | asyncua-Adapter + Browse/Read/History/Recovery | 4 | Very High | 335 Private-Zugriffe, asyncua-Bypass, bidirektionale TYPE_CHECKING-Kopplung | 3-5 | Öffentliche Seams; Cluster zu echten Modulen mit APIs; Coverage-Tier |
| `infrastructure/subscriptions` (1855) | Fan-out, Bins, Monitoring, State | 5 | Very High (1057 LOC) | Ports ok; Monitoring greift Service-Privates | 4 | Per-Subscription-Locks, deque, batched Adds, Caps |
| `bootstrap` (841) | Composition Root, Lifecycle, Middleware | 5 | Very High (315+197 LOC Funktionen) | importiert api, application, infra, mcp, model, prompts | 5 | In App-Builder + Lifecycle-Schritte zerlegen; `connect()` mit Degraded-Mode |
| `model` (1273) | Model-Builder, Mapper, Revision, Semantic Profiles | 6 | Very High (`builder.py:36-567`) | Port nur via Domain | 5 | Phasen extrahieren; inkrementeller Refresh |
| `schemas` (2108) | i3X Contracts, ObjectType-Schemas/Registry, Value-Decoding | 6 | Very High (815/805 LOC) | `eval()` (`annotations.py:288`), 41+39 `Any` | 5 | eval entfernen; Typen für Schema-Fragmente |
| `mcp` (1473) | Tool-Definitions, Overlays, Invocation, Model-Access/Tools | 5 | Very High (768/742) | importiert `api.v1.objecttype_helpers` (`model_access.py:14`, `invocation.py:231`) → Layer-Bypass | 5 | Service-Dispatch; mehr Cached-Tools; Overlay-Schema cwd-unabhängig |
| `prompts` (197) | Prompt-Registry/Renderer | 8 | Low | clean | 8 | `_load_otel_tracing` mit shared Util |
| `config` (136) | Settings + Validatoren | 7 | Low | clean | 7 | Env-Scan cheap machen; `I3X_HOST/PORT` wirklich verbinden; Security-Mode/Cert-Konsistenz prüfen |
| `errors.py` + `http_caching.py` (196) | Error-Envelope, ETag/Conditional GET | 8 | Low | clean; pure ASGI Middleware | 8 | Nothing significant — ETag-Design ist gut |
| `main.py`/`version.py` (31) | Entry-Point, Version | 8 | Low | `main.py` aus Coverage ausgeschlossen | 8 | ok |
| `frontend` (Svelte) | SPA | 4 | Low | kein Test/Lint/Typecheck; `overrides.cookie` Patch | 4 | vitest + svelte-check |
| `tests` | 43 Root-Suiten + 13 Feature-Suiten | 7 | Medium (conftest 550) | greift Impl-Privates; Fake mutiert Werte | 6 | Public Seams, scripted Fake, Conformance in CI |

---

## 13. Refactoring Roadmap

Implemented as a phase-per-PR plan in `fix_plan.md` (this repository). Scope decisions applied:
`/objects` pagination excluded; default credentials/sample keys deferred; no i3X spec violations.

### Phase 1 — Immediate (Tage)

| Action | Priority | Effort | ROI |
|---|---|---|---|
| Compose Credentials aus Env/Secret; keine `admin`/`pw1` im Repo; committed private keys raus | Critical | S — **deferred** | Eliminiert das unsichere Default-Bring-up |
| `branch = true` + Branch-Gate | Critical | S | Sichtbarkeit der Error-Branches im I/O-Layer |
| Scan-before-push in `docker.yml`, Trivy auf PRs, `ignore-unfixed` aus, Secret-Scanning an | Critical | S | Keine CVE-Publication |
| App-Zulassung: Rate-Limit, `I3X_SUBSCRIPTION_MAX_COUNT`, `maxDepth le`, Subtree-Budget | Critical | M | Schließt anonymen Upstream-DoS |
| Startup: `connect()` in try/except mit Degraded-Mode + Supervisor-before-connect, `/ua/status` als Readiness | Critical | M | Availability in Orchestrierung |
| `_reverse_parent_index_cache` → Model-Revision-Fingerprint | High | S | Korrektheit nach Refresh |
| `deque(maxlen)` für `updates`; Per-Stream-Cursor statt Full-Scan | High | S | Direkt CPU-Gewinn bei Streams (Doc-Claim ~340-440 req/s Bereich) |

### Phase 2 — Short Term (Wochen)

| Action | Priority | Effort | ROI |
|---|---|---|---|
| Bulk-Writes parallel unter Write-Semaphore, Read-Back optional (`I3X_VERIFY_WRITE`) | High | M | ~2-5x Write-Throughput |
| Batched `CreateMonitoredItems` per Bin | High | M | Register-Latenz sublinear |
| Server-Paging für `/objects` | **aus Scope ausgeschlossen (Maintainer-Entscheid)** | — | — |
| Öffentliche Adapter-Seams; Private-Zugriffe in values/browse/node_reading/object_types/metadata/recovery eliminieren | Critical | L | Refactorbarkeit des I/O-Layers |
| Typed State Container statt `app.state` | High | M | mypy deckt Grenzen |
| Conformance headless in CI + committeter Report | High | M | Compliance-Nachweis automatisch |
| Duplikat-Cluster (6) zusammenlegen; Enums für Quality/Mode | Medium | M | Weniger Bug-Reproduktion |
| Strukturierte JSON-Logs + Request-ID + Prometheus-Endpoint | High | M | Operabilität |

### Phase 3 — Medium Term (Monate)

| Action | Priority | Effort | ROI |
|---|---|---|---|
| Port-Werttypen statt `Any`, kein asyncua im Domain-Port | Critical | L | Zweiter Adapter, echte Fakes, Coverage-Gewinn |
| Integration-Tier gegen echten asyncua-Fixture für `client.py` | High | L | Riskantestes Modul gemessen |
| `builder.build` + `lifespan` + `create_app` zerlegen; >900-Zeilen-Module splitten | High | L | Review-/Onboarding-Kosten |
| MCP auf Service-Funktionen statt ASGI-Self-Call | High | L | Tool-Latenz, Layer-Integrität |
| Shared-State-Abstraktion (Redis/NATS) für Subscriptions/MCP-Sessions + Sticky-Routing oder Split-Topology | High | XL | Erst dann ist horizontales Scaling möglich |
| `eval()` durch typisierte Annotation-Dekodierung | High | M | Injection-Vektor closed |
| k8s-Artefakte (Manifests/Helm) + liveness/readiness-Trennung | Medium | M | Cloud-Readiness |

### Phase 4 — Long Term

| Action | Effort | Business Value |
|---|---|---|
| Inkrementeller Model-Refresh statt Full-Rebuild | L | Kein 2× Memory-Peak, Refresh bei 10x Nodes |
| Bus-Factor reduzieren: ADRs für Perf-/Security-Grenzen, Pairing-Route, Arch-Tests ausbauen | M | Kontinuität |
| Frontend-Test-Stack (vitest/svelte-check) + Lint in CI | M | SPA-Qualität |
| Deps bereinigen (`trio`, `httptools`, `wsproto`, `httpx`), Digest-Pins, pip-audit, CodeQL | S | Supply-Chain-Hygiene |

---

## 14. Final Verdict

### Strengths

- Maschinell erzwungene Architektur: 5 import-linter-Verträge in CI (`quality.yml:38-60`);
  Layer-Richtungen real sauber (FACT).
- Gate-Hygiene: `ruff check` 0 findings, `ruff format --check` clean, `mypy --strict` Success auf
  88 Dateien; nur 2 `type: ignore`, 3 `# noqa`; 0 TODO/FIXME.
- Contract-Tests gegen `/openapi.json` — Docs und Runtime können nicht divergieren
  (`conftest.py:543-550`).
- Echte Compliance-Evidenz: 56 pass / 0 fail / 2 warn / 2 skip (`i3x-conformance-report.json`);
  Warns sind honest dokumentiert (plain HTTP, unauthentifiziert `/namespaces`).
- Batched Read-Design, bounded Queue mit sichtbaren Sequence-Gaps (`service.py:87-125`), gutes
  ETag/Revision-Design (`http_caching.py`), saubere Shutdown-Reihenfolge und Signal-Chaining
  (`app_factory.py:362-404, 467-490`).
- Containement-Härtung über dem Durchschnitt: non-root uid 10001, `read_only`, `cap_drop ALL`,
  tini, SBOM + Provenance max.
- Ehrliche Doku der Grenzen (`docs/SCALING_GUIDE.md:719` process-local; README:72-73 kein App-Auth).

### Weaknesses

- Vertrauensgrenzen nicht im Code: kein AuthN/AuthZ, kein Rate-Limit, kein Quota; Default-Compose
  shippt Credentials und committed Sample-Keys.
- Ports sind die schwächsten Stellen: `Any`-Returns (`domain/ports/opcua.py:182-228`) und
  asyncua-Import im Domain-Layer — verstößt die eigene Regel (`docs/architecture.md:142-143`).
- Encapsulation-Breach: 335 Private-Zugriffe über 6 Module inkl. fremder Zustands-Mutation und
  asyncua-Bypass.
- Perf-P0s auf den Hauptpfaden: sequenzielle Writes mit Read-Back, globaler Fan-out-Lock,
  O(N)-SSE-Scans, `list.pop(0)`, unbegrenztes Monitoring.
- Reliability-P0: hartes Startup-Failure ohne Upstream, statische Health, kein Readiness-Signal.
- Test-Bild ist breit, aber Branch-Blind und am I/O-Rand dünn; der größte Adapter ist aus
  Coverage ausgeschlossen; Conformance nicht im CI.
- Ops-Lücken: keine strukturierten Logs, kein Prometheus, kein Request-ID; Release scannt nach dem
  Push.
- Maintainability: 3 Module über der eigenen 900-Zeilen-Regel ohne Refactor-Note; Bus-Factor 1;
  Docs↔Code-Drift an mindestens 5 Stellen.
- Horizontale Skalierung strukturell blockiert (kein Shared State, `--workers 1` erzwingt).

### Top 10 Recommendations

1. Zulassung aus der App: Quotas+Rate-Limits bauen (Credentials aus Default-Pfad: deferred).
2. Startup degraded-mode + Supervisor-before-connect + echtes Readiness-Signal (Critical, M).
3. Branch-Coverage instrumentieren und CI-Gate auf Branch heben (Critical, S).
4. Scan vor Push; Trivy auf PRs; Secret-Scanning/CodeQL/pip-audit (Critical, M).
5. Fan-out zu `deque` + Per-Stream-Cursor + Per-Subscription-Locks (High, M).
6. Bulk-Writes parallelisieren, Read-Back optional (High, M).
7. Adapter-Seams öffentlich machen; `client._private`-Zugriffe eliminieren (High, L).
8. Port-Werttypen statt `Any`, asyncua raus aus dem Domain-Port (High, L).
9. `maxDepth`/Subtree-Caps (High, S/M); `/objects`-Paging vom Maintainer ausgeschlossen.
10. Conformance in CI + committeter Report; Docs-Drift bereinigen; Bus-Factor-Maßnahmen (High, M).

### Architecture Decision

**Architecture requires moderate refactoring.**

Begründung mit Evidenz: Das Schichtmodell ist real und erzwungen (FACT: `.importlinter` 5 Verträge,
`quality.yml:38-60`; `infrastructure` importiert kein `api`, `domain` kein `infrastructure`), die
Gates sind sauber, und die Test-Strategie ist contract-driven — ein Rewrite wäre Verschwendung.
Aber die Grenzen sind porös: die Ports geben `Any` zurück und importieren asyncua
(`domain/ports/opcua.py:14,182-228`), der Adapter-Cluster teilt mutable Internals über 335
Private-Zugriffe, `app.state` ist ein stringly Bag (20 Attribute, 6 getattr-Fallbacks), und das
MCP-Paket umgeht die Application-Schicht per ASGI-Self-Call (`mcp/invocation.py:696-760`). Dazu
kommen lokal behebbare, aber kritische Betriebs- und Performance-P0s (`app_factory.py:342-343`,
`service.py:74/651/993`, `object_value_helpers.py:298-305`) sowie Security-Schulden, die
überwiegend Konfiguration und Zulassung betreffen, nicht die Struktur. Deshalb: keine
Re-Architektur, sondern gezielte Refactorierung der seams, der I/O-Grenze, der Zulassung und der
Ops-Schicht.
