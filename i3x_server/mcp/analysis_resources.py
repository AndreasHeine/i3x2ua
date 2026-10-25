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
                "For i3X model questions, use `i3x://guides/i3x-model-analysis`. Use both",
                "perspectives only for a requested comparison or mapping.",
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
                "use `i3x://guides/opcua-analysis`. Use both only for a requested comparison",
                "or mapping.",
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
                "NodeId fields.",
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
