# Contributing

Thank you for contributing to this project.

## License and Contribution Terms

This repository is dual-licensed:

- Open-source distribution under AGPL-3.0-or-later
- Commercial distribution under separate commercial license terms

By submitting a contribution, you agree that your contribution:

- is licensed under AGPL-3.0-or-later for the open-source edition
- may be included in commercially licensed distributions of this project

## Developer Certificate of Origin (DCO)

All commits must include a Signed-off-by trailer.

Use this command when creating commits:

```bash
git commit -s -m "Your commit message"
```

This appends a trailer like:

```text
Signed-off-by: Your Name <your.email@example.com>
```

## Pull Requests

- Keep changes focused and include tests when relevant.
- Ensure CI passes before requesting review.
- If your pull request contains multiple logical changes, split them into separate commits where practical.

## Architecture Guardrails

Use the layered architecture guidance in docs/architecture.md when placing new code.

- Endpoint and protocol handlers belong in presentation modules under i3x_server/api.
- Multi-step orchestration belongs in i3x_server/application/services.
- Protocol-agnostic reusable rules belong in i3x_server/domain.
- External I/O adapters belong in infrastructure-oriented modules (currently i3x_server/infrastructure and related adapter modules).
- Startup and wiring concerns belong in i3x_server/bootstrap.

Avoid adding new logic to compatibility wrappers when a source module exists.

### File Size Guidance

- Preferred module size: <= 400 lines.
- Warning zone: > 600 lines.
- Refactor threshold: > 900 lines unless strongly justified.

If a module enters warning/threshold range, include an extraction note in the pull request description or a follow-up issue.

## Reporting Issues

Please include:

- expected behavior
- observed behavior
- reproduction steps
- environment details (OS, Python version, and relevant configuration)
