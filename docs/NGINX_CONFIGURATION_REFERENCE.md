# NGINX Configuration Reference

This document provides detailed reference information for NGINX configuration in the i3x2ua deployment.

## Table of Contents

1. [Basic Configuration](#basic-configuration)
2. [HTTPS/SSL Configuration](#httpsssl-configuration)
3. [Authentication Configuration](#authentication-configuration)
4. [Proxy Configuration](#proxy-configuration)
5. [Path-Based Routing](#path-based-routing)
6. [Security Headers](#security-headers)
7. [Rate Limiting](#rate-limiting)
8. [Performance Tuning](#performance-tuning)
9. [High-Load Upstream Connections](#high-load-upstream-connections)
10. [Troubleshooting](#troubleshooting)

---

## Basic Configuration

### HTTP Server Block
```nginx
server {
  listen 80;
  server_name api.example.com;

  location / {
    proxy_pass http://i3x2ua_backend;
  }
}
```

## HTTPS/SSL Configuration

### HTTP to HTTPS Redirect
```nginx
server {
  listen 80;
  server_name api.example.com;
  return 301 https://$host$request_uri;
}
```

## Path-Based Routing

### Upstream Blocks
```nginx
upstream instance1_backend {
  server i3x2ua-1:8000;
}

upstream instance2_backend {
  server i3x2ua-2:8000;
}

upstream instance3_backend {
  server i3x2ua-3:8000;
}
```

### URL Path Routing
```nginx
location /i3x/instance1/ {
  rewrite ^/i3x/instance1/(.*) /$1 break;
  proxy_pass http://instance1_backend;
}

location /i3x/instance2/ {
  rewrite ^/i3x/instance2/(.*) /$1 break;
  proxy_pass http://instance2_backend;
}

location /i3x/instance3/ {
  rewrite ^/i3x/instance3/(.*) /$1 break;
  proxy_pass http://instance3_backend;
}
```

This deployment model routes requests by path to independent instances. For upstream-based load balancing examples, see the Load Balancing section below.

### Separating discovery and subscription traffic

For a deployment example that routes direct REST subscription requests to a
dedicated single-worker service while discovery uses a separately scalable
service, see [the discovery/subscription split example](examples/discovery-subscription-split/README.md).

---

## Authentication Configuration

### Basic Authentication
```nginx
location / {
  auth_basic "API Access";
  auth_basic_user_file /etc/nginx/.htpasswd;
  
  # Proxy configuration
  proxy_pass http://backend;
}
```

### Disable Authentication for Specific Paths
```nginx
location /public/ {
  # No auth required
  proxy_pass http://backend;
}

location / {
  auth_basic "API Access";
  auth_basic_user_file /etc/nginx/.htpasswd;
  proxy_pass http://backend;
}
```

### OAuth2 Proxy Integration
```nginx
location /oauth2/auth {
  proxy_pass http://oauth2-proxy:4180;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
}

location / {
  auth_request /oauth2/auth;
  auth_request_set $auth_status $upstream_status;
  error_page 401 = /oauth2/start;
  
  proxy_pass http://backend;
}
```

---

## Proxy Configuration

### Basic Proxy Pass
```nginx
location / {
  proxy_pass http://backend;
}
```

### HTTP Version
```nginx
proxy_http_version 1.1;
```

### Headers
```nginx
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Port $server_port;
proxy_set_header Connection "";
```

### Timeouts
```nginx
proxy_connect_timeout 60s;
proxy_send_timeout 60s;
proxy_read_timeout 60s;
```

### Buffering
```nginx
proxy_buffering on;
proxy_buffer_size 4k;
proxy_buffers 8 4k;
proxy_busy_buffers_size 8k;
```

### WebSocket Support
```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
```

---

## Load Balancing

### Upstream Block
```nginx
upstream i3x2ua_backend {
  server backend1:8000;
  server backend2:8000;
  server backend3:8000;
}
```

### Round Robin (Default)
```nginx
upstream i3x2ua_backend {
  server i3x2ua-1:8000;
  server i3x2ua-2:8000;
  server i3x2ua-3:8000;
}
```

### Least Connections
```nginx
upstream i3x2ua_backend {
  least_conn;
  server i3x2ua-1:8000;
  server i3x2ua-2:8000;
  server i3x2ua-3:8000;
}
```

### IP Hash (Session Persistence)
```nginx
upstream i3x2ua_backend {
  ip_hash;
  server i3x2ua-1:8000;
  server i3x2ua-2:8000;
  server i3x2ua-3:8000;
}
```

### Weighted Distribution
```nginx
upstream i3x2ua_backend {
  server i3x2ua-1:8000 weight=3;
  server i3x2ua-2:8000 weight=2;
  server i3x2ua-3:8000 weight=1;
}
```

### Health Checks
```nginx
upstream i3x2ua_backend {
  server i3x2ua-1:8000 max_fails=3 fail_timeout=30s;
  server i3x2ua-2:8000 max_fails=3 fail_timeout=30s;
  server i3x2ua-3:8000 max_fails=3 fail_timeout=30s;
}
```

### Keepalive Connections
```nginx
upstream i3x2ua_backend {
  server i3x2ua-1:8000;
  server i3x2ua-2:8000;
  server i3x2ua-3:8000;
  keepalive 32;
}

server {
  location / {
    proxy_pass http://i3x2ua_backend;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
  }
}
```

---

## Security Headers

### Recommended Headers
```nginx
# Prevent clickjacking
add_header X-Frame-Options "SAMEORIGIN" always;

# Prevent MIME type sniffing
add_header X-Content-Type-Options "nosniff" always;

# Enable XSS protection
add_header X-XSS-Protection "1; mode=block" always;

# Referrer policy
add_header Referrer-Policy "strict-origin-when-cross-origin" always;

# Permissions policy
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

# HSTS
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

# Content Security Policy (optional)
add_header Content-Security-Policy "default-src 'self'; script-src 'self'" always;
```

### CORS Headers
```nginx
add_header Access-Control-Allow-Origin "$http_origin" always;
add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS" always;
add_header Access-Control-Allow-Headers "Authorization, Content-Type" always;
add_header Access-Control-Max-Age "3600" always;

if ($request_method = 'OPTIONS') {
  return 204;
}
```

---

## Rate Limiting

### Define Rate Limit Zones
```nginx
# Rate limit by IP
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

# Rate limit by X-Forwarded-For (behind proxy)
limit_req_zone $http_x_forwarded_for zone=api_limit_proxy:10m rate=10r/s;

# Rate limit by authentication user
limit_req_zone $remote_user zone=user_limit:10m rate=50r/s;
```

### Apply Rate Limiting
```nginx
location / {
  limit_req zone=api_limit_proxy burst=20 nodelay;
  limit_req_status 429;  # Return 429 Too Many Requests
  
  proxy_pass http://backend;
}
```

### Per-Path Rate Limiting
```nginx
location /api/expensive {
  limit_req zone=api_limit_proxy burst=5;
  proxy_pass http://backend;
}

location / {
  limit_req zone=api_limit_proxy burst=20;
  proxy_pass http://backend;
}
```

---

## Performance Tuning

### Worker Processes
```nginx
worker_processes auto;
worker_rlimit_nofile 65535;

events {
  worker_connections 4096;
}
```

### Keep-Alive
```nginx
keepalive_timeout 65s;
keepalive_requests 1000;
```

### Gzip Compression
```nginx
gzip on;
gzip_vary on;
gzip_proxied any;
gzip_comp_level 6;
gzip_types text/plain text/css text/xml text/javascript 
           application/json application/javascript application/xml+rss 
           application/rss+xml font/truetype font/opentype 
           application/vnd.ms-fontobject image/svg+xml;
```

### Caching
```nginx
proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=my_cache:10m;

location / {
  proxy_cache my_cache;
  proxy_cache_valid 200 10m;
  proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
  add_header X-Cache-Status $upstream_cache_status;
  
  proxy_pass http://backend;
}
```

### Client Body Buffer
```nginx
client_max_body_size 10m;
client_body_buffer_size 128k;
```

## High-Load Upstream Connections

### Why `connect() ... failed (99: Address not available)` Happens

On Linux, `EADDRNOTAVAIL` from NGINX while connecting to an upstream commonly means
the kernel could not allocate a local source address/ephemeral port for that
outbound TCP connection. If NGINX opens and closes a new upstream connection for
every lightweight request, connections to the same backend address and port can
accumulate in `TIME_WAIT`. The source-port range and the time those ports remain
unavailable can then limit connection creation rate. A default range of roughly
28,000 ports combined with a typical 60-second `TIME_WAIT` lifetime is on the
order of 470 new connections/second to one destination before accounting for
reuse and other traffic; this is a useful diagnostic estimate, not a hard
throughput limit.

The error can also arise from an exhausted or constrained port range, local
address/interface changes, or Docker/host networking and conntrack pressure.
Check the NGINX container's network namespace and the exact upstream tuple;
host-level socket counts alone may not show the container's sockets. This error
is different from an upstream timeout or a refused connection. Reusing
upstream TCP connections is the first corrective action; sysctl changes do not
substitute for reuse.

### Active Configuration Used by This Deployment

The image's `/etc/nginx/nginx.conf` sets the worker, descriptor, logging, and
HTTP defaults. The entrypoint generates the `upstream` and `server` blocks in
`/etc/nginx/conf.d/default.conf` using the configured backend host and port.
`NGINX_HTTP_PORT` and `NGINX_HTTPS_PORT` set the container's HTTP and HTTPS
listen ports; the Compose configuration publishes those same container ports
on host ports 8080 and 8443, respectively. `NGINX_HTTPS_REDIRECT_PORT` is
separate and controls the port included in HTTP-to-HTTPS redirects.
The relevant upstream and HTTP proxy settings are:

```nginx
upstream i3x2ua_backend {
  server i3x2ua:8000;
  keepalive 32;
  keepalive_requests 1000;
  keepalive_timeout 60s;
}

location / {
  proxy_http_version 1.1;
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_set_header X-Forwarded-Host $host;
  proxy_set_header X-Forwarded-Port $server_port;
  proxy_set_header Connection "";
  proxy_connect_timeout 5s;
  proxy_send_timeout 60s;
  proxy_read_timeout 60s;
  proxy_pass http://i3x2ua_backend;
}
```

The `keepalive` value is the maximum number of **idle cached upstream
connections per worker**, not a limit on active requests or connections. With
`worker_processes auto`, the theoretical idle cache is 32 multiplied by the
worker count. Tune it to the measured concurrency and available file descriptors.
HTTP/1.1 plus an empty `Connection` header is required for upstream reuse.
Server-Sent Events keep buffering disabled and use longer read/send timeouts;
they use the same upstream pool.

The complete global configuration shipped in `nginx/nginx.conf` is:

```nginx
worker_processes auto;
worker_rlimit_nofile 65535;
error_log /dev/stderr warn;
pid /var/run/nginx.pid;

events {
  worker_connections 4096;
}

http {
  include /etc/nginx/mime.types;
  default_type application/octet-stream;

  log_format upstream_timing
    '$remote_addr - $remote_user [$time_local] "$request" '
    '$status $body_bytes_sent rt=$request_time '
    'ua="$upstream_addr" us="$upstream_status" '
    'uct="$upstream_connect_time" uht="$upstream_header_time" '
    'urt="$upstream_response_time" conn=$connection '
    'conn_reqs=$connection_requests';

  access_log /dev/stdout upstream_timing;
  sendfile on;
  tcp_nopush on;
  keepalive_timeout 65s;
  keepalive_requests 1000;
  server_tokens off;

  include /etc/nginx/conf.d/*.conf;
}
```

`worker_connections` counts both downstream and upstream sockets, so it is not
the number of proxied clients per worker. Each proxied request normally uses
two connections while active. `worker_rlimit_nofile` must be paired with the
container's `nofile` ulimit; the Docker Compose service sets both soft and hard
limits to 65535. These are generous starting values, not a substitute for
measuring memory, process counts, and actual concurrent connections.

The access log goes to Docker stdout and includes upstream address/status and
connect, header, and response timings. A sustained nonzero `uct` is expected
for newly opened connections; after pooling, most reused requests should show a
near-zero connect time (or `-`, depending on NGINX version/log variables).

### Linux Networking

First inspect the active network namespace and host limits. On a Linux Docker
host, the following targets the NGINX container's network namespace:

```bash
NGINX_PID=$(docker inspect --format '{{.State.Pid}}' i3x2ua-nginx)
sudo nsenter -t "$NGINX_PID" -n ss -tan
sudo nsenter -t "$NGINX_PID" -n ss -tan state time-wait
sudo nsenter -t "$NGINX_PID" -n sh -c 'cat /proc/sys/net/ipv4/ip_local_port_range'
sudo nsenter -t "$NGINX_PID" -n ss -tan | grep ':8000'
```

If observations confirm ephemeral-port pressure, a broader range is a
reasonable Linux-host or supported per-container-network-namespace setting:

```conf
# /etc/sysctl.d/99-nginx-proxy.conf
net.ipv4.ip_local_port_range = 10240 65535
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 8192
net.core.netdev_max_backlog = 4096
fs.file-max = 2097152
```

Apply on a Linux host with `sudo sysctl --system`. Verify that any reserved
ports in `net.ipv4.ip_local_reserved_ports` do not conflict with the chosen
range. `somaxconn`, the SYN backlog, and the device backlog concern accepting
connections/packet bursts; they do not create more ephemeral ports. `fs.file-max`
is a host-wide ceiling and should only be raised if host file-table usage
approaches it. Docker Desktop has a Linux VM/network boundary, so host sysctls
may not change the VM or container namespace that owns the sockets.

Do not set `tcp_fin_timeout` to try to shorten `TIME_WAIT`: it does not control
the normal TCP `TIME_WAIT` duration. Do not use the removed
`tcp_tw_recycle` setting or raise `tcp_max_tw_buckets` as a port-exhaustion
fix. Modern Linux may set `net.ipv4.tcp_tw_reuse` to `2` (loopback only) by
default. Leave it at the kernel default unless measurements still show
`TIME_WAIT` port pressure after keepalive and range tuning. Setting it to `1`
can permit safe reuse for outgoing connections on supported kernels, but it is
kernel-version-sensitive and is not required for this NGINX fix. If testing
supports it, apply it temporarily before making it persistent:

```bash
sudo sysctl -w net.ipv4.tcp_tw_reuse=1
```

### Docker and Resource Limits

- Keep the backend on a user-defined bridge network and proxy to its Compose
  service name and container port. Do not publish the backend port to the host
  solely for NGINX-to-backend traffic; the current Compose setup already uses
  `expose` and a shared bridge network.
- NGINX resolves a static `server service-name:port` upstream when its
  configuration loads. If a recreated backend receives a different container
  IP, reload/recreate NGINX so it resolves the new address; dynamic DNS
  resolution requires an explicitly configured resolver and a compatible
  NGINX configuration.
- Ensure the NGINX process's `nofile` soft/hard limits are at least as high as
  its worker needs. Check both the Compose ulimit and `/proc/1/limits` in the
  running container.
- Avoid very low CPU or memory quotas for NGINX. TLS termination can need CPU;
  confirm resource pressure with `docker stats` before changing limits.
- Linux Compose deployments may optionally set `net.ipv4.ip_local_port_range`
  under the NGINX service's `sysctls` if the Docker Engine allows that
  namespace-scoped setting. Do not assume a Compose sysctl changes the host,
  Docker Desktop VM, or another container's namespace.
- Example optional Compose setting (Linux Docker Engine only; validate on the
  target kernel before using):

  ```yaml
  services:
    nginx:
      sysctls:
        net.ipv4.ip_local_port_range: "10240 65535"
  ```
- Do not use host networking as a first-line fix. It changes isolation and
  port exposure and does not address connection churn by itself.

### Verification and Monitoring

```bash
# Configuration and generated upstream block
docker compose exec nginx nginx -t
docker compose exec nginx nginx -T

# Limits, running processes, and resource usage
docker compose exec nginx sh -c 'ulimit -n; grep "open files" /proc/1/limits'
docker compose exec nginx ps
docker stats i3x2ua-nginx i3x2ua

# NGINX logs, including upstream connection timings
docker compose logs --since=10m nginx
docker compose logs --follow nginx

# In the NGINX container namespace on Linux (see nsenter commands above)
ss -tan
ss -tan state time-wait
cat /proc/sys/net/ipv4/ip_local_port_range
ss -tan | grep ':8000'
```

The Alpine NGINX image may not include `ss`; use `nsenter` from the Linux host
or install `iproute2` only in a diagnostic image/container. On Docker Desktop,
run the socket inspection inside its Linux VM/container namespace where
available. Compare `TIME_WAIT` and `ESTAB` counts before and during a sustained
benchmark, and correlate 502 log entries with `$upstream_connect_time` and
`$upstream_status`.

For live connection counters, NGINX's `stub_status` module can be exposed at a
dedicated location restricted to a monitoring network or loopback. Do not
publish an unrestricted status endpoint on the public listener.

### Expected Effect

When source-port exhaustion caused the failures, upstream keepalive should
reduce new backend TCP connection creation from approximately one per request
to approximately one per active/cached upstream connection, sharply lowering
`TIME_WAIT` growth and typically removing this cause of 502s. The active
connection count will follow request concurrency; the idle count is bounded by
the per-worker keepalive cache. Throughput should stay at least comparable and
latency should be more stable because handshakes are avoided. The exact
throughput/latency gain depends on TLS, API work, and request concurrency; no
configuration can guarantee zero 502s if the actual cause is conntrack
exhaustion, resource limits, DNS/address changes, or a backend failure.

---

## Logging

### Access Log
```nginx
access_log /var/log/nginx/access.log;

# Custom format
log_format custom_log '$remote_addr - $remote_user [$time_local] '
                      '"$request" $status $body_bytes_sent '
                      '"$http_referer" "$http_user_agent"';
access_log /var/log/nginx/access.log custom_log;
```

### Error Log
```nginx
error_log /var/log/nginx/error.log warn;
```

### Disable Logging
```nginx
access_log off;
```

---

## Troubleshooting

### View NGINX Configuration
```bash
docker compose exec nginx nginx -T
```

### Test Configuration
```bash
docker-compose exec nginx nginx -t
```

### View Error Log
```bash
docker compose logs --follow nginx
```

### View Access Log
```bash
docker compose logs --follow nginx
```

### Reload Configuration
```bash
docker-compose exec nginx nginx -s reload
```

### Check NGINX Process
```bash
docker-compose exec nginx ps aux | grep nginx
```

### Monitor Connections
```bash
docker compose exec nginx ps
docker stats i3x2ua-nginx
```

---

## Common Configurations

### API Reverse Proxy with Authentication
```nginx
upstream api_backend {
  least_conn;
  server api-1:8000 max_fails=3 fail_timeout=30s;
  server api-2:8000 max_fails=3 fail_timeout=30s;
  server api-3:8000 max_fails=3 fail_timeout=30s;
}

server {
  listen 443 ssl http2;
  server_name api.example.com;
  
  ssl_certificate /etc/nginx/certs/fullchain.pem;
  ssl_certificate_key /etc/nginx/certs/privkey.pem;
  
  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
  add_header X-Frame-Options "SAMEORIGIN" always;
  
  location / {
    auth_basic "API Access";
    auth_basic_user_file /etc/nginx/.htpasswd;
    
    limit_req zone=api_limit_proxy burst=20 nodelay;
    
    proxy_pass http://api_backend;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";
    
    proxy_connect_timeout 60s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
  }
}
```

### Health Check Endpoint
```nginx
location /health {
  access_log off;
  proxy_pass http://api_backend;
  proxy_connect_timeout 2s;
  proxy_read_timeout 2s;
}
```

---

## References

- [NGINX Documentation](https://nginx.org/en/docs/)
- [NGINX Admin Guide](https://nginx.org/en/docs/admin_guide.html)
- [NGINX HTTP Module](https://nginx.org/en/docs/http/ngx_http_core_module.html)
- [NGINX Proxy Module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)

---

**Last Updated**: 2024
