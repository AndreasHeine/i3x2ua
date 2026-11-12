"""Run project checks in sequence.

Commands:
1) uv run --group dev ruff check .
2) uv run --group dev ruff format --check .
3) uv run --group dev mypy .
4) uv run --group dev lint-imports
5) uv run --group dev pytest -q --cov=i3x_server --cov-report=term-missing
6) uv run --group dev python scripts/check_coverage.py

The coverage gate itself lives in ``scripts/check_coverage.py`` (line and branch thresholds),
which CI uses too, so local and CI gates cannot drift apart.
"""

from __future__ import annotations

import subprocess
import sys

BASE_COMMAND = ["uv", "run", "--group", "dev"]

COMMANDS: list[list[str]] = [
    [*BASE_COMMAND, "ruff", "check", "."],
    # ``--check`` mirrors CI: the runner reports formatting drift instead of rewriting files.
    [*BASE_COMMAND, "ruff", "format", "--check", "."],
    [*BASE_COMMAND, "mypy", "."],
    [*BASE_COMMAND, "lint-imports"],
    [*BASE_COMMAND, "pytest", "-q", "--cov=i3x_server", "--cov-report=term-missing"],
    [*BASE_COMMAND, "python", "scripts/check_coverage.py"],
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
