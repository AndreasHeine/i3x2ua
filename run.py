import os
import subprocess
import sys
from pathlib import Path


def _load_local_env(env_path: str = ".env") -> None:
    path = Path(env_path)
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            quote = value[0]
            closing = value.find(quote, 1)
            if closing > 0:
                # python-dotenv ignores whatever follows the closing quote (a comment).
                value = value[1:closing]
        elif value:
            # Unquoted values may carry a trailing ` # comment` (python-dotenv strips it).
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)


def _prepare_environment(env_path: str = ".env") -> None:
    """Load `.env` before applying platform defaults.

    Real shell values always win (everything goes through `setdefault`), but the file must
    be read *first*: on Windows the `I3X_HOST=127.0.0.1` default applied before the load
    silently out-ranked `.env` (`pydantic-settings` prefers the process environment over
    the file), so `python run.py` bound localhost even though `.env` requested `0.0.0.0`.
    """
    _load_local_env(env_path)
    if os.name == "nt":
        # Keep local dev requests on localhost stable unless explicitly overridden.
        os.environ.setdefault("I3X_HOST", "127.0.0.1")


def _uvicorn_command(host: str, port: str, reload_enabled: bool, uvicorn_loop: str) -> list[str]:
    """Build the uvicorn command line.

    ``--limit-concurrency`` sheds load spikes with 503 instead of queueing them on the
    event loop; the value is tunable because the HTTP side has no OPC UA budget attached
    to it. ``0`` or an empty value disables the limit (unlimited concurrency).
    """
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "i3x_server.main:app",
        "--host",
        host,
        "--port",
        port,
        "--loop",
        uvicorn_loop,
    ]
    if reload_enabled:
        command.insert(4, "--reload")

    limit_concurrency = os.getenv("I3X_UVICORN_LIMIT_CONCURRENCY", "200").strip()
    if limit_concurrency and limit_concurrency != "0":
        command += ["--limit-concurrency", limit_concurrency]

    return command


if __name__ == "__main__":
    _prepare_environment()
    host = os.getenv("I3X_HOST", "0.0.0.0")
    port = os.getenv("I3X_PORT", "8000")
    if os.name == "nt":
        reload_default = "0"
    else:
        reload_default = "1"
    reload_enabled = os.getenv("I3X_RELOAD", reload_default).strip().lower() in {"1", "true", "yes", "on"}
    requested_loop = os.getenv("I3X_UVICORN_LOOP", "").strip().lower()
    # uvicorn imports uvicorn.loops (which imports uvloop) for several
    # loop options. On Windows, forcing "none" avoids uvloop import failures.
    if os.name == "nt":
        uvicorn_loop = "none"
    else:
        uvicorn_loop = requested_loop or "auto"

    command = _uvicorn_command(host=host, port=port, reload_enabled=reload_enabled, uvicorn_loop=uvicorn_loop)

    raise SystemExit(subprocess.call(command))
