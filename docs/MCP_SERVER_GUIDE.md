# MCP Server Guide — Tools, Overrides, and Tuning

This guide explains the MCP server in i3x2ua: what tools it exposes, how a call
works, and how you shape it with `overrides/mcp_overrides.json` so your model
behaves well on **your** use case.

Related documents:
[`LOCAL_LLM_GUIDE.md`](LOCAL_LLM_GUIDE.md) (LM Studio setup),
[`VSCODE_MCP_LIVE_DATA_GUIDE.md`](VSCODE_MCP_LIVE_DATA_GUIDE.md) (VS Code setup),
[`QUICK_REFERENCE.md`](QUICK_REFERENCE.md),
[`adr/0003-mcp-support-policy.md`](adr/0003-mcp-support-policy.md).

---

## 60-second overview

- i3x2ua turns an OPC UA address space into an i3X graph: **objects**, **types**,
  and three relationship planes (**hierarchy**, **composition**, **graph**).
- The MCP server exposes that graph as tools, prompts, and resources over
  JSON-RPC at `/mcp`.
- MCP is **off by default**: set `I3X_ENABLE_MCP=1` and restart.
- Tool names, descriptions, prompts, and the tool list are configurable through
  `overrides/mcp_overrides.json`, loaded **once at startup** — restart after editing.
- Every `elementId` you use must come from a tool response. Never invent or guess IDs.

---

## 1. Starting the server

```bash
I3X_ENABLE_MCP=1          # enables tools, prompts, resources, /mcp routes
I3X_MCP_INCLUDE_OPCUA_METADATA=0   # 1 = richer type schemas (bigger payloads)
I3X_CORS_ALLOWED_ORIGINS='["http://localhost:1234"]'  # exact client origin, no wildcard
```

Endpoints:

| Endpoint | Purpose |
|---|---|
| `POST /mcp` | MCP JSON-RPC (initialize, `tools/list`, `tools/call`, `prompts/list`, `prompts/get`, `resources/*`) |
| `GET /mcp/tools` | Full catalog including `priority`, `keywords`, method, path, and schemas — best for debugging your overrides |
| `POST /mcp/call` | Call one tool over plain REST (same argument rules as MCP) |
| `GET /mcp/prompts`, `GET /mcp/prompts/{name}`, `POST /mcp/prompts/execute` | List, inspect, render prompts |
| `GET /mcp/resources`, `POST /mcp/resources/read` | Guides, model overview, roots, OpenAPI, the overrides file itself |

Useful resources: `i3x://model/overview`, `i3x://opcua-to-i3x-mapping`,
`i3x://element-id-and-type-id`, `i3x://openapi`, `i3x://mcp-overrides`,
`i3x://roots/<elementId>`.

> Writing the file is only half the job: **restart the server**, then check
> `GET /mcp/tools` to confirm your changes landed.

---

## 2. The tools

38 tools are defined; the shipped overrides exclude 21 of them, so 17 are
active. The tables below describe implemented tools, including tools disabled
by the shipped overrides.

### A. Discovery — find things by name

| Tool | What it does | Arguments | Limits |
|---|---|---|---|
| `getObjectsV1` | Lists objects. MCP defaults to `root=true`, so you get top-level assets (Plant, Line, Machine…) | `root`, `typeElementId`, `includeMetadata`, `limit`, `offset` | page default **50**, max 500; result compacted to `elementId` + `displayName` |
| `searchModelElements` | Case-insensitive substring search across cached names and metadata, with exact filters | `text` (required), `kind`, `semanticRole`, `namespaceUri`, `typeElementId`, `mappingConfidence`, `appliedProfileId`, `relationshipType`, `limit`, `offset` | page default 25, max 100 |
| `findElementsByName` | Same idea, simpler: name substring lookup on the cached model | `text`, `limit`, `offset` | page default 50 |
| `findElementsByNamespace` | Elements in one namespace URI | `namespaceUri`, `limit`, `offset` | page default 50 |
| `findElementsByRole` | Elements with one semantic role (`asset`, `group`, `component`, `datapoint`, `property`, `unknown`) | `semanticRole`, `limit`, `offset` | page default 50 |

