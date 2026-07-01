from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import math
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from asyncua import Server, ua  # type: ignore[attr-defined]

ATTRIBUTE_IDS = cast(Any, ua).AttributeIds

logger = logging.getLogger("i3x.conformance.server")


@dataclass(slots=True)
class SignalNode:
    name: str
    node: Any
    base: float
    amplitude: float
    period_seconds: float
    phase: float
    variant_type: ua.VariantType


class ConformanceFixtureServer:
    """Small asyncua server fixture for i3x conformance checks.

    This server intentionally exposes objects that produce:
    - deterministic live value changes for subscription checks
    - seeded historical points for history-read checks
    """

    def __init__(
        self,
        endpoint: str,
        namespace_uri: str,
        update_interval_seconds: float,
        history_seed_minutes: int,
        history_sample_seconds: int,
        deep_levels: int,
    ) -> None:
        self._endpoint = endpoint
        self._namespace_uri = namespace_uri
        self._update_interval_seconds = update_interval_seconds
        self._history_seed_minutes = history_seed_minutes
        self._history_sample_seconds = history_sample_seconds
        self._deep_levels = deep_levels
        self._stop_event = asyncio.Event()
        self._signals: list[SignalNode] = []
        self._server: Server | None = None

    async def run(self) -> None:
        self._server = await self._build_server()
        self._install_signal_handlers()

        logger.info("Starting OPC UA conformance fixture endpoint=%s", self._endpoint)
        async with self._server:
            await self._verify_seeded_history()
            logger.info(
                "Fixture ready: live updates every %.2fs, seeded history=%d minutes",
                self._update_interval_seconds,
                self._history_seed_minutes,
            )
            logger.info("Press Ctrl+C to stop")
            updater_task = asyncio.create_task(self._update_loop(), name="conformance-fixture-updater")
            try:
                await self._stop_event.wait()
            finally:
                updater_task.cancel()
                await asyncio.gather(updater_task, return_exceptions=True)

    async def _build_server(self) -> Server:
        server = Server()
        await server.init()
        server.set_endpoint(self._endpoint)
        server.set_server_name("i3x2ua Conformance Fixture")

        idx = await server.register_namespace(self._namespace_uri)
        plant = await server.nodes.objects.add_object(idx, "ConformancePlant")

        line_a = await plant.add_object(idx, "LineA")
        line_b = await plant.add_object(idx, "LineB")

        self._signals.extend(
            await self._create_machine(
                idx=idx,
                parent=line_a,
                machine_name="Mixer-01",
                temperature_base=56.0,
                pressure_base=2.4,
                speed_base=920.0,
            )
        )
        self._signals.extend(
            await self._create_machine(
                idx=idx,
                parent=line_b,
                machine_name="Heater-01",
                temperature_base=84.0,
                pressure_base=1.9,
                speed_base=760.0,
            )
        )

        await self._create_deep_nested_structure(idx=idx, parent=plant)

        await self._configure_history(server)
        if self._history_seed_minutes > 0:
            await self._seed_history()

        return server

    async def _create_machine(
        self,
        idx: int,
        parent: Any,
        machine_name: str,
        temperature_base: float,
        pressure_base: float,
        speed_base: float,
    ) -> list[SignalNode]:
        machine = await parent.add_object(idx, machine_name)

        temperature = await machine.add_variable(idx, "Temperature", temperature_base)
        pressure = await machine.add_variable(idx, "Pressure", pressure_base)
        speed = await machine.add_variable(idx, "Speed", speed_base)
        run_state = await machine.add_variable(idx, "IsRunning", True)

        await temperature.set_writable()
        await pressure.set_writable()
        await speed.set_writable()
        await run_state.set_writable()

        await self._set_historizing_flags(temperature)
        await self._set_historizing_flags(pressure)
        await self._set_historizing_flags(speed)
        await self._set_historizing_flags(run_state)

        return [
            SignalNode("Temperature", temperature, temperature_base, 3.4, 40.0, 0.2, ua.VariantType.Double),
            SignalNode("Pressure", pressure, pressure_base, 0.18, 31.0, 1.1, ua.VariantType.Double),
            SignalNode("Speed", speed, speed_base, 55.0, 47.0, 2.2, ua.VariantType.Double),
            SignalNode("IsRunning", run_state, 1.0, 1.0, 18.0, 0.0, ua.VariantType.Boolean),
        ]

    async def _create_deep_nested_structure(self, idx: int, parent: Any) -> None:
        if self._deep_levels <= 0:
            return

        branch = await parent.add_object(idx, "DeepNested")
        current = branch
        for level in range(1, self._deep_levels + 1):
            level_name = f"Level_{level:02d}"
            current = await current.add_object(idx, level_name)
            depth_number = await current.add_variable(idx, "DepthNumber", level)
            depth_path = await current.add_variable(
                idx,
                "DepthPath",
                "/".join(f"Level_{n:02d}" for n in range(1, level + 1)),
            )
            await depth_number.set_writable()
            await depth_path.set_writable()

    async def _set_historizing_flags(self, node: Any) -> None:
        access_level = (
            int(ua.AccessLevel.CurrentRead)
            | int(ua.AccessLevel.CurrentWrite)
            | int(ua.AccessLevel.HistoryRead)
        )
        data_value = ua.DataValue(ua.Variant(access_level, ua.VariantType.Byte))
        await node.write_attribute(ATTRIBUTE_IDS.AccessLevel, data_value)
        await node.write_attribute(ATTRIBUTE_IDS.UserAccessLevel, data_value)
        historizing = ua.DataValue(ua.Variant(True, ua.VariantType.Boolean))
        await node.write_attribute(ATTRIBUTE_IDS.Historizing, historizing)

    async def _configure_history(self, server: Server) -> None:
        for signal_node in self._signals:
            await self._enable_history_for_node(server, signal_node.node)

    async def _enable_history_for_node(self, server: Server, node: Any) -> None:
        period = timedelta(minutes=max(self._history_seed_minutes, 5))
        count = max(1000, int((period.total_seconds() / max(self._history_sample_seconds, 1)) * 4))
        candidates = ("historize_node_data_change", "enable_history_data_change")

        for method_name in candidates:
            method = getattr(server, method_name, None)
            if method is None:
                continue
            try:
                result = method(node, period=period, count=count)
                if inspect.isawaitable(result):
                    await result
                logger.info("Enabled history using %s for node=%s", method_name, node)
                return
            except TypeError:
                # Compatibility with asyncua versions that only accept positional args.
                result = method(node, period, count)
                if inspect.isawaitable(result):
                    await result
                logger.info("Enabled history using %s for node=%s", method_name, node)
                return
            except Exception:
                logger.exception("Failed to enable history using %s", method_name)

        logger.warning("History API was not found; history reads may return empty results")

    async def _seed_history(self) -> None:
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(minutes=self._history_seed_minutes)
        t = start
        seeded_samples = 0

        while t <= now:
            epoch = t.timestamp()
            for signal_node in self._signals:
                await signal_node.node.write_value(self._data_value_for_signal(signal_node, epoch, t))
            seeded_samples += 1
            t += timedelta(seconds=self._history_sample_seconds)

        logger.info(
            "Seeded %d historical samples per signal across %d signals",
            seeded_samples,
            len(self._signals),
        )

    async def _update_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now(tz=timezone.utc)
            epoch = now.timestamp()
            for signal_node in self._signals:
                try:
                    await signal_node.node.write_value(self._data_value_for_signal(signal_node, epoch, now))
                except Exception:
                    logger.exception("Live update failed for signal=%s", signal_node.name)
            await asyncio.sleep(self._update_interval_seconds)

    async def _verify_seeded_history(self) -> None:
        if self._history_seed_minutes <= 0:
            logger.info("Skipping startup history verification because history seeding is disabled")
            return

        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(minutes=max(self._history_seed_minutes, 1))
        verified = 0

        for signal_node in self._signals:
            try:
                values = await signal_node.node.read_raw_history(
                    starttime=start,
                    endtime=now,
                    numvalues=3,
                    return_bounds=False,
                )
            except TypeError:
                values = await signal_node.node.read_raw_history(start, now, 3, False)
            except Exception:
                logger.exception("Startup history verification failed for signal=%s", signal_node.name)
                continue

            if values:
                verified += 1
                logger.info(
                    "Verified startup history signal=%s samples=%d",
                    signal_node.name,
                    len(values),
                )
            else:
                logger.warning("Startup history verification returned no samples for signal=%s", signal_node.name)

        if verified != len(self._signals):
            logger.warning(
                "Startup history verification incomplete verified=%d total=%d",
                verified,
                len(self._signals),
            )

    def _signal_value(self, signal_node: SignalNode, epoch_seconds: float) -> float:
        angle = (2.0 * math.pi * epoch_seconds / signal_node.period_seconds) + signal_node.phase
        return signal_node.base + (signal_node.amplitude * math.sin(angle))

    def _signal_payload(self, signal_node: SignalNode, epoch_seconds: float) -> bool | float:
        value = self._signal_value(signal_node, epoch_seconds)
        if signal_node.variant_type == ua.VariantType.Boolean:
            return value >= signal_node.base
        return round(value, 3)

    def _data_value_for_signal(
        self,
        signal_node: SignalNode,
        epoch_seconds: float,
        timestamp: datetime,
    ) -> ua.DataValue:
        payload = self._signal_payload(signal_node, epoch_seconds)
        opcua_timestamp = cast(Any, timestamp)
        return ua.DataValue(
            Value=ua.Variant(payload, signal_node.variant_type),
            SourceTimestamp=opcua_timestamp,
            ServerTimestamp=opcua_timestamp,
        )

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except NotImplementedError:
                # Windows ProactorEventLoop may not support this API.
                signal.signal(sig, lambda *_: self._stop_event.set())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="i3x2ua conformance asyncua fixture server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=4840, help="Bind port")
    parser.add_argument(
        "--namespace-uri",
        default="http://example.org/i3x2ua/conformance",
        help="OPC UA namespace URI used by fixture nodes",
    )
    parser.add_argument(
        "--update-interval-seconds",
        type=float,
        default=1.0,
        help="How often live values are updated",
    )
    parser.add_argument(
        "--history-seed-minutes",
        type=int,
        default=65,
        help="How many minutes of synthetic history to pre-seed at startup",
    )
    parser.add_argument(
        "--history-sample-seconds",
        type=int,
        default=15,
        help="Sampling interval for seeded history",
    )
    parser.add_argument(
        "--deep-levels",
        type=int,
        default=50,
        help="How many nested object levels to create under ConformancePlant/DeepNested",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logger level",
    )
    return parser


async def _main_async() -> None:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    endpoint = f"opc.tcp://{args.host}:{args.port}/freeopcua/server/"
    server = ConformanceFixtureServer(
        endpoint=endpoint,
        namespace_uri=args.namespace_uri,
        update_interval_seconds=max(args.update_interval_seconds, 0.2),
        history_seed_minutes=max(args.history_seed_minutes, 0),
        history_sample_seconds=max(args.history_sample_seconds, 1),
        deep_levels=max(args.deep_levels, 0),
    )
    await server.run()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
