# Fix Plan — i3x2ua (phase-per-PR)

Review basis: `review.md` (commit `c9d9525`). Each phase is one PR/commit. Phases are executed
strictly in order; the progress table at the bottom is updated after every phase.

## Scope decisions (from the maintainer, binding for all PRs)

- **Do not violate the i3X spec.** Endpoints/semantics under `/v1` stay spec-conformant; new
  gateway-level diagnostics/admission behavior lives outside `/v1` or uses existing error envelopes.
- **`/objects` is NOT paginated** (excluded from the roadmap).
- **Default credentials can stay for now**: `docker-compose.yml` `admin`/`pw1` and committed
  sample certs/keys are deferred (not touched).
- `original_review.md` is intentionally untracked (gitignored).

## Global acceptance gate (every PR must pass all of these)

| Gate | Command | Must |
|---|---|---|
| Unit/integration tests | `.venv\Scripts\python.exe -m pytest -q --tb=line` | all pass, no new skips/xfails |
| Coverage | `pytest --cov` line-rate | ≥ 87 % baseline (never regress); branch gate active from PR-01 |
| Lint | `.venv\Scripts\python.exe -m ruff check i3x_server tests` | 0 findings |
| Format | `.venv\Scripts\python.exe -m ruff format --check i3x_server tests` | clean |
| Types | `.venv\Scripts\python.exe -m mypy i3x_server` | 0 issues (strict) |
| Architecture | `import-linter --config .importlinter` | contracts pass |
| Spec compliance | `.venv\Scripts\python.exe scripts\run_conformance_headless.py --json <tmp>` | verdict `Full 1.0 Compliance`, suite exit code 0 |

**Test-quality rule per PR:** new/changed tests must assert *behavior* (values, error codes,
ordering, state transitions), not just status codes or line counts. Negative paths and edge cases
are required for every new limit/branch introduced by the PR. Branch coverage must cover the PR's
new branches (visible via `coverage.xml` branch data after PR-01).

---

## PR-00 — Baseline: review, plan, headless conformance gate

- `review.md` (full review), `fix_plan.md` (this file), `scripts/run_conformance_headless.py`
  (fixture + gateway + `i3x-test run`, UTF-8-safe stdout), `.gitignore` entry for
  `original_review.md`.
- Baseline verified before commit: 1139 passed / 1 skipped, 87 % line coverage, gates green,
  conformance `Full 1.0 Compliance` (56 pass / 0 fail / 2 warn / 2 skip).

## PR-01 — Gates & determinism (Phase 1)

