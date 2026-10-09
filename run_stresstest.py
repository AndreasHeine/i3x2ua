"""Stress test the i3X REST API with many parallel client processes.

Spawns, in parallel:
- DISCOVERY_CONCURRENT_INSTANCES copies of samples/client.py, each restarted in an endless loop
- SUBSCRIPTION_CONCURRENT_INSTANCES copies of samples/subscription_client.py, restarted with backoff on failure
- A latency probe hitting GET /info to reveal event-loop blocking while the server is under load

Live statistics are printed every --report-interval seconds. Press Ctrl+C (or let --duration expire) to stop;
children get a stop signal mapped to KeyboardInterrupt so they can delete their subscriptions before being
force-killed after --shutdown-grace seconds.

Usage:
    python run_stresstest.py
    python run_stresstest.py --discovery 20 --subscriptions 50 --duration 600
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

REPO_ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:8000/v1"

DISCOVERY_COMMAND = [
    str(REPO_ROOT / "samples" / "client.py"),
    "--max-nodes",
    "10000",
    "--max-depth",
    "1000",
]
DISCOVERY_CONCURRENT_INSTANCES = 10

SUBSCRIPTION_COMMAND = [
    str(REPO_ROOT / "samples" / "subscription_client.py"),
    "--mode",
    "poll",
    "--poll-interval",
    "3",
    "--element-ids",
    "property-dd1a9a05d251425f",
    "property-7fe339112a5b01e4",
]
SUBSCRIPTION_CONCURRENT_INSTANCES = 10

_WINDOW_SIZE = 1000
_STDERR_TAIL_LINES = 5
_MAX_ERROR_LENGTH = 300
_ERROR_KEY_LENGTH = 160
_TOP_ERRORS = 3
_IS_WINDOWS = sys.platform == "win32"
_STOP_SIGNAL = signal.CTRL_BREAK_EVENT if _IS_WINDOWS else signal.SIGTERM
_CHILD_BOOTSTRAP = (
    "import os, runpy, signal, sys; "
    "signal.signal(getattr(signal, 'SIGBREAK', signal.SIGTERM), signal.default_int_handler); "
    "sys.argv = sys.argv[1:]; "
    "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0]))); "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _format_latencies(values: list[float]) -> str:
    if not values:
        return "n/a"
    return (
        f"avg={statistics.fmean(values):.3f}s p50={_percentile(values, 0.5):.3f}s "
        f"p95={_percentile(values, 0.95):.3f}s max={max(values):.3f}s"
    )


def _last_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1][-_MAX_ERROR_LENGTH:] if lines else ""


def _error_key(error: str) -> str:
    parts = error.split(" | ")
    message = next((part for part in reversed(parts) if "failed" in part.lower()), parts[-1])
    message = re.sub(r"\b(subscription|property|object)-[0-9a-f-]+", r"\1-<id>", message)
    return message.split(": {", 1)[0][:_ERROR_KEY_LENGTH]


@dataclass
class GroupStats:
    name: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: int = 0
    starts: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    aborted: int = 0
    lines: int = 0
    durations: deque[float] = field(default_factory=lambda: deque(maxlen=_WINDOW_SIZE))
    last_error: str = ""
    error_counts: Counter[str] = field(default_factory=Counter)

    def record_start(self) -> None:
        with self.lock:
            self.running += 1
            self.starts += 1

    def record_end(self, duration: float, ok: bool, timed_out: bool, error: str) -> None:
        with self.lock:
            self.running -= 1
            self.durations.append(duration)
            if ok:
                self.successes += 1
            else:
                self.failures += 1
                self.last_error = error
                self.error_counts[_error_key(error)] += 1
            if timed_out:
                self.timeouts += 1

    def record_abort(self) -> None:
        with self.lock:
            self.running -= 1
            self.aborted += 1

    def record_line(self) -> None:
        with self.lock:
            self.lines += 1

    def summary(self, elapsed: float) -> str:
        with self.lock:
            text = (
                f"{self.name:<12} running={self.running} starts={self.starts} ok={self.successes} "
                f"failed={self.failures} timeouts={self.timeouts} aborted={self.aborted} "
                f"runtime[{_format_latencies(list(self.durations))}]"
            )
            if self.lines:
                text += f" output_lines={self.lines} ({self.lines / max(elapsed, 1e-9):.1f}/s)"
            if self.last_error:
                text += f"\n{'':<13}last error: {self.last_error}"
            for error, count in self.error_counts.most_common(_TOP_ERRORS):
                text += f"\n{'':<13}{count:>6}x {error}"
            return text


@dataclass
class ProbeStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=_WINDOW_SIZE))
    samples: int = 0
    errors: int = 0
    slow: int = 0
    worst: float = 0.0

    def record(self, latency: float, ok: bool, slow_threshold: float) -> None:
        with self.lock:
            self.latencies.append(latency)
            self.samples += 1
            self.worst = max(self.worst, latency)
            if not ok:
                self.errors += 1
            if latency >= slow_threshold:
                self.slow += 1

    def summary(self) -> str:
        with self.lock:
            return (
                f"{'probe /info':<12} samples={self.samples} errors={self.errors} slow={self.slow} "
                f"worst={self.worst:.3f}s latency[{_format_latencies(list(self.latencies))}]"
            )


class Supervisor:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.discovery = GroupStats("discovery")
        self.subscription = GroupStats("subscription")
        self.probe = ProbeStats()
        self.processes: set[subprocess.Popen[str]] = set()
        self.processes_lock = threading.Lock()
        self.started_at = time.monotonic()

    def _log(self, message: str) -> None:
        elapsed = time.monotonic() - self.started_at
        print(f"[stresstest +{elapsed:7.1f}s] {message}", flush=True)

    def _spawn(self, command: list[str], capture_stdout: bool) -> subprocess.Popen[str]:
        isolation: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _IS_WINDOWS else {"start_new_session": True}
        )
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD_BOOTSTRAP, *command],
            cwd=str(REPO_ROOT),
            env=_CHILD_ENV,
            **isolation,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with self.processes_lock:
            self.processes.add(process)
        return process

    def _forget(self, process: subprocess.Popen[str]) -> None:
        with self.processes_lock:
            self.processes.discard(process)

    @staticmethod
    def _drain(pipe: IO[str], tail: deque[str]) -> None:
        for line in iter(pipe.readline, ""):
            tail.append(line.rstrip())

    def discovery_worker(self, index: int) -> None:
        command = [*DISCOVERY_COMMAND, "--base-url", self.args.base_url, "--timeout", str(self.args.request_timeout)]
        stats = self.discovery
        while not self.stop_event.is_set():
            stats.record_start()
            started = time.monotonic()
            process = self._spawn(command, capture_stdout=False)
            timed_out = False
            try:
                _, stderr = process.communicate(timeout=self.args.discovery_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                _, stderr = process.communicate()
            finally:
                self._forget(process)
            duration = time.monotonic() - started
            if self.stop_event.is_set():
                stats.record_abort()
                break
            ok = process.returncode == 0 and not timed_out
            error = "timeout" if timed_out else _last_line(stderr)
            stats.record_end(duration, ok, timed_out, error)
            if timed_out:
                self._log(f"discovery#{index} TIMEOUT after {duration:.1f}s (possible server blocking)")
            elif not ok:
                self._log(f"discovery#{index} failed (rc={process.returncode}) after {duration:.1f}s: {error}")
                self.stop_event.wait(self.args.discovery_failure_delay)
            elif duration >= self.args.slow_discovery:
                self._log(f"discovery#{index} SLOW run: {duration:.1f}s")
            if self.args.discovery_pause > 0:
                self.stop_event.wait(self.args.discovery_pause)

    def subscription_worker(self, index: int) -> None:
        command = [*SUBSCRIPTION_COMMAND, "--base-url", self.args.base_url, "--timeout", str(self.args.request_timeout)]
        stats = self.subscription
        backoff = self.args.restart_backoff
        while not self.stop_event.is_set():
            stats.record_start()
            started = time.monotonic()
            process = self._spawn(command, capture_stdout=True)
            assert process.stdout is not None and process.stderr is not None
            stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
            stderr_thread = threading.Thread(target=self._drain, args=(process.stderr, stderr_tail), daemon=True)
            stderr_thread.start()
            for _ in iter(process.stdout.readline, ""):
                stats.record_line()
            process.wait()
            stderr_thread.join(timeout=2)
            self._forget(process)
            duration = time.monotonic() - started
            error = " | ".join(stderr_tail)[-_MAX_ERROR_LENGTH:]
            if self.stop_event.is_set():
                stats.record_abort()
                break
            stats.record_end(duration, False, False, error)
            if duration >= self.args.stable_after:
                backoff = self.args.restart_backoff
            self._log(
                f"subscription#{index} exited (rc={process.returncode}) after {duration:.1f}s, "
                f"restarting in {backoff:.1f}s: {error}"
            )
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, self.args.max_restart_backoff)

    def probe_worker(self) -> None:
        url = f"{self.args.base_url.rstrip('/')}/info"
        while not self.stop_event.is_set():
            started = time.monotonic()
            ok = True
            try:
                with urllib.request.urlopen(url, timeout=self.args.probe_timeout) as response:
                    response.read()
            except (urllib.error.URLError, OSError, ValueError):
                ok = False
            latency = time.monotonic() - started
            self.probe.record(latency, ok, self.args.slow_probe)
            if latency >= self.args.slow_probe:
                self._log(f"probe GET /info took {latency:.3f}s (ok={ok}) -> server may be blocking")
            self.stop_event.wait(max(0.0, self.args.probe_interval - latency))

    def report(self) -> None:
        elapsed = time.monotonic() - self.started_at
        for line in (self.discovery.summary(elapsed), self.subscription.summary(elapsed), self.probe.summary()):
            self._log(line)

    def _shutdown(self) -> None:
        self.stop_event.set()
        self._log("stopping, waiting for clients to clean up...")
        with self.processes_lock:
            running = [process for process in self.processes if process.poll() is None]
        for process in running:
            try:
                process.send_signal(_STOP_SIGNAL)
            except OSError:
                pass
        deadline = time.monotonic() + self.args.shutdown_grace
        while time.monotonic() < deadline:
            with self.processes_lock:
                if all(process.poll() is not None for process in self.processes):
                    break
            time.sleep(0.2)
        with self.processes_lock:
            leftovers = [process for process in self.processes if process.poll() is None]
        for process in leftovers:
            process.kill()
        if leftovers:
            self._log(f"force-killed {len(leftovers)} process(es)")

    def run(self) -> int:
        threads = [threading.Thread(target=self.probe_worker, daemon=True)]
        threads += [
            threading.Thread(target=self.discovery_worker, args=(index,), daemon=True)
            for index in range(self.args.discovery)
        ]
        threads += [
            threading.Thread(target=self.subscription_worker, args=(index,), daemon=True)
            for index in range(self.args.subscriptions)
        ]
        self._log(
            f"target={self.args.base_url} discovery={self.args.discovery} subscriptions={self.args.subscriptions} "
            f"duration={self.args.duration or 'endless'}"
        )
        try:
            for thread in threads:
                thread.start()
                time.sleep(self.args.ramp_up)
            next_report = time.monotonic() + self.args.report_interval
            end_at = time.monotonic() + self.args.duration if self.args.duration else None
            while end_at is None or time.monotonic() < end_at:
                time.sleep(0.5)
                if time.monotonic() >= next_report:
                    self.report()
                    next_report += self.args.report_interval
        except KeyboardInterrupt:
            pass
        try:
            self._shutdown()
            for thread in threads:
                thread.join(timeout=5)
        except KeyboardInterrupt:
            self._log("second Ctrl+C, aborting")
        self._log("final statistics:")
        self.report()
        return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parallel stress test for the i3X REST API")
    parser.add_argument("--base-url", default=BASE_URL, help="i3X API base URL")
    parser.add_argument("--discovery", type=int, default=DISCOVERY_CONCURRENT_INSTANCES, help="Discovery instances")
    parser.add_argument(
        "--subscriptions", type=int, default=SUBSCRIPTION_CONCURRENT_INSTANCES, help="Subscription instances"
    )
    parser.add_argument("--duration", type=float, default=0, help="Test duration in seconds (0 = endless)")
    parser.add_argument("--ramp-up", type=float, default=0.2, help="Delay between starting workers in seconds")
    parser.add_argument("--report-interval", type=float, default=10.0, help="Seconds between statistics reports")
    parser.add_argument("--discovery-timeout", type=float, default=300.0, help="Kill a discovery run after N seconds")
    parser.add_argument(
        "--request-timeout", type=float, default=10.0, help="Per-request HTTP timeout passed to the clients"
    )
    parser.add_argument("--slow-discovery", type=float, default=60.0, help="Log discovery runs slower than N seconds")
    parser.add_argument("--discovery-pause", type=float, default=0.0, help="Pause between discovery runs in seconds")
    parser.add_argument(
        "--discovery-failure-delay", type=float, default=2.0, help="Delay before retrying a failed discovery run"
    )
    parser.add_argument("--restart-backoff", type=float, default=1.0, help="Initial subscription restart delay")
    parser.add_argument("--max-restart-backoff", type=float, default=30.0, help="Maximum subscription restart delay")
    parser.add_argument(
        "--stable-after", type=float, default=30.0, help="Reset restart backoff after a subscription ran N seconds"
    )
    parser.add_argument("--probe-interval", type=float, default=0.5, help="Seconds between GET /info probes")
    parser.add_argument("--probe-timeout", type=float, default=10.0, help="GET /info probe timeout in seconds")
    parser.add_argument("--slow-probe", type=float, default=1.0, help="Log probes slower than N seconds")
    parser.add_argument("--shutdown-grace", type=float, default=10.0, help="Seconds to let clients clean up on stop")
    return parser.parse_args()


def main() -> int:
    return Supervisor(_parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
