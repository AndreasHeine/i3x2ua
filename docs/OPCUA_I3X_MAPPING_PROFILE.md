# OPC UA to i3X Mapping Profile

This document describes the current mapping contract used by the i3X2UA model
builder. It defines how OPC UA nodes and references are translated into i3X
objects while keeping implementations interoperable and provenance explicit.

## Goals

- Keep i3X identifiers stable and queryable.
- Keep OPC UA provenance explicit and lossless.
- Ensure i3X conformance checks can validate namespace and type relationships.
- Keep the model valid even when no semantic profile matches a namespace.
- Prefer generic fallback mapping first, then apply semantic profile overrides.

## Implementation Status

The current implementation already applies the following behavior:

- deterministic `elementId` generation from OPC UA source node ids
- `parent_id`, `is_composition`, `semantic_role`, and `mapping_confidence` on
  mapped i3X nodes
- bidirectional relationships in the `relationships` map
- namespace-aware profile matching with built-in generic and machinery profiles
- hierarchy-first parent selection with graph fallback for non-structural edges

The code currently ships with built-in profiles. External profile loading can be
added later without changing the mapping contract described here.

## Canonical Rules

1. Object instance `elementId`
- Implementation-owned stable identifier.
- Must be unique within the i3X address space.
- Generated as `<kind>-SHA1(ExpandedNodeId)[:16]`, where the ExpandedNodeId
  uses the namespace URI (`nsu=...`) rather than a session-specific namespace
  index (`ns=N`).

2. Object instance `typeElementId`
- The i3X type reference used by clients.
- Should resolve to an entry in `GET /objecttypes` when the type catalog is
  available.
- For OPC UA Variables in this implementation, this is derived from the Variable `DataType` and normalized to expanded NodeId form (`nsu=...;i=...`, `nsu=...;s=...`, ...).

3. Object instance metadata `sourceTypeId`
- Provenance reference to the source-system type identity.
- For OPC UA, this is derived from `TypeDefinition` when available, otherwise from source node identity.
- Always normalized to expanded NodeId form when possible.

4. Object instance metadata `namespaceUri`
- Namespace URI of the source node.
- Canonicalized from the namespace table returned by `GET /namespaces` when
  available.

5. Object instance metadata `typeNamespaceUri`
- Namespace URI of the type definition that `typeElementId` points to.
- Canonicalized to the URI spelling returned by `GET /namespaces` (for example trailing slash normalization).

6. ObjectType `namespaceUri`
- Must match a declared namespace from `GET /namespaces`.
- Namespace URI comparisons are normalized by case-insensitive compare and trailing slash insensitivity, then rewritten to declared canonical spelling.

7. ObjectType `sourceTypeId`
- Source namespace member identifier for the type (OPC UA provenance).
- Kept distinct from ObjectType `elementId`.

8. Unknown or unresolved types
- If a referenced `typeElementId` cannot be resolved from discovered object types:
  - Attempt to synthesize a datatype ObjectType from source metadata.
  - If still unresolved, create an `UnknownType` placeholder ObjectType whose `elementId` equals the unresolved `typeElementId`.
- Placeholder `namespaceUri` is assigned to a declared namespace to preserve conformance expectations.

9. Object instance `semantic_role`
- Current values: `asset | group | component | datapoint | property | unknown`.
- Resolved from profile overrides first, then generic fallback rules.

10. Object instance `mapping_confidence`
- Current values: `high | medium | low`.
- Used as an explainability hint for profile-driven and generic mappings.

11. Object instance `appliedProfileIds`
- Ordered list of profile ids that matched the source node.
- The built-in `generic` profile is always available as a fallback.

## Why this profile exists

Different servers may choose different i3X `elementId` values for the same OPC UA source model.
Interoperability depends on reference consistency, not on identical string formats.

This profile ensures clients can always:

- resolve object `typeElementId` values,
- map type provenance through `metadata.sourceTypeId`, and
- correlate types to declared namespaces.

### OPC UA → i3X Relationship Mapping

The model builder maps OPC UA reference types to i3X relationship planes.
Classification is ancestry-root-first and uses recursively resolved supertypes
(following `HasSubtype` inverse for `ReferenceType` nodes). If a lineage is
ambiguous and contains both hierarchical and non-hierarchical roots, the
non-hierarchical root wins and the edge is mapped to Graph.