### B. Navigation — walk from IDs you already have

| Tool | What it does | Arguments | Limits |
|---|---|---|---|
| `queryRelatedObjectsV1` | Follows relationships from a few real `elementIds`: components, properties, Memory, DataBlocks | `body.elementIds`, optional `body.relationshipType`, `body.includeMetadata`, `body.limit`, `body.offset` | 50 related objects per source per page, `limit` max 500 |
| `getChildren` | Direct children in one plane | `elementId`, `plane` (`hierarchy`/`composition`/`graph`), `limit`, `offset` | page default 50 |
| `getCompositionProperties` | Properties belonging to a few objects | `elementIds` (max 50), `limit`, `offset` | page default 50 |
| `getElementContext` | One call that returns an element plus ancestors, children, relationships, type and metadata | `elementId`, `ancestorDepth`, `childLimit`, `relationshipLimit`, `includeChildren`, `includeRelationships`, `includeTypeAndMetadata` | depth ≤ 32 (default 3), limits ≤ 500 (defaults 100) |
| `summarizeSubtree` | Overview of a subtree without dumping every node | `rootElementId`, `maxDepth`, `includeHierarchy`, `includeComposition`, `includeProperties`, `includeActions`, `maxNodes` | depth ≤ 32 (default 4), nodes ≤ 500 (default 200) |
| `traceRelationshipPath` | Shortest connection between two known elements | `sourceElementId`, `targetElementId`, `relationshipPlanes`, `direction` (`outgoing`/`incoming`/`either`), `maxDepth` | depth ≤ 32 (default 8), search capped at 5000 nodes |
| `getBrowsePath` | Canonical browse path for one element | `elementId` | — |
| `getInstancesOfType` | All instances of a type (and its subtypes) | `typeElementId`, `includeSubtypes`, `offset`, `limit`, `typeContextRevision` | offset ≥ 1 requires `typeContextRevision` |
| `resolveBrowsePath` | Path of display names → element, e.g. `Machines/Line1/Station5/Temperature` | `path` **or** `segments` | one of the two required |

### C. Reading values

| Tool | What it does | Arguments | Limits |
|---|---|---|---|
| `queryLastKnownValuesV1` | Current value, quality, timestamp for a few exact property IDs | `body.elementIds`, `body.maxDepth` | `maxDepth` default 1 (object only); N > 1 recurses up to N composition levels; 0 = unlimited, bounded by `I3X_COMPOSITION_MAX_DEPTH`/`I3X_COMPOSITION_MAX_NODES` (a truncated tree surfaces as HTTP 206 with a top-level `responseDetail`) |
| `readContainerValues` | Many current values under **one** container in one short call — the server finds the descendant properties | `containerId`, `limit`, `offset` | page default 50, max 50; internally batches 10 reads; max 5000 nodes, max depth 32; no container values, no history |
| `queryHistoricalValuesV1` | Samples over a time interval | `body.elementIds`, `body.startTime`/`body.endTime` (RFC 3339 with timezone), `body.maxDepth`, `body.limit`, `body.offset` | 100 samples per page by default, `limit` max 500; composition recursion bounded by `I3X_COMPOSITION_MAX_DEPTH`/`I3X_COMPOSITION_MAX_NODES` (HTTP 206 when truncated) |

Rule of thumb: one or a few signals → `queryLastKnownValuesV1`; a whole DataBlock
or Memory area → `readContainerValues`; a trend → `queryHistoricalValuesV1`.

### D. Server info and schema vocabulary (use sparingly)

