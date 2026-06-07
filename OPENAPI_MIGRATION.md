# OpenAPI Migration Notes

## Goal

This document compares the current implementation in this repository with the provided `openapi.json` contract and outlines the migration impact.

Use this as the decision base for:
- adapting the current backend to the Beta API contract,
- or keeping the current implementation and accepting a contract break.

## High-Level Conclusion

The current implementation is **not OpenAPI-compatible** with the supplied `openapi.json`.

The main reasons are:
- different base path handling (`/v1` in OpenAPI, no version prefix in current app),
- different endpoint surface,
- different response envelopes and payload shapes,
- different semantic model for objects/types/relationships,
- missing update/query/subscription endpoints that the OpenAPI contract requires.

In short: this is not a small patch. It is a contract migration.

## Current Implementation vs OpenAPI Contract

### 1. Base Path and Routing

OpenAPI:
- server base URL: `/v1`

Current implementation:
- mounted at root (`/`)

Impact:
- every OpenAPI path is expected under `/v1/...`
- current frontend or client generated from `openapi.json` will not match the current root routes

### 2. Core Endpoint Surface

#### OpenAPI required endpoints not implemented as-is

The provided OpenAPI contract defines many endpoints that are not present in the current app:

- `GET /v1/info`
- `GET /v1/namespaces`
- `GET /v1/objecttypes`
- `POST /v1/objecttypes/query`
- `GET /v1/relationshiptypes`
- `POST /v1/relationshiptypes/query`
- `GET /v1/objects`
- `POST /v1/objects/list`
- `POST /v1/objects/related`
- `POST /v1/objects/value`
- `POST /v1/objects/history`
- `GET /v1/objects/{elementId}/history`
- `PUT /v1/objects/{elementId}/history`
- `PUT /v1/objects/{elementId}/value`
- `POST /v1/subscriptions`
- `POST /v1/subscriptions/register`
- `POST /v1/subscriptions/unregister`
- `POST /v1/subscriptions/stream`
- `POST /v1/subscriptions/sync`
- `POST /v1/subscriptions/delete`
- `POST /v1/subscriptions/list`

Current implementation provides instead:
- `GET /namespaces`
- `GET /objecttypes`
- `GET /objects`
- `GET /model`
- `GET /model/{id}`
- `GET /model/{id}/children`
- `GET /data/{propertyId}`
- `POST /data/query`
- `POST /action/{actionId}/invoke`

These are not equivalent to the supplied contract.

### 3. Response Shape Mismatches

#### `/namespaces`

OpenAPI expects a wrapped success response and the payload shape is part of the contract.

Current implementation returns:
- top-level array of objects
- fields: `uri`, `displayName`

This is semantically useful, but not guaranteed to match the OpenAPI success wrapper or exact schema.

#### `/objecttypes`

OpenAPI expects:
- OpenAPI query parameter `namespaceUri`
- schema-oriented object type responses
- queryable by namespace
- bulk query endpoint at `/objecttypes/query`

Current implementation returns:
- top-level array of `{ node_id, parent_node_id, browse_name, display_name }`
- no `namespaceUri` filter
- no query endpoint
- no schema payload

#### `/objects`

OpenAPI expects:
- `GET /objects` with query params such as `typeElementId`, `includeMetadata`, `root`
- `POST /objects/list`, `/objects/related`, `/objects/value`, `/objects/history`
- resource-by-id paths for history/value updates

Current implementation returns:
- a top-level array of compatibility projections
- fields: `elementId`, `displayName`, `namespaceUri`, `schema`
- only `includeMetadata` is partially supported
- no list/related/value/history semantics

### 4. Semantic Model Mismatch

The OpenAPI contract is not just a transport contract. It defines a richer semantic model:

- namespaces are first-class data
- object types are schema-bearing types
- objects can be queried by relationship, history, and value
- related objects and recursive value expansion are explicit contract features
- subscriptions are first-class and client-managed

The current implementation is an OPC-UA-backed provider with a much smaller, simpler model:
- model graph
- values
- actions
- diagnostic object type and namespace endpoints

### 5. Subscription and Update Model Gap

OpenAPI includes a full subscription lifecycle:
- create
- register
- unregister
- stream (SSE)
- sync
- delete
- list

