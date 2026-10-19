from __future__ import annotations

import json
import ssl
import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread, current_thread, main_thread
from typing import Any
from unittest.mock import Mock

import pytest

import run_benchmark as benchmark
from samples.client import I3XRequestError


@pytest.fixture
def benchmark_server() -> Iterator[tuple[str, list[tuple[str, dict[str, Any]]]]]:
    requests: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def respond(self, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            requests.append((self.path, {}))
            if self.path.endswith("/info"):
                self.respond({"success": True, "result": {"name": "benchmark fixture"}})
            else:
                self.respond(
                    {
                        "success": True,
                        "result": [
                            {
                                "elementId": "root",
                                "displayName": "Root",
                                "typeElementId": "type",
                                "isComposition": True,
                                "metadata": {"relationships": {"HasComponent": ["property"]}},
                            }
                        ],
                    }
                )

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path.endswith("/subscriptions"):
                self.respond({"success": True, "result": {"subscriptionId": "sub-1"}})
            elif self.path.endswith("/subscriptions/sync"):
                self.respond(
                    {
                        "success": True,
                        "result": [
                            {
                                "updates": [
                                    {
                                        "sequenceNumber": 1,
                                        "elementId": "property",
                                        "value": 42,
                                        "quality": "Good",
                                        "timestamp": "2026-10-17T07:00:00Z",
                                    }
                                ]
                            }
                        ],
                    }
                )
            else:
                ids = body.get("elementIds", body.get("subscriptionIds", []))
                self.respond(
                    {
                        "success": True,
                        "results": [
                            {
                                "success": True,
                                "elementId": element_id,
                                "result": {
                                    "elementId": element_id,
                                    "displayName": element_id,
                                    "typeElementId": "type",
                                    "isComposition": False,
                                },
                            }
                            for element_id in ids
                        ],
                    }
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_metric_percentiles_rates_and_empty_samples() -> None:
    metric = benchmark.Metric(successes=[0.1, 0.02, 0.04, 0.06], failures=1, updates=8)
    summary = metric.summary(2)
    assert summary["attempts"] == 5
    assert summary["error_rate"] == 0.2
    assert summary["attempts_per_second"] == 2.5
    assert summary["successes_per_second"] == 2
    assert summary["updates_per_second"] == 4
    assert summary["latency_ms"] == {"mean": 55, "p50": 40, "p95": 100, "p99": 100, "max": 100}
    empty = benchmark.Metric().summary(0)
    assert empty["latency_ms"]["p99"] is None
    assert empty["successes_per_second"] == 0
    json.dumps(empty, allow_nan=False)


def test_measurement_window_excludes_warmup_and_inflight_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    measurements = benchmark.Measurements()
    measurements.start, measurements.end = 10, 20
    monkeypatch.setattr("run_benchmark.time.perf_counter", lambda: 15)
    measurements.record("warmup", 9)
    measurements.record("valid", 12, updates=3)
    measurements.record("failed", 12, error="HTTP 500")
    monkeypatch.setattr("run_benchmark.time.perf_counter", lambda: 21)
    measurements.record("drained", 19)
    measurements.record("outside error", 19, error="timeout")
    assert set(measurements.metrics) == {"valid", "failed"}
    assert measurements.metrics["valid"].successes == [3]
    assert measurements.metrics["valid"].updates == 3
    assert measurements.metrics["failed"].failures == 1
    assert measurements.excluded_failures == {"outside error: timeout": 1}


@pytest.mark.parametrize(
    "options",
    [
        ["--duration", "0"],
        ["--duration", "nan"],
        ["--warmup", "inf"],
        ["--warmup", "-1"],
        ["--request-timeout", "0"],
        ["--discovery", "-1"],
        ["--subscriptions", "1"],
        ["--max-nodes", "0"],
        ["--max-depth", "-1"],
        ["--probe-interval", "0"],
        ["--poll-interval", "-1"],
        ["--password", "secret"],
        ["--base-url", "ftp://localhost"],
        ["--base-url", "http://user:secret@localhost"],
        ["--base-url", "http://localhost/v1?query=1"],
        ["--element-ids", " "],
    ],
)
def test_invalid_arguments(options: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        benchmark._parse_args(options)
    assert exc.value.code == 2


def test_client_preserves_tls_auth_and_records_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    args = benchmark._parse_args(["--username", "user", "--password", "secret", "--insecure"])
    measurements = benchmark.Measurements()
    measurements.start, measurements.end = 0, 100
    client = benchmark.BenchmarkClient(args, "probe", measurements)
    send = Mock(return_value=(200, b"invalid json"))
    monkeypatch.setattr(client, "_send", send)
    monkeypatch.setattr("run_benchmark.time.perf_counter", lambda: 10)
    with pytest.raises(I3XRequestError):
        client.get_info()
    assert send.call_args.args[3]["Authorization"] == "Basic dXNlcjpzZWNyZXQ="
    assert client._ssl_context.verify_mode == ssl.CERT_NONE
    assert not client._ssl_context.check_hostname
    assert measurements.metrics["probe GET /info"].failures == 1


@pytest.mark.parametrize(
    ("path", "payload", "body"),
    [
        ("/objects/value", {"success": True, "results": []}, {"elementIds": ["missing"]}),
        (
            "/objects/list",
            {"success": True, "results": [{"success": False}]},
            {"elementIds": ["missing"]},
        ),
        ("/objects", {"success": True}, None),
        ("/info", {"success": False}, None),
        ("/subscriptions/sync", {"success": True, "result": [], "responseDetail": {"status": 206}}, None),
        ("/subscriptions/sync", {"success": True, "result": [{"updates": [{"sequenceNumber": -1}]}]}, None),
    ],
)
def test_partial_or_invalid_responses_are_not_successes(
    path: str, payload: dict[str, Any], body: dict[str, Any] | None
) -> None:
    with pytest.raises(I3XRequestError):
        benchmark._validate_response(path, payload, body)


def test_end_to_end_discovery_polling_and_json_report(
    benchmark_server: tuple[str, list[tuple[str, dict[str, Any]]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base_url, requests = benchmark_server
    output = tmp_path / "benchmark.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_benchmark.py",
            "--base-url",
            base_url,
            "--duration",
            "0.8",
            "--warmup",
            "0.1",
            "--discovery",
            "2",
            "--subscriptions",
            "1",
            "--element-ids",
            "property",
            "property",
            "--poll-interval",
            "0.01",
            "--probe-interval",
            "0.02",
            "--username",
            "user",
            "--password",
            "secret",
            "--output",
            str(output),
        ],
    )
    assert benchmark.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["measurement_seconds"] == pytest.approx(0.8)
    assert "password" not in report["configuration"]
    assert "username" not in report["configuration"]
    assert report["configuration"]["element_ids"] == ["property"]
    assert report["requests"]["failures"] == 0
    metrics = report["metrics"]
    assert metrics["discovery workflow"]["successes"] > 0
    for name in (
        "discovery GET /info",
        "discovery GET /objects",
        "discovery POST /objects/list",
        "discovery POST /objecttypes/query",
        "discovery POST /objects/value",
        "probe GET /info",
        "subscription POST /subscriptions/sync",
    ):
        assert metrics[name]["successes"] > 0
        assert metrics[name]["latency_ms"]["p95"] > 0
    assert report["requests"]["attempts"] == sum(
        metric["attempts"] for name, metric in metrics.items() if name != "discovery workflow"
    )
    assert metrics["subscription POST /subscriptions/sync"]["updates"] > 0
    register_bodies = [body for path, body in requests if path.endswith("/subscriptions/register")]
    assert len(register_bodies) == 1
    assert "maxDepth" not in register_bodies[0]
    sync_bodies = [body for path, body in requests if path.endswith("/subscriptions/sync")]
    assert "acknowledgeSequence" not in sync_bodies[0]
    assert sync_bodies[1]["acknowledgeSequence"] == 1
    assert requests[-1][0] == "/v1/subscriptions/delete"
    assert not report["lifecycle_errors"]
    assert "p99 ms" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["register", "delete"])
def test_setup_and_cleanup_failures_are_reported(
    benchmark_server: tuple[str, list[tuple[str, dict[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    base_url, requests = benchmark_server
    args = benchmark._parse_args(
        [
            "--base-url",
            base_url,
            "--discovery",
            "0",
            "--subscriptions",
            "1",
            "--element-ids",
            "property",
            "--warmup",
            "0",
            "--duration",
            "0.1",
        ]
    )
    failing_method = "register" if failure == "register" else "delete_subscription"
    monkeypatch.setattr(benchmark.BenchmarkClient, failing_method, Mock(side_effect=I3XRequestError("test failure")))
    report, exit_code = benchmark.Benchmark(args).run()
    assert exit_code == 1
    assert "test failure" in report["lifecycle_errors"][0]
    if failure == "register":
        assert requests[-1][0] == "/v1/subscriptions/delete"
    json.dumps(report, allow_nan=False)


def test_failed_requests_produce_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(benchmark.BenchmarkClient, "_send", Mock(return_value=(503, b"Service unavailable")))
    args = benchmark._parse_args(
        ["--duration", "0.05", "--warmup", "0", "--discovery", "0", "--probe-interval", "0.01"]
    )
    report, exit_code = benchmark.Benchmark(args).run()
    assert exit_code == 1
    assert report["requests"]["failures"] > 0
    assert report["requests"]["error_rate"] == 1
    assert report["metrics"]["probe GET /info"]["latency_ms"]["mean"] is None


def test_interruption_during_warmup_cleans_up_subscriptions(
    benchmark_server: tuple[str, list[tuple[str, dict[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, requests = benchmark_server
    args = benchmark._parse_args(
        [
            "--base-url",
            base_url,
            "--discovery",
            "0",
            "--subscriptions",
            "1",
            "--element-ids",
            "property",
            "--warmup",
            "5",
            "--duration",
            "5",
        ]
    )
    runner = benchmark.Benchmark(args)
    wait = runner.stop.wait

    def interrupt_main(timeout: float | None = None) -> bool:
        if current_thread() is main_thread():
            raise KeyboardInterrupt
        return wait(timeout)

    monkeypatch.setattr(runner.stop, "wait", interrupt_main)
    report, exit_code = runner.run()
    assert exit_code == 130
    assert report["interrupted"]
    assert report["measurement_seconds"] == 0
    assert report["requests"]["attempts"] == 0
    assert requests[-1][0] == "/v1/subscriptions/delete"
    assert not report["lifecycle_errors"]
    json.dumps(report, allow_nan=False)
