# i3x2ua Benchmark Results

This document consolidates benchmark results obtained from two different OPC UA environments:

**Benchmark 1**
- Cloud-hosted UMATI OPC UA server
- Focus on REST API scalability and worker scaling

**Benchmark 2**
- Siemens S7-1500 PLC
- Focus on edge deployments, subscriptions, high-complexity discovery, and stress testing

The reported figures should be treated as observations from the tested environments rather than capacity guarantees. Actual performance depends on OPC UA server behavior, information model complexity, network topology, latency, hardware resources, subscription volume, and deployment architecture.

---

# Test Systems

## Benchmark 1 - UMATI Cloud Server

### Hardware

- AMD Ryzen 7 7800X3D
- 8 Physical CPU Cores
- 16 Logical CPUs
- 32 GB DDR5 RAM @ 6000 MT/s

### OPC UA Server

```text
opc.tcp://opcua.umati.app:4843
```

### Environment

```text
Cloud-hosted OPC UA server
Internet latency included
```

---

## Benchmark 2 - Siemens S7-1500

### Hardware

- Windows 11 Pro 64-bit
- Docker 29.8.2
- AMD Ryzen 7 7800X3D
- 8 Physical CPU Cores
- 16 Logical CPUs
- 32 GB DDR5 RAM @ 6000 MT/s

### OPC UA Server

```text
Siemens S7-1500
CPU 1510SP-1 PN
6ES7 510-1DJ01-0AB0
Firmware V02.08.03
```

Endpoint:

```text
opc.tcp://192.168.150.22:4840
```

Supported Profiles:

```text
StandardUA
Methods
StandardEventSubscription
DataAccess
```

---

# Benchmark Workload

Each discovery workflow executed:

```text
GET  /v1/info
GET  /v1/objects
POST /v1/objects/list
POST /v1/objects/value
POST /v1/objecttypes/query
```

Each workflow represents:

```text
5 REST API operations
```

continuously executed by each discovery client throughout the benchmark duration.

The report distinguishes throughput (operations completed per second), latency (time taken to complete a workflow), concurrency (the number of simultaneous clients or subscriptions), and notification rate (subscription updates delivered per second). Results describe the tested configurations and should not be interpreted as universal capacity limits.

---

# Benchmark 1 - UMATI Cloud Server

## Single Worker

### Direct HTTP

No HTTPS, NGINX, Authentication or OpenTelemetry.

| Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Workflow Latency |
|---:|---:|---:|---:|
| 1 | 388 | 77 | 13 ms |
| 8 | 443 | 88 | 91 ms |
| 16 | 407 | 81 | 197 ms |
| 32 | 429 | 85 | 373 ms |
| 64 | 438 | 85 | 730 ms |

### Peak Performance

```text
443 HTTP Requests/s
88 Discovery Workflows/s
0% Error Rate
```

---

## Production Deployment

Configuration:

```text
NGINX
HTTPS
Basic Authentication
OpenTelemetry
NGINX Keepalive Pooling
```

---

### Worker Scaling

| Workers | Peak HTTP Requests/s | Peak Discovery Workflows/s |
|---:|---:|---:|
| 1 | 443 | 88 |
| 4 | 1132 | 223 |
| 8 | 1587 | 314 |

### Peak Measurement

```text
1587 HTTP Requests/s
314 Discovery Workflows/s
402 ms Mean Workflow Latency
0% Error Rate
```

using:

```text
8 Uvicorn Workers
128 Discovery Clients
```

---

## Saturation Characteristics

| Discovery Clients | HTTP Requests/s |
|---:|---:|
| 128 | 1587 |
| 256 | 1580 |
| 512 | 1469 |

Observed saturation point:

```text
~1600 HTTP Requests/s
~300 Discovery Workflows/s
```

---

# Benchmark 2 - Siemens S7-1500

Configuration:

```text
1 Uvicorn Worker
NGINX
HTTPS
Basic Authentication
OpenTelemetry
Jaeger
```

---

# Discovery Performance

