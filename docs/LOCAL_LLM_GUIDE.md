# Local LLM Guide: LM Studio with i3x2ua

This guide shows one local setup using LM Studio as the model runtime and MCP
client, with Qwen3.5-9B as the example model. The model runs on your workstation;
i3x2ua connects to the OPC UA server, builds the i3X model, and supplies MCP
tools to the assistant.

## Example workstation

| Component | Example |
|---|---|
| Model | Qwen3.5-9B, configured for an 80K-token context |
| CPU | AMD Ryzen 7 7800X3D |
| Memory | 32 GB DDR5 at 6000 MT/s |
| GPU | AMD Radeon RX 9070 XT with 16 GB VRAM |
| Model runtime and MCP client | LM Studio |

Available memory depends on the model file, quantization, context length, GPU
offload, and other applications. An 80K context can use substantial memory.
Confirm that the model loads and completes long-context requests reliably in
LM Studio; reduce the context length or close other GPU applications if it does
not. Keep only one model instance loaded while tuning.

## 1. Load and check the model in LM Studio

1. Install LM Studio and download a Qwen3.5-9B model file that fits your
   available memory. Check the model card for its supported context length and
   quantization details.
2. Load the model and enable GPU offload using the backend available to your
   LM Studio installation. Confirm in LM Studio that the AMD GPU is being used;
   do not assume all model layers or the full context fit in VRAM.
3. Set the context length to 80K if the selected model build supports it and it
   remains stable on this machine. If loading or inference becomes unreliable,
   start with a shorter context and increase it gradually.
4. For tool-using workflows, start with a low temperature and the model's
   default sampling settings. Change one setting at a time and test the same
   tool-use task after each change.

LM Studio provides the local model and MCP client. It does not itself create
the i3X model or connect to the OPC UA server.

## 2. Configure i3x2ua and limit the discovered branch

Set the OPC UA endpoint and enable MCP in the environment where i3x2ua runs.
For a local development server in PowerShell:

```powershell
$env:I3X_OPCUA_ENDPOINT="opc.tcp://127.0.0.1:4840"
$env:I3X_ENABLE_MCP="1"
$env:I3X_MODEL_PRELOAD_ON_STARTUP="true"
# Optional: include additional OPC UA metadata in generated MCP schemas.
# Enable only when the task needs it; it can increase context usage.
# $env:I3X_MCP_INCLUDE_OPCUA_METADATA="1"
uv run uvicorn i3x_server.main:app --host 127.0.0.1 --port 8000 --loop none
```

Use the endpoint, credentials, and security settings appropriate for your OPC UA
server. Keep credentials out of shared prompts and model conversations.

### Use browse entrypoints to keep the model focused

By default, instance discovery starts at the OPC UA Objects folder. To expose a
smaller part of a large address space, set `I3X_OPCUA_BROWSE_ROOTS` to one or
more URI-based ExpandedNodeIds. For example:

```powershell
$env:I3X_OPCUA_BROWSE_ROOTS='nsu=urn:example:plant;s=MachineA'
```

For multiple roots, use a JSON array:

```powershell
$env:I3X_OPCUA_BROWSE_ROOTS='["nsu=urn:example:plant;s=MachineA","nsu=urn:example:plant;s=MachineB"]'
```

Replace the example namespace URI and identifiers with values from your server.
Configured roots must be readable OPC UA Objects. Namespace-index identifiers
such as `ns=2;...` are not accepted for this setting. Restart i3x2ua after
changing the environment.

Browse entrypoints constrain instance discovery and help keep model results
manageable for smaller models. They are not an authorization boundary:
Object-type discovery is independent, and forward hierarchical cross-links can
lead into other branches or namespaces. Apply access control at the OPC UA
server and deployment proxy as appropriate.

