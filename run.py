import os
import subprocess
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
        if value.startswith(("\"", "'")) and value.endswith(("\"", "'")) and len(value) >= 2:
            value = value[1:-1]
        os.environ.setdefault(key, value)

if __name__ == "__main__":
    _load_local_env()
    host = os.getenv("I3X_HOST", "0.0.0.0")
    port = os.getenv("I3X_PORT", "8000")

    raise SystemExit(
        subprocess.call(
            [
                "uv",
                "run",
                "uvicorn",
                "i3x_server.main:app",
                "--reload",
                "--host",
                host,
                "--port",
                port,
            ]
        )
    )
