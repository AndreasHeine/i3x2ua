# i3X + LM Studio MCP Integration Guide

This guide shows how to use the existing i3X FastAPI server as the source of truth and expose it to an LLM through a small MCP bridge.

The important constraint is that the i3X API itself stays unchanged. The server already serves its OpenAPI document at `/openapi.json`, and that document is the contract the bridge uses to generate tools.

## Architecture

Use three pieces:

1. i3X API server: the existing FastAPI app in `i3x_server.main:app`
2. MCP bridge: a small local service that reads `openapi.json`, generates tools, and forwards calls to i3X
3. LM Studio: configured to connect to the MCP bridge, not directly to the raw REST API

The bridge is what makes the API safe and model-friendly. It can validate arguments, enforce auth, apply guardrails, and normalize errors before LM Studio sees them.

## Step 1: Start the API and confirm OpenAPI is reachable

Run the existing server the usual way:

```bash
uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000
```

Then confirm the contract is available:

- `http://127.0.0.1:8000/openapi.json`
- `http://127.0.0.1:8000/docs`

The OpenAPI document is the source of truth. The bridge should read operation IDs and schemas from that document instead of hard-coding tool definitions.

## Step 2: Generate MCP tools from OpenAPI

Every OpenAPI operation with an `operationId` can become one tool. In i3X, the generated tool name should match the `operationId` exactly so the LLM sees stable, descriptive names such as `getNamespaces`, `queryLastKnownValues`, and `createSubscription`.

Recommended generation rules:

- Use `operationId` as the tool name
- Use `summary` or `description` as the tool description
- Map path parameters and query parameters into top-level tool inputs
- Put JSON request bodies under a `body` field
- Mark required parameters as required in the tool schema
- Skip operations without an `operationId`

Example generator:

```python
import httpx


async def generate_tools_from_openapi(openapi_url: str) -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        spec = (await client.get(openapi_url)).json()

    tools: dict[str, dict] = {}

    for path, methods in spec["paths"].items():
        for method, details in methods.items():
            operation_id = details.get("operationId")
            if not operation_id:
                continue

            input_schema: dict[str, object] = {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            }

            for param in details.get("parameters", []):
                name = param["name"]
                schema = param.get("schema", {"type": "string"})
                input_schema["properties"][name] = schema
                if param.get("required"):
                    input_schema["required"].append(name)

            request_body = details.get("requestBody", {})
            body_schema = (
                request_body.get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if body_schema:
                input_schema["properties"]["body"] = body_schema
                input_schema["required"].append("body")

            tools[operation_id] = {
                "method": method.upper(),
                "path": path,
                "description": details.get("summary", details.get("description", "")),
                "input_schema": input_schema,
            }

    return tools
```

## Step 3: Build the MCP bridge

The bridge can be a separate Python app. Keeping it separate makes the transport boundary explicit and avoids confusing the i3X business API with the MCP protocol itself.

Suggested bridge behavior:

- Fetch the i3X OpenAPI document at startup
- Build tools from `operationId`
- Forward tool calls to `http://localhost:8000` in development
- Pass through auth headers if the upstream API requires them
- Reject unexpected arguments before making the upstream request
- Convert upstream HTTP errors into MCP errors with useful messages

Minimal bridge shape:

```python
from __future__ import annotations

import httpx


I3X_BASE_URL = "http://127.0.0.1:8000"


async def call_i3x_tool(tool: dict, arguments: dict) -> dict:
    url = I3X_BASE_URL + tool["path"]
    body = arguments.pop("body", None)

    for name, value in list(arguments.items()):
        placeholder = "{" + name + "}"
        if placeholder in url:
            url = url.replace(placeholder, str(value))
            arguments.pop(name)

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            tool["method"],
            url,
            params=arguments,
            json=body,
        )

    response.raise_for_status()
    return response.json()
```

If you are using an MCP SDK, expose those generated tools through the SDK’s tool registration API. If you are using streamable HTTP MCP, run the bridge on a separate port and point LM Studio at it.

## Step 4: Use the real i3X operations as tools

The current i3X OpenAPI document exposes the following high-value operation IDs for LLM use:

