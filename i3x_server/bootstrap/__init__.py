"""Bootstrap package surface.

``create_app`` is re-exported lazily via ``__getattr__`` (PEP 562). An eager
re-export here made every ``i3x_server.api.v1`` import chain resolve
``bootstrap`` -> ``app_factory`` -> ``api.v1.router`` while ``api.v1`` was only
partially initialized (routes import ``bootstrap.dependencies``), which raised
a circular ``ImportError`` whenever ``api.v1`` was imported first. Keeping this
module free of package imports also removes the cycle from static analysis.
"""

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from i3x_server.bootstrap.app_factory import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
