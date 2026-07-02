# ADR 0004: V1 API Ownership Model

- Status: Accepted
- Date: 2026-07-02

## Context

The original v1 API implementation lived in a single monolithic route module. As the API grew, that structure mixed unrelated concerns:

- HTTP route registration
- request and response contracts
- object and object-type graph helpers
- serialization helpers
- compatibility behavior used by tests

That made the route layer harder to navigate, increased merge pressure, and encouraged route modules to share private helpers implicitly through a single file.

## Decision

Adopt a service-centric orchestration model with presentation-local helper modules.

### Ownership rules

1. Route modules under `i3x_server/api/v1/*_routes.py` own HTTP adaptation only:
- request parsing
- dependency injection
- response shaping
- protocol-specific status handling

2. Application services under `i3x_server/application/services/*` own multi-step workflow orchestration and business-facing behavior.

3. Shared API request/response models and generic bulk-response helpers live in `i3x_server/api/v1/contracts.py`.

4. Presentation-local helper logic that is HTTP-adjacent but not business orchestration lives in focused helper modules:
- `object_helpers.py`
- `objecttype_helpers.py`
- `common_helpers.py`

5. `i3x_server/api/v1/__init__.py` is the only router aggregator for v1.

6. `i3x_server/api/v1/monolithic.py` becomes a compatibility-oriented module only:
- transitional re-exports
- small compatibility shims still needed by tests or downstream imports
- no new route ownership

## Consequences

Positive:

- lower coupling between unrelated v1 features
- clearer separation between transport adaptation and orchestration
- easier targeted testing for object, object-type, value, and subscription behavior
- smaller future refactor surface for API evolution

Trade-offs:

- more modules to navigate
- temporary compatibility layer remains until tests/imports stop targeting `monolithic.py`
- helper boundaries inside presentation must still be curated to avoid recreating a hidden monolith

## Follow-up

1. Keep moving direct imports away from `monolithic.py` when practical.
2. Prefer adding new shared DTOs to `contracts.py` instead of route modules.
3. Prefer adding new presentation-local helpers to focused helper modules instead of `monolithic.py`.