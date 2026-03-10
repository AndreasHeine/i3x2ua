from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect minimal test data from i3x2ua REST API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL of the REST server")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request on timeout/connect errors")
    parser.add_argument("--retry-delay", type=float, default=1.0, help="Delay between retries in seconds")
    parser.add_argument("--max-root-children", type=int, default=3, help="How many root items to query for children")
    parser.add_argument("--max-data-reads", type=int, default=3, help="How many property values to read via /data")
    parser.add_argument("--verbose", action="store_true", help="Print progress while collecting data")
    return parser.parse_args()


async def fetch_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    retries: int,
    retry_delay: float,
    verbose: bool,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts = max(0, retries) + 1
    for attempt in range(attempts):
        try:
            if verbose:
                print(f"[{attempt + 1}/{attempts}] {method} {path}", flush=True)
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"value": data}
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as exc:
            if attempt >= attempts - 1:
                message = (
                    f"Request failed after {attempts} attempts: {method} {path} ({exc.__class__.__name__}: {exc})"
                )
                raise RuntimeError(message) from exc
            if verbose:
                print(
                    f"Retry after {retry_delay:.1f}s due to {exc.__class__.__name__} on {method} {path}",
                    flush=True,
                )
            await asyncio.sleep(max(0.0, retry_delay))
    raise RuntimeError(f"Request failed: {method} {path}")


async def build_test_dataset(
    base_url: str,
    timeout: float,
    retries: int,
    retry_delay: float,
    max_root_children: int,
    max_data_reads: int,
    verbose: bool,
) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout) as client:
        if verbose:
            print("Checking server docs endpoint...", flush=True)
        docs_status = await fetch_status(
            client,
            "GET",
            "/docs",
            retries=retries,
            retry_delay=retry_delay,
            verbose=verbose,
        )

        if verbose:
            print("Loading model root...", flush=True)
        model_root = await fetch_json(
            client,
            "GET",
            "/model",
            retries=retries,
            retry_delay=retry_delay,
            verbose=verbose,
        )

        root_items_raw = model_root.get("items", [])
        root_items = (
            [item for item in root_items_raw if isinstance(item, dict)]
            if isinstance(root_items_raw, list)
            else []
        )

        selected_roots = root_items[: max(0, max_root_children)]
        children_by_parent: dict[str, dict[str, Any]] = {}
        total_children_count = 0

        for root in selected_roots:
            root_id = str(root.get("id", ""))
            if not root_id:
                continue
            if verbose:
                print(f"Loading children for root {root_id}...", flush=True)
            children_payload = await fetch_json(
                client,
                "GET",
                f"/model/{root_id}/children",
                retries=retries,
                retry_delay=retry_delay,
                verbose=verbose,
            )
            children_raw = children_payload.get("children", [])
            children = (
                [item for item in children_raw if isinstance(item, dict)]
                if isinstance(children_raw, list)
                else []
            )
            total_children_count += len(children)
            children_by_parent[root_id] = {
                "count": len(children),
                "items": children,
            }

        property_ids: list[str] = []
        for payload in children_by_parent.values():
            items_raw = payload.get("items", [])
            items = [item for item in items_raw if isinstance(item, dict)] if isinstance(items_raw, list) else []
            for item in items:
                if str(item.get("kind", "")) != "property":
                    continue
                prop_id = str(item.get("id", ""))
                if prop_id:
                    property_ids.append(prop_id)

        selected_property_ids = property_ids[: max(0, max_data_reads)]
        single_reads: dict[str, dict[str, Any]] = {}
        for property_id in selected_property_ids:
            if verbose:
                print(f"Reading property {property_id}...", flush=True)
            single_reads[property_id] = await fetch_json(
                client,
                "GET",
                f"/data/{property_id}",
                retries=retries,
                retry_delay=retry_delay,
                verbose=verbose,
            )

        batch_reads = (
            await fetch_json(
                client,
                "POST",
                "/data/query",
                retries=retries,
                retry_delay=retry_delay,
                verbose=verbose,
                payload={"property_ids": selected_property_ids},
            )
            if selected_property_ids
            else {"values": []}
        )

        return {
            "meta": {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "base_url": base_url,
                "timeout": timeout,
                "retries": retries,
                "retry_delay": retry_delay,
                "max_root_children": max_root_children,
                "max_data_reads": max_data_reads,
            },
            "connectivity": {
                "docs_status_code": docs_status,
            },
            "model": {
                "root_item_count": len(root_items),
                "root": model_root,
                "children_parent_count": len(children_by_parent),
                "children_total_count": total_children_count,
                "children_by_parent": children_by_parent,
            },
            "data": {
                "discovered_property_count": len(property_ids),
                "selected_property_ids": selected_property_ids,
                "single_reads": single_reads,
                "batch_reads": batch_reads,
            },
        }


async def fetch_status(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    retries: int,
    retry_delay: float,
    verbose: bool,
) -> int:
    attempts = max(0, retries) + 1
    for attempt in range(attempts):
        try:
            if verbose:
                print(f"[{attempt + 1}/{attempts}] {method} {path}", flush=True)
            response = await client.request(method, path)
            return response.status_code
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError) as exc:
            if attempt >= attempts - 1:
                message = (
                    f"Request failed after {attempts} attempts: {method} {path} ({exc.__class__.__name__}: {exc})"
                )
                raise RuntimeError(message) from exc
            if verbose:
                print(
                    f"Retry after {retry_delay:.1f}s due to {exc.__class__.__name__} on {method} {path}",
                    flush=True,
                )
            await asyncio.sleep(max(0.0, retry_delay))
    raise RuntimeError(f"Request failed: {method} {path}")


def write_output(path: str, payload: dict[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def resolve_output_path(output_arg: str | None) -> Path:
    if output_arg is not None:
        return Path(output_arg)
    return Path(__file__).with_name("rest_test_data.json")


async def run() -> int:
    args = parse_args()
    output_path = resolve_output_path(args.output)
    try:
        dataset = await build_test_dataset(
            base_url=args.base_url,
            timeout=args.timeout,
            retries=args.retries,
            retry_delay=args.retry_delay,
            max_root_children=args.max_root_children,
            max_data_reads=args.max_data_reads,
            verbose=args.verbose,
        )
    except RuntimeError as exc:
        print(f"Fehler: {exc}")
        print(
            "Hinweis: Pruefe, ob der Server laeuft "
            "(z. B. uv run uvicorn i3x_server.main:app --reload --host 127.0.0.1 --port 8000)."
        )
        print("Bei langsamen Antworten erhoehe z. B. --timeout 30 --retries 4.")
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"HTTP-Fehler bei {exc.request.method} {exc.request.url}: {exc.response.status_code}")
        print(exc.response.text)
        return 1
    write_output(str(output_path), dataset)
    print(f"Testdaten geschrieben nach: {output_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
