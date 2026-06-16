"""Run project checks in sequence.

Commands:
1) uv run --extra dev ruff check .
2) uv run --extra dev ruff format .
3) uv run --extra dev mypy .
4) uv run --extra dev pytest -q --cov=i3x_server --cov-report=term-missing
"""

from __future__ import annotations

import subprocess
import sys

BASE_COMMAND = ["uv", "run", "--extra", "dev"]

COMMANDS: list[list[str]] = [
    [*BASE_COMMAND, "ruff", "check", "."],
    [*BASE_COMMAND, "ruff", "format", "."],
    [*BASE_COMMAND, "mypy", "."],
    [*BASE_COMMAND, "pytest", "-q", "--cov=i3x_server", "--cov-report=term-missing"],
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
