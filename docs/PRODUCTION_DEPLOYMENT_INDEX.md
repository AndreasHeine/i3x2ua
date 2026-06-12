# Production Documentation Index

Complete guide for deploying i3x2ua in production with HTTPS, path-based routing, and authentication.

## 📚 Documentation Files

### Core Documentation

1. **[PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)** ⭐ START HERE
   - Comprehensive production deployment guide
   - HTTPS/SSL setup with Let's Encrypt and commercial certificates
   - BasicAuth and OAuth authentication
   - Multi-instance setup with path-based routing
   - Security best practices
   - Monitoring and maintenance
   - Complete troubleshooting section
   - **Read time**: 30-45 minutes

2. **[QUICK_REFERENCE.md](QUICK_REFERENCE.md)** 🚀 QUICK START
   - 5-minute quick start guide
   - Common commands and tasks
   - Environment variables reference
   - Quick troubleshooting fixes
   - Useful command snippets
   - **Read time**: 5-10 minutes

3. **[NGINX_CONFIGURATION_REFERENCE.md](NGINX_CONFIGURATION_REFERENCE.md)** ⚙️ REFERENCE
   - Detailed NGINX configuration reference
   - SSL/TLS configuration options
   - Authentication setup
   - Path-based routing strategies
   - Security headers
   - Performance tuning
   - **Read time**: 15-20 minutes

### Configuration & Scripts

4. **[.env.production.example](.env.production.example)** 🔧 CONFIG TEMPLATE
   - Production environment configuration template
   - All configuration options documented
   - Security recommendations
   - Copy to `.env` and customize for your environment

5. **[docker-compose.multi-instance.yml](docker-compose.multi-instance.yml)** 📦 MULTI-INSTANCE SETUP
   - Multi-instance Docker Compose configuration
   - 3 independent i3x2ua instances with separate OPC-UA connections
   - NGINX reverse proxy with path-based routing
   - Prometheus and Grafana monitoring (optional)
   - Each instance completely independent with no shared state
   - Use with: `docker-compose -f docker-compose.multi-instance.yml up -d`

6. **[cert-manager.sh](cert-manager.sh)** 🔐 CERTIFICATE TOOL
   - Certificate management utility
   - Generate, verify, and check certificates
   - Validate certificate chain and domain matching
   - Integration with Let's Encrypt
   - **Usage**: `bash cert-manager.sh help`

7. **[deploy.sh](deploy.sh)** 🚀 DEPLOYMENT TOOL
   - Automated deployment helper script
   - Validates prerequisites and configuration
   - Builds and starts services
   - Runs health checks
   - Backup functionality
   - **Usage**: `bash deploy.sh full`

---

## 🚀 Getting Started

### For First-Time Setup (30 minutes)

1. **Read**: [QUICK_REFERENCE.md](QUICK_REFERENCE.md) (5 min)
2. **Copy**: `.env.production.example` to `.env` and edit
3. **Setup**: Certificates using [cert-manager.sh](cert-manager.sh) (5 min)
4. **Deploy**: Using [deploy.sh](deploy.sh) (5 min)
5. **Verify**: Test your deployment (10 min)

### For Detailed Understanding (2-3 hours)

1. Read [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md) (45 min)
2. Review [.env.production.example](.env.production.example) (15 min)
3. Study [NGINX_CONFIGURATION_REFERENCE.md](NGINX_CONFIGURATION_REFERENCE.md) (30 min)
4. Plan your deployment strategy (30 min)
5. Execute deployment (30 min)

### For Multi-Instance Setup (1 hour)

1. Read [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md) - Multi-Instance Section
2. Review [docker-compose.multi-instance.yml](docker-compose.multi-instance.yml)
3. Customize `.env` with multi-instance settings
4. Deploy: `docker-compose -f docker-compose.multi-instance.yml up -d`
5. Verify with: `docker-compose ps`

---

## 📋 Quick Command Reference

### Certificate Management
```bash
# Check certificate status
bash docs/cert-manager.sh check

# Generate self-signed (dev only)
bash docs/cert-manager.sh generate-self --cert-dir ./certs

# Verify certificate chain
bash docs/cert-manager.sh verify

# Check expiration
bash docs/cert-manager.sh renewal
```

### Deployment
```bash
# Full deployment workflow
bash docs/deploy.sh full

# Initialize only
bash docs/deploy.sh init

# Start services
bash docs/deploy.sh start

# Test deployment
bash docs/deploy.sh test

# Backup data
bash docs/deploy.sh backup

# Show status
bash docs/deploy.sh status
```

