# Production Deployment Quick Reference

## Files Included

| File | Purpose |
|------|---------|
| `PRODUCTION_HTTPS_GUIDE.md` | Comprehensive production deployment guide (START HERE) |
| `.env.production.example` | Example environment configuration |
| `docker-compose.multi-instance.yml` | Multi-instance HA setup with load balancing |
| `cert-manager.sh` | Certificate management utility script |
| `deploy.sh` | Automated deployment helper script |
| `QUICK_REFERENCE.md` | This quick reference guide |

---

## Quick Start (5 minutes)

### 1. Prepare Certificates
```bash
# Option A: Generate self-signed (development)
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/privkey.pem \
  -out certs/fullchain.pem -days 365 -nodes -subj "/CN=your-domain.com"

# Option B: Use Let's Encrypt
certbot certonly --standalone -d your-domain.com
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem certs/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem certs/
```

### 2. Configure Environment
```bash
# Copy example and edit
cp docs/.env.production.example .env

# Edit .env with your values:
# - NGINX_SERVER_NAME=your-domain.com
# - I3X_OPCUA_ENDPOINT=opc.tcp://your-server:4843
# - I3X_OPCUA_USERNAME=your-username
# - I3X_OPCUA_PASSWORD=your-password
```

### 3. Start Services
```bash
# Using provided helper script
bash docs/deploy.sh full

# Or manually
docker-compose up -d
docker-compose ps
curl https://your-domain.com/v1/info
```

---

## Common Tasks

### Check Certificate Status
```bash
bash docs/cert-manager.sh check --cert-dir ./certs
bash docs/cert-manager.sh info
bash docs/cert-manager.sh renewal
```

### Enable BasicAuth
```env
NGINX_BASIC_AUTH_ENABLED=1
NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=secure-password
```

### Multi-Instance Setup
```bash
# Use multi-instance compose file
docker-compose -f docs/docker-compose.multi-instance.yml up -d

# Update .env with per-instance OPC-UA settings
I3X_OPCUA_ENDPOINT_1=opc.tcp://server-1:4843
I3X_OPCUA_ENDPOINT_2=opc.tcp://server-2:4843
I3X_OPCUA_ENDPOINT_3=opc.tcp://server-3:4843
```

### Access Multiple Instances
```bash
# Instance 1
curl https://your-domain.com/i3x/instance1/v1/info

# Instance 2
curl https://your-domain.com/i3x/instance2/v1/info

# Instance 3
curl https://your-domain.com/i3x/instance3/v1/info
```

### View Logs
```bash
docker-compose logs -f nginx
docker-compose logs -f i3x2ua
docker-compose logs -f --tail=50  # Last 50 lines
```

### Monitor Services
```bash
docker-compose ps
docker stats
docker-compose top i3x2ua
```

### Restart Services
```bash
docker-compose restart                  # All services
docker-compose restart nginx            # Specific service
docker-compose restart i3x2ua-1         # Specific instance
```

### Backup Data
```bash
bash docs/deploy.sh backup
```

---

## Environment Variables Quick Reference

```env
# HTTPS/SSL
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.example.com
NGINX_SSL_CERTIFICATE=/etc/nginx/certs/fullchain.pem
NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/privkey.pem

# BasicAuth (optional)
NGINX_BASIC_AUTH_ENABLED=0
NGINX_BASIC_AUTH_USER=admin
NGINX_BASIC_AUTH_PASSWORD=password

# OPC-UA
I3X_OPCUA_ENDPOINT=opc.tcp://server:4843
I3X_OPCUA_USERNAME=user
I3X_OPCUA_PASSWORD=pass

# MCP Support (optional)
I3X_ENABLE_MCP=0

# Logging
I3X_LOG_LEVEL=INFO
```

---

## MCP Support (Model Context Protocol)

MCP is an optional feature that exposes the i3X API as tools for AI models and tool-calling clients.

### Enable MCP

To enable MCP tool support, set:

```env
I3X_ENABLE_MCP=1
```

If running without Docker, pass it to the startup command:

```bash
I3X_ENABLE_MCP=1 uv run uvicorn i3x_server.main:app --host 0.0.0.0 --port 8000
```

### MCP Scope and Capabilities

