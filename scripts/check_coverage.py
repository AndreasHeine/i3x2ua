#!/usr/bin/env python3
"""Gate the coverage report written by ``pytest --cov-report=xml``.

Both CI (``.github/workflows/quality.yml``) and the local runner (``run_tests.py``) call this
script so the thresholds cannot drift apart. The report is parsed from ``coverage.xml``;
line and branch rates are gated separately because a combined ``--cov-fail-under`` would hide
uncovered error branches behind high line counts.

Usage::

    python scripts/check_coverage.py [--xml coverage.xml]

Exit codes: 0 = both gates met, 1 = a gate failed or the report is missing/unparseable.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Line gate matches run_tests.py's historical target; branch gate sits below the measured
# baseline (76.74 %) so it locks progress without blocking feature work on legacy gaps.
TARGET_LINE_PERCENT = 85.0
TARGET_BRANCH_PERCENT = 75.0


def _rate_percent(root: ET.Element, attribute: str) -> float | None:
    raw = root.attrib.get(attribute)
    if raw is None:
        return None
    try:
        return float(raw) * 100.0
    except ValueError:
        return None


def check_coverage(xml_path: Path) -> int:
    if not xml_path.exists():
        print(f"Coverage check failed: {xml_path} not found. Run pytest with --cov-report=xml first.")
        return 1
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        print(f"Coverage check failed: {xml_path} is not valid XML: {exc}")
        return 1

    line_percent = _rate_percent(root, "line-rate")
    branch_percent = _rate_percent(root, "branch-rate")
    if line_percent is None or branch_percent is None:
        print(
            f"Coverage check failed: {xml_path} lacks line-rate/branch-rate attributes "
            "(branch coverage requires [tool.coverage.run] branch = true)."
        )
        return 1

    branches_valid = int(root.attrib.get("branches-valid", "0"))
    branches_covered = int(root.attrib.get("branches-covered", "0"))
    print(f"Coverage report: {xml_path}")
    print(f"  line-rate:   {line_percent:.2f}% (gate >= {TARGET_LINE_PERCENT:.2f}%)")
    print(
        f"  branch-rate: {branch_percent:.2f}% (gate >= {TARGET_BRANCH_PERCENT:.2f}%), "
        f"{branches_valid} branch outcomes, {branches_covered} fully covered"
    )

    failures: list[str] = []
    if line_percent < TARGET_LINE_PERCENT:
        failures.append(f"line coverage {line_percent:.2f}% < {TARGET_LINE_PERCENT:.2f}%")
    if branch_percent < TARGET_BRANCH_PERCENT:
        failures.append(f"branch coverage {branch_percent:.2f}% < {TARGET_BRANCH_PERCENT:.2f}%")
    if failures:
        print("Coverage gates failed: " + "; ".join(failures))
        return 1
    print("Coverage gates passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", default="coverage.xml", help="Path to the coverage XML report.")
    args = parser.parse_args()
    return check_coverage(Path(args.xml))


if __name__ == "__main__":
    sys.exit(main())
