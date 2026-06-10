from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from i3x_server.opcua.client import (
    OpcUaClient,
    _assert_file_exists,
    _chunked,
    _chunked_nodes,
    _to_json_compatible,
)


class _FakeNode:
    def __init__(self, value: Any = None, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    async def read_value(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._value


class _FakeClient:
    def __init__(self) -> None:
        self.security_string: str | None = None
        self.connected = False
        self.disconnected = False

    async def set_security_string(self, value: str) -> None:
        self.security_string = value

    async def connect(self, auto_reconnect: bool, reconnect_max_delay: float) -> None:
        del auto_reconnect, reconnect_max_delay
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    def get_node(self, _node_id: str) -> _FakeNode:
        return _FakeNode(3)

    async def create_subscription(self, publishing_interval_ms: float, handler: Any) -> Any:
        return SimpleNamespace(interval=publishing_interval_ms, handler=handler)


class _FakeSubscription:
    def __init__(self) -> None:
        self.deleted = False
        self.nodes: list[Any] = []

    async def subscribe_data_change(self, nodes: list[Any]) -> list[int]:
        self.nodes = nodes
        return [1 for _ in nodes]

    async def delete(self) -> None:
        self.deleted = True


def test_chunk_and_json_helpers() -> None:
    assert _chunked(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    assert _chunked_nodes([1, 2, 3], 2) == [[1, 2], [3]]

    raw = {
        "now": datetime(2026, 1, 1, 12, 0, 0),
        "list": [1, {"nested": (2, 3)}],
        "other": object(),
    }
    out = _to_json_compatible(raw)
    assert isinstance(out["now"], str)
    assert out["list"][1]["nested"] == [2, 3]
    assert isinstance(out["other"], str)


def test_assert_file_exists(tmp_path: Path) -> None:
    present = tmp_path / "present.pem"
    present.write_text("x", encoding="utf-8")
    _assert_file_exists(present, "cert")
    with pytest.raises(FileNotFoundError):
        _assert_file_exists(tmp_path / "missing.pem", "cert")


@pytest.mark.asyncio
async def test_read_positive_int_and_retry_detection() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    client._client = SimpleNamespace(get_node=lambda _id: _FakeNode(7))
    assert await client._read_positive_int("ns=0;i=1") == 7

    client._client = SimpleNamespace(get_node=lambda _id: _FakeNode(0))
    assert await client._read_positive_int("ns=0;i=1") is None

    client._client = SimpleNamespace(get_node=lambda _id: _FakeNode(error=RuntimeError("boom")))
    assert await client._read_positive_int("ns=0;i=1") is None

    assert client._should_retry_after_disconnect(RuntimeError("connection is closed")) is True
    assert client._should_retry_after_disconnect(RuntimeError("different error")) is False


@pytest.mark.asyncio
async def test_subscription_wrapper_methods() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    client._client = fake

    created = await client.create_datachange_subscription(500.0, handler=object())
    assert created.interval == 500.0

    subscription = _FakeSubscription()
    handles = await client.subscribe_data_changes(subscription, ["ns=1;i=1", "ns=1;i=2"])
    assert handles == [1, 1]
    assert len(subscription.nodes) == 2

    await client.delete_subscription(subscription)
    assert subscription.deleted is True


@pytest.mark.asyncio
async def test_configure_security_none_and_missing_config(tmp_path: Path) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840", security_mode="None")
    client._client = _FakeClient()
    await client._configure_security_if_needed()
    assert client._using_security is False

    with pytest.raises(ValueError):
        bad = OpcUaClient(endpoint="opc.tcp://localhost:4840", security_mode="Sign")
        bad._client = _FakeClient()
        await bad._configure_security_if_needed()

    cert = tmp_path / "client-cert.pem"
    key = tmp_path / "client-key.pem"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")

    secure = OpcUaClient(
        endpoint="opc.tcp://localhost:4840",
        security_mode="Sign",
        security_policy="Basic256Sha256",
        client_cert_path=str(cert),
        client_key_path=str(key),
        client_key_password="pw",
    )
    secure._client = _FakeClient()
    await secure._configure_security_if_needed()
    assert secure._using_security is True
    assert secure._client.security_string is not None
    assert "Basic256Sha256,Sign" in secure._client.security_string


@pytest.mark.asyncio
async def test_reconnect_calls_listeners_even_if_one_fails() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    client._client = fake

    called: list[str] = []

    async def ok_listener() -> None:
        called.append("ok")

    async def failing_listener() -> None:
        called.append("fail")
        raise RuntimeError("listener failed")

    client.add_reconnect_listener(failing_listener)
    client.add_reconnect_listener(ok_listener)

    async def _noop() -> None:
        return None

    client.load_additional_typedefinitions = _noop
    client._configure_security_if_needed = _noop
    await client._reconnect()

    assert fake.connected is True
    assert fake.disconnected is True
    assert called == ["fail", "ok"]