### Docker Commands
```bash
# View container status
docker-compose ps

# View logs
docker-compose logs -f nginx
docker-compose logs -f i3x2ua

# Test service
curl https://your-domain.com/v1/info

# Multi-instance deployment
docker-compose -f docs/docker-compose.multi-instance.yml up -d

# Multi-instance status
docker-compose -f docs/docker-compose.multi-instance.yml ps

# Test multi-instance endpoints
curl https://your-domain.com/i3x/instance1/v1/info
curl https://your-domain.com/i3x/instance2/v1/info
curl https://your-domain.com/i3x/instance3/v1/info

# Restart services
docker-compose restart

# Stop services
docker-compose down
```

---

## 🔒 Security Checklist

Before going to production, ensure:

- [ ] Strong passwords used (16+ characters)
- [ ] HTTPS enabled with valid certificate
- [ ] Read-only filesystem on containers
- [ ] Linux capabilities dropped
- [ ] Firewall configured (UFW/iptables)
- [ ] SSH access restricted
- [ ] Certificate renewal automated
- [ ] BasicAuth or OAuth enabled (if required)
- [ ] Rate limiting configured
- [ ] Regular backups enabled
- [ ] Monitoring/alerting set up
- [ ] SSL/TLS configuration hardened
- [ ] Security headers enabled
- [ ] Logging enabled and monitored

