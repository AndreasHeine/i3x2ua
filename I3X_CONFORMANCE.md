# i3X Beta Conformance Notes

## Purpose

This note documents the current Beta contract exposed by this repository under `/v1`.

The server is intentionally limited to the OpenAPI-described Beta surface. Legacy root routes are not part of the contract.

## Active Beta Endpoints

Implemented and available:

- `GET /v1/info`
- `GET /v1/namespaces`
- `GET /v1/objecttypes`
- `POST /v1/objecttypes/query`
- `GET /v1/objects`
- `POST /v1/objects/list`
- `POST /v1/objects/related`
- `POST /v1/objects/value`
- `POST /v1/subscriptions`
- `POST /v1/subscriptions/register`
- `POST /v1/subscriptions/unregister`
- `POST /v1/subscriptions/sync`
- `POST /v1/subscriptions/list`
- `POST /v1/subscriptions/delete`
- `POST /v1/subscriptions/stream`

## Behavior Notes

- `GET /v1/info` returns a server capability summary with `specVersion`, version metadata, and query/update/subscribe capability flags.
- `GET /v1/namespaces` returns OPC UA namespace entries in server order.
- `GET /v1/objecttypes` and `POST /v1/objecttypes/query` expose object type projections derived from OPC UA ObjectTypes.
- `GET /v1/objects`, `POST /v1/objects/list`, `POST /v1/objects/related`, and `POST /v1/objects/value` expose the Beta object explorer surface.
- Subscription lifecycle endpoints create, register, unregister, sync, list, delete, and stream subscriptions.
- Registered monitors use OPC UA data-change subscriptions when server limits allow; if limits are exceeded, the server falls back to batched polling.
- Non-implemented Beta operations return structured `501` responses instead of disappearing as `404`.

## Contract Scope

The following are not part of the active contract in this repository:

- root-level `/model`, `/data`, `/action` routes
- any non-`/v1` helper routes

## Current Project Status

Implemented and available:

- Beta API under `/v1`
- Structured error and bulk-response envelopes for implemented Beta routes
- OPC UA-backed namespace, object type, and object projections
- Subscription cache and sequence-based sync updates for monitored OPC UA node values
