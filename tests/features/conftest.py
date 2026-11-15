"""
Conftest for features test package.

Re-exports shared fixtures from tests/conftest.py for pytest discovery.
"""

from tests.conftest import client, client_without_mcp, configure_test_app, fastapi_app
from tests.opcua_fake import FakeExtensionObject, FakeMachineConfig, FakeMachineThresholds, FakeOpcUaClient

__all__ = [
    "FakeMachineConfig",
    "FakeMachineThresholds",
    "FakeExtensionObject",
    "FakeOpcUaClient",
    "client",
    "client_without_mcp",
    "configure_test_app",
    "fastapi_app",
]