Current implementation:
- no subscription persistence layer
- no SSE stream endpoint
- no subscription state model
- no change notification pipeline from OPC UA subscriptions to OpenAPI subscription semantics

This is a major architectural gap.

### 6. Error Contract Gap

OpenAPI describes a structured error response contract with a wrapper such as:
- `success: false`
- `error.code`
- `error.message`

Current implementation:
- returns FastAPI-style errors via `HTTPException`
- detail payloads are custom and not guaranteed to match the supplied OpenAPI envelope exactly

### 7. Versioning Gap

OpenAPI contract assumes:
- `/v1/...`

Current implementation:
- no version prefix

This affects:
- generated clients
- frontend requests
- reverse proxies
- future versioning strategy

## Migration Options

### Option A: Adapt the current backend to the OpenAPI contract

This means:
- mount all endpoints under `/v1`
- add `GET /info`
- replace `/objecttypes` and `/objects` with the OpenAPI contract shape
- implement `relationshiptypes`
- implement list/query/bulk endpoints
- implement subscriptions and SSE
- normalize errors to the OpenAPI error envelope
- preserve compatibility for model/data/action only if you intentionally keep them as internal helpers or aliases

Pros:
- existing frontend/client generated from `openapi.json` should work
- contract-first development becomes possible

Cons:
- substantial rewrite
- likely touches routing, schemas, data model, caching, subscriptions, and error handling

### Option B: Keep the current backend and treat it as a separate contract

This means:
- keep current routes and payloads
- document that this service is an OPC-UA backed provider with a different contract
- do not try to conform to the supplied OpenAPI Beta spec
- keep the current frontend/client in sync with this repo-specific API

Pros:
- less work in the short term
- preserves current working implementation

Cons:
- generated clients from `openapi.json` will continue to fail
- frontend and backend remain contract-divergent
- harder to integrate with tools that expect the Beta API

## Recommended Migration Strategy

If you want to align with the supplied `openapi.json`, do it in phases:

### Phase 1: Contract alignment foundation
- add `/v1` prefix
- add `GET /info`
- define shared error envelope
- define OpenAPI-compatible response wrappers
- make `/namespaces` and `/objecttypes` match the expected array/object payloads exactly

### Phase 2: Object model alignment
- implement `/objects` as the canonical object explorer endpoint
- map OPC UA ObjectTypes / Objects / Relationships to the OpenAPI object model
- add `namespaceUri`, `typeElementId`, `root`, `includeMetadata`
- add `/objects/list`, `/objects/related`, `/objects/value`, `/objects/history`

### Phase 3: Update and subscription alignment
- implement history/value update endpoints
- implement subscriptions lifecycle
- add SSE stream and sync semantics

### Phase 4: Compatibility cleanup
- keep legacy routes as temporary aliases only if needed
- update frontend clients and tests to target `/v1`
- remove contract drift endpoints after migration is complete

## Concrete Deviations Summary

### Current repo-specific endpoints
- `/model`
- `/data`
- `/action`
- `/namespaces`
- `/objecttypes`
- `/objects`

### OpenAPI Beta endpoints missing or different
- `/v1/info`
- `/v1/relationshiptypes`
- `/v1/objects/list`
- `/v1/objects/related`
- `/v1/objects/value`
- `/v1/objects/history`
- `/v1/objects/{elementId}/history`
- `/v1/objects/{elementId}/value`
- all subscription endpoints

### Response shape differences
- current top-level arrays vs OpenAPI wrappers
- current object projections vs OpenAPI schema objects
- current error format vs OpenAPI error envelope

## Decision Guidance

Choose **Option A** if:
- the Beta OpenAPI file is the authoritative contract
- clients/frontend are generated from it
- interoperability is more important than keeping the current simplified model

Choose **Option B** if:
- the current implementation is already the product contract
- the OpenAPI file is only a reference
- you want to avoid a broad rewrite

## Short Verdict

The current implementation is closer to a practical OPC-UA adapter, while the supplied OpenAPI describes a richer platform-style contract.

If you need the frontend/client to work with `openapi.json` without custom adapters, a **full contract migration** is the safer path.