See [PRODUCTION_HTTPS_GUIDE.md - Security Best Practices](PRODUCTION_HTTPS_GUIDE.md#security-best-practices) for detailed guidance.

---

## 🐛 Troubleshooting

### Issue: HTTPS Connection Refused
**Reference**: [PRODUCTION_HTTPS_GUIDE.md - HTTPS Connection Issues](PRODUCTION_HTTPS_GUIDE.md#1-https-connection-issues)

```bash
# Check certificate files
docker-compose exec nginx ls -la /etc/nginx/certs/

# Test NGINX configuration
docker-compose exec nginx nginx -t

# Check if port is open
sudo netstat -tuln | grep 443
```

### Issue: Certificate Verification Failed
**Reference**: [cert-manager.sh](cert-manager.sh)

```bash
# Verify certificate chain
bash docs/cert-manager.sh verify

# Check certificate details
bash docs/cert-manager.sh test

# Check if certificate matches domain
bash docs/cert-manager.sh domain
```

### Issue: Service Slow or Unresponsive
**Reference**: [PRODUCTION_HTTPS_GUIDE.md - Performance Issues](PRODUCTION_HTTPS_GUIDE.md#4-performance-issues)

```bash
# Check container resources
docker stats

# Monitor OPC-UA connection
docker-compose logs i3x2ua | grep -i "connection"

# Scale to multi-instance setup for better performance
```

### Issue: BasicAuth Not Working
**Reference**: [PRODUCTION_HTTPS_GUIDE.md - Authentication Issues](PRODUCTION_HTTPS_GUIDE.md#2-authentication-issues)

```bash
# Check htpasswd file
docker-compose exec nginx cat /etc/nginx/.htpasswd

# Test locally
htpasswd -vb /tmp/.htpasswd admin your-password
```

See complete troubleshooting guide in [PRODUCTION_HTTPS_GUIDE.md - Troubleshooting](PRODUCTION_HTTPS_GUIDE.md#troubleshooting).

---

## 📊 Deployment Scenarios

### Scenario 1: Single Instance with HTTPS

**Setup Time**: 15 minutes
**Complexity**: Low
**Scalability**: Limited

```env
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.example.com
NGINX_BASIC_AUTH_ENABLED=1
```

**Reference**: [PRODUCTION_HTTPS_GUIDE.md - Quick Start](PRODUCTION_HTTPS_GUIDE.md#quick-start)

### Scenario 2: Multi-Instance with Path-Based Routing

**Setup Time**: 30 minutes
**Complexity**: Medium
**Scalability**: Horizontal (add more instances easily)

Each instance runs independently with its own OPC-UA connection.

```env
NGINX_HTTPS_ENABLED=1
NGINX_SERVER_NAME=api.example.com

# Instance configurations
I3X_OPCUA_ENDPOINT_1=opc.tcp://server-1:4843
I3X_OPCUA_ENDPOINT_2=opc.tcp://server-2:4843
I3X_OPCUA_ENDPOINT_3=opc.tcp://server-3:4843
```

**Access Pattern**:
- `/i3x/instance1/v1/...` → i3x2ua-1
- `/i3x/instance2/v1/...` → i3x2ua-2
- `/i3x/instance3/v1/...` → i3x2ua-3

**Reference**: [PRODUCTION_HTTPS_GUIDE.md - Multi-Instance Setup](PRODUCTION_HTTPS_GUIDE.md#multi-instance-setup)

### Scenario 3: Multi-Instance with OAuth and Monitoring

**Setup Time**: 60 minutes
**Complexity**: High
**Scalability**: Very High

Uses OAuth2 authentication with Prometheus/Grafana monitoring.

**Reference**: [PRODUCTION_HTTPS_GUIDE.md - OAuth 2.0 Authentication](PRODUCTION_HTTPS_GUIDE.md#2-oauth-20-authentication)

---

## 🔄 Maintenance Tasks

### Weekly
- [ ] Check NGINX logs for errors
- [ ] Monitor resource usage (CPU, memory, disk)
- [ ] Verify all services are healthy

### Monthly
- [ ] Review access logs for suspicious activity
- [ ] Test backup and recovery procedure
- [ ] Check certificate expiration date

### Quarterly
- [ ] Update container images
- [ ] Review security configuration
- [ ] Audit access logs

### Annually
- [ ] Renew certificates (if not Let's Encrypt)
- [ ] Review and update documentation
- [ ] Perform security audit

See [PRODUCTION_HTTPS_GUIDE.md - Monitoring & Maintenance](PRODUCTION_HTTPS_GUIDE.md#monitoring--maintenance) for detailed procedures.

---

## 📞 Support Resources

### Documentation
- [Full Production Guide](PRODUCTION_HTTPS_GUIDE.md)
- [NGINX Configuration Reference](NGINX_CONFIGURATION_REFERENCE.md)
- [Quick Reference Card](QUICK_REFERENCE.md)

### Tools
- [Certificate Manager Script](cert-manager.sh)
- [Deployment Script](deploy.sh)
- [Example Configuration](.env.production.example)

### External Resources
- [NGINX Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt Documentation](https://letsencrypt.org/docs/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [OAuth2-Proxy GitHub](https://github.com/oauth2-proxy/oauth2-proxy)

### Project Documentation
- [i3x2ua README](../README.md)
- [i3X Specification](../i3X/spec/README.md)
- [Contributing Guide](../i3X/Contributing.md)

---

## 📝 File Structure

```
docs/
├── PRODUCTION_HTTPS_GUIDE.md           (Comprehensive guide - 45 min read)
├── QUICK_REFERENCE.md                   (Quick commands - 5 min read)
├── NGINX_CONFIGURATION_REFERENCE.md     (Configuration reference - 20 min read)
├── PRODUCTION_DEPLOYMENT_INDEX.md       (This file)
├── .env.production.example              (Configuration template)
├── docker-compose.multi-instance.yml    (HA setup)
├── cert-manager.sh                      (Certificate management utility)
└── deploy.sh                            (Deployment automation script)
```

---

## ✅ Verification Checklist

After deployment, verify:

- [ ] Services are running: `docker-compose ps`
- [ ] HTTPS is responding: `curl https://your-domain.com/v1/info`
- [ ] Authentication works (if enabled)
- [ ] Health checks pass
- [ ] Logs show no errors
- [ ] Certificates are valid
- [ ] All instances are accessible (if multi-instance)

---

## 🎓 Learning Path

1. **Beginner** (30 min)
   - Read: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
   - Run: [deploy.sh](deploy.sh) full
   - Test: curl your-domain

2. **Intermediate** (2-3 hours)
   - Read: [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md)
   - Study: [.env.production.example](.env.production.example)
   - Review: Your deployment configuration

3. **Advanced** (4-6 hours)
   - Read: [NGINX_CONFIGURATION_REFERENCE.md](NGINX_CONFIGURATION_REFERENCE.md)
   - Customize: NGINX configuration
   - Implement: Multi-instance setup
   - Configure: OAuth or advanced auth

---

## 📌 Important Notes

1. **Never commit `.env`** - Use `.env.production.example` as template
2. **Backup certificates** - Store backups securely
3. **Automate renewal** - Setup certificate auto-renewal
4. **Monitor logs** - Check regularly for errors
5. **Test before production** - Always validate in staging first
6. **Keep documentation updated** - Update as you customize

---

**Last Updated**: 2024
**Status**: Production Ready ✅
**Version**: 1.0

For questions or issues, refer to the [PRODUCTION_HTTPS_GUIDE.md](PRODUCTION_HTTPS_GUIDE.md) or check the [Troubleshooting](PRODUCTION_HTTPS_GUIDE.md#troubleshooting) section.