| Capability | Status | Notes |
|------------|--------|-------|
| Tool discovery and listing | ✅ Supported | All read/query/subscribe operations exposed. |
| Tool calling | ✅ Supported | Dispatches to REST API endpoints. |
| Prompt discovery and execution | ✅ Supported | `prompts/list`, `prompts/get`, `prompts/execute` via JSON-RPC and REST. |
| Resource listing and reading | ✅ Supported | `resources/list`, `resources/read` via JSON-RPC and REST. |
| Root listing | ✅ Supported | `roots/list` via JSON-RPC and REST. |
| JSON-RPC batch requests | ✅ Supported | Array request payloads are handled per JSON-RPC 2.0. |
| Update/write operations | REST-only optional | `PUT` routes are intentionally excluded from MCP tools. Use REST writes with `I3X_ENABLE_WRITES=1`. |
| Server-Sent Event streaming | ✅ Available | Via REST `/v1/subscriptions/stream` endpoint. |

### MCP Endpoints

- **Discovery**: `GET /mcp` — Returns SSE endpoint for MCP discovery.
- **Tool listing**: `GET /mcp/tools` — Returns tool catalog in REST format.
- **Tool calling**: `POST /mcp/call` — REST-style tool invocation.
- **Prompt listing**: `GET /mcp/prompts` — Returns prompt metadata.
- **Prompt definition**: `GET /mcp/prompts/{name}` — Returns a full prompt definition.
- **Prompt execution**: `POST /mcp/prompts/execute` — Renders and executes prompt templates.
- **Resource listing**: `GET /mcp/resources` — Returns MCP resources.
- **Resource reading**: `POST /mcp/resources/read` — Reads a resource by URI.
- **Root listing**: `GET /mcp/roots` — Returns available MCP roots.
- **JSON-RPC**: `POST /mcp` — Standard JSON-RPC 2.0 interface.

### With Authentication (BasicAuth + HTTPS)

When `NGINX_BASIC_AUTH_ENABLED=1`, MCP clients must include credentials in requests:

```bash
curl -u admin:password https://your-domain.com/mcp/tools
```

For JSON-RPC clients (e.g., LM Studio), configure the MCP server URL as:

```
https://admin:password@your-domain.com/mcp
```

For detailed setup, see `docs/LM_STUDIO_MCP_GUIDE.md` and its capability matrix.

---

## Troubleshooting Quick Fixes

### HTTPS Connection Refused
```bash
# Check if NGINX is running
docker-compose ps nginx

# Check certificate files
docker-compose exec nginx ls -la /etc/nginx/certs/

# Verify NGINX config
docker-compose exec nginx nginx -t

# Check if port is open
sudo netstat -tuln | grep 443
```

### Slow Responses
```bash
# Check OPC-UA connection
docker-compose logs i3x2ua | grep -i "connection\|timeout"

# Monitor container resources
docker stats

# Check load across instances (if multi-instance)
docker-compose logs i3x2ua-1 | wc -l
docker-compose logs i3x2ua-2 | wc -l
```

---

## Security Checklist

- [ ] Use strong passwords (16+ characters)
- [ ] Enable HTTPS in production
- [ ] Set read-only filesystem on containers
- [ ] Drop all Linux capabilities except needed ones
- [ ] Enable firewall rules (UFW/iptables)
- [ ] Restrict SSH access
- [ ] Set up certificate auto-renewal
- [ ] Monitor certificate expiration
- [ ] Enable rate limiting on NGINX
- [ ] Use BasicAuth or OAuth for API access
- [ ] Regular backups
- [ ] Monitor access logs
- [ ] Keep containers updated

---

## Monitoring & Alerts

### Check NGINX Health
```bash
curl -I https://your-domain.com/v1/info
```

### Container Health
```bash
docker-compose exec i3x2ua python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/info')"
```

### Disk Usage
```bash
docker system df
```

### Certificate Expiration
```bash
openssl x509 -in certs/fullchain.pem -noout -enddate
```

---

## Performance Tuning

### Increase Workers (Per Instance)
In `docker-compose.yml`:
```yaml
command:
  - --workers
  - "8"  # Increase from 4
```

### Enable Caching Headers
Add to NGINX config in `entrypoint.sh`:
```nginx
add_header Cache-Control "public, max-age=3600" always;
```

### Load Balancing Algorithm
In `.env`:
```env
NGINX_LOAD_BALANCING=least_conn  # Change from round_robin
```

---

## Reference Links

- Full Guide: [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)
- NGINX Docs: https://nginx.org/en/docs/
- Let's Encrypt: https://letsencrypt.org/docs/
- Docker Compose: https://docs.docker.com/compose/
- OAuth2-Proxy: https://github.com/oauth2-proxy/oauth2-proxy

---

## Getting Help

1. Check [Troubleshooting](#troubleshooting-quick-fixes) section
2. Review logs: `docker-compose logs service-name`
3. See full guide: [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)
4. Check Docker health: `docker-compose ps`

---

**Last Updated**: 2024
**Status**: Production Ready
