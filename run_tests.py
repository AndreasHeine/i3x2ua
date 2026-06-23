"""Run project checks in sequence.

Commands:
1) uv run --extra dev ruff check .
2) uv run --extra dev ruff format .
3) uv run --extra dev mypy .
4) uv run lint-imports
5) uv run --extra dev pytest -q --cov=i3x_server --cov-report=term-missing
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_COMMAND = ["uv", "run", "--extra", "dev"]
TARGET_COVERAGE_PERCENT = 85.0
COVERAGE_XML_PATH = Path("coverage.xml")

COMMANDS: list[list[str]] = [
    [*BASE_COMMAND, "ruff", "check", "."],
    [*BASE_COMMAND, "ruff", "format", "."],
    [*BASE_COMMAND, "mypy", "."],
    ["uv", "run", "lint-imports"],
    [*BASE_COMMAND, "pytest", "-q", "--cov=i3x_server", "--cov-report=term-missing"],
]


def _read_total_coverage_percent(path: Path) -> float | None:
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
        line_rate_raw = root.attrib.get("line-rate")
        if line_rate_raw is None:
            return None
        return float(line_rate_raw) * 100.0
    except (ET.ParseError, ValueError):
        return None


def main() -> int:
    for command in COMMANDS:
        print(f"\n>>> Running: {' '.join(command)}")
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            print(f"\nCommand failed with exit code {completed.returncode}: {' '.join(command)}")
            return completed.returncode

    total_coverage = _read_total_coverage_percent(COVERAGE_XML_PATH)
    if total_coverage is None:
        print("\nCoverage target check skipped: coverage.xml not found or unreadable.")
    elif total_coverage >= TARGET_COVERAGE_PERCENT:
        print(f"\nCoverage target reached: {total_coverage:.2f}% >= {TARGET_COVERAGE_PERCENT:.2f}%.")
    else:
        missing = TARGET_COVERAGE_PERCENT - total_coverage
        print(
            f"\nCoverage target not reached: {total_coverage:.2f}% < {TARGET_COVERAGE_PERCENT:.2f}% "
            f"(need +{missing:.2f}%)."
        )

    print("\nAll checks completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
