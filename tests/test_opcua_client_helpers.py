from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua

from i3x_server.infrastructure.opcua.client import (
    OpcUaClient,
    OpcUaNamespaceInfo,
    OpcUaOperationalLimits,
    _assert_file_exists,
    _chunked,
    _chunked_nodes,
    _to_explicit_ua_variant,
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

    async def read_data_value(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._value

    async def read_raw_history(
        self,
        starttime: datetime | None,
        endtime: datetime | None,
        numvalues: int,
        return_bounds: bool,
    ) -> Any:
        del starttime, endtime, numvalues, return_bounds
        if self._error is not None:
            raise self._error
        return self._value


class _FakeClient:
    def __init__(self, node_error: Exception | None = None) -> None:
        self.security_string: str | None = None
        self.connected = False
        self.disconnected = False
        self._node_error = node_error

    async def set_security_string(self, value: str) -> None:
        self.security_string = value

    async def connect(self, auto_reconnect: bool, reconnect_max_delay: float) -> None:
        del auto_reconnect, reconnect_max_delay
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    def get_node(self, _node_id: str) -> _FakeNode:
        return _FakeNode(3, error=self._node_error)

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


def test_to_explicit_ua_variant_builds_typed_variant() -> None:
    variant = _to_explicit_ua_variant(60, "Double")

    assert variant is not None
    assert variant.VariantType == ua.VariantType.Double
    assert variant.Value == 60


def test_to_explicit_ua_variant_returns_none_for_unknown_type() -> None:
    assert _to_explicit_ua_variant(60, "UnknownType") is None


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
    assert client._should_retry_after_disconnect(RuntimeError("Connection reset by peer")) is True
    assert client._should_retry_after_disconnect(RuntimeError("BadSessionClosed")) is True
    assert client._should_retry_after_disconnect(RuntimeError("different error")) is False


@pytest.mark.asyncio
async def test_probe_connection_marks_connected_after_success() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = SimpleNamespace(get_node=lambda _id: _FakeNode(SimpleNamespace()))
    client._set_connection_state("Reconnecting")

    await client._probe_connection()

    assert client.get_connection_snapshot().state == "Connected"


@pytest.mark.asyncio
async def test_probe_connection_marks_disconnected_if_reconnect_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = SimpleNamespace(get_node=lambda _id: _FakeNode(error=RuntimeError("socket closed")))
    client._set_connection_state("Connected")

    async def _fail_reconnect() -> None:
        raise RuntimeError("still down")

    monkeypatch.setattr(client, "_reconnect", _fail_reconnect)

    await client._probe_connection()

    assert client.get_connection_snapshot().state == "Disconnected"


@pytest.mark.asyncio
async def test_read_history_values_treats_unsupported_history_as_empty() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    nodes = {
        "good": _FakeNode([SimpleNamespace(Value=SimpleNamespace(Value=1))]),
        "bad": _FakeNode(error=RuntimeError("BadHistoryOperationUnsupported")),
    }
    cast(Any, client)._client = SimpleNamespace(get_node=lambda node_id: nodes[node_id])

    values = await client.read_history_values(["good", "bad"], None, None)

    assert values["good"]
    assert values["bad"] == []


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
    fake = _FakeClient(node_error=RuntimeError("connection is closed"))
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
    fake = _FakeClient(node_error=RuntimeError("connection is closed"))
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
async def test_reconnect_short_circuits_when_connection_is_healthy() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake = _FakeClient()
    cast(Any, client)._client = fake

    await client._reconnect()

    assert fake.connected is False
    assert fake.disconnected is False
    assert client.get_connection_snapshot().state == "Connected"


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


@pytest.mark.asyncio
async def test_read_values_splits_batches_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BatchLimitedClient:
        def get_node(self, node_id: str) -> SimpleNamespace:
            return SimpleNamespace(node_id=node_id)

        async def read_values(self, nodes: list[Any]) -> list[Any]:
            if len(nodes) > 1:
                raise RuntimeError("too many operations")
            return [f"value-{nodes[0].node_id}"]

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = _BatchLimitedClient()

    async def _limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(max_nodes_per_browse=None, max_nodes_per_read=10)

    monkeypatch.setattr(client, "get_operational_limits", _limits)

    values = await client.read_values(["a", "b", "c"])
    assert values == ["value-a", "value-b", "value-c"]


@pytest.mark.asyncio
async def test_read_values_returns_none_for_failing_single_node(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AlwaysFailingClient:
        def get_node(self, node_id: str) -> SimpleNamespace:
            return SimpleNamespace(node_id=node_id)

        async def read_values(self, nodes: list[Any]) -> list[Any]:
            del nodes
            raise RuntimeError("read failed")

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = _AlwaysFailingClient()

    async def _limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(max_nodes_per_browse=None, max_nodes_per_read=10)

    monkeypatch.setattr(client, "get_operational_limits", _limits)

    values = await client.read_values(["only-node"])
    assert values == [None]


@pytest.mark.asyncio
async def test_get_namespace_infos_tolerates_partial_metadata_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NodeId:
        def __init__(self, value: str) -> None:
            self._value = value

        def to_string(self) -> str:
            return self._value

    class _Node:
        def __init__(
            self,
            node_id: str,
            display_name: str | None = None,
            value: Any = None,
            value_error: Exception | None = None,
        ) -> None:
            self.nodeid = _NodeId(node_id)
            self._display_name = display_name
            self._value = value
            self._value_error = value_error

        async def read_display_name(self) -> SimpleNamespace:
            return SimpleNamespace(Text=self._display_name or "")

        async def read_value(self) -> Any:
            if self._value_error is not None:
                raise self._value_error
            return self._value

    class _NamespaceMetaClient:
        def __init__(self) -> None:
            self._nodes: dict[str, _Node] = {
                "i=11715": _Node("i=11715"),
                "component-ok": _Node("component-ok", display_name="Component Ok"),
                "component-fail": _Node("component-fail", display_name="Component Fail"),
                "uri-ok": _Node("uri-ok", value="urn:ok"),
                "uri-fail": _Node("uri-fail", value_error=TimeoutError("metadata timeout")),
            }

        def get_node(self, node_id: Any) -> _Node:
            key = node_id.to_string() if hasattr(node_id, "to_string") else str(node_id)
            return self._nodes[key]

    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = _NamespaceMetaClient()

    async def _namespaces() -> list[str]:
        return ["urn:ok", "urn:fail"]

    async def _limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(max_nodes_per_browse=16, max_nodes_per_read=16)

    async def _browse_children_descriptions(nodes: list[Any], max_nodes_per_browse: int) -> list[tuple[Any, list[Any]]]:
        del max_nodes_per_browse
        if len(nodes) == 1 and nodes[0].nodeid.to_string() == "i=11715":
            return [
                (
                    nodes[0],
                    [
                        SimpleNamespace(BrowseName=SimpleNamespace(Name="Namespace"), NodeId="component-ok"),
                        SimpleNamespace(BrowseName=SimpleNamespace(Name="Namespace"), NodeId="component-fail"),
                    ],
                )
            ]
        return [
            (
                nodes[0],
                [SimpleNamespace(BrowseName=SimpleNamespace(Name="NamespaceUri"), NodeId="uri-ok")],
            ),
            (
                nodes[1],
                [SimpleNamespace(BrowseName=SimpleNamespace(Name="NamespaceUri"), NodeId="uri-fail")],
            ),
        ]

    monkeypatch.setattr(client, "get_namespaces", _namespaces)
    monkeypatch.setattr(client, "get_operational_limits", _limits)
    monkeypatch.setattr(client, "_browse_children_descriptions", _browse_children_descriptions)

    infos = await client.get_namespace_infos()

    assert infos == [
        OpcUaNamespaceInfo(uri="urn:ok", display_name="Component Ok"),
        OpcUaNamespaceInfo(uri="urn:fail", display_name=""),
    ]
