"""Real asyncua-server contract tests for browse/handle reconciliation (L15).

asyncua's server does not implement BrowseNext continuation points
(``internal_session.py`` raises ``NotImplementedError``), so a vanilla server never
drives the adapter's paging, stale-point reconciliation, or retry bookkeeping. The
fixture extends the server session with spec-shaped continuation behavior - the same
asyncua-internal extension precedent as ``conf-test-server`` - and the tests pin how
the adapter reconciles continuation points, including across a real reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asyncua import ua
from asyncua.client.ua_client import UaClientState
from asyncua.crypto.permission_rules import User, UserRole
from asyncua.server.binary_server_asyncio import BinaryServer, OPCUAProtocol
from asyncua.server.internal_server import InternalServer
from asyncua.server.internal_session import InternalSession
from asyncua.server.server import Server
from asyncua.server.uaprocessor import UaProcessor
from asyncua.ua.object_ids import ObjectIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.ua_binary import struct_from_binary
from asyncua.ua.uaerrors import (
    BadContinuationPointInvalid,
    BadSessionNotActivated,
    BadTooManyOperations,
    BadUserAccessDenied,
    UaStatusCodeError,
)

from i3x_server.infrastructure.opcua.browse import browse_references_descriptions
from i3x_server.infrastructure.opcua.client import OpcUaClient

_CHUNK_SIZE = 2
_CHILD_NAMES = ["Alpha", "Beta", "Gamma", "Delta"]


class _ContinuationSession(InternalSession):
    """Session that pages browse results and tracks continuation points per session."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._continuations: dict[bytes, list[ua.ReferenceDescription]] = {}
        self._next_point = 0
        self.fail_browse_once = False
        self.fail_next_once = False
        self.drop_next_result = False

    async def browse(self, params: ua.BrowseParameters) -> list[ua.BrowseResult]:
        if self.fail_browse_once:
            self.fail_browse_once = False
            raise BadTooManyOperations
        results = await super().browse(params)
        for result in results:
            if len(result.References) > _CHUNK_SIZE:
                point = self._new_point()
                self._continuations[point] = result.References[_CHUNK_SIZE:]
                result.References = result.References[:_CHUNK_SIZE]
                result.ContinuationPoint = point
        return results

    async def browse_next(self, parameters: ua.BrowseNextParameters) -> list[ua.BrowseResult]:
        if self.fail_next_once:
            self.fail_next_once = False
            raise BadContinuationPointInvalid
        results: list[ua.BrowseResult] = []
        for point in parameters.ContinuationPoints:
            pending = self._continuations.pop(point, None)
            if pending is None:
                # Points held by a recreated session report the spec status.
                raise BadContinuationPointInvalid
            if self.drop_next_result:
                self.drop_next_result = False
                continue
            result = ua.BrowseResult()
            result.References = pending[:_CHUNK_SIZE]
            if len(pending) > _CHUNK_SIZE:
                fresh = self._new_point()
                self._continuations[fresh] = pending[_CHUNK_SIZE:]
                result.ContinuationPoint = fresh
            results.append(result)
        return results

    def _new_point(self) -> bytes:
        self._next_point += 1
        return f"{self.session_id}-{self._next_point}".encode("ascii")


class _ContinuationInternalServer(InternalServer):
    """Internal server that hands out continuation-capable sessions."""

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[_ContinuationSession] = []

    def create_session(self, name: str, user: User | None = None, external: bool = False) -> InternalSession:
        session = _ContinuationSession(
            self,
            self.aspace,
            self.subscription_service,
            name,
            user=user if user is not None else User(role=UserRole.Anonymous),
            external=external,
        )
        self.sessions.append(session)
        return session


