# i3x2ua Implementation Roadmap

Turn the findings in [REVIEW_2026-11.md](../reviews/REVIEW_2026-11.md) into trackable work. Complete phases in order where practical; each phase is complete when its tasks and verification are checked off. Keep changes small, add focused tests, and update this roadmap as scope or priorities change.

## Passing bar

Use the required checks in [.github/workflows/quality.yml](../../.github/workflows/quality.yml) as the definition of passing. A phase is not complete until all applicable checks pass; for a release-ready result, all of these gates must pass:

- [ ] Frontend dependencies install with `npm ci` and the frontend builds with `npm run build`.
- [ ] `uv run lint-imports` passes.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass.
- [ ] `uv run mypy .` passes.
- [ ] `uv run pytest -q` and `uv run python scripts/check_coverage.py` pass on Python 3.14 and 3.15, using `uv sync --extra otel --group dev --frozen`.
- [ ] `uv run pip-audit` passes against the locked development environment.
- [ ] The headless i3X conformance suite passes with the verdict `Full 1.0 Compliance`.

## [x] Phase 1 — Immediate reliability and security (days)

- [x] Prevent subscription mapping from hanging on composition cycles: track visited nodes and enforce node/depth budgets before acquiring the registry lock; keep the locked section limited to committing results.
- [x] Add regression tests for cyclic mappings, including the `maxDepth=0` case, and verify the event loop remains responsive.
- [x] Stop returning upstream exception text in API error responses; retain useful diagnostic details in server-side logs and add response tests for affected routes and services.
- [x] Make OpenTelemetry behavior consistent between the published image and deployment configuration: install the required OTel support or disable the advertised setting, and emit a gateway-owned startup diagnostic when tracing is unavailable.
- [x] Add request IDs to nginx access logs, including the incoming request ID; ensure the proxy logs remain useful when application INFO and uvicorn access logs are disabled.
- [x] Add a readiness endpoint based on model-built and upstream-connected state while keeping liveness static.
- [x] Configure a graceful-shutdown budget in the container and server that accommodates the existing task, MCP, and SSE cleanup.
- [x] Remove working default credentials from the shipped compose configuration or fail safely when defaults are used with writes/MCP enabled.
- [x] Remove tracked private sample keys, replace them with safe setup-time generation, and rotate/revoke any credentials or certificates that must no longer be trusted.
- [x] Mark the proxy-less README quickstart as development-only and make the production security boundary and TLS/auth requirements explicit.
- [x] Restrict forwarded-header trust to the known proxy; update rate-limit documentation to use a trusted remote-address key rather than caller-controlled `X-Forwarded-For`.
- [x] Add dependency-bot rules preventing automatic asyncua major/minor upgrades; consolidate Dependabot and Renovate ownership to one bot.
- [x] Pass `workflow_dispatch` values through environment variables rather than interpolating them into shell commands or Docker tags.
- [x] Correct stale CI and coverage comments identified by the review.
- [x] Verify phase: pass the applicable checks in the Passing bar; run focused regression tests; inspect compose, nginx, published-image build configuration, and workflow changes for safe defaults.

## [ ] Phase 2 — Short-term performance, robustness, and CI (weeks)

