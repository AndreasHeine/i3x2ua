# OPC UA to i3X Mapping Profile

This document describes the current mapping contract used by the i3X2UA model
builder. It defines how OPC UA nodes and references are translated into i3X
objects while keeping implementations interoperable and provenance explicit.

## Goals

- Keep i3X identifiers stable and queryable.
- Keep OPC UA provenance explicit and lossless.
- Ensure i3X conformance checks can validate namespace and type relationships.
- Keep the model valid even when no specialized semantic profile matches an
  instance type.
- Select specialized profiles from resolved instance type definitions and
  their supertypes, then retain generic mapping as a fallback.

## Implementation Status

The current implementation already applies the following behavior:

- deterministic `elementId` generation from OPC UA source node ids
- `parent_id`, `is_composition`, `semantic_role`, and `mapping_confidence` on
  mapped i3X nodes
- bidirectional relationships in the `relationships` map
- type-namespace-aware profile matching with built-in generic and machinery
  profiles
- hierarchy-first parent selection with graph fallback for non-structural edges

The code currently ships with built-in profiles. External profile loading can be
added later without changing the mapping contract described here.

## Canonical Rules

OPC UA `Argument` values use `ArrayDimensions: null` when `ValueRank <= 0`,
as required by OPC UA Part 3, section 8.6. This normalization is applied before
subscription change detection and when serializing current and historical values.
It does not mutate upstream values or change dimensions for `ValueRank > 0`.
Argument JSON Schemas permit both arrays and null for `ArrayDimensions`.

1. Object instance `elementId`
- Implementation-owned stable identifier.
- Must be unique within the i3X address space.
- Generated as `<kind>-SHA1(ExpandedNodeId)[:16]`, where the ExpandedNodeId
  uses the namespace URI (`nsu=...`) rather than a session-specific namespace
  index (`ns=N`) when the namespace can be resolved.
- Namespace-zero NodeIds always expand to `http://opcfoundation.org/UA/`.
  If a nonzero namespace index cannot be resolved, the original NodeId is hashed
  instead; stability across namespace-index changes is not guaranteed in that case.
- This is the identity of an individual source node. It is not the identity of
  that node's type.
- The `<kind>` prefix is the mapped i3X node kind: OPC UA `Object` maps to
  `asset`, `Variable` to `property`, and `Method` to `action`. A node with
  `EventNotifier` set maps to `eventSource` regardless of NodeClass; any other
  unrecognized NodeClass defaults to `asset`. For example,
  `eventSource-80bf303db7d51082` identifies an event-source instance, with the
  suffix derived from its source NodeId.

2. Object instance `typeElementId`
- The i3X type reference used by clients.
- The v1 objects endpoint resolves this through the object-type context, using
  the matching ObjectType `elementId` where available. Referenced types absent
  from the upstream type catalog are normally added to that context as
  synthesized datatype, generic, or `UnknownType` entries.
- For Variable-backed properties, the Variable `DataType` is normalized to an
  expanded NodeId form when possible and matched against ObjectType
  `sourceTypeId`. Other instances use their `TypeDefinition` NodeId to find the
  corresponding ObjectType `elementId`.
- If object-type context resolution fails for the objects request, the endpoint
  falls back to the expanded DataType NodeId for properties, or the expanded
  `TypeDefinition` NodeId for other instances. A missing or null type identity
  uses the generated `UnknownType` identifier instead.
