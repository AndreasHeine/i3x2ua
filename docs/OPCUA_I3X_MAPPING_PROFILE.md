# OPC UA to i3X Mapping Profile

This profile defines how OPC UA references are translated into i3X references.
It is intended to keep implementations interoperable while still allowing implementation-specific i3X element IDs.

## Goals

- Keep i3X identifiers stable and queryable.
- Keep OPC UA provenance explicit and lossless.
- Ensure i3X conformance checks can validate namespace and type relationships.

## Canonical Rules

1. Object instance `elementId`
- Implementation-owned stable identifier.
- Must be unique within the i3X address space.

2. Object instance `typeElementId`
- The i3X type reference used by clients.
- Must resolve to an entry in `GET /objecttypes`.
- For OPC UA Variables in this implementation, this is derived from the Variable `DataType` and normalized to expanded NodeId form (`nsu=...;i=...`, `nsu=...;s=...`, ...).

3. Object instance metadata `sourceTypeId`
- Provenance reference to the source-system type identity.
- For OPC UA, this is derived from `TypeDefinition` when available, otherwise from source node identity.
- Always normalized to expanded NodeId form when possible.

4. Object instance metadata `typeNamespaceUri`
- Namespace URI of the type definition that `typeElementId` points to.
- Canonicalized to the URI spelling returned by `GET /namespaces` (for example trailing slash normalization).

5. ObjectType `namespaceUri`
- Must match a declared namespace from `GET /namespaces`.
- Namespace URI comparisons are normalized by case-insensitive compare and trailing slash insensitivity, then rewritten to declared canonical spelling.

6. ObjectType `sourceTypeId`
- Source namespace member identifier for the type (OPC UA provenance).
- Kept distinct from ObjectType `elementId`.

7. Unknown or unresolved types
- If a referenced `typeElementId` cannot be resolved from discovered object types:
  - Attempt to synthesize a datatype ObjectType from source metadata.
  - If still unresolved, create an `UnknownType` placeholder ObjectType whose `elementId` equals the unresolved `typeElementId`.
- Placeholder `namespaceUri` is assigned to a declared namespace to preserve conformance expectations.

## Why this profile exists

Different servers may choose different i3X `elementId` values for the same OPC UA source model.
Interoperability depends on reference consistency, not on identical string formats.

This profile ensures clients can always:

- resolve object `typeElementId` values,
- map type provenance through `metadata.sourceTypeId`, and
- correlate types to declared namespaces.