class _BrowseNextProcessor(UaProcessor):
    """Dispatch BrowseNext to the session; asyncua 2.1's processor never does.

    Unknown service typeids are rejected by the permission check before the dispatch
    chain, so the request is intercepted in ``_process_message`` exactly like the
    ``conf-test-server`` fixture's HistoryUpdate extension.
    """

    async def _process_message(self, typeid: ua.NodeId, requesthdr: Any, seqhdr: Any, body: Any) -> bool:
        if typeid != ua.NodeId(ua.Int32(ObjectIds.BrowseNextRequest_Encoding_DefaultBinary)):
            return bool(await super()._process_message(typeid, requesthdr, seqhdr, body))  # type: ignore[no-untyped-call]
        if self.session is None or self.session.user is None:
            raise BadUserAccessDenied
        if not self.session.is_activated():
            raise BadSessionNotActivated
        self.session_last_activity = time.monotonic()
        self.session.touch()
        params = struct_from_binary(ua.BrowseNextParameters, body)
        response = ua.BrowseNextResponse()
        response.Parameters.Results = await self.session.browse_next(params)
        self.send_response(requesthdr.RequestHandle, seqhdr, response)  # type: ignore[no-untyped-call]
        return True


class _BrowseNextProtocol(OPCUAProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        if self.processor is not None and self.transport is not None:
            # Replace the processor before the scheduled message loop can run.
            self.processor = _BrowseNextProcessor(self.iserver, self.transport, self.limits)
            self.processor.set_policies(self.policies)  # type: ignore[no-untyped-call]


class _BrowseNextBinaryServer(BinaryServer):
    def _make_protocol(self) -> OPCUAProtocol:
        return _BrowseNextProtocol(self.iserver, self._policies, self.clients, self.closing_tasks, self.limits)


class _ContinuationServer(Server):
    """Server whose listener dispatches BrowseNext to continuation-capable sessions."""

    async def start(self) -> None:
        await self._setup_server_nodes()
        await self.iserver.start()
        try:
            host, port = self._get_bind_socket_info()
            if host is None or port is None:
                raise ValueError("Browse contract endpoint requires a host and port")
            self.bserver = _BrowseNextBinaryServer(self.iserver, host, port, self.limits)
            self.bserver.set_policies(self._policies)
            await self.bserver.start()
        except Exception:
            await self.iserver.stop()
            raise


@contextlib.asynccontextmanager
async def _continuation_server(unused_tcp_port: int) -> AsyncIterator[tuple[_ContinuationInternalServer, str]]:
    iserver = _ContinuationInternalServer()
    server = _ContinuationServer(iserver=iserver)
    await server.init()
    endpoint = f"opc.tcp://127.0.0.1:{unused_tcp_port}/freeopcua/server/"
    server.set_endpoint(endpoint)
    ns = ua.Int16(await server.register_namespace("urn:browse-contract"))
    machine = await server.nodes.objects.add_object(ua.NodeId(ua.String("Machine"), ns), "Machine")
    for name in _CHILD_NAMES:
        await machine.add_variable(ua.NodeId(ua.String(name), ns), name, 1.0)
    async with server:
        yield iserver, endpoint


@contextlib.asynccontextmanager
async def _client(endpoint: str) -> AsyncIterator[OpcUaClient]:
    client = OpcUaClient(
        endpoint,
        connection_monitor_interval_seconds=1,
        reconnect_request_timeout_seconds=2,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def _browse_params(node: Any, max_references: int) -> ua.BrowseParameters:
    description = ua.BrowseDescription()
    description.NodeId = node.nodeid
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.IncludeSubtypes = True
    params = ua.BrowseParameters()
    params.NodesToBrowse = [description]
    params.RequestedMaxReferencesPerNode = max_references
    return params


async def test_browse_pages_all_references_with_continuation(unused_tcp_port: int) -> None:
    async with _continuation_server(unused_tcp_port) as (_iserver, endpoint):
        async with _client(endpoint) as client:
            node = client.raw_node("ns=2;s=Machine")
            (_node, refs) = (await browse_references_descriptions(client, [node], 10, ObjectIds.HasComponent))[0]
            assert sorted(ref.BrowseName.Name for ref in refs) == sorted(_CHILD_NAMES)
            assert client.runtime_metrics.browse_next_calls == 1
            assert client.runtime_metrics.browse_next_references == 2


async def test_stale_continuation_point_is_re_browsed_and_deduplicated(unused_tcp_port: int) -> None:
    async with _continuation_server(unused_tcp_port) as (iserver, endpoint):
        async with _client(endpoint) as client:
            iserver.sessions[-1].fail_next_once = True
            node = client.raw_node("ns=2;s=Machine")
            (_node, refs) = (await browse_references_descriptions(client, [node], 10, ObjectIds.HasComponent))[0]

            # The invalid BrowseNext is reconciled by re-browsing the affected nodes;
            # fresh references are merged without duplicates.
            assert sorted(ref.BrowseName.Name for ref in refs) == sorted(_CHILD_NAMES)
            assert len(refs) == len({(ref.NodeId.to_string(), ref.BrowseName.to_string()) for ref in refs})
            assert client.snapshot_request_metrics().failed_request_count == 1


async def test_continuation_points_stale_after_reconnect_record_retry_failure(unused_tcp_port: int) -> None:
    async with _continuation_server(unused_tcp_port) as (_iserver, endpoint):
        async with _client(endpoint) as client:
            node = client.raw_node("ns=2;s=Machine")
            results = await client.browse_raw(_browse_params(node, _CHUNK_SIZE))
            point = results[0].ContinuationPoint
            assert point is not None
            before = client.snapshot_request_metrics().failed_request_count

            transport = client.raw_client.uaclient
            protocol = transport.protocol
            assert protocol is not None
            assert protocol.transport is not None
            async with transport.subscribe_state() as states:
                protocol.transport.abort()
                await states.wait_for_state(UaClientState.RECONNECTING, timeout=5)
                next_params = ua.BrowseNextParameters()
                next_params.ContinuationPoints = [point]
                # The first attempt fails while reconnecting; the retry lands on the
                # recreated session where the point no longer exists.
                with pytest.raises(BadContinuationPointInvalid):
                    retry = client.browse_next_with_retry(next_params, [node.nodeid.to_string()])
                    await asyncio.wait_for(retry, timeout=5)
                await states.wait_for_state(UaClientState.CONNECTED, timeout=5)

            # retry_call records the failed retry (recovery.py); the retry then lands on
            # the recreated session where the point no longer exists and the status-code
            # exception propagates unchanged.
            assert client.snapshot_request_metrics().failed_request_count == before + 1

            # Re-browsing on the live session succeeds without the stale point.
            (_node, refs) = (await browse_references_descriptions(client, [node], 10, ObjectIds.HasComponent))[0]
            assert sorted(ref.BrowseName.Name for ref in refs) == sorted(_CHILD_NAMES)


async def test_browse_next_result_count_mismatch_is_fatal(unused_tcp_port: int) -> None:
    async with _continuation_server(unused_tcp_port) as (iserver, endpoint):
        async with _client(endpoint) as client:
            iserver.sessions[-1].drop_next_result = True
            node = client.raw_node("ns=2;s=Machine")
            with pytest.raises(RuntimeError, match="BrowseNext returned 0 results for 1 points"):
                await browse_references_descriptions(client, [node], 10, ObjectIds.HasComponent)


async def test_browse_service_failure_is_recorded_and_propagated(unused_tcp_port: int) -> None:
    async with _continuation_server(unused_tcp_port) as (iserver, endpoint):
        async with _client(endpoint) as client:
            iserver.sessions[-1].fail_browse_once = True
            node = client.raw_node("ns=2;s=Machine")
            with pytest.raises(BadTooManyOperations):
                await browse_references_descriptions(client, [node], 10, ObjectIds.HasComponent)
            assert client.snapshot_request_metrics().failed_request_count == 1


def _status_error(status_code: int) -> UaStatusCodeError:
    """Build the exception class asyncua's client raises for a service fault."""
    try:
        ua.StatusCode(ua.UInt32(status_code)).check()
    except UaStatusCodeError as exc:
        return exc
    raise AssertionError("status code check did not raise")


def test_continuation_point_invalid_classification() -> None:
    client = OpcUaClient("opc.tcp://127.0.0.1:0")
    assert client.is_continuation_point_invalid(_status_error(StatusCodes.BadContinuationPointInvalid))
    assert client.is_continuation_point_invalid(RuntimeError("BadContinuationPointInvalid"))
    assert not client.is_continuation_point_invalid(_status_error(StatusCodes.BadUserAccessDenied))