For additional environment variables and deployment details, see the
[README Quick Start](../README.md#quick-start) and
[OPC UA browse entrypoints](../README.md#opc-ua-browse-entrypoints).

## 3. Connect LM Studio to i3x2ua

In LM Studio's MCP server configuration, add the i3x2ua endpoint:

```json
{
  "mcpServers": {
    "i3x2ua": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

If you also want the OPC UA reference server shown in your configuration, add
it as a separate server:

```json
{
  "mcpServers": {
    "i3x2ua": {
      "url": "http://localhost:8000/mcp"
    },
    "opc-ua-reference": {
      "url": "https://reference.opcfoundation.org/mcp"
    }
  }
}
```

The reference server is optional and separate from i3x2ua; it does not provide
access to your plant's OPC UA server.

After saving the configuration, reload or reconnect the MCP servers in
LM Studio. Confirm that i3x2ua's tools are listed. If they are missing, check
that i3x2ua started with `I3X_ENABLE_MCP=1` and that the URL and port are
reachable. If tool calls fail, check the i3x2ua logs and confirm the model is
available. If the server reports an untrusted `Origin`, configure the exact LM
Studio origin in `I3X_CORS_ALLOWED_ORIGINS` and restart; do not use a wildcard. The REST-style
`http://localhost:8000/mcp/tools` helper can also be used to inspect the local
tool catalog; MCP clients should connect to `/mcp`.

## 4. Tune tool descriptions for your use case

i3x2ua loads tool and prompt overrides from `overrides/mcp_overrides.json`
when the application starts. The repository's
[`overrides/schema.json`](../overrides/schema.json) defines the accepted
format. Keep both top-level `tools` and `prompts` objects in the file, and
validate JSON syntax before restarting the server. Add tool names to the
optional top-level `excluded_tools` array to omit them from MCP discovery and
calls; names must match the MCP tool names exactly. This also applies to
built-in cached-model tools. In Docker Compose, the repository's `overrides`
directory is mounted at `/app/overrides`.

Use the exact tool names as keys. Overrides apply to the REST-derived MCP tools.
For example, this makes discovery guidance specific to finding and reading a
line-level temperature property:

```json
{
  "$schema": "./schema.json",
  "tools": {
    "getObjectsV1": {
      "description": "DISCOVERY: Find the target line or machine when its elementId is unknown. Start with root objects, match by name, and use only returned elementIds for follow-up navigation. Do not enumerate the full model or guess IDs.",
      "priority": "high",
      "keywords": ["find a machine", "discover line", "asset by name"]
    },
    "queryLastKnownValuesV1": {
      "description": "CURRENT VALUE: Use only after navigating to the exact temperature property's elementId. Read only the selected property with maxDepth=1. Report value, quality, and timestamp; use historical reads only when a trend or past interval is requested.",
      "priority": "high",
      "keywords": ["current temperature", "latest sensor reading", "temperature quality"]
    }
  },
  "prompts": {}
}
```

The cached-model tools (`searchModelElements`, `getElementContext`,
`summarizeSubtree`, `traceRelationshipPath`, the compact model-access tools below,
and the NodeId resolvers) currently
use built-in descriptions; overrides do not replace those descriptions. For
REST-derived tools such as `queryHistoricalValuesV1`, a maintenance-focused
description can specify when history is warranted and require a bounded time
range. Prefer clear selection criteria and safe limits over long descriptions
containing every possible synonym.

### Compact cached-model access

These tools read only the in-memory model, never build or refresh it, and never
contact OPC UA. They return `modelRevision`; do not combine pages from different
revisions. If the model is not cached yet, they return 503 instead of triggering
discovery.

| Tool | Use |
|---|---|
| `resolveBrowsePath` | Resolve `{"path":"Machines/Line1/Station5/Temperature"}` directly to an element. |
| `getBrowsePath` | Return the canonical path and segments for `{"elementId":"..."}`. |
| `getInstancesOfType` | List instances for `{"typeElementId":"...","includeSubtypes":true}`. |
| `getChildren` | List direct children for `{"elementId":"...","plane":"hierarchy"}`; also supports `composition` and outgoing `graph` neighbors. |
| `getCompositionProperties` | List recursive composition properties for `{"elementIds":["...","..."]}`, without reading values. |
| `findElementsByName` | Last-resort lookup for a specific likely name after focused search or traversal fails; matches display-name substrings only. |
| `findElementsByNamespace` | Last-resort lookup for a specific target when its exact `namespaceUri` is already known. |
| `findElementsByRole` | Last-resort lookup for a specific target when its `semanticRole` is already known. |

Do not use the `findElements...` tools for model discovery or inventory. They only
match one criterion against the cached model and return paginated pairs; a page
is not a complete inventory, and paging through broad matches can consume context
without reliably finding the intended element. `searchModelElements` is also a
targeted lookup, not an exhaustive discovery tool: its filters narrow the result
set, and `kind` or `semanticRole` reflects only one classification, not every
capability (for example, an asset may also support events). A filtered miss
does not prove the element or capability is absent. It requires non-empty
targeted search text; filter-only or empty-text listing is rejected. Prefer
focused path/child traversal for navigation, then use searches to locate a
specific target; do not page through broad matches.

Paths use exact, case-sensitive cached display names and include the root.
Canonical paths prefer hierarchy parents, falling back to canonical/composition
parents for composition-only elements. Supply exactly one of `path` or
`segments`; use `{"segments":["Machines","Station5","Pressure/Flow"]}` when a
name contains `/`. `getBrowsePath` returns both forms; use its segments for a
lossless round trip. A missing path returns `found:false`; duplicate paths return
409 rather than choosing an arbitrary element. Unknown element/type IDs return
404. Invalid parent/type cycles return an explicit error.

List tools accept `offset` (default 0) and `limit` (default 50, maximum 100).
Results contain compact `[elementId, displayName]` pairs, sorted by case-folded
display name then element ID, with `total`, `returned`, `offset`, and
`nextOffset` when more results remain. `getCompositionProperties` accepts up to
50 object IDs, deduplicates them, sorts them the same way, and applies pagination
independently to each object's `properties` list. It follows composition only,
including nested components, not hierarchy children. No tool returns live values.

`getInstancesOfType` includes subtypes by default and returns object instances,
not variable properties or actions. It requires the already-warmed object-type
context for the same model revision; an unavailable or stale context returns
503 without refreshing it. Its results also contain `typeContextRevision`; pass
that value as `typeContextRevision` whenever continuing pagination with
`offset` greater than zero. If the type context has changed, the request returns
409 and pagination should restart at offset zero. Derived lookup indexes are
scoped to the current model snapshot/revision and discarded with it. Name
searches of at least three characters use a substring candidate index; shorter
searches scan cached names.

### What descriptions, keywords, priorities, and prompts do

- **Descriptions:** The MCP `tools/list` response includes each tool's
  description and input schema. Write descriptions as direct, short guidance:
  what question calls for the tool, what identifiers it needs, and what to do
  next with its results.
- **Keywords:** The override schema accepts a list of keywords, but the current
  MCP `tools/list` response does not include them. They are visible through the
  local `/mcp/tools` helper only; do not rely on them to route model tool calls.
- **Priority:** `low`, `normal`, or `high` is accepted by the override schema,
  but is also omitted from the MCP `tools/list` response. It does not currently
  rank or hide tools from the model. Use descriptions and tool schemas to
  communicate selection guidance instead.
- **Prompts:** Add prompt definitions under `prompts`, each with `description`,
  `inputs`, and `template`. Prompts are listed by the server and can be selected
  by the client; they are not automatically appended to every conversation.
  Keep templates focused on one repeatable task and state how to ground claims
  in returned values, quality, timestamps, and element IDs.

Keep the LM Studio system prompt short and use it for rules that should apply
to every chat. The MCP server also returns a short instruction string during
initialization, but task-specific MCP prompts still need to be selected by the
client. For example:

```text
Use i3x2ua MCP tools to ground questions about this plant. Never guess
elementIds. Discover the asset and relevant properties first, then read only
the required values. Include value quality and timestamps, and distinguish
returned evidence from inference. Do not create or change subscriptions unless
the user asks for ongoing monitoring.
```

For example, a prompt entry can be added alongside the tool overrides above:

```json
{
  "line_status": {
    "description": "Summarize the current status of a named production line using relevant model evidence.",
    "inputs": ["line_name"],
    "template": "Summarize the current status of {{line_name}}. If its elementId is unknown, call getObjectsV1(root=true) and match the returned name; never guess an ID. Navigate only the relevant branch with queryRelatedObjectsV1. Read current values only for identified property IDs with queryLastKnownValuesV1(maxDepth=1). Report element names, values, quality, and timestamps; distinguish observed facts from inference and say when data is missing."
  }
}
```

Merge the prompt entry into the existing top-level `prompts` object rather than
replacing the file with this partial example. Overrides are loaded at startup,
so restart i3x2ua for changes to take effect. If the JSON does not validate,
the server logs a warning and skips the overrides.

## 5. A compact tool-use workflow

For a question such as “What is the current temperature on Machine A?” guide
the model to:

1. Search or list the configured roots for Machine A; use only an element ID
   returned by the model.
2. Navigate or inspect the relevant branch to locate the temperature property.
3. Read the current value for that exact property, not the container.
4. Report the value with its quality and timestamp, and identify missing or
   stale evidence rather than guessing.

Ask for historical values only when a trend or time interval is requested.
Avoid full-model enumeration, broad relationship traversal, and schema catalogs
unless the task needs them. This keeps both tool outputs and model context
focused.

## 6. Local access and operational safety

Keep LM Studio and i3x2ua on a trusted local machine or network. The MCP endpoint
is a gateway into the data exposed by i3x2ua; use deployment authentication and
HTTPS when it is reachable remotely. Browse entrypoints reduce discovered model
scope but do not replace authorization.

The MCP tool catalog excludes REST `PUT` routes. Subscription tools can change
monitoring state, so the assistant should use them only when the user asks for
ongoing monitoring. See the README's
[API and MCP documentation](../README.md#api-surface) for additional behavior
and limitations.