- [x] Batch application-layer reads for current values and history to eliminate per-component OPC UA round trips.
- [x] Add build-time indexes for model-node lookup and object-type lookup instead of repeated linear scans.
- [x] Produce a stable content token during model construction and use it instead of serializing the full context on cache misses.
- [x] Coordinate lazy builds and refreshes through one in-flight build pipeline; prevent stale results from replacing newer revisions and use one cache-invalidation policy.
- [x] Represent read failures distinctly from missing values; do not turn transport failures into `GoodNoData`.
- [ ] Record an observable sequence gap when subscription notifications are dropped.
- [ ] Add per-client admission/resource controls for subscriptions and MCP streams; cap response sizes, history ranges, base64 decode sizes, and stream queues; enable the HTTP limiter by default when deployed behind a trusted proxy.
- [ ] Add real asyncua-server contract tests for critical transport paths, including write access, variant types, method calls, malformed metadata, browse/handle reconciliation, and reconnect resync.
- [ ] Unify the divergent OPC UA test fakes and add assertions that validate response bodies and behavior, not only status codes or method presence.
- [ ] Replace wall-clock sleeps/deadlines in flake-prone tests with injected clocks or deterministic synchronization.
- [ ] Add a parity test between `.env-example` and `Settings`; document supported log-format configuration and align template/compose values with actual defaults.
- [ ] Exercise the HTTPS/auth deployment stack in CI and run conformance against that secured stack, in addition to the current fixture-server check.
- [ ] Remove the duplicate CodeQL workflow while retaining the required language coverage.
- [ ] Pin GitHub Actions to immutable SHAs; narrow workflow permissions, scope `id-token` to the job that needs it, and add concurrency control to the quality workflow.
- [ ] Add package build and metadata validation (`uv build` plus metadata checks).
- [ ] Add frontend lint, type-check, test, and dependency-audit gates.
- [ ] Pin Docker base images by digest and pin the `uv` installation in the Dockerfile.
- [ ] Replace hot-path `BaseHTTPMiddleware` with pure ASGI middleware; add in-flight, shed, limiter, and streaming-duration metrics.
- [ ] Retain handles for fire-and-forget tasks and surface failures; replace deprecated event-loop acquisition with `asyncio.get_running_loop()`.
- [ ] Add an interpreter compatibility job for Python 3.13 against asyncua/uvicorn support claims.
- [ ] Verify phase: pass the applicable checks in the Passing bar; confirm targeted tests fail before their corresponding fixes and pass afterward; benchmark batched reads and indexed lookups against representative cases.

## [ ] Phase 3 — Medium-term architecture and deployment (months)

- [ ] Make the application layer framework-free and remove asyncua dependencies from schemas; extend import-linter contracts to prevent both layer leaks.
- [ ] Split `GatewayState` by ownership (runtime tasks, presentation caches, and MCP state) and expose intentional public interfaces instead of cross-module private access.
- [ ] Establish and enforce one module-boundary convention for subscription internals, consistent with the OPC UA adapter seam checks.
- [ ] Add Kubernetes manifests or Helm deployment artifacts with model/upstream readiness, resource requests/limits, and a PodDisruptionBudget.
- [ ] Reduce subscription contention with lock sharding, batched releases, and node-based refresh deduplication.
- [ ] Paginate `GET /v1/objects`, cap history ranges, and set a realistic gzip minimum size. (postponed by maintainer! see i3X Spec. 1.0 and/or vNext)
- [ ] Extend mypy coverage to scripts, the conformance server, samples, and root-level runners.
- [ ] Add `.venv-conformance/` and `.import_linter_cache/` to the repository `.gitignore`.
- [ ] Verify phase: pass the applicable checks in the Passing bar; validate deployment probes and resource settings; run focused tests and load/regression checks for pagination and subscription changes.

## [ ] Phase 4 — Long-term scalability and event-driven operations

- [ ] Design and implement shared/distributed subscription state so multiple workers or instances can operate safely; document the selected state-store trade-offs and migration path.
- [ ] Validate multi-worker/multi-instance behavior and benchmark against the measured single-worker baseline before changing the shipped worker count.
- [ ] Reduce model memory use with measured options such as slotted model nodes, shared indexes, or incremental refresh; measure peak memory during rebuilds at representative model sizes.
- [ ] Replace polling fallback paths with an event-driven outbox where appropriate.
- [ ] Add alerts for upstream disconnection, model-build failures, and dropped updates, with operational runbooks for response.
- [ ] Verify phase: pass the applicable checks in the Passing bar; demonstrate horizontal scaling without state inconsistency, document benchmark and memory results, and exercise alert conditions in tests or staging.