| Tool | What it does | Arguments |
|---|---|---|
| `getInfoV1` | Server version and capabilities | none |
| `getNamespacesV1` | OPC UA namespace list — diagnostics only | none |
| `getObjectTypesV1` | Full type catalog | `namespaceUri`, `limit`, `offset` (page 50) |
| `queryObjectTypesV1` | Type schemas for exact IDs | `body.elementIds` |
| `getRelationshipTypesV1` | Full relationship vocabulary | `namespaceUri`, `limit`, `offset` (page 50) |
| `queryRelationshipTypesV1` | Relationship types for exact IDs | `body.elementIds` |

### E. Monitoring (changes server state)

`clientId` is a scope identifier, **not** a credential.

| Tool | What it does | Arguments |
|---|---|---|
| `createSubscriptionV1` | Creates a monitoring subscription | `body.clientId`, `body.displayName` |
| `registerMonitoredItemsV1` | Adds known property IDs to a subscription | `body.clientId`, `body.subscriptionId`, `body.elementIds`, `body.maxDepth` |
| `removeMonitoredItemsV1` | Removes monitored items | `body.clientId`, `body.subscriptionId`, `body.elementIds` |
| `syncSubscriptionV1` | Fetches and acknowledges pending updates (`lastSequenceNumber: -1` discards all) | `body.clientId`, `body.subscriptionId`, `body.lastSequenceNumber` |
| `listSubscriptionsV1` | Inspects subscription configuration | `body.clientId`, `body.subscriptionIds` |
| `deleteSubscriptionsV1` | Deletes subscriptions | `body.clientId`, `body.subscriptionIds` |

### F. Identity mapping

| Tool | What it does | Arguments |
|---|---|---|
| `resolveElementIdsToNodeIds` | i3X `elementId` → OPC UA NodeId | `elementIds` (max 500) |
| `resolveNodeIdsToElementIds` | OPC UA NodeId → i3X `elementId` | `nodeIds` (max 500) |

### Not available through MCP

- `PUT` routes (value/history writes) are never exposed as tools. REST writes
  need `I3X_ENABLE_WRITES=1` and stay outside MCP.
- `streamSubscriptionV1` (SSE streaming) is never exposed; poll with
  `syncSubscriptionV1` instead.
- The shipped overrides exclude these tools: `getUaStatus`, `getUaConnection`,
  `getUaLimits`, `getUaMetrics`, `getModelNamespaceGapsV1`, `getModelMetricsV1`,
  `searchModelElements`, `findElementsByName`, `findElementsByNamespace`,
  `findElementsByRole`, `summarizeSubtree`, `traceRelationshipPath`,
  `getCompositionProperties`, `getElementContext`, `getInstancesOfType`,
  `createSubscriptionV1`, `registerMonitoredItemsV1`,
  `removeMonitoredItemsV1`, `syncSubscriptionV1`, `listSubscriptionsV1`, and
  `deleteSubscriptionsV1`. Remove a name from `excluded_tools` to make that
  tool available again.

### Cached-model tools vs REST tools

The `/mcp/model/*` tools (`searchModelElements`, `getElementContext`,
`getChildren`, `getBrowsePath`, `resolveBrowsePath`, …) read only the
**in-memory model**: they never contact OPC UA and never rebuild the model.
They return `modelRevision`; do not mix pages from different revisions. If the
model is not built yet they answer **503** instead of starting discovery.
The REST-derived tools (`getObjectsV1`, `queryLastKnownValuesV1`, …) may fetch
fresh data from the OPC UA server.

---

## 3. How a tool call works

1. The model sends tool arguments; the server checks them against the tool's
   JSON schema. Extra or missing arguments are rejected with a clear message.
2. Cached-model tools run directly on the model. The other tools are forwarded
   internally to their REST route.
3. Large list results are **compacted** (mostly `elementId` + `displayName`) and
   **paginated**: `pagination.nextOffset` tells you how to continue with the same
   filters.
4. You get `{ statusCode, body }`; batch tools also report per-item `success`.
5. `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` tell the
   client whether a call is safe; subscription mutations are marked accordingly.

The healthy pattern for almost every question:

