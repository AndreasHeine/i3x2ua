from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Protocol

from asyncua import Client
from asyncua.ua import NodeClass

logger = logging.getLogger(__name__)


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
    def __init__(self, endpoint: str, browse_concurrency: int = 16) -> None:
        self._endpoint = endpoint
        self._browse_concurrency = max(1, browse_concurrency)
        self._client = Client(url=endpoint)

    async def connect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA connect started endpoint=%s", self._endpoint)
        await self._client.connect()
        logger.info("OPC UA connect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

    async def disconnect(self) -> None:
        started = perf_counter()
        logger.info("OPC UA disconnect started endpoint=%s", self._endpoint)
        await self._client.disconnect()
        logger.info("OPC UA disconnect finished endpoint=%s duration_s=%.3f", self._endpoint, perf_counter() - started)

    async def browse_tree(self) -> list[OpcUaNodeInfo]:
        started = perf_counter()
        logger.info(
            "OPC UA browse started endpoint=%s browse_concurrency=%d",
            self._endpoint,
            self._browse_concurrency,
        )
        root = self._client.nodes.objects
        output: list[OpcUaNodeInfo] = []
        visited: set[str] = set()
        queue: asyncio.Queue[tuple[Any, str | None]] = asyncio.Queue()
        visited_lock = asyncio.Lock()
        output_lock = asyncio.Lock()

        await queue.put((root, None))

        async def worker() -> None:
            while True:
                node, parent_node_id = await queue.get()
                try:
                    node_id_obj = node.nodeid
                    node_id = node_id_obj.to_string()

                    async with visited_lock:
                        if node_id in visited:
                            continue
                        visited.add(node_id)

                    browse_name_obj, display_name_obj, node_class_obj = await asyncio.gather(
                        node.read_browse_name(),
                        node.read_display_name(),
                        node.read_node_class(),
                    )

                    data_type: str | None = None
                    if node_class_obj == NodeClass.Variable:
                        data_type_obj = await node.read_data_type()
                        data_type = data_type_obj.to_string()

                    event_notifier = (
                        bool(await node.read_event_notifier())
                        if node_class_obj == NodeClass.Object
                        else False
                    )

                    mapped = OpcUaNodeInfo(
                        node_id=node_id,
                        parent_node_id=parent_node_id,
                        browse_name=browse_name_obj.Name,
                        display_name=display_name_obj.Text,
                        node_class=node_class_obj.name,
                        data_type=data_type,
                        event_notifier=event_notifier,
                    )

                    async with output_lock:
                        output.append(mapped)

                    children = await node.get_children()
                    for child in children:
                        queue.put_nowait((child, node_id))
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self._browse_concurrency)]
        await queue.join()
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        logger.info(
            "OPC UA browse finished endpoint=%s node_count=%d duration_s=%.3f",
            self._endpoint,
            len(output),
            perf_counter() - started,
        )
        return output

    async def read_value(self, node_id: str) -> Any:
        started = perf_counter()
        node = self._client.get_node(node_id)
        try:
            value = await node.read_value()
            logger.debug("OPC UA read ok node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            return value
        except Exception:
            logger.exception("OPC UA read failed node_id=%s duration_s=%.3f", node_id, perf_counter() - started)
            raise

    async def call_method(self, object_node_id: str, method_node_id: str, args: list[Any]) -> Any:
        started = perf_counter()
        object_node = self._client.get_node(object_node_id)
        try:
            result = await object_node.call_method(method_node_id, *args)
            logger.info(
                "OPC UA method ok object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            return result
        except Exception:
            logger.exception(
                "OPC UA method failed object_node_id=%s method_node_id=%s arg_count=%d duration_s=%.3f",
                object_node_id,
                method_node_id,
                len(args),
                perf_counter() - started,
            )
            raise
