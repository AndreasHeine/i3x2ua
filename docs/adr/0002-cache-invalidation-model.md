# ADR 0002: Cache Invalidation Model

- Status: Accepted
- Date: 2026-06-16

## Context

The server maintains derived model/cache artifacts (model_cache, object type context cache, namespace metadata views) that improve performance. Unclear invalidation timing risks stale responses and difficult debugging.

## Decision

Adopt explicit invalidation boundaries:

1. Startup preload builds initial model cache when configured.
2. Runtime reads consume cached model data by default.
3. Cache invalidation is explicit and event-driven, not implicit per-request.
4. Namespace and object-type metadata caches are invalidated when model rebuild occurs.
5. Subscription update payload generation must not mutate canonical cache structures.

## Consequences

Positive:

- predictable cache behavior
- better performance stability
- easier incident triage for stale-data reports

Trade-offs:

- explicit invalidation requires discipline and clear trigger points
- rebuild operations can be expensive and should be observable

## Follow-up

Introduce cache metrics/log tags for invalidate/rebuild events to improve operations visibility.
