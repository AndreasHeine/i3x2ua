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
        if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    if os.name == "nt":
        # Keep local dev requests on localhost stable unless explicitly overridden.
        os.environ.setdefault("I3X_HOST", "127.0.0.1")
    # Keep local dev startup responsive unless the caller explicitly set a value.
    os.environ.setdefault("I3X_SKIP_OPCUA_CONNECT", "1")
    _load_local_env()
    host = os.getenv("I3X_HOST", "0.0.0.0")
    port = os.getenv("I3X_PORT", "8000")
    if os.name == "nt":
        reload_default = "0"
    else:
        reload_default = "1"
    reload_enabled = os.getenv("I3X_RELOAD", reload_default).strip().lower() in {"1", "true", "yes", "on"}
    requested_loop = os.getenv("I3X_UVICORN_LOOP", "").strip().lower()
    # uvicorn 0.41 imports uvicorn.loops (which imports uvloop) for several
    # loop options. On Windows, forcing "none" avoids uvloop import failures.
    if os.name == "nt":
        uvicorn_loop = "none"
    else:
        uvicorn_loop = requested_loop or "auto"

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

    raise SystemExit(subprocess.call(command))
