import subprocess

if __name__ == "__main__":
    raise SystemExit(
        subprocess.call(
            [
                "uv",
                "run",
                "uvicorn",
                "i3x_server.main:app",
                "--reload",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ]
        )
    )