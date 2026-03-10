from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from asyncua import Client
from asyncua.ua import NodeClass


@dataclass(slots=True)
class OpcUaNodeInfo:
    node_id: str
    parent_node_id: str | None
    browse_name: str
    display_name: str
    node_class: str
    data_type: str | None
    event_notifier: bool


class OpcUaClientProtocol(Protocol):
    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        ...

    async def read_value(self, node_id: str) -> Any:
        ...

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        ...


class OpcUaClient:
    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._client = Client(url=endpoint)

    async def connect(self) -> None:
        await self._client.connect()

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        root = self._client.nodes.objects
        output: list[OpcUaNodeInfo] = []
        visited: set[str] = set()

        async def walk(node: Any, parent_node_id: str | None) -> None:
            node_id_obj = node.nodeid
            node_id = node_id_obj.to_string()
            if node_id in visited:
                return
            visited.add(node_id)

            browse_name_obj = await node.read_browse_name()
            display_name_obj = await node.read_display_name()
            node_class_obj = await node.read_node_class()

            data_type: str | None = None
            if node_class_obj == NodeClass.Variable:
                data_type_obj = await node.read_data_type()
                data_type = data_type_obj.to_string()

            event_notifier = bool(await node.read_event_notifier()) if node_class_obj == NodeClass.Object else False

            output.append(
                OpcUaNodeInfo(
                    node_id=node_id,
                    parent_node_id=parent_node_id,
                    browse_name=browse_name_obj.Name,
                    display_name=display_name_obj.Text,
                    node_class=node_class_obj.name,
                    data_type=data_type,
                    event_notifier=event_notifier,
                )
            )

            children = await node.get_children()
            for child in children:
                await walk(child, node_id)

        await walk(root, None)
        return output

    async def read_value(self, node_id: str) -> Any:
        node = self._client.get_node(node_id)
        return await node.read_value()

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        object_node = self._client.get_node(object_node_id)
        return await object_node.call_method(method_node_id, *args)
