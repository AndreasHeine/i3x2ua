# ADR 0001: App State Strategy

- Status: Accepted
- Date: 2026-06-16
- Updated: 2026-11-13 (typed state container; PR-10)

## Context

The FastAPI application maintains runtime collaborators (OPC UA client, model builder, subscription service, caches) that must be initialized at startup and shared across route handlers.

Without a consistent strategy, collaborators are re-created in handlers, state ownership becomes unclear, and lifecycle cleanup is fragile.

Starlette's `app.state` is an untyped bag: every attribute was read with `getattr(app.state, "...", default)`, so a typo or a missing field silently degraded instead of failing, and mypy could not check the access paths.

## Decision

Use a centralized, typed app state strategy:

1. All runtime collaborators live in the typed container `GatewayState` (`i3x_server/application/state.py`): every field is declared, so a missing attribute is an `AttributeError`, not a silent fallback, and mypy checks the access paths.
2. The composition root owns construction and lifecycle: `create_app` installs the container with `install_gateway_state(app, GatewayState())` before middleware wiring, and the lifespan fills the collaborator fields (`opcua_client`, `model_builder`, `subscription_service`, MCP fields, tasks).
3. Readers use `gateway_state(request_or_app)` and dependency providers in `i3x_server/application/dependencies.py` (plus `application/services/model_cache.py`); routes depend on `application.dependencies` directly - `bootstrap` is the top layer, so presentation never imports it (enforced by the `.importlinter` layered contract).
4. Keep route handlers thin and avoid direct constructor wiring in handlers.
5. Ensure shutdown cleanup for clients/services/tasks in lifespan finalization.

The container deliberately does not subclass Starlette's `State`: it is a plain `@dataclass(slots=True)` wired into `app.state` by `install_gateway_state`. Collaborators are reached through their ports (`OpcUaClientProtocol`, `SubscriptionServicePort`); MCP-owned shapes (tool definitions, transport sessions) stay opaque in the container.

## Consequences

Positive:

- predictable startup/shutdown behavior
- explicit ownership of runtime resources
- easier test fixture injection and mocking (tests install a `GatewayState` directly)
- static typing covers every state access

Trade-offs:

- the container is still mutable and requires discipline
- readers go through `gateway_state(...)` instead of raw `app.state` attribute access (one helper, not an adapter per field)

## Follow-up

None for state access; decomposition of lifecycle helpers is tracked in the fix plan (PR-11).