```text
discover  getObjectsV1(root=true)      → pick the asset by name
navigate  queryRelatedObjectsV1        → follow only the needed branch
read      queryLastKnownValuesV1       → one or a few exact property IDs
          readContainerValues          → many values under one container
history   queryHistoricalValuesV1      → only when a trend is asked for
```

---

## 4. The overrides file

**File:** `overrides/mcp_overrides.json`
**Schema:** `overrides/schema.json`
**Loaded:** at startup, validated with JSON Schema.

| Top-level key | Required | Meaning |
|---|---|---|
| `tools` | yes | Per-tool overrides, keyed by exact tool name |
| `prompts` | yes | Prompt templates (use `{}` when you have none) |
| `excluded_tools` | no | Tool names removed from discovery **and** calls |
| `$schema` | no | Points at `./schema.json` so editors flag mistakes |

Tool override fields:

| Field | Required | Type | Effect |
|---|---|---|---|
| `description` | yes | string | The text your model reads when choosing and using the tool |
| `priority` | no | `low` \| `normal` \| `high` | Catalog metadata; see section 7 |
| `keywords` | no | array of strings | Catalog metadata; see section 7 |

Prompt override fields (all three required):

| Field | Type | Effect |
|---|---|---|
| `description` | string | Shown in `prompts/list`; says when the prompt is meant to be used |
| `inputs` | array of non-empty strings | Placeholder names the caller must supply |
| `template` | string | Message text; `{{name}}` placeholders are filled from the arguments |

Rules worth knowing:

- Keys must match tool names exactly (`getObjectsV1`, not `get_objects_v1`).
- A key for a tool that does not exist is ignored at runtime, but the test suite
  flags it — keep the file in sync with `GET /mcp/tools`.
- **Any invalid JSON or schema violation disables *all* overrides.** The server
  logs a warning like `MCP overrides validation failed … Overrides were not
  applied.` and starts with built-in descriptions. An unreadable file (missing
  permissions, a directory instead of a file, a non-UTF-8 payload) is skipped the
  same way — the server starts with built-in descriptions instead of failing at
  startup.
- Overrides apply to REST-derived tools, `readContainerValues`, and the
  cached-model tools.
- Omitted fields keep the built-in default.
- In Docker Compose the repository's `overrides` directory is mounted at
  `/app/overrides`; edit the host file and restart the container.

Minimal valid file:

```json
{
  "$schema": "./schema.json",
  "tools": {
    "getObjectsV1": {
      "description": "DISCOVERY: start here for unknown assets. Defaults to root=true and returns 50 compact objects; continue with pagination.nextOffset.",
      "priority": "high",
      "keywords": ["discover assets", "root assets"]
    }
  },
  "prompts": {},
  "excluded_tools": ["getUaMetrics"]
}
```

Check your work:

```bash
curl http://localhost:8000/mcp/tools          # effective descriptions, priority, keywords
curl http://localhost:8000/mcp/prompts        # prompt names and descriptions
# or read the resource i3x://mcp-overrides through MCP
```

---

## 5. Turning misbehaving tools off

Some models pick the wrong tool, enumerate the whole model, or burn turns on
catalogs. Removing the tempting tools is the fastest fix.

Add names to `excluded_tools`; they disappear from `tools/list`, `/mcp/tools`,
and any call returns `Unknown tool <name>`.

```json
{
  "$schema": "./schema.json",
  "tools": { /* keep your descriptions */ },
  "prompts": { /* keep your prompts */ },
  "excluded_tools": [
    "getUaStatus",
    "getUaConnection",
    "getUaLimits",
    "getUaMetrics",
    "getModelNamespaceGapsV1",
    "getModelMetricsV1",
    "getObjectTypesV1",
    "getRelationshipTypesV1",
    "queryRelationshipTypesV1",
    "getNamespacesV1"
  ]
}
```

Recipes:

