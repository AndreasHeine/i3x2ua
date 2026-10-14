from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asyncua import ua
from asyncua.client.client import Client
from asyncua.client.ua_client import UaClient, UaClientState
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import UaStatusCodeError

from i3x_server.application.services.model_query import ModelQueryService
from i3x_server.domain.ports.opcua import OpcUaNodeInfo
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
from i3x_server.model.builder import ModelBuilder


def _status_error(code: int) -> UaStatusCodeError:
    return UaStatusCodeError(code)  # type: ignore[no-untyped-call]


@pytest.mark.asyncio
async def test_reference_names_come_from_reference_types_not_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    refs = [
        ua.ReferenceDescription(
            NodeId=ua.ExpandedNodeId(ua.Int32(index), ua.Int16(2)),
            ReferenceTypeId=ua.NodeId(ua.Int32(type_id), ua.Int16(namespace)),
            BrowseName=ua.QualifiedName(target_name, 2),
        )
        for index, type_id, namespace, target_name in [
            (1001, 36, 0, "JobOrderResults"),
            (1002, 36, 0, "MyVar"),
            (1003, 5001, 2, "AckedState"),
        ]
    ]
    reads: list[str] = []

    async def read_names(nodes: list[Any], attr: AttributeIds, batch_size: int | None = None) -> list[ua.DataValue]:
        del batch_size
        assert attr == AttributeIds.BrowseName
        reads.extend(node.nodeid.to_string() for node in nodes)
        names = {"i=36": "HasEventSource", "ns=2;i=5001": "Monitors"}
        return [ua.DataValue(ua.Variant(ua.QualifiedName(names[node.nodeid.to_string()]))) for node in nodes]

    monkeypatch.setattr(client, "_read_attribute_batch_limited", read_names)
    cache: dict[str, str] = {}
    await client._read_reference_type_browse_names(refs, cache)
    converted = client._to_reference_infos(refs, cache)
    assert [ref.reference_browse_name for ref in converted] == ["HasEventSource", "HasEventSource", "Monitors"]
    assert [ref.target_node_id for ref in converted] == [ref.NodeId.to_string() for ref in refs]
    assert [ref.reference_type_id for ref in converted] == [ref.ReferenceTypeId.to_string() for ref in refs]
    assert reads == ["i=36", "ns=2;i=5001"]
    await client._read_reference_type_browse_names(refs, cache)
    assert reads == ["i=36", "ns=2;i=5001"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_name", [None, ua.QualifiedName(""), ua.QualifiedName(None)])
async def test_reference_type_name_failure_does_not_use_target_name(
    monkeypatch: pytest.MonkeyPatch, invalid_name: Any
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    refs = [
        ua.ReferenceDescription(
            NodeId=ua.ExpandedNodeId(ua.Int32(1001), ua.Int16(2)),
            ReferenceTypeId=ua.NodeId(ua.Int32(36)),
            BrowseName=ua.QualifiedName("JobOrderResults", 2),
        )
    ]

    async def read_names(nodes: list[Any], attr: AttributeIds, batch_size: int | None = None) -> list[ua.DataValue]:
        del nodes, attr, batch_size
        return [ua.DataValue(ua.Variant(invalid_name))]

    monkeypatch.setattr(client, "_read_attribute_batch_limited", read_names)
    with pytest.raises(ValueError, match="Cannot read BrowseName for reference type"):
        await client._read_reference_type_browse_names(refs, {})


@pytest.mark.asyncio
async def test_browse_tree_to_relationship_types_uses_type_names(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    client._limits_cache = OpcUaOperationalLimits(max_nodes_per_browse=1, max_nodes_per_read=1)
    reads: list[str] = []

    async def read_infos(entries: list[tuple[Any, str | None]]) -> list[OpcUaNodeInfo]:
        return [
            OpcUaNodeInfo(
                node_id=node.nodeid.to_string(),
                parent_node_id=parent_id,
                browse_name="MyVar" if parent_id else "Objects",
                display_name="MyVar" if parent_id else "Objects",
                node_class="Object",
                data_type=None,
            )
            for node, parent_id in entries
        ]

    async def browse_refs(
        nodes: list[Any], max_nodes_per_browse: int, reference_type_id: int = 0
    ) -> list[tuple[Any, list[ua.ReferenceDescription]]]:
        del max_nodes_per_browse, reference_type_id
        output: list[tuple[Any, list[ua.ReferenceDescription]]] = []
        for node in nodes:
            source_id = node.nodeid.to_string()
            target_id = {"i=85": 1001, "ns=2;i=1001": 1002}.get(source_id)
            refs = (
                [
                    ua.ReferenceDescription(
                        NodeId=ua.ExpandedNodeId(ua.Int32(target_id), ua.Int16(2)),
                        ReferenceTypeId=ua.NodeId(ua.Int32(36)),
                        BrowseName=ua.QualifiedName("MyVar", 2),
                    )
                ]
                if target_id is not None
                else []
            )
            output.append((node, refs))
        return output

    async def read_names(nodes: list[Any], attr: AttributeIds, batch_size: int | None = None) -> list[ua.DataValue]:
        del batch_size
        assert attr == AttributeIds.BrowseName
        reads.extend(node.nodeid.to_string() for node in nodes)
        return [ua.DataValue(ua.Variant(ua.QualifiedName("HasEventSource"))) for _ in nodes]

    async def namespaces() -> list[OpcUaNamespaceInfo]:
        return [
            OpcUaNamespaceInfo(uri="http://opcfoundation.org/UA/", display_name="OPC UA"),
            OpcUaNamespaceInfo(uri="urn:server", display_name="Server"),
            OpcUaNamespaceInfo(uri="urn:instances", display_name="Instances"),
        ]

    async def supertypes(reference_type_id: str) -> list[str]:
        assert reference_type_id == "i=36"
        return ["HasEventSource", "NonHierarchicalReferences"]

    monkeypatch.setattr(client, "_read_node_infos_limited", read_infos)
    monkeypatch.setattr(client, "_browse_references_descriptions", browse_refs)
    monkeypatch.setattr(client, "_browse_children_descriptions", browse_refs)
    monkeypatch.setattr(client, "_read_attribute_batch_limited", read_names)
    monkeypatch.setattr(client, "get_namespace_infos", namespaces)
    monkeypatch.setattr(client, "resolve_reference_type_supertype_browse_names", supertypes)
    model = await ModelBuilder(client).build()
    items = await ModelQueryService(client, model).get_relationship_types("http://opcfoundation.org/UA/")
    assert {item["elementId"] for item in items} == {"HasEventSource", "inverseOf_HasEventSource"}
    assert reads == ["i=36"]
    assert model.graph_relationship_names == {"HasEventSource"}
    assert sum("HasEventSource" in relationships for relationships in model.relationships_by_id.values()) == 2


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

    async def connect(
        self,
        auto_reconnect: bool,
        reconnect_max_delay: float,
        reconnect_request_timeout: float,
    ) -> None:
        del auto_reconnect, reconnect_max_delay, reconnect_request_timeout
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


def test_to_explicit_ua_variant_preserves_schema_approved_null() -> None:
    variant = _to_explicit_ua_variant(None, "Double")

    assert variant is not None
    assert variant.VariantType == ua.VariantType.Null
    assert variant.Value is None


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


def test_retry_detection_distinguishes_connection_loss_from_timeouts() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")

    assert client._should_retry_after_disconnect(_status_error(StatusCodes.BadSessionClosed)) is True
    assert client._should_retry_after_disconnect(_status_error(StatusCodes.BadNodeIdUnknown)) is False
    assert client._should_retry_after_disconnect(ConnectionError("client is disconnected")) is True
    assert client._should_retry_after_disconnect(TimeoutError()) is False
    assert client._should_retry_after_disconnect(asyncio.TimeoutError()) is False
    assert (
        client._should_retry_after_disconnect(ConnectionError("Timed out waiting for client to reconnect after 10s"))
        is False
    )


def _client_with_real_uaclient() -> tuple[OpcUaClient, UaClient]:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840", reconnect_request_timeout_seconds=0.2)
    uaclient = UaClient()
    cast(Any, client)._client = SimpleNamespace(uaclient=uaclient)
    return client, uaclient


def test_apply_client_state_maps_asyncua_states() -> None:
    client, uaclient = _client_with_real_uaclient()

    client._apply_client_state(UaClientState.CONNECTED)
    assert client.get_connection_snapshot().state == "Connected"

    client._apply_client_state(UaClientState.RECONNECTING)
    assert client.get_connection_snapshot().state == "Reconnecting"

    uaclient._disconnect_requested = True
    client._apply_client_state(UaClientState.DISCONNECTED)
    assert client.get_connection_snapshot().state == "Disconnected"


@pytest.mark.asyncio
async def test_wait_for_reconnect_returns_once_connected() -> None:
    client, uaclient = _client_with_real_uaclient()
    uaclient._set_state(UaClientState.RECONNECTING)

    waiter = asyncio.create_task(client._wait_for_reconnect(RuntimeError("connection is closed")))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    uaclient._set_state(UaClientState.CONNECTED)

    await asyncio.wait_for(waiter, timeout=1)


@pytest.mark.asyncio
async def test_wait_for_reconnect_raises_connection_error_on_timeout() -> None:
    client, uaclient = _client_with_real_uaclient()
    uaclient._set_state(UaClientState.RECONNECTING)

    with pytest.raises(ConnectionError, match="did not reconnect"):
        await client._wait_for_reconnect(RuntimeError("connection is closed"))


@pytest.mark.asyncio
async def test_wait_for_reconnect_reports_lost_session_to_asyncua(monkeypatch: pytest.MonkeyPatch) -> None:
    client, uaclient = _client_with_real_uaclient()
    uaclient._set_state(UaClientState.CONNECTED)
    notified: list[bool] = []
    monkeypatch.setattr(uaclient, "notify_transport_lost", lambda: notified.append(True))

    await client._wait_for_reconnect(_status_error(StatusCodes.BadSessionIdInvalid))
    await client._wait_for_reconnect(RuntimeError("connection is closed"))

    assert notified == [True]


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


def test_get_monitored_item_handles_maps_node_ids_to_current_server_handles() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")

    def _item(node_id: str, server_handle: int) -> SimpleNamespace:
        return SimpleNamespace(node=SimpleNamespace(nodeid=ua.NodeId.from_string(node_id)), server_handle=server_handle)

    subscription = SimpleNamespace(
        is_deleted=False,
        subscription_id=7,
        _monitored_items={1: _item("ns=2;s=a", 101), 2: _item("ns=2;i=5", 102)},
    )

    handles = client.get_monitored_item_handles(subscription, ["ns=2;s=a", "ns=2;i=5", "ns=2;s=gone"])
    assert handles == {"ns=2;s=a": 101, "ns=2;i=5": 102}

    subscription.subscription_id = None
    assert client.get_monitored_item_handles(subscription, ["ns=2;s=a"]) is None
    subscription.subscription_id = 7
    subscription.is_deleted = True
    assert client.get_monitored_item_handles(subscription, ["ns=2;s=a"]) is None


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
async def test_handle_reconnected_calls_listeners_even_if_one_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")

    called: list[str] = []

    async def ok_listener() -> None:
        called.append("ok")

    async def failing_listener() -> None:
        called.append("fail")
        raise RuntimeError("listener failed")

    client.add_reconnect_listener(failing_listener)
    client.add_reconnect_listener(ok_listener)

    typedef_loads = 0

    async def _count_typedef_load() -> None:
        nonlocal typedef_loads
        typedef_loads += 1

    monkeypatch.setattr(client, "load_additional_typedefinitions", _count_typedef_load)

    client._on_asyncua_reconnected(True)
    await client._handle_reconnected()
    assert called == ["fail", "ok"]
    assert typedef_loads == 0

    client._on_asyncua_reconnected(False)
    await client._handle_reconnected()
    assert typedef_loads == 1
    await client._handle_reconnected()
    assert typedef_loads == 1


@pytest.mark.asyncio
async def test_reconnect_handler_loop_runs_follow_up_work_outside_supervisor() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    handled = asyncio.Event()

    async def listener() -> None:
        handled.set()

    client.add_reconnect_listener(listener)
    task = asyncio.create_task(client._reconnect_handler_loop())
    try:
        client._on_asyncua_reconnected(True)
        await asyncio.wait_for(handled.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_supervised_client_reports_finished_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    asyncua_client = cast(Any, client)._client
    assert asyncua_client.reconnected_callback == client._on_asyncua_reconnected

    async def _base_finalize(self: Any, reused: bool) -> None:
        del self, reused

    monkeypatch.setattr(Client, "_finalize_reconnect", _base_finalize)
    client._set_connection_state("Reconnecting")

    asyncio.run(asyncua_client._finalize_reconnect(False))

    assert client.get_connection_snapshot().state == "Connected"
    assert client._reconnected_event.is_set()


def test_reconnect_keeps_browse_metadata_as_stale_and_clears_limits() -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840", metadata_cache_ttl_seconds=0)

    cast(Any, client)._limits_cache = object()
    cast(Any, client)._subscription_caps_cache = object()
    cast(Any, client)._namespace_infos_cache = (1.0, ["old-ns"])
    cast(Any, client)._object_types_cache = (1.0, ["old-type"])

    client._on_asyncua_reconnected(True)

    assert cast(Any, client)._limits_cache is None
    assert cast(Any, client)._subscription_caps_cache is None
    ns_cached_at, ns_value = cast(Any, client)._namespace_infos_cache
    types_cached_at, types_value = cast(Any, client)._object_types_cache
    assert ns_value == ["old-ns"]
    assert types_value == ["old-type"]
    # Even with TTL 0 (never expire) the reconnect must force a refresh.
    assert not client._is_metadata_cache_entry_fresh(ns_cached_at)
    assert not client._is_metadata_cache_entry_fresh(types_cached_at)


@pytest.mark.asyncio
async def test_metadata_invalidated_by_reconnect_is_served_stale_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._object_types_cache = client._invalidate_metadata_entry((time.perf_counter(), ["stale"]))
    release = asyncio.Event()
    calls = 0

    async def _slow_load() -> list[Any]:
        nonlocal calls
        calls += 1
        await release.wait()
        fresh: list[Any] = ["fresh"]
        cast(Any, client)._object_types_cache = (time.perf_counter(), fresh)
        return fresh

    monkeypatch.setattr(client, "_load_object_types", _slow_load)

    results = await asyncio.wait_for(asyncio.gather(*(client.get_object_types() for _ in range(10))), 1)

    assert all(cast(list[Any], result) == ["stale"] for result in results)
    assert calls == 1
    release.set()
    await cast(Any, client)._metadata_load_tasks["object_types"]
    assert cast(list[Any], await client.get_object_types()) == ["fresh"]


@pytest.mark.asyncio
async def test_operational_limits_and_subscription_caps_are_loaded_once_for_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    release = asyncio.Event()
    calls = {"limits": 0, "caps": 0}
    limits = OpcUaOperationalLimits(max_nodes_per_browse=10, max_nodes_per_read=20)
    caps = object()

    async def _slow_limits() -> OpcUaOperationalLimits:
        calls["limits"] += 1
        await release.wait()
        cast(Any, client)._limits_cache = limits
        return limits

    async def _slow_caps() -> Any:
        calls["caps"] += 1
        await release.wait()
        cast(Any, client)._subscription_caps_cache = caps
        return caps

    monkeypatch.setattr(client, "_load_operational_limits", _slow_limits)
    monkeypatch.setattr(client, "_load_subscription_capabilities", _slow_caps)

    limit_waiters = [asyncio.create_task(client.get_operational_limits()) for _ in range(20)]
    caps_waiters = [asyncio.create_task(client.get_subscription_capabilities()) for _ in range(20)]
    await asyncio.sleep(0)
    release.set()

    assert all(result is limits for result in await asyncio.gather(*limit_waiters))
    assert all(result is caps for result in await asyncio.gather(*caps_waiters))
    assert calls == {"limits": 1, "caps": 1}


@pytest.mark.asyncio
async def test_expired_metadata_cache_serves_stale_while_one_background_reload_runs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._object_types_cache = (-1e9, ["stale"])
    calls = 0
    release = asyncio.Event()

    async def _slow_load() -> list[Any]:
        nonlocal calls
        calls += 1
        await release.wait()
        fresh: list[Any] = ["fresh"]
        cast(Any, client)._object_types_cache = (time.perf_counter(), fresh)
        return fresh

    monkeypatch.setattr(client, "_load_object_types", _slow_load)

    with caplog.at_level("WARNING"):
        results = await asyncio.wait_for(asyncio.gather(*(client.get_object_types() for _ in range(20))), 1)

    stale_logs = [r for r in caplog.records if "serving stale metadata" in r.getMessage()]
    assert len(stale_logs) == 20
    assert all(r.levelname == "WARNING" and "key=object_types" in r.getMessage() for r in stale_logs)
    assert all(cast(list[Any], result) == ["stale"] for result in results)
    assert calls == 1
    release.set()
    await cast(Any, client)._metadata_load_tasks["object_types"]
    assert cast(list[Any], await client.get_object_types()) == ["fresh"]
    assert calls == 1
    assert cast(Any, client)._metadata_load_tasks == {}


@pytest.mark.asyncio
async def test_failed_background_metadata_reload_keeps_serving_stale_value(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._namespace_infos_cache = (-1e9, ["stale"])

    async def _failing_load() -> list[Any]:
        raise RuntimeError("browse failed")

    monkeypatch.setattr(client, "_load_namespace_infos", _failing_load)

    assert cast(list[Any], await client.get_namespace_infos()) == ["stale"]
    with pytest.raises(RuntimeError):
        await cast(Any, client)._metadata_load_tasks["namespace_infos"]
    assert cast(list[Any], await client.get_namespace_infos()) == ["stale"]


@pytest.mark.asyncio
async def test_cold_metadata_cache_is_loaded_once_for_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    calls = 0
    release = asyncio.Event()

    async def _slow_load() -> list[Any]:
        nonlocal calls
        calls += 1
        await release.wait()
        return ["fresh"]

    monkeypatch.setattr(client, "_load_object_types", _slow_load)

    waiters = [asyncio.create_task(client.get_object_types()) for _ in range(20)]
    await asyncio.sleep(0)
    assert not any(waiter.done() for waiter in waiters)
    release.set()
    results = await asyncio.gather(*waiters)

    assert calls == 1
    assert all(cast(list[Any], result) == ["fresh"] for result in results)
    assert cast(Any, client)._metadata_load_tasks == {}


@pytest.mark.asyncio
async def test_metadata_load_failure_is_shared_and_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    calls = 0

    async def _failing_load() -> list[Any]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        raise RuntimeError("browse failed")

    monkeypatch.setattr(client, "_load_namespace_infos", _failing_load)

    results = await asyncio.gather(*(client.get_namespace_infos() for _ in range(5)), return_exceptions=True)

    assert calls == 1
    assert all(isinstance(result, RuntimeError) for result in results)
    with pytest.raises(RuntimeError):
        await client.get_namespace_infos()
    assert calls == 2


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_shared_metadata_load(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    release = asyncio.Event()

    async def _slow_load() -> list[Any]:
        await release.wait()
        return ["fresh"]

    monkeypatch.setattr(client, "_load_object_types", _slow_load)

    cancelled = asyncio.create_task(client.get_object_types())
    survivor = asyncio.create_task(client.get_object_types())
    await asyncio.sleep(0)
    cancelled.cancel()
    release.set()

    assert cast(list[Any], await survivor) == ["fresh"]
    with pytest.raises(asyncio.CancelledError):
        await cancelled


@pytest.mark.asyncio
async def test_browse_retry_after_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    fake_uaclient = _FakeUaClient()
    fake_uaclient.fail_browse_once = True
    cast(Any, client)._client = SimpleNamespace(uaclient=fake_uaclient)

    reconnect_calls = 0

    async def _fake_wait_for_reconnect(exc: Exception) -> None:
        del exc
        nonlocal reconnect_calls
        reconnect_calls += 1

    monkeypatch.setattr(client, "_wait_for_reconnect", _fake_wait_for_reconnect)

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

    async def _fake_wait_for_reconnect(exc: Exception) -> None:
        del exc
        nonlocal reconnect_calls
        reconnect_calls += 1

    monkeypatch.setattr(client, "_wait_for_reconnect", _fake_wait_for_reconnect)

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
                "i=2254": _Node("i=2254", value=["urn:test:server"]),
                "i=11715": _Node("i=11715"),
                "component-ok": _Node("component-ok", display_name="Component Ok"),
                "component-fail": _Node("component-fail", display_name="Component Fail"),
                "uri-ok": _Node("uri-ok", value="urn:ok"),
                "uri-fail": _Node("uri-fail", value_error=TimeoutError("metadata timeout")),
                "version-ok": _Node("version-ok", value="2.0"),
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
                [
                    SimpleNamespace(BrowseName=SimpleNamespace(Name="NamespaceUri"), NodeId="uri-ok"),
                    SimpleNamespace(BrowseName=SimpleNamespace(Name="NamespaceVersion"), NodeId="version-ok"),
                ],
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
        OpcUaNamespaceInfo(
            uri="urn:ok",
            display_name="Component Ok",
            namespace_version="2.0",
            server_uri="urn:test:server",
        ),
        OpcUaNamespaceInfo(uri="urn:fail", display_name="", server_uri="urn:test:server"),
    ]


class _RefTypeNode:
    def __init__(self, node_id: str, browse_name: str) -> None:
        self.nodeid = SimpleNamespace(to_string=lambda: node_id)
        self._browse_name = browse_name

    async def read_browse_name(self) -> SimpleNamespace:
        return SimpleNamespace(Name=self._browse_name)


class _RefTypeClient:
    """Reference type tree where HasComponent and HasProperty share ancestors."""

    SUPERTYPES = {
        "HasComponent": "Aggregates",
        "HasProperty": "Aggregates",
        "Aggregates": "HasChild",
        "HasChild": "HierarchicalReferences",
        "HierarchicalReferences": "References",
        "References": None,
    }

    def __init__(self) -> None:
        self.browsed: list[str] = []

    def get_node(self, node_id: Any) -> _RefTypeNode:
        key = node_id.to_string() if hasattr(node_id, "to_string") else str(node_id)
        return _RefTypeNode(key, key)


@pytest.mark.asyncio
async def test_resolve_reference_type_supertypes_caches_every_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _RefTypeClient()
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = fake_client

    async def _browse_references_descriptions(
        nodes: list[Any],
        max_nodes_per_browse: int,
        reference_type_id: int,
        browse_direction: Any = None,
        include_subtypes: bool = True,
    ) -> list[tuple[Any, list[Any]]]:
        del max_nodes_per_browse, reference_type_id, browse_direction, include_subtypes
        node = nodes[0]
        node_id = node.nodeid.to_string()
        fake_client.browsed.append(node_id)
        supertype = _RefTypeClient.SUPERTYPES[node_id]
        if supertype is None:
            return [(node, [])]
        return [(node, [SimpleNamespace(NodeId=SimpleNamespace(to_string=lambda s=supertype: s))])]

    monkeypatch.setattr(client, "_browse_references_descriptions", _browse_references_descriptions)

    assert await client.resolve_reference_type_supertype_browse_names("HasComponent") == [
        "HasComponent",
        "Aggregates",
        "HasChild",
        "HierarchicalReferences",
        "References",
    ]
    assert fake_client.browsed == [
        "HasComponent",
        "Aggregates",
        "HasChild",
        "HierarchicalReferences",
        "References",
    ]

    # The shared ancestors are already cached, so only HasProperty itself is browsed.
    fake_client.browsed.clear()
    assert await client.resolve_reference_type_supertype_browse_names("HasProperty") == [
        "HasProperty",
        "Aggregates",
        "HasChild",
        "HierarchicalReferences",
        "References",
    ]
    assert fake_client.browsed == ["HasProperty"]

    fake_client.browsed.clear()
    assert await client.resolve_reference_type_supertype_browse_names("HasChild") == [
        "HasChild",
        "HierarchicalReferences",
        "References",
    ]
    assert fake_client.browsed == []


class _BatchReadNode:
    def __init__(self, node_id: str) -> None:
        self.nodeid = node_id

    async def read_data_value(self) -> ua.DataValue:
        return ua.DataValue(ua.Variant(f"single:{self.nodeid}"))


class _BatchReadClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.batches: list[list[str]] = []

    def get_node(self, node_id: Any) -> _BatchReadNode:
        return _BatchReadNode(str(node_id))

    async def read_attributes(self, nodes: list[_BatchReadNode], attr: Any = None) -> list[ua.DataValue]:
        assert attr == AttributeIds.Value
        self.batches.append([node.nodeid for node in nodes])
        if self.error is not None:
            raise self.error
        return [ua.DataValue(ua.Variant(f"batch:{node.nodeid}")) for node in nodes]


def _variant_values(values: list[ua.DataValue]) -> list[Any]:
    return [value.Value.Value if value.Value is not None else None for value in values]


def _batch_read_client(monkeypatch: pytest.MonkeyPatch, fake: _BatchReadClient) -> OpcUaClient:
    client = OpcUaClient(endpoint="opc.tcp://localhost:4840")
    cast(Any, client)._client = fake

    async def _limits() -> OpcUaOperationalLimits:
        return OpcUaOperationalLimits(max_nodes_per_browse=None, max_nodes_per_read=3)

    monkeypatch.setattr(client, "get_operational_limits", _limits)
    return client


@pytest.mark.asyncio
async def test_read_data_values_uses_one_read_call_per_max_nodes_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _BatchReadClient()
    client = _batch_read_client(monkeypatch, fake)
    node_ids = [f"ns=2;i={idx}" for idx in range(7)]

    values = await client.read_data_values(node_ids)

    assert fake.batches == [node_ids[0:3], node_ids[3:6], node_ids[6:7]]
    assert _variant_values(values) == [f"batch:{node_id}" for node_id in node_ids]


@pytest.mark.asyncio
async def test_read_data_values_falls_back_to_per_node_reads_when_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _BatchReadClient(error=RuntimeError("batched read failed"))
    client = _batch_read_client(monkeypatch, fake)
    node_ids = ["ns=2;i=1", "ns=2;i=2"]

    values = await client.read_data_values(node_ids)

    assert fake.batches == [node_ids]
    assert _variant_values(values) == [f"single:{node_id}" for node_id in node_ids]