| Discovery Clients | HTTP Requests/s | Discovery Workflows/s | Workflow Latency |
|---:|---:|---:|---:|
| 1 | 224 | 44 | 22 ms |
| 8 | 380 | 75 | 105 ms |
| 16 | 399 | 79 | 201 ms |
| 32 | 414 | 81 | 388 ms |
| 64 | 419 | 81 | 766 ms |
| 128 | 405 | 77 | 1565 ms |

---

## Peak Performance

```text
419 HTTP Requests/s
81 Discovery Workflows/s
0% Error Rate
```

using:

```text
64 Discovery Clients
```

---

## Saturation Characteristics

| Discovery Clients | HTTP Requests/s |
|---:|---:|
| 64 | 419 |
| 128 | 405 |

Latency increased significantly while throughput remained stable.

This indicates:

```text
Worker saturation
Queueing effects
Stable processing behavior
```

rather than failures.

---

# Cloud vs PLC Comparison

| Metric | UMATI Cloud | Siemens S7-1500 |
|---|---:|---:|
| Peak HTTP Requests/s | 443 | 419 |
| Peak Discovery Workflows/s | 88 | 81 |
| Error Rate | 0% | 0% |

Difference:

```text
HTTP Throughput: -5%
Workflow Throughput: -8%
```

The local S7-1500 performed remarkably close to the cloud-hosted benchmark despite serving a real industrial controller.

---

# High-Complexity Discovery Benchmark

To deliberately increase OPC UA workload:

```bash
--max-nodes 10000
--max-depth 5
```

Configuration:

```text
64 Discovery Clients
10,000 Maximum Nodes
Browse Depth 5
```

---

## Results

| Metric | Result |
|---|---:|
| HTTP Requests/s | 343 |
| Discovery Workflows/s | 29 |
| Workflow Latency | 2034 ms |
| Error Rate | 0% |

---

## Complexity Impact

| Metric | Standard Discovery | High Complexity |
|---|---:|---:|
| HTTP Requests/s | 419 | 343 |
| Workflows/s | 81 | 29 |
| Workflow Latency | 766 ms | 2034 ms |

Despite significantly increased browse pressure:

```text
0% Errors
```

were observed.

---

# Understanding Subscription Throughput

The subscription benchmark used the following i3x2ua configuration:

```yaml
I3X_SUBSCRIPTION_INTERVAL_SECONDS: 1
```

In this configuration, i3x2ua processes subscription updates approximately once per second. This interval affects subscription latency, update batching, synchronization behavior, and the resulting notification rate. It describes the cadence used by this benchmark; it is not a maximum throughput limit of the platform or protocol.

## How i3x2ua Creates Monitored Items

i3x2ua creates OPC UA monitored items through asyncua and explicitly requests a sampling interval when subscribing. In the benchmarked implementation, the call specifies:

```python
subscribe_data_change(
    ...
    sampling_interval=50.0
)
```

asyncua passes this value into the OPC UA `CreateMonitoredItems` request as `mparams.SamplingInterval`. Unless another value is supplied, i3x2ua therefore requests:

```text
SamplingInterval = 50 ms
```

## Requested and Effective Sampling Intervals

The requested `SamplingInterval` originates in i3x2ua. The OPC UA server may revise it and return a different effective interval. The value used at runtime is therefore:

```text
Requested SamplingInterval
          ↓
Server Revision
          ↓
Effective SamplingInterval
```

The effective interval, rather than the requested value alone, determines how frequently the server samples the monitored value. OPC UA servers may revise requested sampling or publishing intervals to account for server-side limits and available resources.

## Publishing Interval

The `PublishingInterval` belongs to the OPC UA subscription. It controls how often the server publishes accumulated notifications to the client; it is separate from the monitored item's `SamplingInterval`.

For example:

```text
SamplingInterval = 50 ms

PublishingInterval = 1000 ms
```

During one 1000 ms publish cycle, the server may sample a variable approximately twenty times. Notifications are sent when the next publishing cycle occurs, subject to the monitored item's filters, queue behavior, and server handling.

OPC UA servers may also revise the requested `PublishingInterval`. For example:

```text
Requested PublishingInterval:
250 ms

Server-revised PublishingInterval:
1000 ms
```

The revised value is the effective publishing interval.

## asyncua Behavior

