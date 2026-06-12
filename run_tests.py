"""Run project checks in sequence.

Commands:
1) uv run ruff format .
2) uv run mypy .
3) uv run pytest -q --cov=i3x_server --cov-report=term-missing
"""

from __future__ import annotations

import subprocess
import sys

COMMANDS: list[list[str]] = [
    ["uv", "run", "ruff", "check", ".", "--fix"],
    ["uv", "run", "ruff", "format", "."],
    ["uv", "run", "mypy", "."],
    ["uv", "run", "pytest", "-q", "--cov=i3x_server", "--cov-report=term-missing"],
]


def main() -> int:
    for command in COMMANDS:
        print(f"\n>>> Running: {' '.join(command)}")
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            print(f"\nCommand failed with exit code {completed.returncode}: {' '.join(command)}")
            return completed.returncode

    print("\nAll checks completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
