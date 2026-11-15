# HTTPS Sample Certificate for Nginx

This directory holds a development-only HTTPS certificate bundle for the nginx reverse proxy.
The bundle is generated at setup and is not tracked by git (`certs/**/*.pem` is ignored), so a
fresh clone contains only this README.

Generate the files with:

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

The script writes `fullchain.pem` (self-signed server certificate) and `privkey.pem` (matching
private key, unencrypted) into `certs/https-sample/`.

Use with Docker Compose nginx HTTPS settings:

- NGINX_HTTPS_ENABLED=1
- NGINX_SSL_CERTS_DIR=./certs
- NGINX_SSL_CERTIFICATE=/etc/nginx/certs/https-sample/fullchain.pem
- NGINX_SSL_CERTIFICATE_KEY=/etc/nginx/certs/https-sample/privkey.pem

Important:

- The bundle is generated at setup and never committed: private keys stay out of the repository,
  and git ignores `certs/**/*.pem`.
- The sample bundle that used to be committed here is **withdrawn**. Its private key was published
  in a public repository, so it must not be trusted by any client or proxy; generate your own.
- This bundle is not intended for production use.
- Browsers will show a warning for self-signed certificates unless the certificate is trusted locally.
- nginx refuses to start when `NGINX_HTTPS_ENABLED=1` and the certificate or key is missing, so
  generate the bundle before `docker compose up -d`.
- For production, use certificates from your CA or Let's Encrypt.
