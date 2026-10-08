"""
i3X subscription client using only the Python standard library.

Usage:
    python samples/subscription_client.py --mode poll --element-ids "property-dd1a9a05d251425f"
    python samples/subscription_client.py --mode sse --element-ids "property-dd1a9a05d251425f"

Notifications are flushed as JSON lines to stdout; status/errors go to stderr.
Ctrl+C stops reception and deletes the subscription. Transport failures are
reported rather than automatically retried. See --help for connection options.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING or __package__:
    from .client import I3XClient, I3XRequestError
else:
    from client import I3XClient, I3XRequestError


def _sse_events(lines: Iterable[bytes]) -> Iterator[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    for raw_line in lines:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data or event == "close":
                yield event, "\n".join(data)
            event, data = "message", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)


def _check_bulk_result(payload: dict[str, Any], operation: str, expected_count: int) -> None:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != expected_count:
        raise I3XRequestError(f"{operation}: missing or incomplete per-item results: {payload}")
    failures = [item for item in results if not isinstance(item, dict) or item.get("success") is not True]
    if payload.get("success") is not True or failures:
        raise I3XRequestError(f"{operation} failed: {json.dumps(failures or payload)}")


class I3XSubscriptionClient(I3XClient):
    """Reuse the discovery client's REST transport, TLS, and Basic Auth support."""

    def create_subscription(self, client_id: str) -> str:
        payload = self._post("/v1/subscriptions", {"clientId": client_id})
        result = payload.get("result")
        if payload.get("success") is not True or not isinstance(result, dict):
            raise I3XRequestError(f"Create subscription failed: {payload}")
        subscription_id = result.get("subscriptionId")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise I3XRequestError(f"Create subscription returned no subscriptionId: {payload}")
        return subscription_id

    def register(self, client_id: str, subscription_id: str, element_ids: list[str], max_depth: int | None) -> None:
        payload = self._post(
            "/v1/subscriptions/register",
            {
                "clientId": client_id,
                "subscriptionId": subscription_id,
                "elementIds": element_ids,
                "maxDepth": max_depth,
            },
        )
        _check_bulk_result(payload, "Register monitored items", len(element_ids))

    def delete_subscription(self, client_id: str, subscription_id: str) -> None:
        payload = self._post("/v1/subscriptions/delete", {"clientId": client_id, "subscriptionIds": [subscription_id]})
        _check_bulk_result(payload, "Delete subscription", 1)

    @staticmethod
    def print_updates(updates: Any) -> int | None:
        if not isinstance(updates, list):
            raise I3XRequestError(f"Expected an array of subscription updates: {updates}")
        last_sequence: int | None = None
        for update in updates:
            if not isinstance(update, dict) or not {
                "sequenceNumber",
                "elementId",
                "value",
                "quality",
                "timestamp",
            }.issubset(update):
                raise I3XRequestError(f"Invalid subscription update: {update}")
            sequence = update["sequenceNumber"]
            if type(sequence) is not int or sequence < 0:
                raise I3XRequestError(f"Invalid sequenceNumber in subscription update: {update}")
            print(json.dumps(update, ensure_ascii=True), flush=True)
            last_sequence = sequence if last_sequence is None else max(last_sequence, sequence)
        return last_sequence

    def poll(self, client_id: str, subscription_id: str, interval: float) -> None:
        last_sequence: int | None = None
        while True:
            body: dict[str, Any] = {"clientId": client_id, "subscriptionId": subscription_id}
            if last_sequence is not None:
                body["acknowledgeSequence"] = last_sequence
            payload = self._post("/v1/subscriptions/sync", body)
            if payload.get("responseDetail"):
                print(f"Sync warning: {json.dumps(payload['responseDetail'])}", file=sys.stderr, flush=True)
            batches = payload.get("result")
            if payload.get("success") is not True or not isinstance(batches, list):
                raise I3XRequestError(f"Sync failed: {payload}")
            for batch in batches:
                if not isinstance(batch, dict) or "updates" not in batch:
                    raise I3XRequestError(f"Invalid sync batch: {batch}")
                sequence = self.print_updates(batch["updates"])
                if sequence is not None:
                    last_sequence = sequence if last_sequence is None else max(last_sequence, sequence)
            time.sleep(interval)

    def stream(self, client_id: str, subscription_id: str) -> None:
        path = "/v1/subscriptions/stream"
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self._auth_header is not None:
            headers["Authorization"] = self._auth_header
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps({"clientId": client_id, "subscriptionId": subscription_id}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self._ssl_context) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise I3XRequestError(f"{path}: expected text/event-stream, got {response.headers['Content-Type']}")
                for event, data in _sse_events(response):
                    if event == "close":
                        print("Stream closed by server.", file=sys.stderr, flush=True)
                        return
                    self.print_updates(json.loads(data))
                raise I3XRequestError("SSE connection ended without a server close event")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise I3XRequestError(f"POST {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise I3XRequestError(f"POST {path} -> {exc.reason}") from exc


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description="Print i3X subscription notifications as JSON lines")
    parser.add_argument("--element-ids", nargs="+", required=True, help="Element IDs to monitor (quote each ID)")
    parser.add_argument("--mode", choices=("poll", "sse"), default="poll", help="Receive mode (default: poll)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="i3X server base URL")
    parser.add_argument("--client-id", default=f"sample-subscription-{uuid4()}", help="Subscription owner ID")
    parser.add_argument("--poll-interval", type=_positive_float, default=1.0, help="Seconds between sync calls")
    parser.add_argument(
        "--max-depth", type=_nonnegative_int, default=None, help="Composition depth (default: all descendants)"
    )
    parser.add_argument("--timeout", type=_positive_float, default=30.0, help="HTTP/socket read timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--username", default=None, help="HTTP Basic Auth username")
    parser.add_argument("--password", default=None, help="HTTP Basic Auth password")
    args = parser.parse_args()
    if any(not element_id.strip() for element_id in args.element_ids):
        parser.error("--element-ids must not contain empty IDs")
    if not args.client_id.strip():
        parser.error("--client-id must not be empty")

    client = I3XSubscriptionClient(
        args.base_url,
        verify_ssl=not args.insecure,
        timeout=args.timeout,
        username=args.username,
        password=args.password,
    )
    subscription_id: str | None = None
    exit_code = 0
    try:
        subscription_id = client.create_subscription(args.client_id)
        client.register(args.client_id, subscription_id, list(dict.fromkeys(args.element_ids)), args.max_depth)
        print(f"Subscribed ({args.mode}): {subscription_id}. Press Ctrl+C to stop.", file=sys.stderr, flush=True)
        if args.mode == "poll":
            client.poll(args.client_id, subscription_id, args.poll_interval)
        else:
            client.stream(args.client_id, subscription_id)
    except KeyboardInterrupt:
        print("\nStopping subscription.", file=sys.stderr, flush=True)
    except (I3XRequestError, OSError, ValueError) as exc:
        print(f"Subscription failed: {exc}", file=sys.stderr, flush=True)
        exit_code = 1
    finally:
        if subscription_id is not None:
            try:
                client.delete_subscription(args.client_id, subscription_id)
                print(f"Deleted subscription: {subscription_id}", file=sys.stderr, flush=True)
            except (I3XRequestError, OSError, ValueError) as exc:
                print(f"Subscription cleanup failed: {exc}", file=sys.stderr, flush=True)
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
