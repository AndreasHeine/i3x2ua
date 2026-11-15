# Security Policy

## Security Overview

This project takes security seriously and strives to follow industry best practices regarding secure software development, dependency management, vulnerability handling, and release management.

## Supported Versions

The latest released version is actively maintained and receives security updates.

| Version | Supported |
|----------|------------|
| Latest Release | ✅ |
| Older Releases | ❌ |

## Reporting a Vulnerability

If you discover a potential security vulnerability, please report it responsibly.

Please include as much information as possible:

- Description of the issue
- Impact assessment
- Steps to reproduce
- Affected versions
- Proof of concept (if available)

Please do **not** disclose vulnerabilities publicly before they have been reviewed and addressed.

E-Mail: info@andreas-heine.net

## Security Practices

### Dependency Management

Dependency updates are owned by a single bot, Renovate. It proposes upgrades for the Python pins and
the uv lockfile, the Dockerfile and Compose images, GitHub Actions pins, and the frontend packages.
Upgrades arrive as pull requests and are never auto-merged, so every one is reviewed and must pass CI.

`asyncua` is not upgraded automatically for major or minor versions, and lockfile maintenance does not
move it. The gateway's transport adapter and the OPC UA fixture server rely on that library's internal
state, so a bump is a deliberate change reviewed against the conformance suite. Patch updates are
proposed, but they must pass CI before merging.

Vulnerability detection is separate from upgrade proposals and runs in CI:

- `pip-audit` checks the locked Python environment against the PyPI advisory database
- Trivy scans the built image before anything is published
- The frontend is installed from its committed lockfile with `npm ci`

The bot regularly:

- Proposes dependency upgrades for every managed dependency source
- Keeps the lockfiles refreshed within their pinned ranges
- Helps maintain version currency

### Release Management

The project follows a regular release process.

- Security fixes are included in the next available release.
- Releases are published regularly.
- Dependency and vulnerability updates are continuously integrated.

### Secure Development

The project aims to follow secure coding practices including:

- Input validation
- Principle of least privilege
- Secure defaults
- Error handling without information leakage
- Dependency minimization

### Supply Chain Security

To reduce software supply chain risks:

- Dependencies are reviewed before adoption.
- Automated dependency update tooling is enabled.
- Security advisories are monitored continuously.
- Unsupported and abandoned libraries are avoided whenever possible.

## Authentication and Authorization

The gateway itself does not enforce an HTTP authentication scheme: TLS termination and
authentication belong to the reverse proxy, which is the security boundary. Running the app
without a proxy (`uv run uvicorn ...`, or `docker run -p 8080:8000`) therefore exposes the API in
plaintext, including the write endpoints when `I3X_ENABLE_WRITES=1`. Keep such runs on loopback.

Production deployments should:

- Run behind a reverse proxy that terminates TLS and enforces authentication; the shipped Compose
  stack enables nginx Basic Auth and refuses to start when `NGINX_BASIC_AUTH_USER` or
  `NGINX_BASIC_AUTH_PASSWORD` is missing
- Use HTTPS/TLS exclusively
- Enforce authentication for protected endpoints
- Implement role-based access control where applicable
- Store credentials securely
- Rotate secrets periodically
- Keep `/v1/info` public, as i3X requires it; `clientId` on subscriptions is a scope identifier,
  not a credential

## Infrastructure Security

Production environments should:

- Run behind a reverse proxy (e.g. NGINX)
- Enable TLS termination
- Enforce authentication and authorization
- Restrict administrative access
- Enable audit logging (the shipped nginx proxy logs every request, successful ones included)
- Keep operating systems and containers updated

## Sample Certificates and Keys

Private key material is not part of the repository. Development certificate bundles are generated
at setup:

```bash
uv run python scripts/generate_https_dev_cert.py
uv run python scripts/generate_opcua_client_cert.py
```

The sample bundles that were committed earlier - `certs/https-sample/privkey.pem` with its
certificate and `certs/opcua-client-sample/client-key.pem` with its certificate - are
**withdrawn**. Their private keys were published in a public repository and remain reachable in
git history, so they carry no confidentiality or identity value and must not be trusted anywhere:

- Untrust the sample OPC UA client certificate on any server that previously trusted it, and
  issue new per-environment material for real clients.
- Reissue TLS material for any endpoint that used the sample key; anyone with the repository can
  read it and impersonate the edge.
- Never use the sample material in production or as client credentials.
- Generate unique material with the scripts above (or a CA), keep private keys out of the
  repository and out of image builds, and note that regenerated material under `certs/` is
  ignored by git.

## Vulnerability Handling Process

When a vulnerability is identified:

1. Assess severity and impact.
2. Validate reproducibility.
3. Develop a remediation.
4. Validate the fix.
5. Publish an updated release.
6. Communicate remediation guidance if required.

## Security Updates

Security-related updates are delivered through the normal release process.

Users are encouraged to:

- Keep deployments updated
- Regularly review release notes
- Apply updates in a timely manner

## Disclaimer

While every effort is made to provide a secure implementation, no software can be considered 