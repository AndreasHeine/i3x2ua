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
9. [Troubleshooting](#troubleshooting)

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
  server i3x2ua-2:8001;
}

upstream instance3_backend {
  server i3x2ua-3:8002;
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

This deployment model routes requests by path to independent instances. It does not use NGINX load-balancing algorithms such as round-robin, least_conn, or ip_hash.

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
```

### Keep-Alive
```nginx
keepalive_timeout 65;
keepalive_requests 100;
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
docker-compose exec nginx cat /etc/nginx/conf.d/default.conf
```

### Test Configuration
```bash
docker-compose exec nginx nginx -t
```

### View Error Log
```bash
docker-compose exec nginx tail -f /var/log/nginx/error.log
```

### View Access Log
```bash
docker-compose exec nginx tail -f /var/log/nginx/access.log
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
docker-compose exec nginx netstat -an | grep ESTABLISHED | wc -l
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
