"""Measure the sample clients' REST performance without subprocess or printing overhead.

Run ``python run_benchmark.py --help`` for workload and connection options.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from samples.client import I3XRequestError
from samples.subscription_client import I3XSubscriptionClient, _check_bulk_result


@dataclass
class Metric:
    successes: list[float] = field(default_factory=list)
    failures: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    updates: int = 0

    def summary(self, elapsed: float) -> dict[str, Any]:
        ordered = sorted(self.successes)
        attempts = len(ordered) + self.failures

        def percentile(fraction: float) -> float | None:
            return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] * 1000 if ordered else None

        return {
            "attempts": attempts,
            "successes": len(ordered),
            "failures": self.failures,
            "error_rate": self.failures / attempts if attempts else 0.0,
            "attempts_per_second": attempts / elapsed if elapsed else 0.0,
            "successes_per_second": len(ordered) / elapsed if elapsed else 0.0,
            "latency_ms": {
                "mean": statistics.fmean(ordered) * 1000 if ordered else None,
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
                "max": ordered[-1] * 1000 if ordered else None,
            },
            "updates": self.updates,
            "updates_per_second": self.updates / elapsed if elapsed else 0.0,
            "top_errors": dict(self.errors.most_common(5)),
        }


class Measurements:
    def __init__(self) -> None:
        self.start = math.inf
        self.end = math.inf
        self.metrics: dict[str, Metric] = {}
        self.excluded_failures: Counter[str] = Counter()
        self.lock = threading.Lock()

    def record(self, name: str, started: float, error: str = "", updates: int = 0) -> None:
        finished = time.perf_counter()
        with self.lock:
            if not self.start <= started <= finished <= self.end:
                if error:
                    self.excluded_failures[f"{name}: {error}"] += 1
                return
            metric = self.metrics.setdefault(name, Metric())
            if error:
                metric.failures += 1
                metric.errors[error[:300]] += 1
            else:
                metric.successes.append(finished - started)
                metric.updates += updates


def _validate_response(path: str, payload: dict[str, Any], body: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        raise I3XRequestError(f"{path}: expected a JSON object")
    if payload.get("success") is False or payload.get("responseDetail"):
        raise I3XRequestError(f"{path}: unsuccessful or partial response: {payload}")
    if path in {"/objects/list", "/objecttypes/query", "/objects/value"}:
        _check_bulk_result(payload, path, len((body or {})["elementIds"]))
    if path == "/objects":
        if payload.get("success") is not True or not isinstance(payload.get("result"), list):
            raise I3XRequestError(f"{path}: missing object results: {payload}")
    if path != "/subscriptions/sync":
        return 0
    batches = payload.get("result")
    if payload.get("success") is not True or not isinstance(batches, list):
        raise I3XRequestError(f"Sync failed: {payload}")
    count = 0
    for batch in batches:
        if not isinstance(batch, dict) or not isinstance(batch.get("updates"), list):
            raise I3XRequestError(f"Invalid sync batch: {batch}")
        for update in batch["updates"]:
            if (
                not isinstance(update, dict)
                or not {"sequenceNumber", "elementId", "value", "quality", "timestamp"}.issubset(update)
                or type(update["sequenceNumber"]) is not int
                or update["sequenceNumber"] < 0
            ):
                raise I3XRequestError(f"Invalid subscription update: {update}")
            count += 1
    return count


class BenchmarkClient(I3XSubscriptionClient):
    def __init__(self, args: argparse.Namespace, group: str, measurements: Measurements) -> None:
        super().__init__(
            args.base_url,
            verify_ssl=not args.insecure,
            timeout=args.request_timeout,
            username=args.username,
            password=args.password,
        )
        self.group = group
        self.measurements = measurements

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        error = ""
        updates = 0
        try:
            payload = super()._request(method, path, params, body)
            updates = _validate_response(path, payload, body)
            return payload
        except (I3XRequestError, ValueError) as exc:
            error = str(exc)
            raise I3XRequestError(error) from exc
        finally:
            self.measurements.record(f"{self.group} {method} {path}", started, error, updates)


@dataclass
class Worker:
    client: BenchmarkClient
    subscription_id: str | None = None
    client_id: str = field(default_factory=lambda: f"benchmark-{uuid4()}")
    last_sequence: int | None = None

    def step(self, args: argparse.Namespace) -> None:
        if self.client.group == "probe":
            self.client.get_info()
        elif self.client.group == "discovery":
            self.client.objects_by_id.clear()
            self.client.object_types_by_id.clear()
            self.client.get_info()
            self.client.discover(max_nodes=args.max_nodes, max_depth=args.max_depth)
            if not args.no_values:
                roots = [obj.element_id for obj in self.client.objects_by_id.values() if obj.is_composition]
                self.client.get_values(roots, max_depth=args.value_max_depth)
        else:
            body: dict[str, Any] = {"clientId": self.client_id, "subscriptionId": self.subscription_id}
            if self.last_sequence is not None:
                body["acknowledgeSequence"] = self.last_sequence
            payload = self.client._post("/subscriptions/sync", body)
            for batch in payload["result"]:
                for update in batch["updates"]:
                    sequence = update["sequenceNumber"]
                    self.last_sequence = sequence if self.last_sequence is None else max(self.last_sequence, sequence)


class Benchmark:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.measurements = Measurements()
        self.stop = threading.Event()
        self.workers: list[Worker] = []
        self.lifecycle_errors: list[str] = []

    def _prepare(self) -> None:
        for group, count in (
            ("discovery", self.args.discovery),
            ("subscription", self.args.subscriptions),
            ("probe", 1),
        ):
            for _ in range(count):
                worker = Worker(BenchmarkClient(self.args, group, self.measurements))
                self.workers.append(worker)
                if group == "subscription":
                    worker.subscription_id = worker.client.create_subscription(worker.client_id)
                    worker.client.register(
                        worker.client_id,
                        worker.subscription_id,
                        self.args.element_ids,
                        self.args.subscription_max_depth,
                    )

    def _work(self, worker: Worker) -> None:
        while not self.stop.is_set() and time.perf_counter() < self.measurements.end:
            started = time.perf_counter()
            error = ""
            try:
                worker.step(self.args)
            except I3XRequestError as exc:
                error = str(exc)
            if worker.client.group == "discovery":
                self.measurements.record("discovery workflow", started, error)
            if worker.client.group == "probe":
                pause = self.args.probe_interval
            elif worker.client.group == "subscription":
                pause = self.args.poll_interval
            else:
                pause = self.args.failure_delay if error else 0.0
            self.stop.wait(min(pause, max(0.0, self.measurements.end - time.perf_counter())))

    def _cleanup(self) -> None:
        for worker in self.workers:
            try:
                if worker.subscription_id is not None:
                    worker.client.delete_subscription(worker.client_id, worker.subscription_id)
            except I3XRequestError as exc:
                self.lifecycle_errors.append(f"Cleanup failed: {exc}")
            finally:
                worker.client._close_connection()

    def run(self) -> tuple[dict[str, Any], int]:
        interrupted = False
        try:
            self._prepare()
            self.measurements.start = time.perf_counter() + self.args.warmup
            self.measurements.end = self.measurements.start + self.args.duration
            with ThreadPoolExecutor(max_workers=len(self.workers)) as executor:
                futures = [executor.submit(self._work, worker) for worker in self.workers]
                try:
                    self.stop.wait(max(0.0, self.measurements.end - time.perf_counter()))
                except KeyboardInterrupt:
                    interrupted = True
                    with self.measurements.lock:
                        self.measurements.end = min(self.measurements.end, time.perf_counter())
                finally:
                    self.stop.set()
                for future in futures:
                    future.result()
        except I3XRequestError as exc:
            self.lifecycle_errors.append(f"Setup failed: {exc}")
        finally:
            self.stop.set()
            self._cleanup()
        elapsed = max(0.0, self.measurements.end - self.measurements.start)
        if not math.isfinite(elapsed):
            elapsed = 0.0
        metrics = {name: metric.summary(elapsed) for name, metric in sorted(self.measurements.metrics.items())}
        request_metrics = [metric for name, metric in metrics.items() if name != "discovery workflow"]
        attempts = sum(metric["attempts"] for metric in request_metrics)
        failures = sum(metric["failures"] for metric in request_metrics)
        failed = (
            failures > 0 or bool(self.lifecycle_errors) or bool(self.measurements.excluded_failures) or attempts == 0
        )
        report = {
            "schema_version": 1,
            "configuration": {
                key: value for key, value in vars(self.args).items() if key not in {"username", "password", "output"}
            },
            "measurement_seconds": elapsed,
            "interrupted": interrupted,
            "requests": {
                "attempts": attempts,
                "failures": failures,
                "successes_per_second": (attempts - failures) / elapsed if elapsed else 0.0,
                "error_rate": failures / attempts if attempts else 0.0,
            },
            "metrics": metrics,
            "excluded_failures": dict(self.measurements.excluded_failures),
            "lifecycle_errors": self.lifecycle_errors,
        }
        return report, 130 if interrupted else int(failed)


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def _nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and nonnegative")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _positive_int(value: str) -> int:
    number = _nonnegative_int(value)
    if not number:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark sample-client request latency and throughput")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--username", help="HTTP Basic Auth username")
    parser.add_argument("--password", help="HTTP Basic Auth password")
    parser.add_argument("--request-timeout", type=_positive_float, default=10.0)
    parser.add_argument("--discovery", type=_nonnegative_int, default=1, help="Parallel discovery clients (default: 1)")
    parser.add_argument(
        "--subscriptions", type=_nonnegative_int, default=0, help="Parallel polling clients (default: 0)"
    )
    parser.add_argument("--element-ids", nargs="+", help="IDs to monitor; required with --subscriptions")
    parser.add_argument("--duration", type=_positive_float, default=30.0, help="Measured seconds (default: 30)")
    parser.add_argument("--warmup", type=_nonnegative_float, default=5.0, help="Unmeasured seconds (default: 5)")
    parser.add_argument("--max-nodes", type=_positive_int, default=200)
    parser.add_argument("--max-depth", type=_nonnegative_int, default=1)
    parser.add_argument("--value-max-depth", type=_nonnegative_int, default=1, help="Value read composition depth")
    parser.add_argument("--no-values", action="store_true", help="Skip discovery value reads")
    parser.add_argument("--subscription-max-depth", type=_nonnegative_int, default=None)
    parser.add_argument(
        "--poll-interval", type=_nonnegative_float, default=1.0, help="Seconds between syncs; 0 = no delay"
    )
    parser.add_argument("--probe-interval", type=_positive_float, default=0.5)
    parser.add_argument("--failure-delay", type=_positive_float, default=0.2, help="Backoff after discovery errors")
    parser.add_argument("--output", type=Path, help="Write JSON results to this file")
    args = parser.parse_args(argv)
    target = urlsplit(args.base_url)
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
        parser.error("--base-url must be an HTTP(S) URL without embedded credentials")
    if target.query or target.fragment:
        parser.error("--base-url must not contain a query or fragment")
    if args.password is not None and args.username is None:
        parser.error("--password requires --username")
    if args.subscriptions and not args.element_ids:
        parser.error("--subscriptions requires --element-ids from your server")
    if args.element_ids:
        if any(not element_id.strip() for element_id in args.element_ids):
            parser.error("--element-ids must not be empty")
        args.element_ids = list(dict.fromkeys(args.element_ids))
    return args


def _print_report(report: dict[str, Any]) -> None:
    print(f"\nMeasurement: {report['measurement_seconds']:.3f}s (warm-up, setup and cleanup excluded)")
    print(
        f"{'Workload / operation':<44} {'OK':>7} {'Fail':>6} {'OK/s':>9} "
        f"{'mean ms':>10} {'p50 ms':>10} {'p95 ms':>10} {'p99 ms':>10} {'max ms':>10}"
    )
    for name, metric in report["metrics"].items():
        latencies = [metric["latency_ms"][key] for key in ("mean", "p50", "p95", "p99", "max")]
        formatted = " ".join(f"{value:10.3f}" if value is not None else f"{'n/a':>10}" for value in latencies)
        print(
            f"{name:<44} {metric['successes']:7} {metric['failures']:6} "
            f"{metric['successes_per_second']:9.2f} {formatted}"
        )
        if name.endswith("/subscriptions/sync"):
            print(f"  Notifications: {metric['updates']} ({metric['updates_per_second']:.2f}/s)")
        for error, count in metric["top_errors"].items():
            print(f"  {count}x {error}", file=sys.stderr)
    print(
        f"HTTP total: {report['requests']['successes_per_second']:.2f} successful requests/s; "
        f"error rate {report['requests']['error_rate']:.2%}"
    )
    for error, count in report["excluded_failures"].items():
        print(f"Outside measurement: {count}x {error}", file=sys.stderr)
    for error in report["lifecycle_errors"]:
        print(error, file=sys.stderr)
    if not report["requests"]["attempts"]:
        print("No requests completed inside the measurement window.", file=sys.stderr)


def main() -> int:
    args = _parse_args()
    print(
        f"Benchmarking {args.base_url}: discovery={args.discovery}, subscriptions={args.subscriptions}, "
        f"warmup={args.warmup:g}s, measurement={args.duration:g}s",
        flush=True,
    )
    try:
        report, exit_code = Benchmark(args).run()
    except KeyboardInterrupt:
        print("Benchmark interrupted during setup or cleanup.", file=sys.stderr)
        return 130
    _print_report(report)
    if args.output is not None:
        try:
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"Cannot write benchmark report: {exc}", file=sys.stderr)
            return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
