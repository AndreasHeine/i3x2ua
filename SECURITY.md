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

Dependencies are continuously monitored and updated through automated tooling:

- Dependabot
- Renovate

These tools regularly:

- Identify vulnerable dependencies
- Propose dependency upgrades
- Track new security advisories
- Help maintain version currency

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

Production deployments should:

- Use HTTPS/TLS exclusively
- Enforce authentication for protected endpoints
- Implement role-based access control where applicable
- Store credentials securely
- Rotate secrets periodically

## Infrastructure Security

Production environments should:

- Run behind a reverse proxy (e.g. NGINX)
- Enable TLS termination
- Enforce authentication and authorization
- Restrict administrative access
- Enable audit logging
- Keep operating systems and containers updated

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