asyncua supports explicitly specifying a monitored item's `SamplingInterval`, as i3x2ua does in the benchmarked implementation. If no sampling interval is specified, server defaults may be used. The benchmarked implementation explicitly requests 50 ms; it does not rely on asyncua automatically deriving the sampling interval from the publishing interval.

## Subscription Update Flow

The benchmark's notification rate is affected by multiple stages:

```text
PLC Variable
      ↓
SamplingInterval (requested 50 ms)
      ↓
OPC UA Monitored Item
      ↓
PublishingInterval
      ↓
OPC UA Notification
      ↓
asyncua Subscription
      ↓
i3x2ua Subscription Manager
      ↓
I3X_SUBSCRIPTION_INTERVAL_SECONDS
      ↓
REST Subscription Sync
      ↓
Benchmark Client
```

The `SamplingInterval` controls how often the PLC is checked for a monitored value change. The `PublishingInterval` controls how often the OPC UA server publishes notifications. The i3x2ua update interval controls how often subscription updates are synchronized to REST clients. These are distinct stages, even when configured to similar values.

## Interpreting the Subscription Results

The measured rates were:

```text
64 subscriptions
≈ 63 notifications/s

128 subscriptions
≈ 125 notifications/s
```

These are delivered update rates observed by the benchmark clients, not measurements of how often the PLC was sampled. They must not be interpreted as `125 samples/s` or as evidence that the `SamplingInterval` was one second. The reported rate reflects the behavior of the full subscription flow, including the explicitly requested sampling interval, the effective server intervals, and the i3x2ua update synchronization cadence:

```yaml
I3X_SUBSCRIPTION_INTERVAL_SECONDS: 1
```

The benchmark demonstrates stable subscription handling, predictable scaling behavior, and near-linear notification scaling.

## Important Benchmark Note

```text
125 notifications/s
```

is not a limit of i3x2ua, OPC UA, asyncua, or the S7-1500. It is the result observed with this benchmark configuration. Changing the poll interval, `PublishingInterval`, `SamplingInterval`, number of monitored items, or number of subscriptions can produce significantly different results.

---

# Subscription Benchmark

The Siemens clock byte was used as a constantly changing monitored variable.

Element:

```text
property-d0878cb63e6202c3
```

Subscription transport:

```text
POST /v1/subscriptions/sync
```

---

## 64 Subscriptions

Configuration:

```text
64 Discovery Clients
64 Subscriptions
10000 Nodes
Browse Depth 5
```

### Results

| Metric | Result |
|---|---:|
| HTTP Requests/s | 349 |
| Discovery Workflows/s | 25 |
| Sync Requests/s | 48 |
| Notifications/s | 63 |
| Workflow Latency | 2334 ms |
| Error Rate | 0% |

Notifications:

```text
1879 Total
62.63 Updates/s
```

---

## 128 Subscriptions

Configuration:

```text
64 Discovery Clients
128 Subscriptions
10000 Nodes
Browse Depth 5
```

### Results

| Metric | Result |
|---|---:|
| HTTP Requests/s | 342 |
| Discovery Workflows/s | 21 |
| Sync Requests/s | 87 |
| Notifications/s | 125 |
| Workflow Latency | 2752 ms |
| Error Rate | 0% |

Notifications:

```text
3744 Total
124.80 Updates/s
```

---

# Subscription Scaling

| Metric | 64 Subs | 128 Subs |
|---|---:|---:|
| Sync Requests/s | 48 | 87 |
| Notifications/s | 63 | 125 |
| Error Rate | 0% | 0% |

Observation:

```text
Near-linear notification scaling
```

The measured rates:

```text
64 → 63 notifications/s
128 → 125 notifications/s
```

closely match expectations for:

```text
I3X_SUBSCRIPTION_INTERVAL_SECONDS = 1
```

and therefore validate subscription scalability and stability, while maintaining 0% observed failures.

---

# Burn-In Test

Purpose:

```text
Verify system responsiveness during extreme sustained load.
```

Parallel workloads:

### Background Stress

```text
64 Discovery Clients
128 Polling Subscriptions
```

### Interactive User Session

```text
i3X Explorer
29 Item Subscription
SSE Streaming
```

### Additional Benchmark

```text
1 Discovery Client
```

