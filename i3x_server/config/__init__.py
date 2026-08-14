"""Canonical settings live in i3x_server.config.settings.

The ``settings`` singleton is deliberately not re-exported here: the name would
shadow the submodule and make ``import i3x_server.config.settings as s`` bind
the instance instead of the module.
"""

from i3x_server.config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
