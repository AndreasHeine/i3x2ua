"""Headless i3X conformance gate: OPC UA fixture + gateway + i3X test suite.

``run_conformancetest.py`` starts an interactive web UI and requires a human to run the
suite. This variant is the CI/local gate: it starts the fixture and the gateway, waits for
both to be actually ready (no fixed sleeps), runs ``i3x-test run`` headlessly, writes the
report as JSON, and exits with the suite's exit code (0 == compliant).

Usage:
    .venv\\Scripts\\python.exe scripts\\run_conformance_headless.py
    .venv\\Scripts\\python.exe scripts\\run_conformance_headless.py --json i3x-conformance-report.json
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from io import TextIOWrapper
from pathlib import Path
from typing import Any

FIXTURE_STARTUP_TIMEOUT_SECONDS = 30.0
GATEWAY_STARTUP_TIMEOUT_SECONDS = 60.0
SUITE_TIMEOUT_SECONDS = 300.0
# The compliance gate: anything weaker (partial verdicts, unknown verdict) fails CI even if
# the suite itself exits 0, so a report cannot silently downgrade the requirement.
REQUIRED_VERDICT = "Full 1.0 Compliance"


def _load_orchestrator() -> Any:
    """Import the root orchestrator, which reuses the conformance env and process helpers.

    ``scripts/`` is ``sys.path[0]`` when this file is executed directly, so the repository
    root has to be added before the import. Doing it here keeps the module-level imports
    resolvable for the linter.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import run_conformancetest

    return run_conformancetest


def _reconfigure_output_streams() -> None:
    """Force UTF-8 with replacement on the console streams.

    Child output is streamed into this process's stdout, and on Windows the console default is
    cp1252, which cannot encode every byte a Node stack trace may emit. Only ``TextIOWrapper``
    instances support ``reconfigure``; a replaced or piped stream is left as it is.
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _port_is_open(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _wait_for_http(url: str, timeout: float) -> bool:
    """Poll a REST endpoint until it answers 200 (the model is built and serving)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the i3X conformance suite headlessly")
    parser.add_argument("--opcua-host", default="127.0.0.1")
    parser.add_argument("--opcua-port", type=int, default=4840)
    parser.add_argument("--i3x-host", default="127.0.0.1")
    parser.add_argument("--i3x-port", type=int, default=8000)
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Per-request timeout for the suite")
    parser.add_argument(
        "--json",
        default=None,
        help="Where to write the JSON report (default: no report file)",
    )
    parser.add_argument("--no-writes", action="store_true", help="Skip the Update/Subscribe write tests")
    return parser


def main() -> int:
    # Child output streams through _stream_output into this process's stdout; the UTF-8
    # reconfiguration keeps that stream from failing to encode a Node stack trace.
    _reconfigure_output_streams()

    orchestrator = _load_orchestrator()
    args = _build_parser().parse_args()

    node_command = orchestrator._require_conformance_tests()
    fixture_path = orchestrator.REPO_ROOT / "conf-test-server" / "server.py"
    if not fixture_path.is_file():
        raise RuntimeError(f"OPC UA fixture not found: {fixture_path}")

    endpoint = f"http://{args.i3x_host}:{args.i3x_port}/v1"
    json_path = Path(args.json).resolve() if args.json else None

    fixture_process = None
    gateway_process = None
    exit_code = 1
    try:
        fixture_process = orchestrator._start_process(
            "opcua-fixture",
            orchestrator._python_command(
                "conf-test-server/server.py", "--host", args.opcua_host, "--port", str(args.opcua_port)
            ),
            cwd=orchestrator.REPO_ROOT,
            env=os.environ.copy(),
        )
        if not _port_is_open(args.opcua_host, args.opcua_port, FIXTURE_STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(
                f"OPC UA fixture did not open port {args.opcua_port} within {FIXTURE_STARTUP_TIMEOUT_SECONDS}s"
            )

        gateway_env = os.environ.copy()
        gateway_env.update(orchestrator.I3X_CONFORMANCE_ENV)
        gateway_env["I3X_OPCUA_ENDPOINT"] = f"opc.tcp://{args.opcua_host}:{args.opcua_port}/freeopcua/server/"
        gateway_process = orchestrator._start_process(
            "i3x2ua",
            orchestrator._python_command(
                "-m",
                "uvicorn",
                "i3x_server.main:app",
                "--host",
                args.i3x_host,
                "--port",
                str(args.i3x_port),
                "--loop",
                "none",
            ),
            cwd=orchestrator.REPO_ROOT,
            env=gateway_env,
        )
        # Blocking preload is set in I3X_CONFORMANCE_ENV, so a 200 on /v1/info means the model
        # is built; the suite would otherwise race against a cold model.
        if not _wait_for_http(f"{endpoint}/info", GATEWAY_STARTUP_TIMEOUT_SECONDS):
            raise RuntimeError(f"gateway did not answer {endpoint}/info within {GATEWAY_STARTUP_TIMEOUT_SECONDS}s")

        suite_command = [
            node_command,
            str(orchestrator.CONFORMANCE_TESTS_ENTRY),
            "run",
            endpoint,
            "--timeout",
            str(args.timeout_ms),
            "--quiet",
        ]
        if json_path is not None:
            suite_command += ["--json", str(json_path)]
        if args.no_writes:
            suite_command += ["--no-writes"]

        suite = orchestrator._start_process(
            "i3x-test-suite", suite_command, cwd=orchestrator.CONFORMANCE_TESTS_DIR, env=os.environ.copy()
        )
        exit_code = int(suite.wait(timeout=SUITE_TIMEOUT_SECONDS))
    finally:
        orchestrator._stop_process("i3x2ua", gateway_process)
        orchestrator._stop_process("opcua-fixture", fixture_process)

    if json_path is not None and json_path.is_file():
        report = json.loads(json_path.read_text(encoding="utf-8"))
        counts = report.get("summary", {}).get("counts", {})
        verdict = report.get("summary", {}).get("verdict")
        print(
            f"[conformance] verdict={verdict} "
            f"pass={counts.get('pass')} fail={counts.get('fail')} warn={counts.get('warn')} skip={counts.get('skip')}"
        )
        if exit_code == 0 and verdict != REQUIRED_VERDICT:
            print(f"[conformance] gate failed: verdict {verdict!r} != required {REQUIRED_VERDICT!r}")
            return 1
    elif exit_code == 0:
        # Without a report the verdict cannot be verified; the exit code alone is weaker.
        print("[conformance] verdict gate not checked (no --json report written)")
    print(f"[conformance] suite exit code {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