executed simultaneously.

---

## Results Under Load

| Metric | Result |
|---|---:|
| HTTP Requests/s | 5.67 |
| Discovery Workflows/s | 0.83 |
| Workflow Latency | 1175 ms |
| Error Rate | 0% |

Despite being heavily saturated:

```text
Discovery
Polling Subscriptions
SSE Streaming
HTTPS
Authentication
OpenTelemetry
```

all remained operational.

---

# Resource Consumption

## Standard Benchmark

Docker Statistics:

| Container | CPU | Memory |
|---|---:|---:|
| i3x2ua | 102% | 126 MB |
| NGINX | 46% | 53 MB |
| Jaeger | <1% | 110 MB |

Total:

```text
~289 MB RAM
```

for the complete deployment stack.

---

# Combined Results Overview

| Scenario | HTTP Requests/s | Workflows/s | Error Rate |
|---|---:|---:|---:|
| UMATI Cloud - 1 Worker | 443 | 88 | 0% |
| Siemens S7-1500 - 1 Worker | 419 | 81 | 0% |
| S7 High Complexity | 343 | 29 | 0% |
| S7 + 64 Subscriptions | 349 | 25 | 0% |
| S7 + 128 Subscriptions | 342 | 21 | 0% |
| UMATI Cloud - 4 Workers | 1132 | 223 | 0% |
| UMATI Cloud - 8 Workers | 1587 | 314 | 0% |

---

# Key Findings

## Discovery Scaling

Single-worker performance is highly stable.

Observed throughput:

```text
400-450 HTTP Requests/s
80-90 Discovery Workflows/s
```

with either:

```text
Cloud OPC UA Server
Local Siemens PLC
```

---

## Worker Scaling

Increasing workers on the UMATI benchmark produced:

```text
1 Worker  → 443 HTTP Requests/s

4 Workers → 1132 HTTP Requests/s

8 Workers → 1587 HTTP Requests/s
```

representing approximately:

```text
3.6× throughput improvement
```

from:

```text
1 Worker → 8 Workers
```

---

## High-Complexity Discovery

The largest impact on throughput came from:

```text
Increased Browse Depth
Increased Node Count
```

rather than network latency.

---

## Subscription Performance

The benchmark successfully handled concurrent subscriptions while discovery workflows were running, including high-complexity browsing. The observed notification rate reflects the configured one-second processing interval, not a platform limit:

```text
128 Concurrent Subscriptions
125 Notifications/s
87 Sync Requests/s
0% Error Rate
```

These results show successful concurrent subscriptions, simultaneous discovery load, high-complexity browsing, stability, and zero observed failures.

---

## Reliability

Across both benchmarks:

```text
Cloud OPC UA Server
Siemens PLC
Deep Browse Operations
Subscription Workloads
SSE Streams
HTTPS
Authentication
OpenTelemetry
```

the observed result was:

```text
0% Error Rate
```

throughout all benchmark and burn-in tests.

---

# Executive Summary

The benchmarks demonstrate that i3x2ua can sustain substantial industrial workloads across both cloud-hosted and real-world PLC environments.

### Single Worker

```text
~400 HTTP Requests/s
~80 Discovery Workflows/s
0% Error Rate
```

### Factory Scale (4 Workers)

```text
~1100 HTTP Requests/s
~220 Discovery Workflows/s
0% Error Rate
```

### Enterprise Scale (8 Workers)

```text
~1600 HTTP Requests/s
~314 Discovery Workflows/s
0% Error Rate
```

### Subscription Workloads

```text
128 Concurrent Subscriptions
125 Notifications/s
87 Sync Requests/s
0% Error Rate
```

The subscription notification rate is consistent with the configured one-second interval and is not a throughput limitation of i3x2ua, asyncua, OPC UA, or the S7-1500. The benchmarks instead demonstrate successful concurrent subscriptions, simultaneous discovery load, high-complexity browsing, stability, and zero observed failures.

The most notable result is not peak throughput but stability:

```text
No benchmark configuration produced failures.
```

Even under deep browsing, large node counts, subscription load, SSE traffic, HTTPS, authentication, and OpenTelemetry tracing, the system maintained stable operation with no observed errors.