| Situation | Exclude | Why |
|---|---|---|
| Small local model wanders through catalogs | `getObjectTypesV1`, `getRelationshipTypesV1`, `queryRelationshipTypesV1`, `getNamespacesV1` | Full listings are long and never answer an asset question |
| Model enumerates the whole model | `listObjectsByIdV1`, `getInstancesOfType` | Enables bulk enumeration; keep discovery on `getObjectsV1` |
| Read-only assistant, no monitoring | `createSubscriptionV1`, `registerMonitoredItemsV1`, `removeMonitoredItemsV1`, `syncSubscriptionV1`, `deleteSubscriptionsV1`, `listSubscriptionsV1` | Removes every state-changing call |
| Weak tool caller | also `traceRelationshipPath`, `summarizeSubtree`, `getModelNamespaceGapsV1` | Advanced graph tools need precise IDs |
| Two overlapping search tools confuse the model | `findElementsByName`, `findElementsByNamespace`, `findElementsByRole` | Keep `searchModelElements` as the single search entry point |

Never exclude the core path: `getObjectsV1`, `queryRelatedObjectsV1`,
`queryLastKnownValuesV1`. Without them the model cannot answer data questions.

Other scope knobs that help before you touch descriptions:

| Setting | Effect |
|---|---|
| `I3X_OPCUA_BROWSE_ROOTS='["nsu=urn:example:plant;s=Line1"]'` | Limits the discovered model, so every tool returns less |
| `I3X_MCP_INCLUDE_OPCUA_METADATA=0` | Keeps type schemas compact |
| `I3X_ENABLE_WRITES=0` | No REST writes at all |
| `I3X_MODEL_REFRESH_INTERVAL_SECONDS=0` | Stops background refresh churn |

---

## 6. Fine-tuning tool descriptions

The description is the only thing the model sees when choosing a tool. Write it
as a **decision rule**, not a feature list.

Anatomy of a good description:

1. **Label the job** in one word at the front: `DISCOVERY:`, `NAVIGATION:`,
   `READ CURRENT VALUES`, `ADVANCED:`.
2. **When to use it** — the condition, not the capability.
3. **When not to use it** — name the tool that should be used instead.
4. **The limit** — page size, `maxDepth`, how to continue.
5. **The safety rule** — never guess IDs, never enumerate, never read containers.

Keep it around 40–70 words. Long synonym lists dilute the signal.

Before → after for a model that kept dumping the whole model:

```json
{
  "getObjectsV1": {
    "description": "DISCOVERY: Start unknown-asset searches here. Defaults to root=true and returns at most 50 compact objects; follow pagination.nextOffset with the same filters. Set root=false only when full-model enumeration is explicitly requested. Never guess typeElementId values; filter only with an exact known type ID.",
    "priority": "high",
    "keywords": ["discover assets", "root assets"]
  },
  "queryRelatedObjectsV1": {
    "description": "NAVIGATION: Follow relationships from real elementIds returned by discovery, at most 50 per source; continue with body.offset. Follow only the branch you need. Leave relationshipType unset unless its exact value is known. Not a full-model search.",
    "priority": "high",
    "keywords": ["navigate", "components", "properties"]
  },
  "readContainerValues": {
    "description": "READ MANY CURRENT VALUES under one discovered container. Pass the exact containerId; the server finds descendant properties and returns at most 50 per page. Continue with offset. Do not use on a plant root unless asked for all its values.",
    "priority": "high",
    "keywords": ["bulk current values", "datablock values", "memory variables"]
  }
}
```

Use-case recipes (replace the shipped text with the variant that matches your
deployment):

| Use case | Emphasis in descriptions |
|---|---|
| PLC / DataBlock inspection | Push `readContainerValues` with the container ID; forbid assembling long `elementIds` arrays; forbid guessed type IDs such as `"plc"` |
| Machine health / alarm triage | Keep `queryLastKnownValuesV1` with `maxDepth=1` as the primary read; make history conditional on an explicit interval |
| Process / energy analysis | History first, narrow window, only named signal IDs; type and namespace catalogs marked `ADVANCED` |
| Mapping / integration work | Allow `getNamespacesV1` and type lookups only for diagnosed mapping issues |

Testing loop:

