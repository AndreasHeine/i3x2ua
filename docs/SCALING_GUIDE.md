# i3x2ua Scaling Guide

This guide summarizes measured REST API scaling results and provides practical guidance for sizing Uvicorn workers, choosing deployment architectures, handling subscriptions, and operating i3x2ua at scale.

The reported figures should be treated as observations from specific test environments, not as capacity guarantees. Actual performance depends on OPC UA server behavior, information model size, request patterns, subscription volume, network latency, available hardware, and deployment topology.

---

# Scaling Philosophy

When scaling i3x2ua it is important to distinguish between four different concerns:

- Discovery scaling
- Query scaling
- Subscription scaling
- OPC UA connection scaling

Not all workloads scale the same way.

Most i3x2ua APIs are stateless and scale naturally across multiple workers.

Subscription management currently relies on process-local state and therefore requires additional architectural considerations.

---

# API Categories

Understanding which API category a request belongs to helps determine the best scaling strategy.

## Stateless APIs

These APIs scale naturally across multiple workers and instances.

### Information

```text
GET /v1/info
```

### Discovery

```text
GET  /v1/namespaces

GET  /v1/relationshiptypes
POST /v1/relationshiptypes/query

GET  /v1/objecttypes
POST /v1/objecttypes/query

GET  /v1/objects
POST /v1/objects/list
POST /v1/objects/related
```

### Query

```text
POST /v1/objects/value
POST /v1/objects/history
```

### Update

```text
PUT /v1/objects/value
PUT /v1/objects/history
```

### Diagnostics

```text
GET /model/metrics
GET /model/namespace-gaps

GET /ua/status
GET /ua/connection
GET /ua/limits
GET /ua/metrics
```

### MCP

```text
GET  /mcp/tools
POST /mcp/call

GET  /mcp/prompts
GET  /mcp/prompts/{name}
POST /mcp/prompts/execute

GET  /mcp/resources
POST /mcp/resources/read

GET  /mcp/roots

POST /mcp
```

These APIs generally benefit directly from additional workers.

---

# Subscription APIs

The subscription APIs are different.

```text
POST /v1/subscriptions
POST /v1/subscriptions/register
POST /v1/subscriptions/unregister
POST /v1/subscriptions/stream
POST /v1/subscriptions/sync
POST /v1/subscriptions/delete
POST /v1/subscriptions/list
```

These APIs maintain subscription state which currently exists only inside the worker process that owns the subscription.

As a result, discovery scaling and subscription scaling behave differently.

---

# Scaling Models

i3x2ua supports several deployment models with different trade-offs.

## Deployment Model Comparison

| Model | Discovery | Query | Polling Subscriptions | SSE Streams | OPC UA Sessions | Complexity |
|---------|---------|---------|---------|---------|---------|---------|
| Single Worker | ✅ | ✅ | ✅ | ✅ | 1 | Low |
| Multiple Workers | ✅✅✅ | ✅✅✅ | ⚠ | ✅ | N | Low |
| Discovery / Subscription Split | ✅✅✅ | ✅✅✅ | ✅ | ✅ | N+1 | Medium |
| Multi-Instance Routing | ✅✅✅ | ✅✅✅ | ✅ | ✅ | 1 per instance | Medium |
| Hybrid Deployment | ✅✅✅ | ✅✅✅ | ✅ | ✅ | Multiple | High |

---

# Single Worker Deployment

## Architecture

```text
Client
   │
   ▼
NGINX
   │
   ▼
i3x2ua (1 Worker)
   │
   ▼
OPC UA Server
```

## Advantages

- Lowest memory footprint
- Lowest OPC UA session count
- Shared cache state
- Simplest deployment
- Full subscription compatibility
- Recommended default deployment

## Recommended For

- Machines
- CNCs
- Robots
- Packaging systems
- Test stations
- Production cells

---

# Multi-Worker Deployment

## Architecture

```text
Client
   │
   ▼
NGINX
   │
   ▼
┌─────────┐
│ Worker1 │
├─────────┤
│ Worker2 │
├─────────┤
│ Worker3 │
├─────────┤
│ Worker4 │
└─────────┘
   │
   ▼
OPC UA Server
```

Example:

```bash
uvicorn i3x_server.main:app --workers 4
```

or

```bash
uvicorn i3x_server.main:app --workers 8
```

## Advantages

- Higher throughput
- Better CPU utilization
- Better discovery performance
- Better query performance
- Better MCP performance

## Trade-offs

- More memory
- Duplicate caches
- Additional OPC UA sessions
- Subscription state becomes worker-local

## Recommended For

- Discovery-heavy workloads
- Asset browsing
- Query-heavy environments
- MCP workloads
- Analytics workloads

---

# Polling Subscription Scaling

Polling subscriptions follow this workflow:

```text
POST /v1/subscriptions

POST /v1/subscriptions/register

POST /v1/subscriptions/sync

POST /v1/subscriptions/sync

POST /v1/subscriptions/sync

POST /v1/subscriptions/delete
```

The subscription is stored within the worker process that created it.

Example:

```text
Worker A
    creates subscription
```

Later:

```text
Worker B
    receives sync request
```

Worker B does not necessarily know the subscription.

Possible effects:

```text
Subscription not found

Missing updates

Unexpected results
```

This is why polling subscriptions currently require additional deployment considerations.

---

# SSE Streaming

SSE follows a different behavior pattern.

