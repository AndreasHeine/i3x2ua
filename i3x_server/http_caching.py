"""HTTP model revision headers and conditional GET support.

Handlers record the revisions of the data they actually used (model, namespaces,
object type context). The middleware turns them into ``I3X-Model-Revision`` and,
for opted-in GET routes, a weak ``ETag`` plus ``304 Not Modified`` on ``If-None-Match``.
Recording what was used (instead of reading the newest state at response time) keeps
a response served from stale caches from being tagged with a newer revision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from fastapi import Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MODEL_REVISION_HEADER = "I3X-Model-Revision"
REVISIONS_SCOPE_KEY = "i3x.revisions"
MODEL_PART = "model"
NAMESPACES_PART = "namespaces"
OBJECT_TYPES_PART = "objecttypes"
_ETAG_ENABLED = "_etag"
_NOT_CACHEABLE = "_not_cacheable"
_BODY_HEADERS = ("content-length", "content-type", "content-encoding")


class NotModifiedError(Exception):
    """Raised by a handler to answer 304 before building an expensive response body."""


def _revision_parts(request: Any) -> dict[str, str] | None:
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    parts = scope.get(REVISIONS_SCOPE_KEY)
    return parts if isinstance(parts, dict) else None


def record_revision(request: Any, name: str, revision: str) -> None:
    parts = _revision_parts(request)
    if parts is not None and revision:
        parts[name] = revision


def mark_not_cacheable(request: Any) -> None:
    """Suppress the ETag for a degraded response (e.g. a failed optional lookup)."""
    record_revision(request, _NOT_CACHEABLE, "1")


def enable_etag(request: Request) -> None:
    """Route dependency that opts a GET endpoint into ETag / conditional GET handling."""
    record_revision(request, _ETAG_ENABLED, "1")


def content_revision(values: Iterable[object]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(repr(value).encode())
        digest.update(b"\n")
    return digest.hexdigest()[:32]


def representation_etag(parts: dict[str, str]) -> str | None:
    if _ETAG_ENABLED not in parts or _NOT_CACHEABLE in parts:
        return None
    revisions = sorted((name, value) for name, value in parts.items() if not name.startswith("_"))
    if not revisions:
        return None
    # Weak: the representation is equivalent, but not byte-identical across encodings.
    return f'W/"{content_revision(revisions)}"'


def _opaque_tag(tag: str) -> str:
    tag = tag.strip()
    return tag[2:] if tag.startswith("W/") else tag


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    expected = _opaque_tag(etag)
    return any(_opaque_tag(candidate) == expected for candidate in if_none_match.split(","))


def raise_if_not_modified(request: Request) -> None:
    """Short-circuit with 304 once all revisions are recorded, before serializing the body."""
    if request.method not in {"GET", "HEAD"}:
        return
    parts = _revision_parts(request)
    if parts is None:
        return
    etag = representation_etag(parts)
    if etag is not None and etag_matches(request.headers.get("if-none-match"), etag):
        raise NotModifiedError


class ModelRevisionMiddleware:
    """Pure ASGI middleware so streaming responses (SSE) pass through unbuffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        parts: dict[str, str] = {}
        scope[REVISIONS_SCOPE_KEY] = parts
        is_read = scope["method"] in {"GET", "HEAD"}
        if_none_match = Headers(scope=scope).get("if-none-match")
        drop_body = False

        async def send_with_revision(message: Message) -> None:
            nonlocal drop_body
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                model_revision = parts.get(MODEL_PART)
                if model_revision:
                    headers[MODEL_REVISION_HEADER] = model_revision
                etag = representation_etag(parts) if is_read else None
                status = message["status"]
                if etag is not None and status in {200, 304}:
                    headers["ETag"] = etag
                    headers["Cache-Control"] = "no-cache"
                    if status == 200 and etag_matches(if_none_match, etag):
                        message["status"] = 304
                        drop_body = True
                    if message["status"] == 304:
                        for name in _BODY_HEADERS:
                            if name in headers:
                                del headers[name]
            elif message["type"] == "http.response.body" and drop_body:
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app(scope, receive, send_with_revision)
