"""Structural conformance of `SubscriptionService` against `SubscriptionServicePort` (L8).

The dependency helper used to `cast` the service to the port, masking the fact
that strict mypy never verified the implementation against the protocol. These
assignments are checked by the `mypy .` gate: any drift between port and
implementation fails the type check instead of relying on runtime duck typing.
"""

from __future__ import annotations

from typing import cast

from i3x_server.application.ports.opcua import OpcUaClientProtocol
from i3x_server.application.ports.subscription import SubscriptionServicePort
from i3x_server.infrastructure.subscriptions.service import SubscriptionService


class _StubOpcUaClient:
    """Minimal client stub: the constructor only registers a reconnect listener."""

    def add_reconnect_listener(self, listener: object) -> None:
        del listener


# The assignment itself is the assertion: mypy strict verifies every protocol member.
_port_conformance: SubscriptionServicePort = SubscriptionService(
    opcua_client=cast(OpcUaClientProtocol, _StubOpcUaClient()),
    interval_seconds=1.0,
)


def test_subscription_service_structurally_satisfies_the_port() -> None:
    assert _port_conformance is not None