Workflow:

```text
POST /v1/subscriptions

POST /v1/subscriptions/register

POST /v1/subscriptions/stream
```

Once the stream is established:

```text
Client
   │
   ▼
Worker A
```

The TCP connection remains attached to the same worker.

Because the subscription state and the stream remain in the same process:

```text
SSE generally scales well in multi-worker deployments.
```

---

# Discovery / Subscription Split

This deployment pattern isolates polling subscriptions from discovery traffic.

## Architecture

```text
NGINX
 ├── Discovery Service (4 Workers)
 └── Subscription Service (1 Worker)
```

NGINX routes:

```text
/v1/subscriptions*
```

to:

```text
Subscription Service
```

and everything else to:

```text
Discovery Service
```

## Advantages

```text
✅ Scalable discovery

✅ Stable polling subscriptions

✅ Stable SSE streams

✅ No worker affinity required
```

## Trade-offs

```text
Additional OPC UA client

Additional OPC UA session

Additional containers
```

## Recommended For

- Factory gateways
- MES integration
- Dashboard environments
- Mixed workloads

---

# Multi-Instance Deployment

Multiple independent i3x2ua instances.

## Architecture

```text
/i3x/instance1/*
/i3x/instance2/*
/i3x/instance3/*
```

Each instance maintains:

```text
Own OPC UA Connection

Own Cache

Own Subscription Registry

Own Worker Process
```

## Advantages

```text
Isolation between systems

Independent lifecycle

Independent subscriptions

Independent OPC UA sessions
```

## Recommended For

```text
Multiple OPC UA servers

Tenant isolation

Departmental environments

Hub-and-spoke architectures
```

---

# Hybrid Enterprise Deployment

Large deployments often combine multiple patterns.

## Architecture

```text
NGINX

├── Discovery Cluster
│    ├── Worker
│    ├── Worker
│    ├── Worker
│    └── Worker
│
├── Subscription Service
│
├── Instance A
│
├── Instance B
│
└── Instance C
```

## Advantages

```text
Maximum discovery throughput

Stable subscriptions

Independent server groups

Clear operational boundaries
```

## Recommended For

- Enterprise gateways
- Digital twins
- Multiple factories
- Corporate analytics platforms

---

# NGINX Connection Reuse

NGINX upstream keepalive pooling can significantly improve throughput and reduce backend connection churn.

Recommended configuration:

```nginx
upstream i3x2ua_backend {
    server i3x2ua:8000;

    keepalive 32;
    keepalive_requests 1000;
    keepalive_timeout 60s;
}

location / {
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    proxy_connect_timeout 5s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;

    proxy_pass http://i3x2ua_backend;
}
```

Additional recommendations:

```nginx
worker_processes auto;

events {
    worker_connections 4096;
}
```

Docker:

```yaml
ulimits:
  nofile:
    soft: 65535
    hard: 65535
```

---

# Worker Selection Guide

## Single Machine

```text
1 Worker
```

Typical:

- CNC
- Robot
- Packaging Machine
- Test Station

---

## Production Cell

```text
1 Worker
```

Typical:

- Several machines
- Local dashboard
- Engineering access

---

## Production Line

```text
1-2 Workers
```

Typical:

- MES integration
- Multiple clients
- Operational dashboards

---

## Factory Gateway

Recommended:

```text
4 Workers
```

or:

```text
Discovery / Subscription Split
```

Typical:

- Multiple machines
- Multiple dashboards
- Continuous discovery queries

---

## Enterprise Gateway

Recommended:

```text
4-8 Workers
```

or:

```text
Hybrid Deployment
```

Typical:

- Multiple OPC UA servers
- Digital Twin Platforms
- Analytics
- Enterprise Applications

---

# MCP Scaling Considerations

The MCP endpoints are stateless and scale similarly to discovery APIs.

Examples:

```text
GET  /mcp/tools

POST /mcp/call

GET  /mcp/prompts

POST /mcp/prompts/execute

GET  /mcp/resources

POST /mcp/resources/read
```

These operations scale naturally across multiple workers.

However:

```text
MCP Tool
      ↓
POST /v1/subscriptions/sync
```

inherits the same subscription behavior as the underlying REST API.

If an MCP tool internally uses subscription APIs, the same process-local limitations apply.

---

# Diagnostics for Load Testing

Useful endpoints while benchmarking:

```text
GET /ua/status

GET /ua/connection

GET /ua/limits

GET /ua/metrics
```

Monitor:

- OPC UA session count
- Subscription counts
- Connection health
- Server capability detection
- OPC UA limits

Model diagnostics:

```text
GET /model/metrics

GET /model/namespace-gaps
```

These help correlate benchmark results with information-model complexity.

---

# Important Subscription Limitation

Subscription state is currently process-local.

The following APIs rely on local worker state:

```text
POST /v1/subscriptions

POST /v1/subscriptions/register

POST /v1/subscriptions/unregister

POST /v1/subscriptions/sync

POST /v1/subscriptions/list

POST /v1/subscriptions/delete
```

Polling-based clients should therefore use one of:

```text
Single Worker
```

```text
Discovery / Subscription Split
```

```text
Sticky Routing
```

```text
Multi-Instance Deployment
```

when subscription consistency is important.

SSE streaming via:

```text
POST /v1/subscriptions/stream
```

is generally unaffected because the long-lived stream remains attached to the worker handling the request.

---