"""Orchestrate the full i3X conformance test stack.

Starts, in order:
1. The OPC UA conformance fixture server (conf-test-server/server.py)
2. i3x2ua (uvicorn) configured with conformance-relevant environment variables
    URL: http://localhost:8000/v1
3. The i3X conformance test tool web UI (node bin/i3x-test.js serve)
    URL: http://localhost:8330

Press Ctrl+C to stop all three processes.

Run it through the project environment so the children inherit the installed
dependencies, e.g.:
    uv run python run_conformancetest.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

REPO_ROOT = Path(__file__).resolve().parent
CONFORMANCE_TESTS_DIR = REPO_ROOT / "i3X" / "conformance-tests"
CONFORMANCE_TESTS_ENTRY = CONFORMANCE_TESTS_DIR / "bin" / "i3x-test.js"

_IS_WINDOWS = sys.platform == "win32"
# CTRL_BREAK_EVENT is the graceful Windows equivalent of SIGTERM; SIGTERM alone is not
# available there, and terminate() would kill the children without letting them release
# their ports.
_STOP_SIGNAL: int = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM) if _IS_WINDOWS else signal.SIGTERM

# Environment variables i3x2ua needs to talk to the local OPC UA fixture,
# taken from conf-test-server/README.md.
I3X_CONFORMANCE_ENV = {
    "I3X_OPCUA_SECURITY_MODE": "None",
    "I3X_OPCUA_SECURITY_POLICY": "",
    "I3X_SUBSCRIPTIONS_INITIAL_VALUES": "true",
    "I3X_SUBSCRIPTION_INTERVAL_SECONDS": "0.2",
    "I3X_MODEL_PRELOAD_ON_STARTUP": "true",
    "I3X_MODEL_PRELOAD_BLOCKING": "true",
    "I3X_SKIP_OPCUA_CONNECT": "0",
    "I3X_ENABLE_WRITES": "1",
    "I3X_ENABLE_MCP": "1",
    # The repo .env pins a production browse root (Machinery nsu) that the fixture does not
    # expose; the fixture tree hangs under Objects (NodeId 84 in namespace 0). Process env takes
    # precedence over dotenv in pydantic-settings, so this override neutralizes the .env value.
    "I3X_OPCUA_BROWSE_ROOTS": "nsu=http://opcfoundation.org/UA/;i=84",
}


def _stream_output(prefix: str, pipe: IO[str]) -> None:
    # A child can emit bytes that are not valid UTF-8 (e.g. a Node stack trace). Decoding
    # with errors="replace" keeps this reader thread alive instead of killing it silently
    # while the child keeps running.
    for raw_line in iter(pipe.readline, ""):
        sys.stdout.write(f"[{prefix}] {raw_line}")
        sys.stdout.flush()


def _process_isolation() -> dict[str, Any]:
    """Put each child in its own process group.

    Ctrl+C then reaches only this script, which stops the children explicitly; otherwise
    Windows leaves ports 8000 and 8330 bound by the surviving processes.
    """
    if _IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _start_process(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    print(f"[run-conformance] starting {name}: {' '.join(command)} (cwd={cwd})")
    child_env = {**env, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **_process_isolation(),
    )
    assert process.stdout is not None
    thread = threading.Thread(target=_stream_output, args=(name, process.stdout), daemon=True)
    thread.start()
    return process


def _stop_process(name: str, process: subprocess.Popen[str] | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"[run-conformance] stopping {name} (pid={process.pid})")
    try:
        process.send_signal(_STOP_SIGNAL)
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[run-conformance] {name} did not stop in time, killing it")
        process.kill()
        process.wait(timeout=timeout)


def _python_command(*args: str) -> list[str]:
    """Start a child with the interpreter running this script.

    A bare ``uv`` is not guaranteed to resolve on Windows (PATH entry may be ``uv.exe``),
    and ``uv run`` wraps the child in a second process whose termination does not reach the
    server on Windows. ``sys.executable`` is the project interpreter when started via
    ``uv run python run_conformancetest.py``.
    """
    return [sys.executable, *args]


def _require_conformance_tests() -> str:
    """Resolve the node executable after checking that the submodule is usable."""
    if not CONFORMANCE_TESTS_DIR.is_dir():
        raise RuntimeError(
            f"Conformance test tool directory not found: {CONFORMANCE_TESTS_DIR}. "
            "It is a git submodule; initialize it with 'git submodule update --init i3X'."
        )
    if not CONFORMANCE_TESTS_ENTRY.is_file():
        raise RuntimeError(
            f"Conformance test tool entry point not found: {CONFORMANCE_TESTS_ENTRY}. "
            "Initialize the submodule with 'git submodule update --init i3X'."
        )
    # The tool ships dependency-free (Node builtins only); npm install never creates a
    # node_modules directory for that, so only declared dependencies must be installed.
    package_json = CONFORMANCE_TESTS_DIR / "package.json"
    dependencies: dict[str, str] = {}
    if package_json.is_file():
        manifest = json.loads(package_json.read_text(encoding="utf-8"))
        dependencies = manifest.get("dependencies") or {}
    if dependencies and not (CONFORMANCE_TESTS_DIR / "node_modules").is_dir():
        raise RuntimeError(
            f"Conformance test tool dependencies are not installed in {CONFORMANCE_TESTS_DIR}. "
            "Run 'npm install' inside that directory first."
        )
    # shutil.which resolves PATHEXT (.cmd/.exe) so CreateProcess can find node on Windows.
    node_command = shutil.which("node")
    if node_command is None:
        raise RuntimeError("'node' was not found on PATH; install Node.js >= 18.17 to run the test tool")
    return node_command


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the full i3X conformance test stack")
    parser.add_argument("--opcua-host", default="127.0.0.1", help="Host for the OPC UA fixture server")
    parser.add_argument("--opcua-port", type=int, default=4840, help="Port for the OPC UA fixture server")
    parser.add_argument("--i3x-host", default="127.0.0.1", help="Host for i3x2ua")
    parser.add_argument("--i3x-port", type=int, default=8000, help="Port for i3x2ua")
    parser.add_argument("--test-tool-port", type=int, default=8330, help="Port for the i3X test tool web UI")
    parser.add_argument(
        "--startup-delay-seconds",
        type=float,
        default=3.0,
        help="Delay between starting each process, to let the previous one initialize",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    node_command = _require_conformance_tests()
    if not (REPO_ROOT / "conf-test-server" / "server.py").is_file():
        raise RuntimeError(f"OPC UA fixture not found: {REPO_ROOT / 'conf-test-server' / 'server.py'}")

    opcua_endpoint = f"opc.tcp://{args.opcua_host}:{args.opcua_port}/freeopcua/server/"
    i3x_endpoint = f"http://{args.i3x_host}:{args.i3x_port}/v1"

    opcua_process: subprocess.Popen[str] | None = None
    i3x_process: subprocess.Popen[str] | None = None
    test_tool_process: subprocess.Popen[str] | None = None

    try:
        opcua_process = _start_process(
            "opcua-fixture",
            _python_command("conf-test-server/server.py", "--host", args.opcua_host, "--port", str(args.opcua_port)),
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        )
        time.sleep(args.startup_delay_seconds)

        i3x_env = os.environ.copy()
        i3x_env.update(I3X_CONFORMANCE_ENV)
        i3x_env["I3X_OPCUA_ENDPOINT"] = opcua_endpoint
        i3x_process = _start_process(
            "i3x2ua",
            _python_command(
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
            cwd=REPO_ROOT,
            env=i3x_env,
        )
        time.sleep(args.startup_delay_seconds)

        test_tool_process = _start_process(
            "i3x-test-tool",
            [node_command, "bin/i3x-test.js", "serve", "-p", str(args.test_tool_port)],
            cwd=CONFORMANCE_TESTS_DIR,
            env=os.environ.copy(),
        )

        print(
            "\n[run-conformance] Stack is up:"
            f"\n  OPC UA fixture:  {opcua_endpoint}"
            f"\n  i3x2ua REST API: {i3x_endpoint}"
            f"\n  Test tool UI:    http://localhost:{args.test_tool_port}"
            "\nOpen the test tool UI and point it at the i3x2ua REST API above."
            "\nPress Ctrl+C to stop all processes.\n"
        )

        while True:
            for name, process in (
                ("opcua-fixture", opcua_process),
                ("i3x2ua", i3x_process),
                ("i3x-test-tool", test_tool_process),
            ):
                exit_code = process.poll()
                if exit_code is not None:
                    raise RuntimeError(f"{name} exited unexpectedly with code {exit_code}")
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[run-conformance] received interrupt, shutting down...")
    finally:
        _stop_process("i3x-test-tool", test_tool_process)
        _stop_process("i3x2ua", i3x_process)
        _stop_process("opcua-fixture", opcua_process)


if __name__ == "__main__":
    main()
