# HTTPS Sample Certificate for Nginx

This directory contains a development-only HTTPS certificate bundle for the nginx reverse proxy.

Files:

- fullchain.pem: self-signed server certificate (PEM)
- privkey.pem: matching private key (PEM, unencrypted)

Generate or refresh these files with:

```bash
uv run python scripts/generate_https_dev_cert.py
```

Optional custom values:

```bash
uv run python scripts/generate_https_dev_cert.py \
  --common-name localhost \
  --dns localhost --dns my-dev-host \
  --ip 127.0.0.1 \
  --days-valid 365 \
  --include-hostname
```

Use `--ip` for numeric addresses so they are written as SAN `iPAddress` entries; `--dns`
entries are written as `dNSName`. `--days-valid` must be positive. `--include-hostname` is off
by default so the certificate does not carry your machine name.

Use with Docker Compose nginx HTTPS settings:

- NGINX_HTTPS_ENABLED=1
- NGINX_SSL_CERTS_DIR=./certs
- NGINX_SSL_CERTIFICATE=/etc/nginx/certs/https-sample/fullchain.pem
- NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/https-sample/privkey.pem

Important:

- The files in this directory are committed to a public repository, so the private key is
  public sample material with no confidentiality or identity value.
- This bundle is not intended for production use.
- Browsers will show a warning for self-signed certificates unless the certificate is trusted locally.
- For production, use certificates from your CA or Let's Encrypt.
