# Reference Architecture

## Integration Flow (Simple Baseline)

This baseline is useful for fast local prototyping with minimal moving parts.

```mermaid
flowchart LR
    A[User question]
    B[Single model + prompt]
    C[Direct MCP call]
    D[i3x2ua /mcp]
    E[FastAPI handlers]
    F[OPC UA + plant data]
    G[Answer to user]

    A --> B --> C --> D --> E --> F --> E --> D --> B --> G
```

Simple path: user -> model -> MCP call -> i3x2ua MCP server -> FastAPI handlers -> OPC UA/plant systems -> response back to the model.

### Limitations of the Simpler Approach

- Lower reliability under production load because there is no explicit timeout/retry/circuit-breaker layer.
- Weaker governance because policy checks and argument guardrails are not modeled as a separate control gate.
- Higher operational risk for write/update actions because no human-approval checkpoint is included.
- Less predictable tool behavior because tool-priority and keyword-driven routing are not emphasized in orchestration.
- Reduced observability because telemetry/evaluation loops are not first-class parts of the flow.
- No retrieval branch for unstructured context, so answers depend mostly on live tool outputs and prompt context.

## Integration Flow (Advanced AI Tooling)

The diagram below is a true integration flowchart with a required path and optional advanced branches.

```mermaid
flowchart TD
    A[User asks AI question in app]
    B[Orchestrator interprets intent]
    BR{Use dynamic model routing?}
    MR[Model router selects best model]
    BM[Default model]
    BF[Fallback model]
    C{Need live operational data?}
    C2{High-impact action?}
    D[Call MCP client\ninitialize tools/list tools/call]
    AUTH[AuthN/AuthZ\nservice identity RBAC ABAC]
    RES[Resilience\ntimeout retry circuit breaker]
    subgraph I3X2UA[i3x2ua contribution]
        E[i3x2ua MCP server /mcp]
        F[i3x2ua FastAPI handlers]
        G[i3x2ua OPC UA client + i3x mapping]
        R[i3x2ua overrides/mcp_overrides.json\ntool/prompt/feature metadata]
    end
    H[Plant assets return data]
    I[Structured tool result to model]
    J[AI returns grounded answer to user]
    HITL[Human approval gate]

    K{Need policy checks?}
    L[Guardrails\nallow-lists + arg validation]

    M{Need unstructured document context?}
    N[Index docs/SOP/tickets]
    O[(Vector DB optional)]
    P[Retrieve semantic context]

    Q[Record telemetry\nlogs traces tool metrics]
    EV[Eval loop\nquality safety latency regressions]

    A --> B --> BR
    BR -- Yes --> MR --> BM
    BR -- No --> BM
    BM -- Failure or low confidence --> BF
    BM --> C
    BF --> C

    C -- Yes --> K
    K -- Yes --> L --> D
    K -- No --> D
    D --> AUTH --> RES --> C2
    C2 -- Yes --> HITL --> E
    C2 -- No --> E
    E --> F --> G --> H --> G --> F --> E --> I --> J
    C -- No --> M
    M -- Yes --> N --> O --> P --> I
    M -- No --> I

    D -. observe .-> Q
    E -. observe .-> Q
    J -. evaluate .-> EV
    EV -. improve prompts/tooling .-> B
    R -. influences tool selection .-> E

    style I3X2UA fill:#e6f4ea,stroke:#2e7d32,stroke-width:2px
```

Required integration path: AI app -> orchestrator -> MCP client -> i3xua MCP server -> FastAPI handlers -> OPC UA/plant systems -> response back to the model.

Optional advanced path: add observability, guardrails, semantic retrieval (vector DB), model fallback, approval gates, and continuous evaluations while keeping live operational data grounded through MCP tools.

The green group in the flowchart is the i3x2ua value layer you bring to the AI stack: MCP exposure, API/tool dispatch, industrial data mapping, and tool metadata shaping.
