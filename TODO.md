# TODO i3x2ua

This file contains only open or partially completed work items.

Details about implemented items are listed in the status section of `README.md`.

## Partially Completed

- [ ] Add integration tests with a real OPC UA server (manually validated so far, but not yet automated)
- [ ] Add structured logs and correlation IDs for `/v1` requests

## Open Beta Core Features

- [ ] Move `POST /v1/objects/history` from `501` to functional implementation (if required)
- [ ] Move `GET /v1/objects/{elementId}/history` and `PUT /v1/objects/{elementId}/history` from `501` to functional implementation (if required)
- [ ] Implement `PUT /v1/objects/{elementId}/value` if write access is required in the target system
- [ ] Align history/update capabilities in `GET /v1/info` with actual runtime support
- [ ] Harden error formatting against the Beta schema
- [ ] Complete OpenAPI documentation with examples and error cases

## Optional Features from Requirements

- [ ] Extend history read/write features for objects and values
- [ ] Authorization model for access control
- [ ] OPC-UA User Authentication (Client-Auth)
- [ ] Multi-server support (multiple OPC UA backends)
- [ ] Provide Dockerfile and optional Docker Compose setup

## Security and Operations

- [ ] Enable TLS for the REST API (certificate configuration, secure defaults)
- [ ] Make OPC UA SecurityModes Sign / SignAndEncrypt configurable and tested
- [ ] Provide health endpoints (e.g., `/health`, `/ready`) for operations and monitoring
- [ ] Expand configurable caching strategies (TTL, invalidation, refresh strategy)

## Documentation and Delivery

- [ ] Expand operational documentation (deployment, security, monitoring, troubleshooting)
- [ ] Provide configuration examples for dev/test/prod
- [ ] Map acceptance criteria to a testable checklist in automated tests