1. Edit one description at a time.
2. Restart, then `GET /mcp/tools` to confirm it applied.
3. Ask the same realistic question three times (find a signal, read a value,
   ask for a trend).
4. If the model still misuses a tool, tighten the "when not to use it" clause or
   exclude the competing tool.
5. Keep the file under version control so a bad tweak is one revert away.

---

## 7. Fine-tuning prompts

Prompts are ready-made instructions your client can attach before the model
starts working. They are listed in `prompts/list` and also appear as resources
`i3x://prompts/<name>`. Rendering rules:

- Every `{{placeholder}}` in `template` must appear in `inputs`.
- Calling a prompt without all inputs returns `400 Missing prompt inputs: …`.
- Placeholders accept any value the caller passes — an element ID *or* a name;
  say so in the template.

Write a prompt that encodes your workflow, not your prose:

```json
{
  "prompts": {
    "read_one_signal": {
      "description": "Locate one named signal and return its current value",
      "inputs": ["asset_name", "signal_name"],
      "template": "Find signal {{signal_name}} under asset {{asset_name}}.\n\n1. If the asset ID is unknown: getObjectsV1(root=true), match by name.\n2. Navigate only the relevant branch with queryRelatedObjectsV1.\n3. Read the matching property with queryLastKnownValuesV1(elementIds=[exact ID], maxDepth=1).\nNever guess IDs, never enumerate the model, never read history unless asked.\nReport value, quality, timestamp, and the property path; say so if nothing matched."
    }
  }
}
```

Prompt guidelines:

- 6–12 lines. Each step names the exact tool and the argument shape.
- Include the "do not" list — it is what stops enumeration loops.
- Ask for an explicit answer format (value, quality, timestamp, path).
- One prompt per recurring task; a long "do everything" prompt gets ignored.
- Keep the shipped prompts you do not use? They cost nothing at runtime, but a
  shorter list is easier for clients that show all prompts to the user.

---

## 8. What `priority` and `keywords` actually do

Be precise here, because they are *not* a selection mechanism:

- They are stored on the tool definition and returned by `GET /mcp/tools`.
- MCP `tools/list` sends only `name`, `description`, `inputSchema`,
  `outputSchema`, and `annotations` — **the model never sees priority or
  keywords**.
- So they are useful as catalog metadata: documenting intent, keeping a readable
  file, and ranking importance when you or a teammate inspect the server.

If you want behaviour changes, change the **description** (and remove competing
tools). That is the lever that works.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Tools missing in the client | MCP disabled | `I3X_ENABLE_MCP=1`, restart, reconnect |
| Descriptions unchanged after editing | Server not restarted, or validation failed | Restart; check logs for `Overrides were not applied`; fix JSON |
| `Unknown tool <name>` | Tool listed in `excluded_tools` | Remove it from the array |
| `Unexpected arguments: …` | Argument not part of that tool's schema | Use only documented fields; body fields go inside `body` |
| `503` from `searchModelElements` etc. | Model cache not built yet | Let discovery/preload run, or call a REST discovery tool first |
| Results look truncated | Pagination | Follow `pagination.nextOffset` / `body.offset` with the same filters |
| Huge payloads | Catalog or metadata tools | Exclude catalogs; `I3X_MCP_INCLUDE_OPCUA_METADATA=0`; narrow browse roots |
| Client rejected with untrusted `Origin` | CORS | Set the exact origin in `I3X_CORS_ALLOWED_ORIGINS`, no wildcard |

---

## 10. Golden rules

1. IDs come from responses, never from imagination.
2. Discover → navigate → read; one branch at a time.
3. Values: few IDs with `maxDepth=1`; bulk under a container via
   `readContainerValues`; history only on request, with a bounded window.
4. Descriptions are decision rules, ~40–70 words, with a "do not" clause.
5. Exclude tempting tools instead of writing longer warnings.
6. Edit → validate → restart → verify with `GET /mcp/tools`.
7. Keep monitoring tools available only when someone actually wants live updates.
