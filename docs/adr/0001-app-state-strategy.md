# ADR 0001: App State Strategy

- Status: Accepted
- Date: 2026-06-16

## Context

The FastAPI application maintains runtime collaborators (OPC UA client, model builder, subscription service, caches) that must be initialized at startup and shared across route handlers.

Without a consistent strategy, collaborators are re-created in handlers, state ownership becomes unclear, and lifecycle cleanup is fragile.

## Decision

Use a centralized app state strategy:

1. Initialize long-lived runtime collaborators in bootstrap lifespan.
2. Store collaborators on app.state.
3. Access collaborators through dependency providers in i3x_server/dependencies.py and i3x_server/application/dependencies.py.
4. Keep route handlers thin and avoid direct constructor wiring in handlers.
5. Ensure shutdown cleanup for clients/services in lifespan finalization.

## Consequences

Positive:

- predictable startup/shutdown behavior
- explicit ownership of runtime resources
- easier test fixture injection and mocking

Trade-offs:

- app.state is mutable and requires discipline
- type hints around app.state require care and adapter helpers

## Follow-up

As architecture hardens, consider typed state access wrappers to reduce stringly-typed state access patterns.
