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
- `GET /objecttypes`
- `GET /objects?includeMetadata=false`

These endpoints are not part of the strict i3X core contract in this project. They are helper/compatibility endpoints for OPC UA transparency, frontend integration, and debugging.

## What `/namespaces` Should Return from OPC UA

The endpoint should return namespace entries derived from OPC UA namespace information in index order.

Expected behavior:

- Preserve order exactly as defined by the OPC UA server (`ns=0`, `ns=1`, ...).
- Return stable URI strings that can be used to interpret NodeIds (`uri`).
- Return a readable namespace label (`displayName`) preferably from OPC UA namespace metadata (`i=11715`), with a deterministic fallback strategy.
- Handle unavailable namespace reads with a clear provider error response.

Example response:

```json
[
  {
    "uri": "http://opcfoundation.org/UA/",
    "displayName": "OPC UA"
  },
  {
    "uri": "urn:vendor:server",
    "displayName": "Vendor"
  }
]
```

## What `/objecttypes` and `/objects` Return

- `GET /objecttypes`: Top-level array of object type entries from OPC UA ObjectTypes hierarchy.
- `GET /objects`: Compatibility projection used by frontend consumers, exposing `elementId`, `displayName`, `namespaceUri`, and `schema`.

## Why This Matters for i3X Mapping

Even though `/namespaces` is optional, NamespaceArray visibility improves:

- NodeId traceability during mapping and debugging
- Interoperability checks between server instances
- Deterministic interpretation of namespaced identifiers

## Current Project Status

Implemented and available:

- Core i3X endpoints for model, data, and action
- Optional/helper endpoints: `GET /namespaces`, `GET /objecttypes`, `GET /objects`
