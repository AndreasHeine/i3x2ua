"""Domain port contracts for OPC UA access.

This module provides a stable import surface for OPC UA protocols and data
contracts used by application/domain logic. It currently re-exports the
existing contract surface to enable incremental migration.
"""

from i3x_server.infrastructure.opcua.client import *  # noqa: F403
