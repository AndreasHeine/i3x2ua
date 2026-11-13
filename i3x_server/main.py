"""ASGI entry point: the module-level ``app`` that uvicorn serves."""

from i3x_server.bootstrap.app_factory import create_app

app = create_app()