- `getInfo`
- `getNamespaces`
- `getObjectTypes`
- `queryObjectTypesById`
- `getRelationshipTypes`
- `queryRelationshipTypesById`
- `getObjects`
- `listObjectsById`
- `queryRelatedObjects`
- `queryLastKnownValues`
- `queryHistoricalValues`
- `getHistoricalValues`
- `createSubscription`
- `registerMonitoredItems`
- `removeMonitoredItems`
- `syncSubscription`
- `streamSubscription`
- `listSubscriptions`
- `deleteSubscriptions`

For LLM prompting, the most important semantics are:

- Use `queryLastKnownValues` for current or last-value questions
- Use `queryHistoricalValues` or `getHistoricalValues` for time-range questions
- Use `getNamespaces` and `getObjectTypes` for exploration
- Use `createSubscription`, `registerMonitoredItems`, and `syncSubscription` for live updates

## Step 5: Tell the model how to use the tools

Give the model a short, explicit system or developer instruction block. Keep it operational rather than descriptive.

Example:

```text
You are connected to an i3X industrial data API through MCP tools.

Use `getInfo` first when you need server capabilities or versioning.
Use `getNamespaces` and `getObjectTypes` to explore the information model.
Use `queryLastKnownValues` for current values.
Use `queryHistoricalValues` for time-series questions.
Use `createSubscription` and `syncSubscription` for live updates.

Prefer the narrowest tool that answers the user’s request.
Do not call broad recursive queries unless the user explicitly asks for all descendant values.
Treat `maxDepth=0` as recursive traversal and use it only when the user asks for a full tree.
```

Useful few-shot examples:

```text
User: What is the current temperature of sensor-123?
Assistant: Call queryLastKnownValues with {"elementIds":["sensor-123"],"maxDepth":1}.

User: Show the temperature trend for sensor-123 from 2024-01-01 to 2024-01-02.
Assistant: Call queryHistoricalValues with {"elementIds":["sensor-123"],"startTime":"2024-01-01T00:00:00Z","endTime":"2024-01-02T00:00:00Z","maxDepth":1}.

User: What namespaces and object types are available?
Assistant: Call getNamespaces first, then getObjectTypes if needed.
```

## Step 6: Configure LM Studio

Point LM Studio at the MCP bridge, not the i3X REST API directly.

If your LM Studio version uses an `mcp.json` configuration, add an entry for the bridge using the transport that your LM Studio build supports. The exact on-disk path can vary by platform and LM Studio release, but the key idea is the same: register the bridge as an MCP server and restart LM Studio.

Example shape:

```json
{
  "mcpServers": {
    "i3x-api-mcp": {
      "url": "http://127.0.0.1:9000"
    }
  }
}
```

If your LM Studio setup expects a local command instead of a URL, run the bridge as a local process and register that command instead.

## Step 7: Validate the loop

1. Start the i3X API.
2. Start the MCP bridge.
3. Confirm the bridge can fetch `http://127.0.0.1:8000/openapi.json`.
4. Confirm the bridge exposes the expected tool list.
5. Open LM Studio and verify the bridge appears as an MCP server.
6. Ask a current-value question, a historical question, and an exploration question.
7. Watch the tool calls and confirm the upstream i3X requests match the prompt.

If a tool call fails, inspect two things first:

- The generated arguments versus the OpenAPI schema
- The upstream i3X response envelope and status code

## Step 8: Security and reliability guidance

- Keep the i3X API behind auth in production; the bridge should forward credentials rather than inventing them
- Treat the bridge as a policy layer, not just a proxy
- Reject unbounded queries when they are obviously unsafe for the current environment
- Prefer narrow queries over recursive traversal unless the user explicitly wants the full tree
- Log tool name, upstream method, path, and status code separately so you can audit model behavior
- Regenerate the tool map whenever the OpenAPI document changes

## Recommended implementation note

Do not add ad-hoc `/mcp/tools` and `/mcp/call` routes to the i3X API unless you are intentionally implementing the MCP transport there. The safer default is to keep the REST API as the product surface and layer MCP on top as a separate bridge.