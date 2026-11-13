"""i3X gateway package.

The application is assembled in ``bootstrap``; this module exposes the server version that
``GET /v1/server-info`` reports."""

from __future__ import annotations

from .version import get_server_version

__all__ = ["get_server_version"]
