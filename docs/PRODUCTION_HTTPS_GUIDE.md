# Production Deployment Guide: HTTPS, NGINX Reverse Proxy & Multi-Instance Setup

This guide provides comprehensive instructions for deploying i3x2ua in a production environment with NGINX as a reverse proxy, supporting HTTPS, BasicAuth, OAuth, and multi-instance setups.

## Table of Contents

1. [Quick Start](#quick-start)
2. [HTTPS Configuration](#https-configuration)
3. [Authentication Methods](#authentication-methods)
4. [Multi-Instance Setup](#multi-instance-setup)
5. [Security Best Practices](#security-best-practices)
6. [Monitoring & Maintenance](#monitoring--maintenance)
7. [Troubleshooting](#troubleshooting)

---

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- Domain name (for HTTPS)
- SSL certificate and key files
- Understanding of Docker networking

### Basic HTTPS Deployment

```bash
# 1. Create certificates directory
mkdir -p certs

# 2. Place your SSL certificates in the certs/ directory
# - fullchain.pem
# - privkey.pem

# 3. Create .env file with production settings
cat > .env << EOF
# HTTPS Configuration
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=your-domain.com
NGINX_SSL_CERTIFICATE=/etc/nginx/certs/fullchain.pem
NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/privkey.pem
NGINX_SSL_CERTS_DIR=./certs

# Port Configuration
NGINX_HTTP_PORT=80
NGINX_HTTPS_PORT=443

# i3x2ua Configuration
I3X_OPCUA_ENDPOINT=opc.tcp://your-opcua-server:4843
I3X_OPCUA_USERNAME=your-username
I3X_OPCUA_PASSWORD=your-password
I3X_LOG_LEVEL=INFO

# BasicAuth (optional)
NGINX_BASIC_AUTH_ENABLED=0
EOF

# 4. Start the services
docker-compose up -d

# 5. Verify the deployment
curl https://your-domain.com/v1/info
```

---

## HTTPS Configuration

### 1. Obtain SSL Certificates

#### Option A: Using Let's Encrypt with Certbot

```bash
# Install certbot
sudo apt-get install certbot

# Generate certificate
sudo certbot certonly --standalone \
  -d your-domain.com \
  -d api.your-domain.com

# Certificates will be in /etc/letsencrypt/live/your-domain.com/
# Copy to your project
mkdir -p certs
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/
sudo chown $USER:$USER certs/*
chmod 400 certs/privkey.pem
```

#### Option B: Using Self-Signed Certificate (Development Only)

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:4096 \
  -keyout certs/privkey.pem \
  -out certs/fullchain.pem \
  -days 365 -nodes \
  -subj "/CN=your-domain.com"
```

#### Option C: Using Commercial Certificate

1. Obtain certificate and key from your provider
2. Combine certificate chain if needed:
   ```bash
   cat your-cert.crt your-intermediate.crt > fullchain.pem
   cp your-key.key privkey.pem
   ```
3. Place both files in the `certs/` directory

### 2. Configure Docker Compose for HTTPS

Update your `.env` file:

```env
# HTTPS Settings
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.your-domain.com
NGINX_SSL_CERTIFICATE=/etc/nginx/certs/fullchain.pem
NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/privkey.pem
NGINX_SSL_CERTS_DIR=./certs

# Optional: Customize ports
NGINX_HTTP_PORT=80
NGINX_HTTPS_PORT=443
```

### 3. Enable HSTS (HTTP Strict Transport Security)

For enhanced security, modify `nginx/entrypoint.sh` to add HSTS header:

```bash
# In the HTTPS server block, add:
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

### 4. SSL Certificate Renewal (Let's Encrypt)

```bash
# Automatic renewal cron job
# Add to crontab: sudo crontab -e

0 2 * * * certbot renew && cp /etc/letsencrypt/live/your-domain.com/fullchain.pem /path/to/project/certs/ && cp /etc/letsencrypt/live/your-domain.com/privkey.pem /path/to/project/certs/ && docker-compose -f /path/to/project/docker-compose.yml restart nginx

# Or use renewal hooks
sudo certbot --deploy-hook "cp /etc/letsencrypt/live/your-domain.com/fullchain.pem /path/to/project/certs/ && cp /etc/letsencrypt/live/your-domain.com/privkey.pem /path/to/project/certs/ && docker-compose -f /path/to/project/docker-compose.yml restart nginx"
```

---

## Authentication Methods

### 1. Basic Authentication

Basic Authentication provides simple username/password protection using HTTP Basic Auth.

#### Configuration

```env
# .env
NGINX_BASIC_AUTH_ENABLED=1
NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=your-secure-password
NGINX_BASIC_AUTH_REALM=i3x2ua API
```

#### Usage

```bash
# Test with curl
curl -u admin:your-secure-password https://your-domain.com/v1/info

# In browser: use URL format
https://admin:your-secure-password@your-domain.com/v1/info
```

#### Managing Multiple Users

To support multiple users with BasicAuth:

1. Create an `.env.local` file (not committed)
2. Modify `nginx/entrypoint.sh` to support multiple users:

```bash
# In entrypoint.sh, replace the htpasswd command:
if is_truthy "$basic_auth_enabled"; then
  if [ ! -f "/etc/nginx/.htpasswd" ]; then
    touch "$auth_file"
  fi
  
  # Add user (create if doesn't exist, update if exists)
  htpasswd -bB "$auth_file" "$NGINX_BASIC_AUTH_USER" "$NGINX_BASIC_AUTH_PASSWORD"
  
  # To add additional users, create a volume with pre-populated .htpasswd
fi
```

### 2. OAuth 2.0 Authentication

For OAuth integration, use nginx-lua or an external service like oauth2-proxy.

#### Option A: Using oauth2-proxy

```yaml
# Add to docker-compose.yml
  oauth2-proxy:
    image: ghcr.io/oauth2-proxy/oauth2-proxy:v7.4.0
    restart: unless-stopped
    ports:
      - "4180:4180"
    environment:
      OAUTH2_PROXY_CLIENT_ID: ${OAUTH_CLIENT_ID}
      OAUTH2_PROXY_CLIENT_SECRET: ${OAUTH_CLIENT_SECRET}
      OAUTH2_PROXY_OIDC_ISSUER_URL: ${OAUTH_ISSUER_URL}
      OAUTH2_PROXY_REDIRECT_URL: https://your-domain.com/oauth2/callback
      OAUTH2_PROXY_ALLOWED_EMAILS: ${OAUTH_ALLOWED_EMAILS}
      OAUTH2_PROXY_SKIP_PROVIDER_BUTTON: "true"
      OAUTH2_PROXY_COOKIE_SECURE: "true"
      OAUTH2_PROXY_COOKIE_HTTPONLY: "true"
      OAUTH2_PROXY_COOKIE_SAMESITE: "Lax"
      OAUTH2_PROXY_SET_XAUTHREQUEST: "true"
    networks:
      - backend
```

#### Configuration

```env
# .env
# OAuth2-Proxy Settings
OAUTH_CLIENT_ID=your-oauth-client-id
OAUTH_CLIENT_SECRET=your-oauth-client-secret
OAUTH_ISSUER_URL=https://accounts.google.com/o/oauth2/v2/auth
OAUTH_ALLOWED_EMAILS=user1@example.com,user2@example.com
```

#### NGINX Configuration for OAuth2-Proxy

Modify `nginx/entrypoint.sh` to include OAuth proxy locations:

```bash
# Add to nginx config:
location /oauth2/auth {
  proxy_pass http://oauth2-proxy:4180;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
  proxy_pass_request_body off;
  proxy_set_header Content-Length "";
}

location /oauth2/start {
  proxy_pass http://oauth2-proxy:4180;
  proxy_redirect / /oauth2/start;
}

location /oauth2/callback {
  proxy_pass http://oauth2-proxy:4180;
}

location / {
  # OAuth2 check
  auth_request /oauth2/auth;
  auth_request_set $auth_status $upstream_status;
  error_page 401 = /oauth2/start;
  
  # Your API proxy config
  proxy_http_version 1.1;
  proxy_pass http://i3x2ua:8000;
}
```

#### Google OAuth Example

```env
OAUTH_CLIENT_ID=your-app.apps.googleusercontent.com
OAUTH_CLIENT_SECRET=your-client-secret
OAUTH_ISSUER_URL=https://accounts.google.com
OAUTH_ALLOWED_EMAILS=admin@your-company.com,dev@your-company.com
```

---

## Multi-Instance Setup

Run multiple independent i3x2ua instances behind NGINX, each with its own OPC-UA connection and state. NGINX routes requests to different instances based on URL path.

Each instance is completely independent:
- `/i3x/instance1/v1/...` → i3x2ua-1
- `/i3x/instance2/v1/...` → i3x2ua-2  
- `/i3x/instance3/v1/...` → i3x2ua-3

### 1. Docker Compose Multi-Instance Configuration

```yaml
version: '3.8'

services:
  i3x2ua-1:
    image: ghcr.io/andreasheine/i3x2ua:master
    container_name: i3x2ua-1
    restart: unless-stopped
    command:
      - python
      - -m
      - uvicorn
      - i3x_server.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8000"
      - --proxy-headers
      - --forwarded-allow-ips
      - "*"
      - --workers
      - "4"
    environment:
      I3X_OPCUA_ENDPOINT: ${I3X_OPCUA_ENDPOINT_1}
      I3X_OPCUA_USERNAME: ${I3X_OPCUA_USERNAME_1}
      I3X_OPCUA_PASSWORD: ${I3X_OPCUA_PASSWORD_1}
      I3X_LOG_LEVEL: ${I3X_LOG_LEVEL:-INFO}
    expose:
      - "8000"
    read_only: true
    tmpfs:
      - /tmp:size=64m,noexec,nosuid
      - /home/app:size=16m,noexec,nosuid
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/info', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - backend

  i3x2ua-2:
    image: ghcr.io/andreasheine/i3x2ua:master
    container_name: i3x2ua-2
    restart: unless-stopped
    command:
      - python
      - -m
      - uvicorn
      - i3x_server.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8001"
      - --proxy-headers
      - --forwarded-allow-ips
      - "*"
      - --workers
      - "4"
    environment:
      I3X_OPCUA_ENDPOINT: ${I3X_OPCUA_ENDPOINT_2}
      I3X_OPCUA_USERNAME: ${I3X_OPCUA_USERNAME_2}
      I3X_OPCUA_PASSWORD: ${I3X_OPCUA_PASSWORD_2}
      I3X_LOG_LEVEL: ${I3X_LOG_LEVEL:-INFO}
    expose:
      - "8001"
    read_only: true
    tmpfs:
      - /tmp:size=64m,noexec,nosuid
      - /home/app:size=16m,noexec,nosuid
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/v1/info', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - backend

  i3x2ua-3:
    image: ghcr.io/andreasheine/i3x2ua:master
    container_name: i3x2ua-3
    restart: unless-stopped
    command:
      - python
      - -m
      - uvicorn
      - i3x_server.main:app
      - --host
      - 0.0.0.0
      - --port
      - "8002"
      - --proxy-headers
      - --forwarded-allow-ips
      - "*"
      - --workers
      - "4"
    environment:
      I3X_OPCUA_ENDPOINT: ${I3X_OPCUA_ENDPOINT_3}
      I3X_OPCUA_USERNAME: ${I3X_OPCUA_USERNAME_3}
      I3X_OPCUA_PASSWORD: ${I3X_OPCUA_PASSWORD_3}
      I3X_LOG_LEVEL: ${I3X_LOG_LEVEL:-INFO}
    expose:
      - "8002"
    read_only: true
    tmpfs:
      - /tmp:size=64m,noexec,nosuid
      - /home/app:size=16m,noexec,nosuid
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/v1/info', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    networks:
      - backend

  nginx:
    build:
      context: ./nginx
    container_name: i3x2ua-nginx
    restart: unless-stopped
    depends_on:
      i3x2ua-1:
        condition: service_healthy
      i3x2ua-2:
        condition: service_healthy
      i3x2ua-3:
        condition: service_healthy
    ports:
      - "${NGINX_HTTP_PORT:-80}:80"
      - "${NGINX_HTTPS_PORT:-443}:443"
    environment:
      NGINX_SERVER_NAME: ${NGINX_SERVER_NAME}
      NGINX_HTTPS_ENABLED: ${NGINX_HTTPS_ENABLED}
      NGINX_SSL_CERTIFICATE: ${NGINX_SSL_CERTIFICATE:-/etc/nginx/certs/fullchain.pem}
      NGINX_SSL_CERTIFICATE_KEY: ${NGINX_SSL_CERTIFICATE_KEY:-/etc/nginx/certs/privkey.pem}
      NGINX_BASIC_AUTH_ENABLED: ${NGINX_BASIC_AUTH_ENABLED:-0}
      NGINX_BASIC_AUTH_USER: ${NGINX_BASIC_AUTH_USER:-}
      NGINX_BASIC_AUTH_PASSWORD: ${NGINX_BASIC_AUTH_PASSWORD:-}
      NGINX_BASIC_AUTH_REALM: ${NGINX_BASIC_AUTH_REALM:-i3x2ua}
    volumes:
      - ${NGINX_SSL_CERTS_DIR:-./certs}:/etc/nginx/certs:ro
    networks:
      - backend
    cap_drop:
      - ALL
      - NET_RAW
    cap_add:
      - NET_BIND_SERVICE
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /var/run:size=32m,noexec,nosuid
      - /var/cache:size=32m,noexec,nosuid

networks:
  backend:
    driver: bridge
```

### 2. NGINX Path-Based Routing Configuration

Modify `nginx/entrypoint.sh` to route requests by path:

```bash
# Add to the nginx config generation section:

path_routing_block=$(cat <<EOF
upstream instance1_backend {
  server i3x2ua-1:8000;
}

upstream instance2_backend {
  server i3x2ua-2:8001;
}

upstream instance3_backend {
  server i3x2ua-3:8002;
}

server {
  listen 443 ssl http2;
  server_name \$server_name;
  ssl_certificate \$cert_file;
  ssl_certificate_key \$key_file;
  ssl_protocols TLSv1.2 TLSv1.3;
  
  # Instance 1 at /i3x/instance1/
  location /i3x/instance1/ {
    \$(printf '%s\n' "\$auth_block")
    rewrite ^/i3x/instance1/(.*) /\$1 break;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Path /instance1;
    proxy_pass http://instance1_backend;
  }
  
  # Instance 2 at /i3x/instance2/
  location /i3x/instance2/ {
    \$(printf '%s\n' "\$auth_block")
    rewrite ^/i3x/instance2/(.*) /\$1 break;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Path /instance2;
    proxy_pass http://instance2_backend;
  }
  
  # Instance 3 at /i3x/instance3/
  location /i3x/instance3/ {
    \$(printf '%s\n' "\$auth_block")
    rewrite ^/i3x/instance3/(.*) /\$1 break;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_set_header X-Forwarded-Path /instance3;
    proxy_pass http://instance3_backend;
  }
}
EOF
)
```

### 3. Environment Variables for Multi-Instance

Each instance can connect to a different OPC-UA server:

```env
# Instance 1 Configuration
I3X_OPCUA_ENDPOINT_1=opc.tcp://opcua-server-1:4843
I3X_OPCUA_USERNAME_1=user1
I3X_OPCUA_PASSWORD_1=password1

# Instance 2 Configuration
I3X_OPCUA_ENDPOINT_2=opc.tcp://opcua-server-2:4843
I3X_OPCUA_USERNAME_2=user2
I3X_OPCUA_PASSWORD_2=password2

# Instance 3 Configuration
I3X_OPCUA_ENDPOINT_3=opc.tcp://opcua-server-3:4843
I3X_OPCUA_USERNAME_3=user3
I3X_OPCUA_PASSWORD_3=password3

# NGINX Configuration
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.your-domain.com
NGINX_BASIC_AUTH_ENABLED=0

# Logging
I3X_LOG_LEVEL=INFO
```

### 4. Accessing Multiple Instances

```bash
# Instance 1
curl https://api.your-domain.com/i3x/instance1/v1/info

# Instance 2
curl https://api.your-domain.com/i3x/instance2/v1/info

# Instance 3
curl https://api.your-domain.com/i3x/instance3/v1/info
```

### 5. Scaling to More Instances

To add more instances (e.g., instance4, instance5):

1. Add new service to docker-compose.yml:
```yaml
i3x2ua-4:
  image: ghcr.io/andreasheine/i3x2ua:master
  container_name: i3x2ua-4
  expose:
    - "8003"
  environment:
    I3X_OPCUA_ENDPOINT: ${I3X_OPCUA_ENDPOINT_4}
    I3X_OPCUA_USERNAME: ${I3X_OPCUA_USERNAME_4}
    I3X_OPCUA_PASSWORD: ${I3X_OPCUA_PASSWORD_4}
```

2. Add upstream and location block to nginx/entrypoint.sh

3. Add environment variables to .env:
```env
I3X_OPCUA_ENDPOINT_4=opc.tcp://opcua-server-4:4843
I3X_OPCUA_USERNAME_4=user4
I3X_OPCUA_PASSWORD_4=password4
```

4. Restart services:
```bash
docker-compose up -d
```

---

## Security Best Practices

### 1. Network Security

```yaml
# docker-compose.yml - Security configurations
services:
  i3x2ua-1:
    read_only: true
    tmpfs:
      - /tmp:size=64m,noexec,nosuid
      - /home/app:size=16m,noexec,nosuid
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 256
```

### 2. NGINX Security Headers

Add to `nginx/entrypoint.sh`:

```bash
# Security headers
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

### 3. SSL/TLS Configuration

```bash
# Strong SSL configuration in entrypoint.sh
ssl_protocols TLSv1.2 TLSv1.3;
ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384';
ssl_prefer_server_ciphers on;
ssl_session_timeout 1d;
ssl_session_cache shared:SSL:50m;
ssl_session_tickets off;
```

### 4. Rate Limiting

```bash
# Add to nginx config
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;
limit_req_zone $http_x_forwarded_for zone=api_limit_proxy:10m rate=10r/s;

location / {
  limit_req zone=api_limit_proxy burst=20 nodelay;
  limit_req_status 429;
  # ... rest of configuration
}
```

### 5. CORS Configuration

```bash
# Add to nginx config if needed
add_header Access-Control-Allow-Origin "$http_origin" always;
add_header Access-Control-Allow-Methods "GET, POST, PUT, DELETE, OPTIONS" always;
add_header Access-Control-Allow-Headers "Authorization, Content-Type" always;

if ($request_method = 'OPTIONS') {
  return 204;
}
```

### 6. Firewall Rules (UFW Example - Linux)

```bash
# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP
sudo ufw allow 80/tcp

# Allow HTTPS
sudo ufw allow 443/tcp

# Deny everything else
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Enable firewall
sudo ufw enable
```

### 7. Password Security

For BasicAuth passwords, use strong hashing:

```bash
# Generate bcrypt-hashed password
htpasswd -cbB /tmp/.htpasswd username password

# Use the generated value in .env
NGINX_BASIC_AUTH_PASSWORD=$(cat /tmp/.htpasswd | awk -F: '{print $2}')
```

---

## Monitoring & Maintenance

### 1. Health Checks

Monitor your deployment:

```bash
# Check NGINX status
docker-compose exec nginx nginx -t

# View NGINX logs
docker-compose logs -f nginx

# Check i3x2ua health
curl https://your-domain.com/v1/info

# Monitor with health endpoint
watch curl https://your-domain.com/v1/info
```

### 2. Log Management

```bash
# Configure log rotation (logrotate)
cat > /etc/logrotate.d/i3x2ua << EOF
/var/lib/docker/containers/*/*.log {
  rotate 10
  daily
  compress
  delaycompress
  copytruncate
  size 100M
}
EOF
```

### 3. Backup Strategy

```bash
# Backup SSL certificates
cp -r certs/ certs_backup_$(date +%Y%m%d)

# Automated backup script for certificates
#!/bin/bash
BACKUP_DIR="/backups/i3x2ua"
mkdir -p $BACKUP_DIR

# Daily certificate backup
cp -r certs/ $BACKUP_DIR/certs_backup_$(date +%Y%m%d_%H%M%S)

# Keep only last 30 days
find $BACKUP_DIR -name "certs_backup_*" -mtime +30 -exec rm -rf {} \;
```

### 4. Monitoring Stack (Prometheus + Grafana)

```yaml
# Add to docker-compose.yml
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    ports:
      - "9090:9090"
    networks:
      - backend

  grafana:
    image: grafana/grafana:latest
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD}
    ports:
      - "3000:3000"
    volumes:
      - grafana_data:/var/lib/grafana
    networks:
      - backend

volumes:
  prometheus_data:
  grafana_data:
```

### 5. Certificate Expiration Monitoring

```bash
# Check certificate expiration
openssl x509 -in certs/fullchain.pem -noout -enddate

# Create monitoring script
#!/bin/bash
CERT_FILE="certs/fullchain.pem"
EXPIRY_DATE=$(openssl x509 -in $CERT_FILE -noout -enddate | cut -d= -f2)
EXPIRY_EPOCH=$(date -d "$EXPIRY_DATE" +%s)
NOW_EPOCH=$(date +%s)
DAYS_LEFT=$(( ($EXPIRY_EPOCH - $NOW_EPOCH) / 86400 ))

if [ $DAYS_LEFT -lt 30 ]; then
  echo "WARNING: Certificate expires in $DAYS_LEFT days"
  # Send alert/email
fi
```

---

## Troubleshooting

### 1. HTTPS Connection Issues

**Problem**: "Connection refused" on HTTPS port

```bash
# Check if port is listening
netstat -tuln | grep 443

# Check NGINX configuration
docker-compose exec nginx nginx -t

# Check certificate files exist and are readable
docker-compose exec nginx ls -la /etc/nginx/certs/

# Verify certificate validity
docker-compose exec nginx openssl x509 -in /etc/nginx/certs/fullchain.pem -text -noout
```

**Problem**: "SSL certificate problem" / "certificate verify failed"

```bash
# Verify certificate chain
openssl verify -CAfile fullchain.pem fullchain.pem

# Test with verbose curl
curl -v https://your-domain.com/v1/info

# Check certificate matches domain
openssl x509 -noout -text -in certs/fullchain.pem | grep -A1 "Subject Alternative Name"
```

### 2. Authentication Issues

**Problem**: BasicAuth not working

```bash
# Verify htpasswd file was created
docker-compose exec nginx cat /etc/nginx//.htpasswd

# Test authentication manually
htpasswd -vb /tmp/.htpasswd admin your-password

# Check NGINX logs for auth errors
docker-compose logs nginx | grep -i auth
```

**Problem**: OAuth tokens not validating

```bash
# Check oauth2-proxy logs
docker-compose logs oauth2-proxy

# Verify OAuth client credentials
docker-compose exec oauth2-proxy printenv | grep OAUTH

# Test token endpoint
curl -X POST https://your-oauth-provider/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET"
```

### 3. Load Balancing Issues

**Problem**: Requests only going to one backend

```bash
# Check upstream configuration in NGINX
docker-compose exec nginx cat /etc/nginx/conf.d/default.conf | grep -A 10 upstream

# Test individual backends directly
curl http://i3x2ua-1:8000/v1/info
curl http://i3x2ua-2:8001/v1/info
curl http://i3x2ua-3:8002/v1/info

# Check backend health
docker-compose ps
```

**Problem**: Uneven distribution across backends

```bash
# Monitor requests per backend
docker-compose logs i3x2ua-1 | wc -l
docker-compose logs i3x2ua-2 | wc -l
docker-compose logs i3x2ua-3 | wc -l

# Change load balancing algorithm
# Update NGINX_LOAD_BALANCING in .env and restart
```

### 4. Performance Issues

**Problem**: Slow response times

```bash
# Check container resource usage
docker stats

# Monitor network traffic
docker-compose exec nginx netstat -an | grep ESTABLISHED | wc -l

# Check OPC-UA connection latency
docker-compose logs i3x2ua | grep -i "connection\|latency"

# Scale up instances or increase workers
# Update docker-compose.yml --workers parameter
```

**Problem**: High memory usage

```bash
# Check process memory usage
docker exec i3x2ua-1 ps aux

# Adjust uvicorn workers
# Reduce --workers parameter in docker-compose.yml

# Monitor memory trends
docker stats --no-stream --format "{{.Container}}\t{{.MemUsage}}"
```

### 5. Certificate Renewal Failures

**Problem**: Certbot renewal fails

```bash
# Test renewal manually
certbot renew --dry-run

# Check logs
sudo tail -f /var/log/letsencrypt/letsencrypt.log

# Ensure ports are accessible
sudo netstat -tuln | grep :80
sudo netstat -tuln | grep :443

# Manual renewal
certbot renew --force-renewal

# Copy to project
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/

# Restart NGINX
docker-compose restart nginx
```

### 7. General Debugging

**Collect diagnostic information**:

```bash
#!/bin/bash
# Diagnostics script
echo "=== Docker Version ==="
docker --version

echo -e "\n=== Container Status ==="
docker-compose ps

echo -e "\n=== NGINX Configuration ==="
docker-compose exec nginx cat /etc/nginx/conf.d/default.conf

echo -e "\n=== Recent NGINX Logs ==="
docker-compose logs --tail=50 nginx

echo -e "\n=== Recent i3x2ua Logs ==="
docker-compose logs --tail=50 i3x2ua-1

echo -e "\n=== Network Status ==="
docker network inspect i3x2ua_backend

echo -e "\n=== Disk Usage ==="
docker system df

# Save to file for analysis
```

---

## Examples and Use Cases

### Example 1: Small Production Setup (Single Instance)

```env
# .env
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.mycompany.com
NGINX_SSL_CERTS_DIR=./certs
NGINX_BASIC_AUTH_ENABLED=1
NGINX_BASIC_AUTH_USER=apiuser
NGINX_BASIC_AUTH_PASSWORD=SecurePassword123!
I3X_OPCUA_ENDPOINT=opc.tcp://opcua.mycompany.com:4843
I3X_OPCUA_USERNAME=opcua_user
I3X_OPCUA_PASSWORD=opcua_pass
I3X_LOG_LEVEL=INFO
```

### Example 2: High-Availability Multi-Instance Setup

```env
# .env - HA Configuration
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.mycompany.com
NGINX_LOAD_BALANCING=least_conn
NGINX_UPSTREAM_HOSTS=i3x2ua-1:8000 i3x2ua-2:8001 i3x2ua-3:8002
DB_USER=i3x2ua
DB_PASSWORD=StrongDbPassword123!
I3X_OPCUA_ENDPOINT=opc.tcp://opcua.mycompany.com:4843
I3X_LOG_LEVEL=WARN
```

### Example 3: OAuth Protected Setup

```env
# .env - OAuth Configuration
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.mycompany.com
OAUTH_CLIENT_ID=your-client-id.apps.googleusercontent.com
OAUTH_CLIENT_SECRET=your-client-secret
OAUTH_ISSUER_URL=https://accounts.google.com
OAUTH_ALLOWED_EMAILS=admin@mycompany.com,dev@mycompany.com
```

## Additional Resources

- [NGINX Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [OAuth2-Proxy GitHub](https://github.com/oauth2-proxy/oauth2-proxy)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [i3x2ua README](../README.md)

---

## Support and Contribution

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review the [i3X Documentation](../i3X/)
3. Open an issue on GitHub

Last updated: 2024
