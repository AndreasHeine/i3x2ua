"""Frontend asset wiring: static mounts, inline-script CSP hashes, canonical page redirects.

The UI is a prerendered SvelteKit bundle in ``dist/`` served through ``app.frontend()``. This
module owns the filesystem-side knowledge the composition root needs: where the assets live,
which inline scripts the CSP must allow, and which page paths need the trailing-slash redirect.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import posixpath
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from i3x_server.api.openapi import DocumentedFastAPI

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _is_tag_name_boundary(value: str, index: int) -> bool:
    if index >= len(value):
        return True
    return not (value[index].isalnum() or value[index] in {"_", "-", ":"})


def _extract_inline_script_bodies(html: str) -> list[str]:
    """Inline script bodies, tolerant of attribute whitespace and trailing text after the tag.

    SvelteKit emits `<script data-sveltekit-preload-data="hover">` and closing tags such as
    `</script   >`, so the tag edges are matched by boundary rather than exact text.
    """
    lower_html = html.lower()
    cursor = 0
    script_bodies: list[str] = []

    while True:
        start = lower_html.find("<script", cursor)
        if start == -1:
            break
        if not _is_tag_name_boundary(lower_html, start + len("<script")):
            cursor = start + 1
            continue

        open_end = html.find(">", start)
        if open_end == -1:
            break

        close_start = lower_html.find("</script", open_end + 1)
        if close_start == -1:
            break
        if not _is_tag_name_boundary(lower_html, close_start + len("</script")):
            cursor = close_start + 1
            continue

        close_end = html.find(">", close_start)
        if close_end == -1:
            break

        script_bodies.append(html[open_end + 1 : close_start])
        cursor = close_end + 1

    return script_bodies


def _frontend_inline_script_hashes(frontend_dist: Path) -> tuple[str, ...]:
    """`sha256-` source hashes of every non-empty inline script in the prerendered pages.

    The CSP for UI pages lists these instead of `'unsafe-inline'`, so the page's own boot
    script stays executable while injected inline markup is blocked.
    """
    if not frontend_dist.exists():
        return ()
    hashes: set[str] = set()
    for html_file in frontend_dist.rglob("*.html"):
        try:
            html = html_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for script_body in _extract_inline_script_bodies(html):
            if not script_body.strip():
                continue
            digest = hashlib.sha256(script_body.encode("utf-8")).digest()
            encoded = base64.b64encode(digest).decode("ascii")
            hashes.add(f"'sha256-{encoded}'")
    return tuple(sorted(hashes))


def frontend_dist_directory() -> Path:
    """Directory holding the prerendered UI bundle (may not exist)."""
    return PROJECT_ROOT / "dist"


def inline_script_hashes(frontend_dist: Path) -> tuple[str, ...]:
    """CSP source hashes for the inline scripts of the pages under ``frontend_dist``."""
    return _frontend_inline_script_hashes(frontend_dist)


def mount_static_assets(app: FastAPI) -> None:
    """Mount `/static` from ``static/``, falling back to the legacy ``img/`` folder."""
    static_dir = PROJECT_ROOT / "static"
    if not static_dir.exists():
        static_dir = PROJECT_ROOT / "img"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _frontend_page_paths(frontend_dist: Path) -> set[str]:
    """URL paths whose directory contains an ``index.html`` (excluding the bundle root).

    Walked once at startup so per-request canonicalization is a path lookup, not filesystem stats.
    """
    frontend_dist_root = frontend_dist.resolve()
    page_paths: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(frontend_dist_root):
        if "index.html" not in filenames:
            continue
        relative = os.path.relpath(dirpath, frontend_dist_root)
        if relative == ".":
            continue
        page_paths.add("/" + relative.replace("\\", "/"))
    return page_paths


def install_frontend(app: DocumentedFastAPI, frontend_dist: Path) -> None:
    """Serve the UI bundle and redirect bare page URLs to their canonical trailing-slash form.

    Prerendered pages carry relative asset URLs ("./_app/..."), and the boot script derives the
    asset base from the URL. ``app.frontend()`` serves ``dist/<path>/index.html`` for "/path"
    without a canonical redirect, so a directly entered or shared URL like "/view?endpoint=..."
    renders HTML whose modules resolve to "/view/_app/..." and 404: the page stays an inert
    "Loading..." skeleton. Answer GET/HEAD requests for paths whose directory contains an
    ``index.html`` with a redirect to the trailing-slash URL (query preserved), where relative
    assets resolve correctly.
    """
    if not frontend_dist.exists():
        logger.warning("Frontend dist directory not found at %s; UI pages are unavailable", frontend_dist)
        return

    app.frontend("/", directory=str(frontend_dist), fallback=None)
    frontend_page_paths = _frontend_page_paths(frontend_dist)
    app.add_middleware(FrontendRedirectMiddleware, page_paths=frontend_page_paths)


class FrontendRedirectMiddleware:
    """Pure ASGI middleware that answers a bare page URL with a redirect to its canonical form.

    ``app.middleware("http")`` wraps ``BaseHTTPMiddleware``, which copies the scope and re-serialises
    every body chunk of every request - including API and SSE requests, which are never a page - just
    to decide on a path. The decision is made from the scope before the handler runs, so a request
    that is not a page passes straight through.
    """

    def __init__(self, app: ASGIApp, page_paths: set[str]) -> None:
        self.app = app
        self._page_paths = page_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"GET", "HEAD"} or scope["path"].endswith("/"):
            await self.app(scope, receive, send)
            return

        normalized = "/" + posixpath.normpath(scope["path"].lstrip("/"))
        if normalized not in self._page_paths:
            await self.app(scope, receive, send)
            return

        redirect_url = scope["path"] + "/"
        query = scope.get("query_string")
        if isinstance(query, bytes) and query:
            redirect_url += f"?{query.decode()}"
        await RedirectResponse(url=redirect_url)(scope, receive, send)
