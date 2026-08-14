"""Orchestrate the full i3X conformance test stack.

Starts, in order:
1. The OPC UA conformance fixture server (conf-test-server/server.py)
2. i3x2ua (uvicorn) configured with conformance-relevant environment variables
    URL: http://localhost:8000/v1
3. The i3X conformance test tool web UI (node bin/i3x-test.js serve)
    URL: http://localhost:8330

Press Ctrl+C to stop all three processes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

REPO_ROOT = Path(__file__).resolve().parent
CONFORMANCE_TESTS_DIR = REPO_ROOT / "i3X" / "conformance-tests"

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
}


def _stream_output(prefix: str, pipe: IO[str]) -> None:
    for raw_line in iter(pipe.readline, ""):
        if not raw_line:
            break
        sys.stdout.write(f"[{prefix}] {raw_line}")
        sys.stdout.flush()


def _start_process(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[str]:
    print(f"[run-conformance] starting {name}: {' '.join(command)} (cwd={cwd})")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    thread = threading.Thread(target=_stream_output, args=(name, process.stdout), daemon=True)
    thread.start()
    return process


def _stop_process(name: str, process: subprocess.Popen[str] | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"[run-conformance] stopping {name} (pid={process.pid})")
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[run-conformance] {name} did not stop in time, killing it")
        process.kill()
        process.wait(timeout=timeout)


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

    opcua_endpoint = f"opc.tcp://{args.opcua_host}:{args.opcua_port}/freeopcua/server/"
    i3x_endpoint = f"http://{args.i3x_host}:{args.i3x_port}/v1"

    opcua_process: subprocess.Popen[str] | None = None
    i3x_process: subprocess.Popen[str] | None = None
    test_tool_process: subprocess.Popen[str] | None = None

    try:
        opcua_process = _start_process(
            "opcua-fixture",
            [
                "uv",
                "run",
                "python",
                "conf-test-server/server.py",
                "--host",
                args.opcua_host,
                "--port",
                str(args.opcua_port),
            ],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        )
        time.sleep(args.startup_delay_seconds)

        i3x_env = os.environ.copy()
        i3x_env.update(I3X_CONFORMANCE_ENV)
        i3x_env["I3X_OPCUA_ENDPOINT"] = opcua_endpoint
        i3x_process = _start_process(
            "i3x2ua",
            [
                "uv",
                "run",
                "uvicorn",
                "i3x_server.main:app",
                "--host",
                args.i3x_host,
                "--port",
                str(args.i3x_port),
                "--loop",
                "none",
            ],
            cwd=REPO_ROOT,
            env=i3x_env,
        )
        time.sleep(args.startup_delay_seconds)

        # shutil.which resolves PATHEXT (.cmd/.exe) so CreateProcess can find node on Windows.
        node_command = shutil.which("node")
        if node_command is None:
            raise RuntimeError("'node' was not found on PATH; install Node.js >= 18.17 to run the test tool")
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