Classification order:

1. Resolve normalized tokens from reference type id, browse name, and supertype browse names.
2. If lineage contains `NonHierarchicalReferences`, map to Graph.
3. Else if lineage contains `HierarchicalReferences`, map inside the hierarchical family:
   - `HasProperty` lineage maps to Composition.
   - `HasComponent` / `HasOrderedComponent` lineage maps by target NodeClass:
     - target `Variable` -> Composition
     - target non-`Variable` -> Hierarchy
   - other hierarchical lineage maps to Hierarchy.
4. `HasTypeDefinition` and `HasSubtype` map to type metadata only.
5. If roots are unavailable, apply id/name compatibility fallback.
6. If still unresolved, map deterministically to Graph.

Current implementation detail:

- profile rules can override the generic hierarchy/composition/graph mapping for
  known namespace patterns
- graph relationships do not participate in parent selection
- hierarchy selection remains deterministic and uses priority order
  `Organizes > HasComponent > HasOrderedComponent`

Precedence rule for ambiguous or malformed lineages:

- If both `NonHierarchicalReferences` and `HierarchicalReferences` appear, `NonHierarchicalReferences` wins and the edge is mapped to Graph.

| OPC UA Reference | i3X Relationship Plane | i3X Types |
|---|---|---|
| `HierarchicalReferences`, `Organizes` and recursively resolved subtypes | Hierarchy | `HasChildren` / `HasParent` |
| `HasComponent`, `HasOrderedComponent` and recursively resolved subtypes | target `Variable`: Composition; target non-`Variable`: Hierarchy | Composition: `HasComponent` / `ComponentOf`; Hierarchy: `HasChildren` / `HasParent` |
| `HasProperty` and recursively resolved subtypes | Composition | `HasComponent` / `ComponentOf` |
| `NonHierarchicalReferences` and recursively resolved subtypes | Graph | custom label |
| `HasTypeDefinition` | Type metadata only | - |
| `HasSubtype` | Type metadata only | - |

If a reference type cannot be classified after lineage root checks and fallback
id/name checks, it is mapped deterministically to the Graph plane.

`POST /objects/value` and `POST /objects/history` recurse through **composition**
children only when `maxDepth > 1`. Hierarchy-only children are never included in
value recursion.

## Current Output Shape

### Scalar Datatypes in ObjectType JSON Schemas

Standard OPC UA scalar datatypes are resolved using their namespace-zero
identity. Compact (`i=26`), indexed (`ns=0;i=26`), expanded
(`nsu=http://opcfoundation.org/UA/;i=26`), and named (`Number`) representations
produce the same scalar schema.

- `Number`, `Float`, and `Double` map to JSON Schema `number`.
- `Integer`, `UInteger`, concrete integer types, and `Enumeration` map to `integer`.
- `Boolean` maps to `boolean`; `DateTime` maps to `string` with `date-time` format.
- Declared arrays apply the scalar mapping to their `items`.

Numeric identifiers in custom namespaces must not be interpreted as built-in
datatypes merely because their identifier matches a namespace-zero type.
Missing or unresolved scalar datatypes use an unconstrained schema (`{}`),
accepting any JSON value, including null. Known string datatypes remain `string`.
Arrays of unresolved datatypes retain `type: array` with `items: {}`. Member
metadata and mandatory modelling rules are preserved. Unconstrained datatype
schemas are not wrapped in nullable/array `oneOf` branches, which would overlap.
Custom datatype ancestry resolution is not provided by this scalar mapper.

The mapped i3X object currently carries the following fields in code:

- `id`
- `name`
- `kind`
- `type`
- `children`
- `source_node_id`
- `source_type_id`
- `parent_id`
- `is_composition`
- `semantic_role`
- `mapping_confidence`
- `relationships`
- `metadata`

The `metadata` map is currently used to preserve:

- `uaNodeId`
- `uaTypeNodeId`
- `namespaceUri`
- `appliedProfileIds`

## maxDepth Semantics (Hierarchy vs Composition)

Value/history recursion follows only the composition plane (`HasComponent` /
`ComponentOf`). Hierarchy edges (`HasChildren` / `HasParent`) are used for
structure/navigation and root selection, not for value recursion.

