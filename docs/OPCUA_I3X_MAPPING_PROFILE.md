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

### OPC UA → i3X Relationship Mapping

The model builder maps OPC UA reference types to i3X relationship planes.
Classification is ancestry-root-first and uses recursively resolved supertypes
(following `HasSubtype` inverse for `ReferenceType` nodes).

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
