from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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


class _FakeUaClient:
    def __init__(self) -> None:
        self.browse_calls = 0
        self.browse_next_calls = 0
        self.fail_browse_once = False
        self.fail_browse_next_once = False
        self.browse_continuation_point: bytes | None = None

    async def browse(self, params: Any) -> list[Any]:
        del params
        self.browse_calls += 1
        if self.fail_browse_once:
            self.fail_browse_once = False
            raise RuntimeError("connection is closed")
        result = SimpleNamespace(
            References=[],
            ContinuationPoint=self.browse_continuation_point,
        )
        return [result]

    async def browse_next(self, params: Any) -> list[Any]:
        del params
        self.browse_next_calls += 1
        if self.fail_browse_next_once:
            self.fail_browse_next_once = False
            raise RuntimeError("connection is not open")
        result = SimpleNamespace(
            References=[],
            ContinuationPoint=None,
        )
        return [result]


class _FakeNodeId:
    def __init__(self, value: str) -> None:
        self._value = value

    def to_string(self) -> str:
        return self._value


class _FakeBrowseNode:
    def __init__(self, value: str) -> None:
        self.nodeid = _FakeNodeId(value)


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
    cast(Any, client)._client = SimpleNamespace(get_node=lambda _id: _FakeNode(7))
    assert await client._read_positive_int("ns=0;i=1") == 7

    cast(Any, client)._client = SimpleNamespace(get_node=lambda _id: _FakeNode(0))
    assert await client._read_positive_int("ns=0;i=1") is None

    cast(Any, client)._client = SimpleNamespace(get_node=lambda _id: _FakeNode(error=RuntimeError("boom")))
    assert await client._read_positive_int("ns=0;i=1") is None

    assert client._should_retry_after_disconnect(RuntimeError("connection is closed")) is True
    assert client._should_retry_after_disconnect(RuntimeError("different error")) is False


@pytest.mark.asyncio
async def test_subscription_wrapper_methods() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    cast(Any, client)._client = fake

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
    cast(Any, client)._client = _FakeClient()
    await client._configure_security_if_needed()
    assert client._using_security is False

    with pytest.raises(ValueError):
        bad = OpcUaClient(endpoint="opc.tcp://localhost:4840", security_mode="Sign")
        cast(Any, bad)._client = _FakeClient()
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
    secure_fake = _FakeClient()
    cast(Any, secure)._client = secure_fake
    await secure._configure_security_if_needed()
    assert secure._using_security is True
    assert secure_fake.security_string is not None
    assert "Basic256Sha256,Sign" in secure_fake.security_string


@pytest.mark.asyncio
async def test_reconnect_calls_listeners_even_if_one_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    cast(Any, client)._client = fake

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

    monkeypatch.setattr(client, "load_additional_typedefinitions", _noop)
    monkeypatch.setattr(client, "_configure_security_if_needed", _noop)
    await client._reconnect()

    assert fake.connected is True
    assert fake.disconnected is True
    assert called == ["fail", "ok"]


@pytest.mark.asyncio
async def test_reconnect_clears_metadata_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    cast(Any, client)._client = fake

    cast(Any, client)._limits_cache = object()
    cast(Any, client)._subscription_caps_cache = object()
    cast(Any, client)._namespace_infos_cache = (1.0, [])
    cast(Any, client)._object_types_cache = (1.0, [])

    async def _noop() -> None:
        return None

    monkeypatch.setattr(client, "load_additional_typedefinitions", _noop)
    monkeypatch.setattr(client, "_configure_security_if_needed", _noop)

    await client._reconnect()

    assert cast(Any, client)._limits_cache is None
    assert cast(Any, client)._subscription_caps_cache is None
    assert cast(Any, client)._namespace_infos_cache is None
    assert cast(Any, client)._object_types_cache is None


@pytest.mark.asyncio
async def test_browse_retry_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake_uaclient = _FakeUaClient()
    fake_uaclient.fail_browse_once = True
    cast(Any, client)._client = SimpleNamespace(uaclient=fake_uaclient)

    reconnect_calls = 0

    async def _fake_reconnect() -> None:
        nonlocal reconnect_calls
        reconnect_calls += 1

    monkeypatch.setattr(client, "_reconnect", _fake_reconnect)

    fake_node = _FakeBrowseNode("ns=1;i=100")
    out = await client._browse_references_descriptions(
        [fake_node],
        max_nodes_per_browse=10,
        reference_type_id=33,
    )

    assert reconnect_calls == 1
    assert fake_uaclient.browse_calls == 2
    assert len(out) == 1


@pytest.mark.asyncio
async def test_browse_next_retry_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake_uaclient = _FakeUaClient()
    fake_uaclient.fail_browse_next_once = True
    fake_uaclient.browse_continuation_point = b"cp1"
    cast(Any, client)._client = SimpleNamespace(uaclient=fake_uaclient)

    reconnect_calls = 0

    async def _fake_reconnect() -> None:
        nonlocal reconnect_calls
        reconnect_calls += 1

    monkeypatch.setattr(client, "_reconnect", _fake_reconnect)

    fake_node = _FakeBrowseNode("ns=1;i=200")
    out = await client._browse_references_descriptions(
        [fake_node],
        max_nodes_per_browse=10,
        reference_type_id=33,
    )

    assert reconnect_calls == 1
    assert fake_uaclient.browse_next_calls == 2
    assert len(out) == 1
