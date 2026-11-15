"""Headless i3X conformance gate: OPC UA fixture + gateway + i3X test suite.

``scripts/run_conformancetest.py`` starts an interactive web UI and requires a human to run the
suite. This variant is the CI/local gate: it starts the fixture and the gateway, waits for
both to be actually ready (no fixed sleeps), runs ``i3x-test run`` headlessly, writes the
report as JSON, and exits with the suite's exit code (0 == compliant).

Usage:
    .venv\\Scripts\\python.exe scripts\\run_conformance_headless.py
    .venv\\Scripts\\python.exe scripts\\run_conformance_headless.py --json i3x-conformance-report.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
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
# The gateway's own binding. The public endpoint defaults to it; a run through a reverse proxy
# overrides the host and port (nginx publishes 8443 and proxies the gateway port).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _load_orchestrator() -> Any:
    """Import the interactive runner, which reuses the conformance env and process helpers.

    ``scripts/`` is ``sys.path[0]`` when this file is executed directly, so the repository
    root has to be added before the import.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import scripts.run_conformancetest

    return scripts.run_conformancetest


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


def _endpoint(scheme: str, host: str, port: int) -> str:
    """The i3X REST base the suite runs against, i.e. what the caller actually reaches."""
    return f"{scheme}://{host}:{port}/v1"


def _ssl_context(insecure: bool) -> ssl.SSLContext | None:
    """A verification-disabled context for self-signed development certificates, else the default.

    ``None`` keeps ``urllib``'s default context, which verifies the certificate chain and the
    hostname: a secured run without ``--insecure`` therefore cannot pass against a self-signed
    proxy, so dropping the opt-out is visible instead of silently claiming a verified TLS stack.
    """
    if not insecure:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _auth_header(basic: str | None) -> dict[str, str]:
    """The ``Authorization`` header for ``user:password``, empty for an unauthenticated run."""
    if basic is None:
        return {}
    user, separator, password = basic.partition(":")
    if not separator or not user:
        raise ValueError(f"basic credentials must be 'user:password', got {basic!r}")
    credential = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {credential}"}


def _wait_for_http(url: str, timeout: float, *, insecure: bool = False, basic: str | None = None) -> bool:
    """Poll a REST endpoint until it answers 200 (the model is built and serving).

    Behind the secured stack the probe crosses TLS and Basic Auth, so both must be applied here:
    a 200 through the proxy is the only evidence that the model is ready *and* reachable.
    """
    deadline = time.monotonic() + timeout
    context = _ssl_context(insecure)
    headers = _auth_header(basic)
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=2.0, context=context) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def _suite_command(
    node_command: str,
    test_entry: Path,
    endpoint: str,
    basic: str | None,
    insecure: bool,
    json_path: Path | None,
    timeout_ms: int = 30000,
    no_writes: bool = False,
) -> list[str]:
    """The ``i3x-test run`` invocation for a given endpoint and transport configuration."""
    command = [node_command, str(test_entry), "run", endpoint, "--timeout", str(timeout_ms), "--quiet"]
    if basic is not None:
        command += ["--basic", basic]
    if insecure:
        command += ["--insecure"]
    if json_path is not None:
        command += ["--json", str(json_path)]
    if no_writes:
        command += ["--no-writes"]
    return command


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the i3X conformance suite headlessly")
    parser.add_argument("--opcua-host", default=DEFAULT_HOST)
    parser.add_argument("--opcua-port", type=int, default=4840)
    parser.add_argument("--i3x-host", default=DEFAULT_HOST)
    parser.add_argument("--i3x-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--scheme", choices=["http", "https"], default="http")
    parser.add_argument(
        "--public-host",
        default=DEFAULT_HOST,
        help="Host the suite reaches (a proxy in front of the gateway overrides this)",
    )
    parser.add_argument(
        "--public-port",
        type=int,
        default=DEFAULT_PORT,
        help="Port the suite reaches (nginx publishes 8443 for TLS)",
    )
    parser.add_argument("--basic", default=None, help="Basic Auth credentials as 'user:password'")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Accept self-signed TLS certificates (development stacks only)",
    )
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

    endpoint = _endpoint(args.scheme, args.public_host, args.public_port)
    json_path = Path(args.json).resolve() if args.json else None
    # Reject a malformed 'user:password' before any process starts: nginx would answer 401 to
    # every request and the suite would report contract failures, not a credential problem.
    _auth_header(args.basic)

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
        # is built; the suite would otherwise race against a cold model. Through a proxy the probe
        # also proves TLS and Basic Auth work, which the gateway's own port cannot show.
        ready = _wait_for_http(
            f"{endpoint}/info",
            GATEWAY_STARTUP_TIMEOUT_SECONDS,
            insecure=args.insecure,
            basic=args.basic,
        )
        if not ready:
            raise RuntimeError(f"gateway did not answer {endpoint}/info within {GATEWAY_STARTUP_TIMEOUT_SECONDS}s")

        suite_command = _suite_command(
            node_command,
            orchestrator.CONFORMANCE_TESTS_ENTRY,
            endpoint,
            args.basic,
            args.insecure,
            json_path,
            timeout_ms=args.timeout_ms,
            no_writes=args.no_writes,
        )

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
