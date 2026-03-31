# i3X Conformance Notes

## Purpose

This note separates strict i3X API requirements from optional helper endpoints in this OPC UA backed provider.

## Core i3X Endpoints (Required)

The core surface in this project is:

- `GET /model`
- `GET /model/{id}`
- `GET /model/{id}/children`
- `GET /data/{propertyId}`
- `POST /data/query`
- `POST /action/{actionId}/invoke`

These endpoints represent the required functional contract for model, data access, and action invocation.

## Optional or Diagnostic Endpoints

Endpoints outside the core contract can be exposed for diagnostics and interoperability support.

- `GET /namespaces`

`/namespaces` is not part of the i3X core contract in this project. It is a helper endpoint for OPC UA transparency and debugging.

## What `/namespaces` Should Return from OPC UA

The endpoint should return the OPC UA `NamespaceArray` in index order.

Expected behavior:

- Preserve order exactly as defined by the OPC UA server (`ns=0`, `ns=1`, ...).
- Return stable URI strings that can be used to interpret NodeIds.
- Handle unavailable namespace reads with a clear provider error response.

Example response:

```json
{
  "count": 3,
  "items": [
    "http://opcfoundation.org/UA/",
    "urn:vendor:server",
    "http://example.org/custom"
  ]
}
```

## Why This Matters for i3X Mapping

Even though `/namespaces` is optional, NamespaceArray visibility improves:

- NodeId traceability during mapping and debugging
- Interoperability checks between server instances
- Deterministic interpretation of namespaced identifiers

## Current Project Status

Implemented and available:

- Core i3X endpoints for model, data, and action
- Optional diagnostic endpoint: `GET /namespaces`
