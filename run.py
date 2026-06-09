import os
import subprocess

if __name__ == "__main__":
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
