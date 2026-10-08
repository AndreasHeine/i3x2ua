from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua.attribute_ids import AttributeIds
from asyncua.ua.status_codes import StatusCodes
from asyncua.ua.uaerrors import BadHistoryOperationUnsupported, BadNotWritable, BadUserAccessDenied
from fastapi import FastAPI

from i3x_server.api.v1.object_value_routes import router
from i3x_server.infrastructure.opcua.client import OpcUaClient
from i3x_server.infrastructure.opcua.history import historical_data_value
from i3x_server.schemas.i3x import ModelNode
from i3x_server.schemas.state import BuildResult


def _fixture_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "conf-test-server" / "server.py"
    spec = importlib.util.spec_from_file_location("conformance_fixture_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_fixture_history_upserts_over_opcua(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _fixture_module()
    endpoint = "opc.tcp://127.0.0.1:0/freeopcua/server/"
    fixture = module.ConformanceFixtureServer(endpoint, "urn:i3x:fixture-test", 1.0, 0, 15, 1)
    server = await fixture._build_server()
    signal = fixture._signals[0].node
    timestamp = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with server:
        assert server.bserver is not None
        endpoint = f"opc.tcp://127.0.0.1:{server.bserver.port}/freeopcua/server/"
        async with Client(endpoint) as wire_client:
            adapter = OpcUaClient(endpoint)
            monkeypatch.setattr(adapter, "_client", wire_client)
            node_id = signal.nodeid.to_string()
            current = await signal.read_value()
            write_timestamp = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
            await adapter.write_value(node_id, 26.5, "Double", "Uncertain", write_timestamp)
            written_value = await signal.read_data_value(raise_on_bad_status=False)
            assert written_value.Value is not None and written_value.Value.Value == 26.5
            assert written_value.StatusCode is not None
            assert written_value.StatusCode.value == StatusCodes.Uncertain
            assert written_value.SourceTimestamp == write_timestamp
            current = 26.5
            assert await adapter.read_history_write_access(node_id) == (True, True)
            await adapter.write_history_value(node_id, 19.5, "Good", timestamp)
            remote = wire_client.get_node(node_id)
            records = await remote.read_raw_history(timestamp, timestamp, 0, False)
            assert len(records) == 1
            assert records[0].Value is not None and records[0].Value.Value == 19.5
            await adapter.write_history_value(node_id, 21.0, "Uncertain", timestamp)
            records = await remote.read_raw_history(timestamp, timestamp, 0, False)
            assert len(records) == 1
            assert records[0].Value is not None and records[0].Value.Value == 21.0
            assert records[0].StatusCode is not None
            assert records[0].StatusCode.value == StatusCodes.Uncertain
            assert records[0].SourceTimestamp == timestamp
            current_data_value = await signal.read_data_value(raise_on_bad_status=False)
            assert current_data_value.Value is not None and current_data_value.Value.Value == current
            assert adapter.snapshot_request_metrics().history_write_count == 2
            details = ua.UpdateDataDetails(
                NodeId=remote.nodeid,
                PerformInsertReplace=ua.PerformUpdateType.Update,
                UpdateValues=[
                    historical_data_value("wrong type", "Good", timestamp, ua.VariantType.String, -1, None),
                    historical_data_value(
                        23.0, "Good", timestamp + timedelta(seconds=1), ua.VariantType.Double, -1, None
                    ),
                ],
            )
            result = await remote.history_update(details)
            assert [status.value for status in result.OperationResults] == [
                StatusCodes.BadTypeMismatch,
                StatusCodes.Good,
            ]
            records = await remote.read_raw_history(timestamp, timestamp + timedelta(seconds=2), 0, False)
            assert len(records) == 2
            current_data_value = await signal.read_data_value(raise_on_bad_status=False)
            assert current_data_value.Value is not None and current_data_value.Value.Value == current

            monkeypatch.setenv("I3X_ENABLE_WRITES", "1")
            app = FastAPI()
            app.include_router(router)
            app.state.opcua_client = adapter
            app.state.model_cache = BuildResult(
                nodes_by_id={
                    "history-signal": ModelNode(
                        id="history-signal",
                        name="ConformanceHistorySignal",
                        kind="property",
                        type="nsu=http://opcfoundation.org/UA/;i=11",
                        children=[],
                        source_node_id=node_id,
                        source_type_id=None,
                    )
                },
                root_ids=["history-signal"],
                children_by_id={},
                instances_by_type_id={},
                property_to_node={"history-signal": node_id},
                action_to_method={},
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as rest:
                response = await rest.put(
                    "/v1/objects/history",
                    json={
                        "updates": [
                            {
                                "elementId": "history-signal",
                                "value": {
                                    "value": 25.0,
                                    "quality": "Good",
                                    "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                                },
                            }
                        ]
                    },
                )
                assert response.status_code == 200
                assert response.json()["success"] is True, response.json()
                assert response.json()["results"][0]["result"] is None
                response = await rest.post(
                    "/v1/objects/history",
                    json={
                        "elementIds": ["history-signal"],
                        "startTime": timestamp.isoformat(),
                        "endTime": (timestamp + timedelta(seconds=2)).isoformat(),
                    },
                )
                assert response.status_code == 200
                assert response.json()["success"] is True, response.json()
                values = response.json()["results"][0]["result"]["values"]
                assert len(values) == 2
                assert values[0]["value"] == 25.0
                current_data_value = await signal.read_data_value(raise_on_bad_status=False)
                assert current_data_value.Value is not None and current_data_value.Value.Value == current

            details.PerformInsertReplace = ua.PerformUpdateType.Insert
            with pytest.raises(BadHistoryOperationUnsupported):
                await remote.history_update(details)
            details.PerformInsertReplace = ua.PerformUpdateType.Update
            details.NodeId = server.nodes.objects.nodeid
            with pytest.raises(BadNotWritable):
                await remote.history_update(details)
            details.NodeId = remote.nodeid
            await signal.write_attribute(
                AttributeIds.UserAccessLevel, ua.DataValue(Value=ua.Variant(5, ua.VariantType.Byte))
            )
            with pytest.raises(BadUserAccessDenied):
                await remote.history_update(details)
            await fixture._set_historizing_flags(signal)
            await adapter.write_history_value(node_id, None, "Bad", timestamp)
            records = await remote.read_raw_history(timestamp, timestamp, 0, False)
            assert len(records) == 1
            assert records[0].Value is not None and records[0].Value.Value is None
            assert records[0].StatusCode is not None and records[0].StatusCode.is_bad()
            current_data_value = await signal.read_data_value(raise_on_bad_status=False)
            assert current_data_value.Value is not None and current_data_value.Value.Value == current