| Request | Traversal behavior |
|---|---|
| `maxDepth = 1` | No recursion. Return only the requested element's value/history payload. |
| `maxDepth = 2` | Include direct composition children only. |
| `maxDepth = n (>1)` | Include composition descendants up to depth `n-1` from the root query element. |
| `maxDepth = 0` | Unlimited recursion across composition descendants. |

Example:

- Hierarchy: `Plant -> Line -> Pump`
- Composition: `Pump -> Temperature`, `Pump -> Pressure`

`POST /objects/value` for `Plant` with `maxDepth = 0` does **not** include `Pump`
values via hierarchy.
`POST /objects/value` for `Pump` with `maxDepth = 0` includes `Temperature` and
`Pressure` as `components`.

`POST /objects/related` returns relationships across all three planes (hierarchy,
composition, and graph) for each requested element.

## Write Validation

Both current-value write routes and the bulk historical write route validate
values against the target's published i3X ObjectType JSON Schema before checking
permissions or submitting a mutation, including before accepting a current-value
no-op. Constraints include enums, bounds, required fields, formats, and declared
nullability. OPC UA type/access checks still apply after schema validation.
Element IDs must be nonempty and printable, without surrounding whitespace.

Schema discovery uses the same type registry as object/type endpoints. Missing,
malformed, or unresolvable schemas fail explicitly with a 502 error; schema-invalid
values fail with 400. Local schema references are supported, but external references
are rejected and schemas are never downloaded during validation. Bulk routes
report these failures per item.

## Historical Updates

`PUT /v1/objects/history` accepts `{"updates": [{"elementId": "...", "value":
{"value": 19.5, "quality": "Good", "timestamp": "2026-01-01T10:00:00Z"}}]}`.
All three VQT fields are required. Timestamps must be RFC 3339 UTC with a `Z`
suffix (case-insensitive); timezone offsets, including `+00:00`, are rejected.
Extra fractional digits beyond microsecond precision are accepted only when all
are zero, preserving the instant exactly without rounding or substituting the
current time. Null values require `Bad` or `GoodNoData`, and both qualities require
null. Null is accepted only if the published ObjectType schema permits it.

With `I3X_ENABLE_WRITES=1`, each Variable-backed property is mapped to
`UpdateDataDetails(NodeId=..., PerformInsertReplace=Update)`. The typed value,
quality StatusCode, and source timestamp are written to the historian only;
the current Value attribute is not modified. No composition traversal is performed.
Built-in Boolean, numeric, String, DateTime, Guid, and base64 ByteString values
are supported. Arrays must match ValueRank and not exceed declared ArrayDimensions
maximum lengths; zero means an unknown maximum. The optional ArrayDimensions
attribute may be absent.
Custom structured values are rejected explicitly.

Both AccessLevel and UserAccessLevel must include `HistoryWrite`, independently
of `CurrentWrite` and `Historizing`. Each submitted entry produces a bulk result
in request order, including repeated element IDs. Success has `result: null`;
failures include `responseDetail`. An explicit `BadServiceUnsupported` from
HistoryUpdate returns HTTP 501 with ordered item results and top-level
`responseDetail` if no earlier item succeeded; subsequent eligible entries are
not sent upstream again. If earlier items succeeded, the HTTP 200 mixed bulk
response is preserved. Node-specific `BadHistoryOperationUnsupported` and
`BadNotImplemented` produce item-level 501 errors; `BadNotWritable` and user-access
denial produce 403 errors and do not establish service-wide unavailability.
Both the overall HistoryUpdate status and per-value OperationResults are checked.

Bulk updates are not atomic and historical mutations are not automatically
retried: a timeout may occur after the historian applied an update. Clients
should verify history before deciding whether to resubmit an ambiguous failure.
The gateway does not add a historian or modify the upstream server. In particular,
the standard asyncua 2.1.0 server still rejects HistoryUpdate.

## Practical Notes for Implementers

- Keep the generic fallback path active at all times.
- Treat profile matches as enrichments, not as hard requirements.
- Preserve deterministic ordering when multiple parents or multiple profiles are
  available.
- Do not drop graph edges just because a structural parent was already selected.
- If a new profile is added later, it should only extend the contract, not change
  the meaning of the current fields.
