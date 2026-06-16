# ADR 0003: MCP Protocol Support Policy

- Status: Accepted
- Date: 2026-06-16

## Context

MCP support is optional and evolving, but must remain stable for supported capabilities (tools, prompts, resources, roots, JSON-RPC). Uncontrolled changes can break clients unexpectedly.

## Decision

Define MCP support policy:

1. MCP is feature-gated by I3X_ENABLE_MCP.
2. When disabled, MCP routes are absent from OpenAPI and endpoint behavior remains deterministic.
3. JSON-RPC request validation remains strict (jsonrpc=2.0 and proper error shapes).
4. Supported capability surfaces (tools/prompts/resources/roots) are treated as contract surfaces and require tests for behavior changes.
5. Backward-compatible route and method evolution is preferred; breaking behavior requires explicit versioning or migration notes.

## Consequences

Positive:

- clear expectations for MCP consumers
- safer evolution with contract-driven tests
- easier release communication

Trade-offs:

- stricter compatibility bar can slow rapid protocol experimentation

## Follow-up

Publish explicit MCP compatibility notes in release changelog when behavior or capability declarations change.
