from __future__ import annotations

from typing import Any, TypedDict


class McpAnalysisResource(TypedDict):
    name: str
    description: str
    text: str


MCP_ANALYSIS_RESOURCES: dict[str, McpAnalysisResource] = {
    "i3x://guides/opcua-analysis": {
        "name": "OPC UA endpoint analysis guide",
        "description": (
            "Guidance for analyzing the upstream OPC UA endpoint, reporting NodeIds, and "
            "distinguishing direct endpoint evidence from the i3X mapped model."
        ),
        "text": "\n".join(
            [
                "# OPC UA endpoint analysis",
                "",
                "Use this guide for questions about the connected remote OPC UA server, its",
                "standard address space, status, namespaces, or server-reported profiles.",
                "For i3X model questions, use `i3x://guides/i3x-model-analysis`. For how",
                "the Gateway maps OPC UA nodes and references into i3X, use",
                "`i3x://guides/opcua-i3x-mapping`. Use multiple perspectives for a",
                "requested comparison or mapping.",
                "",
                "## Evidence scope",
                "",
                "- `getUaStatus` directly reads upstream ServerStatus (standard NodeId `i=2256`).",
                "- `getUaConnection` reports gateway connection state and configured endpoint",
                "  URI; it does not establish remote identity or capabilities.",
                "- `getInfoV1` describes the i3X gateway, not the remote OPC UA server.",
                "- Model searches, namespaces, types, and mapped values describe what is",
                "  exposed in the i3X-mapped model. Missing mapped content does not prove",
                "  absence from the remote server.",
                "- Use `resolveElementIdsToNodeIds` to resolve a discovered i3X ElementId to",
                "  its OPC UA source NodeId. Do not invent NodeIds or infer a namespace index",
                "  from a URI. Include the namespace URI when available.",
                "- For a mapped Server object, verify source type standard ServerType (`i=2004`);",
                "  a display-name match is insufficient. Use `getUaStatus` if ServerStatus",
                "  is not mapped.",
                "",
                "## Analysis limits",
                "",
                "Treat namespace presence, ServerProfileArray entries, type definitions,",
                "action nodes, limits, and capability declarations as reported or mapped",
                "evidence—not proof of conformance, successful operation, or complete",
                "address-space coverage. A history declaration does not prove that a",
                "particular variable has stored history. A mapped alarm source does not",
                "prove active events or a working event subscription. Writable metadata",
                "does not establish that a write is safe or authorized.",
                "",
                "For exhaustive requests, paginate to completion within the relevant tool",
                "scope and label that scope. Do not claim a complete remote address-space",
                "inventory from mapped-model results. Check per-item errors and distinguish",
                "failed reads from confirmed absence.",
                "",
                "## Output identifiers",
                "",
                "For OPC UA findings, cite the source NodeId returned or resolved, plus",
                "namespace URI and mapped browse path when available. Keep the i3X ElementId",
                "in a separate field; never present it as a NodeId. Mixed reports must have",
                "separate OPC UA NodeId and i3X ElementId fields and state the evidence source.",
            ]
        )
        + """

## Evidence ladders

Evidence levels are not interchangeable. Report only the highest level directly
supported; a lower level never proves a higher one.

### Companion Specifications

1. **Namespace observed** — a namespace URI was returned for this endpoint.
2. **Relevant type observed** — a related type definition or instance was observed;
   record its OPC UA ExpandedNodeId and source.
3. **Required model elements observed** — relevant required nodes and relationships
   were inspected. State the inspected scope and any missing or unreadable nodes.
4. **Behavior verified** — relevant behavior was successfully exercised or read;
   record operation, result, session scope, and time.
5. **Conformance evidence available** — authoritative conformance evidence was
   inspected and its endpoint, model, version, and scope were checked.

Namespace URI observed ≠ specification implemented ≠ conformant ≠ certified.
Do not infer level 5 from levels 1–4.

### Historical Access

1. **Folder observed** — a `HistoricalAccess` folder or similarly named structure
   was found.
2. **Capability declared** — a history-related profile, property, or metadata
   declaration was observed.
3. **HistoryRead attempted** — a request was made for a specific variable and time
   range; record status and per-item errors.
4. **HistoryRead succeeded** — the server returned success for that request. It
   may still return no samples.
5. **Historical samples returned** — one or more samples were returned for that
   variable and range; report timestamps and quality.

A successful read for one variable and range does not prove history for other
variables or ranges, or general historian availability.

### Methods / Control

1. **Method exists** — the method node was observed; identify its instance
   ExpandedNodeId.
2. **Executable observed** — the server-reported `Executable` flag was read.
   Report `UserExecutable` separately when available, with session/user scope.
3. **Authorization confirmed** — caller-specific authorization for this operation
   was confirmed by explicit evidence.
4. **Invocation attempted** — a specific invocation was made; report arguments,
   status, and output.
5. **Invocation succeeded** — the server returned success. This alone does not
   prove the intended physical or business outcome.

Do not invoke a control method merely to test it. Invoke only when requested and
authorized; account for side effects.

## Canonical reporting language

Use these formulations when evidence is limited. Fill in bracketed fields and do
not strengthen the claim beyond the observed result.

- **Namespace:** “The namespace URI `[URI]` was observed in `[source/tool]`.
  Implementation, conformance, and certification were not verified.”
- **Method:** “Method `[instance ExpandedNodeId]` was observed. Executability,
  caller authorization, and operational effect were not established.”
- **Executable flags:** “The server reported `Executable=[value]`
  [and `UserExecutable=[value]`] for `[session/user scope]`. Invocation success
  and operational outcome were not established.”
- **History folder:** “A `[folder name]` history-related structure was observed.
  HistoryRead support and samples for individual variables were not verified.”
- **History declaration:** “A history capability was declared in `[source]`.
  Successful HistoryRead and variable history were not verified.”
- **Successful HistoryRead with no samples:** “HistoryRead succeeded for
  `[variable ExpandedNodeId]` over `[range]`, but returned no samples. History
  outside this request was not assessed.”
- **Historical samples:** “HistoryRead returned `[count]` samples for
  `[variable ExpandedNodeId]` over `[range]`. Availability for other variables
  or ranges was not assessed.”
- **Mapped model:** “The i3X mapped model contains `[element]` with i3X ElementId
  `[id]` and source OPC UA ExpandedNodeId `[id or unknown]`. This establishes
  mapped-model presence, not complete upstream address-space coverage.”
- **Pagination incomplete:** “The query returned `[count]` results for
  `[filters/scope]`; `nextOffset=[value]` indicates more results. This inventory
  is partial; I cannot claim ‘all’ for this query.”
- **Pagination complete:** “Pagination completed for `[tool and exact filters]`,
  returning `[count]` results in the current i3X mapped model. This is complete
  only for that query and scope; complete remote OPC UA coverage was not
  established.”
- **Capability declaration:** “`[capability]` was declared in `[source]`.
  Runtime operation was not verified.”
- **Failed call:** “The `[read/call]` returned `[error/status]` and did not
  establish `[claim]`. The result is unknown, not evidence that the capability
  is absent.”
- **Unsupported recommendation:** “The available evidence does not establish
  that `[feature]` exists and is operational, so I cannot recommend enabling
  it. Verify its presence and runtime state first.”

## Pagination rules

- If `nextOffset` is present, more results remain for that query. Continue from
  the returned offset until it is absent, or state why pagination stopped.
- If pagination is incomplete, report count, filters, scope, and next offset.
  Do not claim “all,” “every,” “none,” or a total inventory.
- If pagination completes, completeness applies only to that tool, exact filters,
  and query scope. It does not prove complete coverage of the remote address
  space.
- Zero results means only that this query/page returned no results. It does not
  prove absence.
- Do not combine totals from different filters or scopes into one inventory.
- Report errors, truncation, and per-item failures. Do not count failed items as
  absent.

## Evidence source boundaries

Use these source-specific boundaries:

- **Gateway metadata** (`getInfoV1`, connection state): valid: “The Gateway
  reports `[metadata/state]`.” Invalid: “The remote endpoint supports
  `[feature]`.”
- **i3X model search/context:** valid: “The mapped model contains `[element]`.”
  Invalid: “The remote server contains no other elements.”
- **Resolved source identity:** valid: “This ElementId resolves to OPC UA
  ExpandedNodeId `[id]` in this mapping.” Invalid: “A guessed namespace index
  is the source NodeId.”
- **Direct upstream read:** valid: “The endpoint returned `[value/status]` for
  `[node]` during this read.” Invalid: “This result is always available to all
  users.”
- **Successful runtime operation:** valid: “This operation succeeded for
  `[scope/time/arguments]`.” Invalid: “The capability is universally operational
  or safe.”
- **Conformance document:** valid: “This evidence states `[claim]` for
  `[document scope]`.” Do not claim the endpoint is certified unless the
  document's applicability to this endpoint and scope is verified.

Gateway metadata describes the Gateway or its configuration. The mapped model
describes what the Gateway built and exposes. Direct OPC UA reads/calls are
endpoint evidence for the requested node and session. Runtime verification
requires a successful operation or read; declarations and mapped fields alone
are not runtime verification.

## Recommendations

Recommend enabling or configuring a feature only when evidence establishes that
it exists and is operational for the relevant scope, and the requested action is
in scope. Otherwise state what is unknown and what evidence is needed; do not
prescribe a feature-specific change as though availability were established.

## Common Invalid Conclusions

1. **Invalid:** “The server supports ISA95.”
   **Why invalid:** Only the namespace URI was observed.
   **Correct:** “The ISA95-related namespace URI was observed. Implementation
   and conformance were not verified.”
2. **Invalid:** “The server is ISA95 conformant.”
   **Why invalid:** Namespace, type, or node presence is not conformance evidence.
   **Correct:** “The inspected nodes/types were observed. Conformance evidence
   was not available.”
3. **Invalid:** “The server is certified.”
   **Why invalid:** No applicable certification evidence was inspected.
   **Correct:** “Certification was not verified from the available evidence.”
4. **Invalid:** “History is supported.”
   **Why invalid:** A folder or declaration does not establish successful
   HistoryRead.
   **Correct:** “A history-related structure/declaration was observed. Runtime
   HistoryRead support was not verified.”
5. **Invalid:** “This variable has history.”
   **Why invalid:** No HistoryRead samples for this variable and range were
   observed.
   **Correct:** “History for this variable and range was not verified.”
6. **Invalid:** “The historian is available.”
   **Why invalid:** One successful request does not establish general historian
   availability.
   **Correct:** “HistoryRead succeeded for `[variable, range]`; broader
   historian availability was not assessed.”
7. **Invalid:** “The method is usable.”
   **Why invalid:** Method presence does not establish executability,
   authorization, or successful invocation.
   **Correct:** “The method exists. Executability, authorization, and successful
   invocation were not verified.”
8. **Invalid:** “`Executable=true` means this user can run the method.”
   **Why invalid:** Executability and caller-specific authorization are distinct.
   **Correct:** “The server reported `Executable=true`; caller-specific
   authorization and invocation success were not established.”
9. **Invalid:** “The method call will operate the machine.”
   **Why invalid:** Even a successful call does not establish physical or
   business outcome.
   **Correct:** “The method call returned `[result]`. Its physical/business
   outcome was not independently verified.”
10. **Invalid:** “All machines were inspected.”
    **Why invalid:** Results may be paginated, filtered, or limited to mapped
    elements.
    **Correct:** “`[count]` mapped machine results were inspected for `[scope]`;
    this is not a complete upstream inventory.”
11. **Invalid:** “There are no alarms.”
    **Why invalid:** An empty page or mapped result does not establish upstream
    absence.
    **Correct:** “No alarm elements were returned for `[query/scope]`; upstream
    absence was not established.”
12. **Invalid:** “All namespaces are implemented.”
    **Why invalid:** Namespace enumeration reports URI presence, not
    implementation or conformance.
    **Correct:** “These namespace URIs were observed: `[URIs]`. Their
    implementation and conformance were not verified.”
13. **Invalid:** “The capability property proves the feature works.”
    **Why invalid:** A property is a declaration, not a runtime test.
    **Correct:** “The capability was declared. Runtime operation was not
    verified.”
14. **Invalid:** “The feature should be enabled.”
    **Why invalid:** Feature availability and operational state were not
    established.
    **Correct:** “Evidence is insufficient to recommend enabling the feature.
    Verify its presence and runtime state first.”
15. **Invalid:** “This i3X ElementId is the OPC UA NodeId.”
    **Why invalid:** They identify different things.
    **Correct:** “i3X ElementId: `[id]`; OPC UA ExpandedNodeId: `[resolved id or
    unknown]`.”
16. **Invalid:** “The type NodeId is the instance NodeId.”
    **Why invalid:** A TypeDefinition identifies a type; an instance NodeId
    identifies an instance.
    **Correct:** “Instance ExpandedNodeId: `[id]`; TypeDefinition
    ExpandedNodeId: `[id or unknown]`.”
17. **Invalid:** “`ns=4;i=1234` is the stable identifier for this node.”
    **Why invalid:** Namespace indexes are server/session-specific.
    **Correct:** “Use the returned ExpandedNodeId with namespace URI when
    available; the namespace index alone is not a stable cross-session identity.”
18. **Invalid:** “The element is absent from OPC UA.”
    **Why invalid:** It may be absent only from a mapped/filtered result, or the
    read may have failed.
    **Correct:** “The element was not returned by `[tool/query]` for `[scope]`;
    upstream absence was not established.”
19. **Invalid:** “Pagination completed, so the remote address space is fully
    covered.”
    **Why invalid:** Completion is scoped to the query and mapped model.
    **Correct:** “Pagination completed for `[query]` in the current mapped model;
    complete upstream coverage was not established.”
20. **Invalid:** “The Gateway is connected, so the endpoint is operational.”
    **Why invalid:** Connection state does not validate every read, capability,
    or service.
    **Correct:** “The Gateway reports `[connection state]`. The requested
    operation/capability was `[not tested/result]`.”

## OPC UA Audit Mode

When the user requests an audit, conformance assessment, capability assessment,
or high-confidence OPC UA conclusion, use OPC UA Audit Mode.

1. Classify every claim as **observed**, **inferred**, **verified**, or
   **unknown**:
   - **Observed:** directly returned by a named tool/read, with identifier and
     scope.
   - **Inferred:** derived from observed facts; state the reasoning and do not
     present it as a direct server statement.
   - **Verified:** the relevant read or operation succeeded for the specified
     node, request, endpoint/session, and time. State exactly what was verified;
     do not generalize beyond that scope.
   - **Unknown:** not returned, not inspected, failed, truncated, or unsupported.
     Unknown does not mean false or absent.
2. For every OPC UA node claim, report the OPC UA ExpandedNodeId
   (`nsu=...;i=...`, `nsu=...;s=...`, etc.) when available. Report the i3X
   ElementId and TypeDefinition ExpandedNodeId in separate fields. Never
   construct an identifier from a display name or guess a namespace index.
3. For each result, record evidence source/tool, query or operation, relevant
   filters/time range, returned status, and timestamp when available. Preserve
   per-item errors, quality, and source timestamps.
4. Treat namespace URIs, type definitions, profiles, capability properties,
   access flags, and mapped metadata as observations or declarations—not proof
   of implementation, conformance, authorization, or runtime behavior.
5. For history, distinguish folder/declaration evidence from successful
   HistoryRead and from samples actually returned for the named variable/range.
6. For methods/control, distinguish method presence, `Executable`,
   `UserExecutable`, caller authorization, invocation result, and independently
   verified effect. Do not invoke a method to test it unless requested and
   authorized.
7. Follow pagination while `nextOffset` is present. If pagination cannot be
   completed, mark the inventory partial and state the last offset. Completed
   pagination applies only to the named query and mapped-model scope, not the
   full remote address space.
8. Include a **Missing evidence / unknowns** section. Name the evidence required
   to strengthen each conclusion.
9. Do not recommend actions that assume a capability exists or is operational.
   State what must be verified before recommending the action.
10. Do not state “conformant,” “certified,” “supported,” “available,”
    “authorized,” or “operational” unless the evidence ladder supports that
    exact term and scope.

OPC UA Audit Mode reduces unsupported claims; it does not itself establish
conformance or replace an authoritative audit or certification process.
""",
    },
    "i3x://guides/opcua-i3x-mapping": {
        "name": "OPC UA to i3X mapping guide",
        "description": (
            "Explains how the Gateway ModelBuilder maps OPC UA node identity, types, "
            "references, and semantic profiles into the i3X model."
        ),
        "text": "\n".join(
            [
                "# OPC UA to i3X mapping",
                "",
                "Use this guide when asked how the Gateway or ModelBuilder translates",
                "OPC UA nodes and references into the i3X model. It describes the",
                "Gateway's mapping contract, not proof that every upstream node was",
                "discovered or mapped. For a specific live element, inspect the mapped",
                "model using MCP tools and report observed values separately from these",
                "general rules.",
                "",
                "## Identity and types",
                "",
                "- i3X `elementId` is a Gateway-generated identifier, not an OPC UA",
                "  NodeId. It is deterministically derived from the node kind and an",
                "  ExpandedNodeId using the source namespace URI.",
                "- `source_node_id` / metadata `uaNodeId` retain OPC UA source identity.",
                "  `typeElementId` is the i3X type reference; `source_type_id` / metadata",
                "  `uaTypeNodeId` retain type provenance. For OPC UA Variables, the i3X",
                "  type is derived from the DataType.",
                "- Namespace URIs are used to preserve source namespace identity; do not",
                "  treat a session-specific namespace index as a stable identity.",
                "- Unresolved types are synthesized from available source metadata when",
                "  possible; otherwise an `UnknownType` placeholder preserves the",
                "  unresolved reference.",
                "",
                "## Reference relationships",
                "",
                "- `HierarchicalReferences` and `Organizes` map to the i3X Hierarchy",
                "  plane (`HasChildren` / `HasParent`).",
                "- `HasComponent` and `HasOrderedComponent` map to Composition when the",
                "  target is a Variable, otherwise to Hierarchy.",
                "- `HasProperty` maps to Composition (`HasComponent` / `ComponentOf`).",
                "- `NonHierarchicalReferences` map to the Graph plane; discovered graph",
                "  relationships retain their OPC UA ReferenceType namespace.",
                "- `HasTypeDefinition` and `HasSubtype` provide type metadata, not",
                "  instance relationships. When reference lineage is ambiguous, the",
                "  non-hierarchical classification takes precedence.",
                "- Value and history recursion follows Composition only, not Hierarchy.",
                "",
                "## Semantic profiles",
                "",
                "- The ModelBuilder matches profiles against the resolved instance type",
                "  and its `HasSubtype` ancestry, using type namespace and, where",
                "  configured, BrowseName. The instance node's namespace alone does not",
                "  select a profile.",
                "- Specialized profiles may override semantic role and relationship",
                "  classification. The built-in `generic` profile is the fallback.",
                "- Mapped elements can expose `semantic_role`, `mapping_confidence`, and",
                "  `appliedProfileIds` as explainability metadata. Confidence is a hint,",
                "  not proof that a companion specification is implemented, conformant,",
                "  or certified.",
                "- A profile match and a declared capability are mapping/model evidence;",
                "  they do not verify upstream runtime behavior.",
                "",
                "## Inspecting a live mapping",
                "",
                "- Use `searchModelElements` with focused filters or `findElementsByName`",
                "  to discover mapped elements; do not guess ElementIds.",
                "- Use `getElementContext` for an element's mapped metadata and",
                "  `getBrowsePath` for its mapped path.",
                "- Use `resolveElementIdsToNodeIds` to obtain the OPC UA source NodeId",
                "  for a discovered ElementId. Keep the two identifiers in separate",
                "  fields.",
                "- Use `queryRelatedObjectsV1` or `getChildren` to inspect relationships",
                "  and the relevant plane. Check results and per-item errors.",
                "- MCP model tools describe the current mapped model, not necessarily",
                "  the full OPC UA address space. A missing mapped element does not prove",
                "  that the upstream node is absent.",
                "- Keep the OPC UA instance ExpandedNodeId, TypeDefinition ExpandedNodeId,",
                "  and i3X ElementId in separate fields. Prefer returned ExpandedNodeIds",
                "  (`nsu=...;i=...`, `nsu=...;s=...`) and never invent IDs or guess a",
                "  stable namespace index.",
                "- If a result contains `nextOffset`, more results remain. Continue",
                "  pagination or report the inventory as partial. Even completed",
                "  pagination is limited to the query and mapped-model scope.",
                "- For a stricter assessment, read",
                "  `i3x://guides/opcua-analysis` and follow its **OPC UA Audit Mode** and",
                "  **Common Invalid Conclusions** rules.",
                "",
                "The full mapping profile is documented in",
                "`docs/OPCUA_I3X_MAPPING_PROFILE.md` in the project repository.",
            ]
        ),
    },
    "i3x://guides/i3x-model-analysis": {
        "name": "i3X mapped-model analysis guide",
        "description": (
            "Guidance for analyzing the cached i3X model, navigating ElementIds, and "
            "reporting mapped-model scope without overclaiming OPC UA coverage."
        ),
        "text": "\n".join(
            [
                "# i3X mapped-model analysis",
                "",
                "Use this guide for questions about the i3X model, assets, relationships,",
                "properties, mapped values, or model coverage. For upstream OPC UA facts,",
                "use `i3x://guides/opcua-analysis`. For mapping behavior, use",
                "`i3x://guides/opcua-i3x-mapping`. Use multiple perspectives only for a",
                "requested comparison or mapping.",
                "",
                "## Discovery and navigation",
                "",
                "- Start unknown top-level asset searches with `getObjectsV1(root=true)`.",
                "  Navigate only relevant branches using discovered i3X ElementIds and",
                "  `queryRelatedObjectsV1` or `getChildren` with the appropriate plane.",
                "- Use `getElementContext` for one known element, and focused name, role,",
                "  namespace, or metadata searches. An unfiltered search may return an",
                "  arbitrary first page and is not necessarily exhaustive.",
                "- Use `getBrowsePath` for a mapped display path. Use",
                "  `resolveElementIdsToNodeIds` only when the OPC UA source NodeId is needed.",
                "- Discover exact property ElementIds before reading values. Use",
                "  `queryLastKnownValuesV1(maxDepth=1)` for a few current values;",
                "  `readContainerValues` for many values under one known, focused container.",
                "  Use `queryHistoricalValuesV1` only when history is requested.",
                "- Check each item's success and errors. Preserve the returned value, quality,",
                "  and timestamp; last-known values may be stale.",
                "",
                "## Coverage and evidence",
                "",
                "Model tools describe the current i3X-mapped model, not necessarily the",
                "complete upstream OPC UA address space. Report counts and inventories as",
                "mapped-model results. Paginate to completion within the relevant tool's",
                "scope and disclose failures, truncation, and unknowns.",
                "",
                "Mapped presence, a namespace/type association, action node, alarm structure,",
                "or capability declaration does not establish OPC UA conformance or",
                "successful operation. A missing mapped element does not prove absence",
                "from the remote server.",
                "",
                "## Output identifiers",
                "",
                "For i3X findings, cite the exact i3X ElementId returned by the tool and",
                "include its mapped path when useful. If a source NodeId is relevant, resolve",
                "it and report it separately as “OPC UA NodeId”. Never substitute one ID type",
                "for the other. Mixed reports must have separate i3X ElementId and OPC UA",
                "NodeId fields. Prefer the resolved OPC UA ExpandedNodeId and distinguish",
                "the instance NodeId from its TypeDefinition NodeId.",
                "",
                "## Scope and evidence",
                "",
                "Gateway metadata describes the Gateway; model search/context describes",
                "only the current mapped model; an upstream OPC UA read describes the",
                "result for that node and session. Do not treat these as interchangeable.",
                "A namespace URI, type association, profile match, capability declaration,",
                "or mapped action does not prove implementation, conformance, authorization,",
                "or runtime operation.",
                "",
                "If `nextOffset` is present, continue pagination or report the result as",
                "partial. Completed pagination is complete only for that tool, query, and",
                "mapped-model scope—not necessarily the upstream address space. Report",
                "filters, result count, errors, and truncation. An empty mapped result does",
                "not establish upstream absence.",
                "",
                "For Companion Specification, historical access, and method/control",
                "evidence ladders, canonical wording, and **Common Invalid Conclusions**,",
                "read `i3x://guides/opcua-analysis`. Use **OPC UA Audit Mode** for strict",
                "assessments and explicitly label observed, inferred, verified, and unknown",
                "evidence.",
            ]
        ),
    },
}


def list_mcp_analysis_resources() -> list[dict[str, Any]]:
    return [
        {
            "uri": uri,
            "name": resource["name"],
            "description": resource["description"],
            "mimeType": "text/markdown",
        }
        for uri, resource in MCP_ANALYSIS_RESOURCES.items()
    ]