- `[tool.coverage.run] branch = true` in `pyproject.toml`; keep existing omit list.
- Coverage gate alignment: CI `--cov-fail-under=85` (matches `run_tests.py`'s 85) and a new
  branch gate at the measured baseline (e.g. `--cov-fail-under` for branch via
  `coverage report --fail-under` after measuring; set to measured value minus small slack).
- Wire conformance gate into CI: new `conformance` job in `.github/workflows/quality.yml`
  (ubuntu, node 24, `uv sync`, run `scripts/run_conformance_headless.py --json`, upload report
  artifact; do not overwrite committed `i3x-conformance-report.json`).
- Deterministic test fixes: replace fixed sleeps (`tests/test_subscription_service.py:611,936,1003`,
  `tests/features/subscriptions/test_subscriptions.py:48`) with event/queue signaling or polling
  until condition; remove/replace wall-clock assertion `< 0.5`
  (`tests/test_opcua_client_helpers.py:1931`) with deterministic fake-clock or generous bound +
  semantics assertion.
- Tests added: the rewritten tests keep asserting the same semantics (sequence gaps, drop counts,
  update ordering) — no behavior change, only determinism.

## PR-02 — Startup reliability & readiness (Phase 2)

- Guard `await opcua_client.connect()` (`bootstrap/app_factory.py:342-343`): try/except; create
  the reconnect supervisor *before* the first connect attempt so post-start reconnects work.
- Opt-in degraded startup: `I3X_ALLOW_DEGRADED_STARTUP` (default `false` → today's hard-fail
  behavior preserved). When true and connect fails: serve with empty model (reuse
  `skip_opcua_connect` fallback path), log a loud warning, supervisor keeps retrying.
- Readiness: keep `/health` deliberately static (intentional, spec-neutral); make `/ua/status`
  (`api/ua.py:121`) report honest connection state (connected/reconnecting/unconfigured) so
  orchestrators can probe upstream readiness without flipping container health.
- Tests: startup with an unreachable endpoint under degraded mode serves `/v1/info` with empty
  model and `/ua/status` reports not-connected; supervisor retry loop exercised with a fake
  client that fails twice then succeeds; default behavior still raises.

## PR-03 — Admission control & cache correctness (Phase 3)

- Settings: `I3X_SUBSCRIPTION_MAX_COUNT` (default e.g. 1000), `I3X_SUBTREE_MAX_NODES`
  (default e.g. 5000), `I3X_REQUEST_RATE_LIMIT_PER_MINUTE` (default 0 = off, opt-in middleware
  per client IP, applied outside `/v1` semantics — returns 429 with spec-neutral error envelope).
- Contracts: `maxDepth` gets `le` bound (spec sets no upper bound; gateway admission bound is
  allowed and fixture/conformance trees are far below it).
- `infrastructure/subscriptions/mapping.py`: node-count budget for subtree monitoring → reject
  with existing error envelope (`BadRequest` style), never silently truncate.
- `infrastructure/subscriptions/service.py`: subscription count cap → reject beyond max via
  existing error envelope.
- Bin floor caps: process-local floors when server capability vars are absent
  (`metadata.py:117-121`, `monitoring.py:150-155`).
- Cache correctness: `_reverse_parent_index_cache` fingerprint → `BuildResult.revision`
  (`api/v1/object_helpers.py:233-236`).
- `zip(..., strict=False)` → `strict=True` at `object_value_helpers.py:239,251,260` with
  aligned-length guarantees proven in tests.
- Tests: cap enforcement (N+1 subscriptions rejected with proper envelope), subtree budget
  rejection, `maxDepth` validation (boundary values), stale-fingerprint regression (rebuild of
  same-size model with different parents must not serve stale parents), zip mismatch raises.

## PR-04 — Subscription fan-out performance (Phase 4)

- `updates` → `collections.deque(maxlen=self._max_updates_per_subscription)`; drop via maxlen
  instead of `pop(0)` (`service.py:993-994`).
- Per-stream cursor delivery: each stream tracks `after_sequence`; delivery resolves position in
  O(1) via `first_sequence` bookkeeping (sequences are contiguous integers) instead of O(N) scans
  (`service.py:651,740,756`). Sequence-gap semantics preserved.
- Lock granularity: single `_lock` (`service.py:74`) → registry lock + per-subscription lock so
  fan-out ticks of different subscriptions don't serialize.
- Batched `CreateMonitoredItems` per bin in `monitoring.py` (asyncua accepts lists) instead of
  1 node/RPC (`monitoring.py:270,355`).
- `_dropped_node_datachanges` entries cleared on monitor release.
- Tests: ordering across drops, cursor correctness after eviction (old cursor before deque head
  → gap semantics), batched add call counts via fake client, concurrent fan-out smoke test with
  multiple subscriptions, per-subscription isolation.

## PR-05 — Bulk write throughput (Phase 5)

- Parallelize bulk writes across **distinct** node ids under a write semaphore (configurable
  `I3X_WRITE_CONCURRENCY`); items targeting the same node stay sequential to preserve ordering.
- Mandatory read-back stays spec-required but becomes **one batched `read_values`** after the
  write phase instead of per-item sequential reads (`object_value_helpers.py:298-305`).
- Per-item outcome attribution (order, error classification, no-op fallback) unchanged.
- Tests: bulk outcomes identical to old sequential semantics (same ordering/errors), duplicate
  node-id ordering, mixed success/error classification, batched read-back call count via fake,
  partial failure attribution.

## PR-06 — Security code hardening (Phase 6; credentials deferred)

- Remove `eval()` in `schemas/objecttype_annotations.py:288` → `ast.literal_eval` / typed parse;
  invalid annotations degrade with warning, never execute code.
- MCP origin guard: enforce `I3X_MCP_ALLOWED_ORIGINS` comparison for **present** `Origin` headers
  on *all* MCP routes (transport + helper routes: `tools.py`, `resources.py`, `prompts.py`,
  `roots.py`), not just the JSON-RPC transport; absent `Origin` stays allowed for non-browser
  MCP clients (documented defense-in-depth given app-auth is deferred). Do not compare against
  spoofable `request.base_url` — compare against configured origins.
- Audit principal: honor `x-principal` only when `I3X_TRUST_PRINCIPAL_HEADER=true` (default false
  → `"anonymous"`), so attacker headers can't forge audit records.
- Tests: `eval()` removal regression (malicious annotation string is parsed inertly), origin
  mismatch → 403 on helper routes, absent-origin still allowed, principal trust toggle behavior.

## PR-07 — CI supply chain + dependency cleanup (Phase 7)

- `docker.yml`: Trivy scan **before** push step; scan also on PRs; `ignore-unfixed: false`;
  add `timeout-minutes` + `concurrency` group; pin runner version.
- New `codeql-analysis.yml` (python + javascript) and `pip-audit` step in `quality.yml`.
- `dependabot.yml` tracked (github-actions, pip, npm/frontend) — makes SECURITY.md's claim true.
- Dependency cleanup: drop `trio`, `wsproto`, `httptools` from `pyproject.toml` (0 imports);
  replace single `httpx.QueryParams` use (`mcp/invocation.py:703`) with stdlib
  `urllib.parse.urlencode`; regenerate `uv.lock`; keep `uvicorn` perf options working without
  direct httptools pin (verify uvicorn still selects httptools if available via optional extras —
  use `uvicorn[standard]` if needed).
- Tests: import suite passes; MCP query encoding behavior covered by existing protocol tests plus
  one new encoding test (unicode/special chars in query params).

## PR-08 — Adapter seams (Phase 8, effort L)

- Public typed seams on `OpcUaClient`: `record_read_calls(n)`, `record_write_calls(n)`,
  `runtime_metrics`, `metadata_cache`, `object_type_cache`, `set_history_update_supported(bool)`,
  `should_retry_after_disconnect(exc)`, `wait_for_reconnect(exc)`, `browse_raw(params)`,
  `raw_node(node_id)`, `limits()`.
- Replace all 335 `client._private*` accesses in `values.py`, `browse.py`, `node_reading.py`,
  `object_types.py`, `metadata.py`, `recovery.py`, `monitoring.py` with seams; asyncua bypasses
  go through `browse_raw`/`raw_node` (still one adapter-owned escape hatch, now explicit).
- Update tests that assert privates (`test_app_factory_helpers.py:264`,
  `test_subscription_service.py:769,1414,1478`, `test_mcp_model_access.py:379,531`,
  `test_opcua_client_helpers.py:1115,1222`) to public seams.
- Regression guard test: static scan asserting sibling `infrastructure/opcua/*` modules contain no
  `client._` attribute accesses (excluding `client.py` itself).
- Behavior must remain identical; conformance must pass.

## PR-09 — Typed ports (Phase 9, effort L)

- Introduce `OpcUaValue`/`DataValueDTO` (+ typed subscription handle/callback) in
  `domain/ports/opcua.py`; remove `from asyncua import ua` from the domain port (violates
  `docs/architecture.md:142-143` today).
- Adapter converts asyncua `ua.DataValue`/`ua.Variant` to DTOs at the boundary; application/api
  consume DTOs only. Extend `.importlinter` with a contract forbidding `asyncua` imports in
  `i3x_server.domain` (permanent guard).
- Tests: DTO conversion matrix (int/float/string/bool/array/statuscode/null) against fake ua
  variants; port protocol conformance test for adapter + test doubles; import-linter asyncua guard
  passes.

## PR-10 — Typed app state + layer wiring (Phase 10, effort M)

- Typed state container (dataclass/Pydantic) replacing the stringly `app.state` bag: all 20
  attributes typed, no string-keyed `getattr` fallbacks (`app_factory.py:259,273,289,467,472,478`,
  `api/mcp/jsonrpc.py:77,91`, `objecttype_helpers.py:1129,1137`).
- Routes depend on `application.dependencies` instead of `bootstrap.dependencies`; add
  `bootstrap` as top layer in `.importlinter` layered contract so presentation→bootstrap is
  structurally forbidden.
- Update ADR 0001 to match reality (file paths, typed state).
- Tests: typed access paths; a test asserting a missing state field raises (no silent fallback);
  import-linter extended contracts pass.

## PR-11 — Decomposition & deduplication (Phase 11, effort L)

- Split >900-line modules along existing responsibilities: `api/v1/objecttype_helpers.py` (1263),
  `infrastructure/subscriptions/service.py` (1057), `infrastructure/opcua/client.py` (999).
- Decompose God functions: `model/builder.py:36-567` into phases; `lifespan` and `create_app` in
  `app_factory.py` into lifecycle steps.
- Consolidate the 6 duplicate clusters (`_runtime_settings`, `_stream_debug_enabled`,
  `_load_otel_tracing`, `_to_namespace`/`_relationship_type_items`, quality/VQT/timestamp
  normalizers, `_to_json_safe_value`, `_extract_attribute_value`/`_coerce_node_class`,
  BrowseDescription blocks).
- Enums for quality strings (`Good/Bad/GoodNoData/Uncertain`) and security mode; constants for
  magic numbers; remove `import *` re-exports and `_`-private cross-layer re-exports; fix bare
  `type: ignore`; docstring baseline for public APIs.
- Tests: extracted modules keep full suite passing; new unit tests for consolidated helpers
  (normalizers, otel loader); coverage of split modules must not regress.

## PR-12 — Observability (Phase 12, effort M)

- Structured logs opt-in: `I3X_LOG_FORMAT=json` (JSON lines, level/logger/message/timestamp);
  plain text stays default.
- Request-id middleware: generate/propagate `X-Request-ID`, include in request log lines.
- Prometheus scrape endpoint `/metrics` (outside `/v1`, spec-neutral; `prometheus_client` dep):
  request counters/latency histograms + gauge metrics mirroring existing JSON diagnostics.
- Tests: middleware sets/echoes request id; JSON log format assertions via capsys; `/metrics`
  exposes prometheus text format with expected metric names.

## PR-13 — Docs drift + repo hygiene + report refresh (Phase 13, effort S)

- Fix docs↔code drift: ADR 0001 (`i3x_server/dependencies.py` → real paths),
  `docs/architecture.md:66` (`api/mcp/monolithic.py` → real module), SECURITY.md Dependabot claim
  (true after PR-07), BENCHMARK_RESULTS.md sampling-interval claim vs real client behavior.
- Shipped nginx hardening promised by docs: `limit_req_zone`, HSTS, `X-Frame-Options`, nginx
  healthcheck for the i3x service; nginx logging beyond non-200 per SECURITY.md audit claim.
- Repo hygiene: `git rm --cached fixme.md tests/features/.tmp_pytest_out.txt
  tests/features/pytest_report.xml` (already gitignored); dedupe `img/`/`static/` assets.
- Refresh committed `i3x-conformance-report.json` from a headless run (verdict `Full 1.0
  Compliance`); `.dockerignore` additions (`certs/`, `.env`, `i3X/`, `run_*.py`, coverage
  artifacts).

---

## Progress tracking

| PR | Phase | Scope | Status | Tests | Coverage | Conformance | Commit |
|---|---|---|---|---|---|---|---|
| PR-00 | Baseline | review.md, fix_plan.md, headless gate | ☐ | baseline 1139 pass | 87 % line | Full 1.0 (56/0/2/2) | — |
| PR-01 | 1 | branch coverage, gate alignment, determinism, conformance-in-CI | ☐ | | | | |
| PR-02 | 2 | startup degraded mode, supervisor-before-connect, readiness | ☐ | | | | |
| PR-03 | 3 | admission caps, rate limit, cache fingerprint, zip strict | ☐ | | | | |
| PR-04 | 4 | fan-out: deque, cursor, per-sub locks, batched adds | ☐ | | | | |
| PR-05 | 5 | bulk writes parallel + batched read-back | ☐ | | | | |
| PR-06 | 6 | eval removal, origin guards, audit principal | ☐ | | | | |
| PR-07 | 7 | scan-before-push, CodeQL, pip-audit, dependabot, dep cleanup | ☐ | | | | |
| PR-08 | 8 | adapter seams, private access removal | ☐ | | | | |
| PR-09 | 9 | typed ports, asyncua out of domain | ☐ | | | | |
| PR-10 | 10 | typed state, bootstrap layer contract | ☐ | | | | |
| PR-11 | 11 | module splits, dedup, enums, docstrings | ☐ | | | | |
| PR-12 | 12 | JSON logs, request-id, Prometheus | ☐ | | | | |
| PR-13 | 13 | docs drift, nginx hardening, hygiene, report refresh | ☐ | | | | |

Status legend: ☐ pending / ◐ in progress / ☒ merged & gates green. Update this table after each PR;
record the commit hash and gate results in the row.

## Deferred (not in any PR for now)

- Default compose credentials (`admin`/`pw1`) and committed sample keys — maintainer decision.
- `/objects` server-side pagination — maintainer decision (out of roadmap).
- Redis/NATS shared state, k8s manifests, frontend tests — long-term roadmap items, no PR yet.