- ObjectType `elementId` values are generated separately from instance
  `elementId` values. Listed and synthesized ObjectTypes use the form
  `urn:<server>:objecttype:<namespace>:v-<namespace-version>:<name>:<source-id>`,
  with each component normalized to a lowercase alphanumeric/hyphen token.
  The source ID component comes from the ObjectType NodeId (or the synthesized
  type's source DataType NodeId); the name is its BrowseName or inferred display
  name. Thus `typeElementId` normally points to an ObjectType `elementId`, not
  to a hash of the instance NodeId. The expanded source type NodeId is a
  fallback only when the object-type context cannot be resolved.

3. Object instance metadata `sourceTypeId`
- Provenance reference to the source-system type identity.
- For OPC UA, this is derived from `TypeDefinition` when available, otherwise from source node identity.
- Always normalized to expanded NodeId form when possible.

4. Internal model metadata `namespaceUri`
- Namespace URI of the source node.
- Resolved from the namespace table returned by `GET /namespaces` when available.
- This is internal model metadata, not a field in the public v1 object-instance
  metadata response.

5. Object instance metadata `typeNamespaceUri`
- Namespace URI of the type definition that `typeElementId` points to.
- Canonicalized to the URI spelling returned by `GET /namespaces` (for example trailing slash normalization).

6. ObjectType `namespaceUri`
- Must match a declared namespace from `GET /namespaces`.
- Namespace URI comparisons are normalized by case-insensitive compare and trailing slash insensitivity, then rewritten to declared canonical spelling.

7. ObjectType `sourceTypeId`
- Source namespace member identifier for the type (OPC UA provenance).
- Kept distinct from ObjectType `elementId`.
- Synthetic ObjectTypes for custom scalar DataTypes use the resolved DataType
  BrowseName in their generated `elementId` when hierarchy metadata provides
  it; the expanded source NodeId remains the identity suffix and provenance.

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

11. Internal model metadata `appliedProfileIds`
- Ordered list of profile ids matched against the resolved instance type and
  its `HasSubtype` ancestry.
- A specialized profile must match its configured type namespace fragment and,
  when configured, a type BrowseName on the same type or ancestor.
- `force_hierarchy` and `force_composition` on a matching type override control
  the incoming relationship for that instance, taking precedence over the
  relationship-reference classification.
- The built-in `generic` profile is always available as a fallback.

Profile selection does not use the namespace of the instance NodeId or a
`RequiredModel` declaration. Type-definition NodeIds are resolved to their
BrowseNames and namespace URIs; matching considers the direct type and its
supertypes, allowing an instance in an application namespace to inherit
semantics from a normative companion-model type.

Type hierarchy discovery batches BrowseName reads and inverse `HasSubtype`
browses within the server's operational limits. Shared ancestors are discovered
once and cached under the metadata TTL; reconnect invalidates this cache.
Warm builds reuse cached chains. Logs report type-resolution duration, cache
hits, and the number of newly resolved types separately from overall mapping.

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

`GET /v1/relationshiptypes` lists the mapped relationship vocabulary, not every
ReferenceType declared by the upstream server. The four built-in relationships
(`HasParent`, `HasChildren`, `HasComponent`, and `ComponentOf`) belong to
`https://cesmii.org/i3x`. Discovered graph relationships retain the namespace of
their OPC UA ReferenceType, resolved from its NodeId and the namespace table
(or directly from an expanded NodeId). Namespace zero is
`http://opcfoundation.org/UA/`, not its HTTPS variant. Generated inverse
relationships inherit the forward relationship's namespace; the synthetic
secondary-parent relationship `OrganizedBy` belongs to the i3X namespace.
Relationship names come from the ReferenceType node's `BrowseName`, read using
`ReferenceDescription.ReferenceTypeId`, not from `ReferenceDescription.BrowseName`
(which names the target object). ReferenceType names are batch-read once per
distinct type during each model browse; failed or empty names abort the browse
rather than exposing target-object names as relationship types.

The `namespaceUri` filter uses exact namespace identifiers, including scheme and
trailing slash. An empty result is valid when no mapped relationships belong to
the requested namespace. Unambiguous relationship IDs remain browse-name based.
If a name occurs in multiple namespaces or conflicts with a reserved ID (the
four built-ins or `inverseOf_...`), its ID becomes
`<namespace-uri>:<BrowseName.Text>` (expanded QualifiedName style, not a NodeId).
Names containing `:` are also qualified to avoid collisions with qualified IDs.
Components are not percent-encoded in JSON. Treat IDs as opaque strings: namespace
URIs and browse names can both contain colons, so do not split IDs on `:`.
URL-encode the complete value only when placing it in a URL.
Generated inverse IDs prefix the forward ID with `inverseOf_`; display names
retain the original browse name. These IDs are used consistently in relationship
type queries and related-object responses. An unresolvable ReferenceType namespace
fails model construction explicitly rather than publishing an incorrect namespace.
If distinct namespace/name pairs produce the same expanded qualified string,
model construction also fails explicitly rather than merging their relationships.

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

### ObjectType and DataType JSON Schemas

`GET /v1/objecttypes` returns schemas for discovered OPC UA ObjectTypes and may
also include synthetic ObjectType entries for DataTypes referenced by mapped
instances. This does not imply that every OPC UA DataType is published as a
separate top-level entry.

An OPC UA ObjectType schema describes an object: its members become JSON Schema
properties, its `HasSubtype` lineage is represented through local `$defs` and
`allOf`, and members with a mandatory modelling rule are required. A member's
schema is derived from its OPC UA DataType and structure/value metadata; declared
array rank makes it an array whose `items` use the corresponding scalar or
structured schema.

DataType schemas are also resolved when describing members and when creating a
synthetic type entry for a referenced DataType. Structured DataTypes are
generated from the available asyncua structure definition/registry and include
their fields, including nested structures. Custom scalar DataTypes use their
resolved `HasSubtype` hierarchy to find a standard scalar ancestor.

- `Number`, `Float`, and `Double` map to JSON Schema `number`.
- `Integer`, `UInteger`, concrete integer types, and `Enumeration` map to `integer`.
- `Boolean` maps to `boolean`; `DateTime` maps to `string` with `date-time` format.
- These scalar rules apply to the datatype value; when used by an array member,
  the scalar schema is placed under `items`.

Numeric identifiers in custom namespaces must not be interpreted as built-in
datatypes merely because their identifier matches a namespace-zero type.
The custom scalar resolver uses the nearest concrete namespace-zero scalar
ancestor when available. Integer-derived aliases retain the built-in integer
range constraints (for example, `Byte` is `0..255` and `UInt16` is
`0..65535`). When OPC UA schema annotations are enabled, a member's own expanded
DataType NodeId is retained as `x-opcua-dataTypeId`.

The unconstrained fallback (`{}`) applies to an ObjectType member whose scalar
DataType is missing or unresolved; it accepts any JSON value, including null.
Known string datatypes remain `string`. An unresolved array member retains
`type: array` with `items: {}`. Member metadata and mandatory modelling rules
are preserved. These member fallbacks are not the same as standalone synthetic
DataType entries: those entries use a resolved scalar/structured schema when
available, otherwise a generic, opaque, or unknown-type fallback schema. If
custom DataType hierarchy lookup fails, custom namespace identifiers are never
guessed from their numeric part.

The internal mapped `ModelNode` currently carries the following fields in code.
This is not the public v1 object-instance response shape:

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

The internal `metadata` map always contains:

- `uaNodeId`: original source NodeId.
- `expandedNodeId`: namespace-URI identity used for hashing, or the original
  NodeId when expansion is unavailable.
- `uaTypeNodeId`: original TypeDefinition NodeId, or null.
- `namespaceUri`: resolved source namespace URI, or null.
- `appliedProfileIds`: ordered list of matched profile IDs.

When the corresponding source attributes are available, it also contains:

- `description`
- `accessLevel`
- `userAccessLevel`
- `writable`: derived from the `CurrentWrite` bit (`0x02`) of `userAccessLevel`.
- `executable`
- `userExecutable`

Public v1 object-instance responses instead expose `elementId`, `displayName`,
`typeElementId`, `parentId`, `isComposition`, `isExtended` (currently false), and
optional `metadata`. With `includeMetadata=true`, that metadata is built
separately with `typeNamespaceUri`, `sourceTypeId`, `description`,
`compositionParentId`, and `relationships`; the internal metadata map is not
copied into it. If no nonempty source description is available, the response uses
`Derived from model node <name>`.

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
Built-in Boolean, numeric, String, DateTime, Guid, and ByteString values are
supported. ByteStrings use the OPC UA JSON encoding: a Base64-encoded JSON string,
with `contentEncoding: base64` in the JSON Schema. Arrays must match ValueRank and not exceed declared ArrayDimensions
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
