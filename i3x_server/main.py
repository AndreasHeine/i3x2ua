from __future__ import annotations

import asyncio
import http
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from html import escape
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from i3x_server.api.mcp import router as mcp_router
from i3x_server.api.ua import router as ua_router
from i3x_server.api.v1 import router as v1_router
from i3x_server.config.settings import settings
from i3x_server.mcp import build_mcp_tools, get_api_prefix
from i3x_server.model.builder import ModelBuilder
from i3x_server.opcua.client import OpcUaClient
from i3x_server.schemas.state import BuildResult
from i3x_server.subscriptions.service import SubscriptionService

logger = logging.getLogger(__name__)


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _configure_logging() -> None:
    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.WARNING)


async def _run_model_preload(app: FastAPI) -> None:
    try:
        started = asyncio.get_running_loop().time()
        app.state.opcua_client.reset_runtime_metrics()
        build_started = asyncio.get_running_loop().time()
        preload = await app.state.model_builder.build()
        build_duration_s = asyncio.get_running_loop().time() - build_started
        app.state.model_cache = preload
        metrics = app.state.opcua_client.snapshot_runtime_metrics()
        duration_s = asyncio.get_running_loop().time() - started
        logger.info(
            "Model preload finished nodes=%d roots=%d properties=%d actions=%d build_s=%.3f total_s=%.3f",
            len(preload.nodes_by_id),
            len(preload.root_ids),
            len(preload.property_to_node),
            len(preload.action_to_method),
            build_duration_s,
            duration_s,
        )
        logger.info(
            "Model preload metrics duration_s=%.3f rpc_calls=%d browse_calls=%d browse_nodes=%d "
            "browse_initial_references=%d browse_next_calls=%d browse_next_references=%d "
            "read_calls=%d read_nodes=%d history_read_calls=%d history_read_nodes=%d method_calls=%d "
            "browse_tree_calls=%d browse_tree_nodes=%d namespace_reads=%d namespaces=%d "
            "namespace_info_builds=%d namespace_info_count=%d object_type_reads=%d object_type_count=%d",
            duration_s,
            metrics.browse_calls
            + metrics.browse_next_calls
            + metrics.read_calls
            + metrics.history_read_calls
            + metrics.method_calls,
            metrics.browse_calls,
            metrics.browse_nodes,
            metrics.browse_initial_references,
            metrics.browse_next_calls,
            metrics.browse_next_references,
            metrics.read_calls,
            metrics.read_nodes,
            metrics.history_read_calls,
            metrics.history_read_nodes,
            metrics.method_calls,
            metrics.browse_tree_calls,
            metrics.browse_tree_nodes_last,
            metrics.namespace_reads,
            metrics.namespace_count_last,
            metrics.namespace_info_builds,
            metrics.namespace_info_count_last,
            metrics.object_type_reads,
            metrics.object_type_count_last,
        )
        logger.info("Namespace metadata preload skipped at startup; loading on first request")
    except Exception:
        logger.exception("Model preload failed")
        if settings.fail_startup_on_model_preload_error and settings.model_preload_blocking:
            raise
        logger.warning("Continuing without preloaded model; model will build lazily on demand")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    mcp_enabled = _env_flag("I3X_ENABLE_MCP")
    opcua_client = OpcUaClient(
        endpoint=settings.opcua_endpoint,
        username=settings.opcua_username,
        password=settings.opcua_password,
        security_mode=settings.opcua_security_mode,
        security_policy=settings.opcua_security_policy,
        client_cert_path=settings.opcua_client_cert_path,
        client_key_path=settings.opcua_client_key_path,
        client_key_password=settings.opcua_client_key_password,
        server_cert_path=settings.opcua_server_cert_path,
        browse_concurrency=settings.opcua_browse_concurrency,
        metadata_cache_ttl_seconds=settings.opcua_metadata_cache_ttl_seconds,
    )
    skip_connect = os.getenv("I3X_SKIP_OPCUA_CONNECT", "0") == "1"
    logger.info(
        "App startup opcua_endpoint=%s skip_connect=%s log_level=%s "
        "browse_concurrency=%d metadata_cache_ttl_seconds=%d auth_configured=%s security_mode=%s mcp_enabled=%s",
        settings.opcua_endpoint,
        skip_connect,
        settings.log_level,
        settings.opcua_browse_concurrency,
        settings.opcua_metadata_cache_ttl_seconds,
        bool(settings.opcua_username and settings.opcua_password),
        settings.opcua_security_mode,
        mcp_enabled,
    )
    if not skip_connect:
        await opcua_client.connect()
    app.state.opcua_client = opcua_client
    app.state.model_builder = ModelBuilder(opcua_client)
    app.state.object_type_context_cache = None
    app.state.subscription_service = SubscriptionService(
        opcua_client=opcua_client,
        interval_seconds=settings.subscription_interval_seconds,
        max_updates_per_subscription=settings.subscription_max_updates,
        ttl_seconds=settings.subscription_ttl_seconds,
    )
    app.state.model_lock = asyncio.Lock()
    app.state.model_preload_task = None
    if mcp_enabled:
        openapi_spec = app.openapi()
        app.state.mcp_tools = build_mcp_tools(openapi_spec)
        app.state.mcp_api_prefix = get_api_prefix(openapi_spec)
    if skip_connect:
        app.state.model_cache = BuildResult(
            nodes_by_id={},
            root_ids=[],
            children_by_id={},
            instances_by_type_id={},
            property_to_node={},
            action_to_method={},
        )
    else:
        app.state.model_cache = None
        if settings.model_preload_on_startup:
            if settings.model_preload_blocking:
                logger.info("Model preload at startup enabled (blocking)")
                await _run_model_preload(app)
            else:
                logger.info("Model preload at startup enabled (background)")
                app.state.model_preload_task = asyncio.create_task(_run_model_preload(app))
    try:
        yield
    finally:
        logger.info("App shutdown started")
        preload_task = getattr(app.state, "model_preload_task", None)
        if preload_task is not None and not preload_task.done():
            preload_task.cancel()
            with suppress(asyncio.CancelledError):
                await preload_task
        await app.state.subscription_service.close()
        if not skip_connect:
            await opcua_client.disconnect()
        logger.info("App shutdown finished")


def create_app() -> FastAPI:
    mcp_enabled = _env_flag("I3X_ENABLE_MCP")
    description = (
        "Industrial Information Interface eXchange API - 1.0. "
        "Scope: read/query/subscribe are implemented; update/write operations are optional "
        "and may return 501 Not Implemented. "
        "MCP endpoints are optional and available only when I3X_ENABLE_MCP=1."
    )
    app = FastAPI(title="i3X API 1.0", version="1.0", description=description, lifespan=lifespan)
    app.add_middleware(GZipMiddleware, minimum_size=1)

    openapi_doc_path = Path(__file__).resolve().parents[1] / "openapi.json"
    openapi_override: dict[str, object] | None = None

    def custom_openapi() -> dict[str, object]:
        nonlocal openapi_override
        if openapi_override is None:
            openapi_override = json.loads(openapi_doc_path.read_text(encoding="utf-8"))
        return openapi_override

    app.openapi = custom_openapi  # type: ignore[method-assign]

    project_root = Path(__file__).resolve().parents[1]
    static_dir = project_root / "static"
    # Backward-compatible fallback for local setups that still use the legacy img folder.
    if not static_dir.exists():
        static_dir = project_root / "img"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def landing() -> HTMLResponse:
        openapi_spec = custom_openapi()
        title = "i3X API Gateway"
        description = ""
        if isinstance(openapi_spec, dict):
            info = openapi_spec.get("info")
            if isinstance(info, dict):
                title = str(info.get("title", title))
                description = str(info.get("description", ""))

        links: list[tuple[str, str]] = [
            ("API Documentation", "/docs"),
            ("i3X Server Info", "/view?endpoint=/v1/info&label=i3X%20Server%20Info"),
            ("OPC UA State", "/view?endpoint=/ua/state&label=OPC%20UA%20State"),
            ("OPC UA Connection", "/view?endpoint=/ua/connection&label=OPC%20UA%20Connection"),
            ("OPC UA Limits", "/view?endpoint=/ua/limits&label=OPC%20UA%20Limits"),
            ("OPC UA Metrics", "/view?endpoint=/ua/metrics&label=OPC%20UA%20Metrics"),
        ]
        if mcp_enabled:
            links.append(("MCP Tools", "/mcp-tools-viewer"))

        cards = "".join(
            f'<a class="card" href="{href}"><span>{label}</span><span class="arrow">&rarr;</span></a>'
            for label, href in links
        )

        html = f"""
<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>The i3X API Gateway for OPC UA</title>
<style>
    :root {{
        --bg-a: #f7f9fc;
        --bg-b: #e9eef7;
        --panel: #ffffffcc;
        --text: #162033;
        --muted: #5e6b80;
        --line: #d7e0ef;
        --accent: #0b6ef3;
        --accent-soft: #e8f1ff;
        --radius: 14px;
        --shadow: 0 14px 40px rgba(16, 29, 56, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; margin: 0; }}
    body {{
        font-family: "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
        color: var(--text);
        background: radial-gradient(circle at 15% 10%, #ffffff, var(--bg-a) 40%, var(--bg-b) 100%);
        display: grid;
        place-items: center;
        padding: 24px;
    }}
    .shell {{
        width: min(920px, 100%);
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: calc(var(--radius) + 4px);
        box-shadow: var(--shadow);
        backdrop-filter: blur(6px);
        overflow: hidden;
    }}
    .hero {{
        padding: 38px 28px 24px;
        display: grid;
        place-items: center;
        gap: 14px;
        text-align: center;
    }}
    .logo {{ width: min(230px, 60vw); height: auto; display: block; }}
    h1 {{ margin: 0; font-weight: 650; letter-spacing: 0.2px; font-size: clamp(1.2rem, 1rem + 1vw, 1.8rem); }}
    p {{ margin: 0; color: var(--muted); max-width: 60ch; line-height: 1.45; }}
    .grid {{ display: grid; gap: 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 0 20px 22px; }}
    .card {{
        text-decoration: none;
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: var(--radius);
        padding: 12px 14px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        background: #fff;
        transition: border-color .2s ease, transform .2s ease, background-color .2s ease;
    }}
    .card:hover {{ border-color: #bdd4fb; background: var(--accent-soft); transform: translateY(-1px); }}
    .arrow {{ color: var(--accent); font-size: 1.1rem; }}
    @media (max-width: 700px) {{ .grid {{ grid-template-columns: 1fr; }} .hero {{ padding-top: 28px; }} }}
</style>
</head>
<body>
<main class=\"shell\">
    <section class=\"hero\">
        <img class=\"logo\" src=\"/static/logo-small.png\" alt=\"i3X logo\" />
        <h1>{title}</h1>
        <p>{description}</p>
    </section>
    <section class=\"grid\">{cards}</section>
</main>
</body>
</html>
"""
        return HTMLResponse(content=html)

    @app.get("/view", response_class=HTMLResponse, include_in_schema=False)
    async def api_viewer() -> HTMLResponse:
        openapi_spec = custom_openapi()
        title = "i3X API Gateway for OPC UA"
        if isinstance(openapi_spec, dict):
            info = openapi_spec.get("info")
            if isinstance(info, dict):
                title = str(info.get("title", title))

        safe_title = escape(title)

        html = f"""
<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>API Result - The i3X API Gateway for OPC UA</title>
    <style>
        :root {{
            --bg-a: #f7f9fc;
            --bg-b: #e9eef7;
            --panel: #ffffffcc;
            --text: #162033;
            --muted: #5e6b80;
            --line: #d7e0ef;
            --accent: #0b6ef3;
            --accent-soft: #e8f1ff;
            --radius: 14px;
            --shadow: 0 14px 40px rgba(16, 29, 56, 0.08);
        }}
        * {{ box-sizing: border-box; }}
        html, body {{ height: 100%; margin: 0; }}
        body {{
            font-family: "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
            color: var(--text);
            background: radial-gradient(circle at 15% 10%, #fff, var(--bg-a) 40%, var(--bg-b) 100%);
            padding: 24px;
        }}
        .container {{ max-width: 920px; margin: 0 auto; }}
        .hero, .header, .code-block {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: calc(var(--radius) + 4px);
            box-shadow: var(--shadow);
            backdrop-filter: blur(6px);
        }}
        .hero {{
            padding: 28px;
            margin-bottom: 20px;
            display: grid;
            place-items: center;
            gap: 12px;
            text-align: center;
        }}
        .logo {{ width: min(120px, 30vw); height: auto; }}
        .hero-title {{ margin: 0; font-weight: 650; font-size: 1.4rem; }}
        .header {{
            padding: 28px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        h1 {{ margin: 0; font-weight: 650; font-size: 1.6rem; }}
        .back-link {{ text-decoration: none; color: var(--accent); font-size: 0.95rem; }}
        .code-block {{ padding: 12px; }}
        pre {{
            margin: 0;
            padding: 20px;
            overflow: auto;
            line-height: 1.5;
            font-family: "Monaco", "Menlo", "Ubuntu Mono", monospace;
            font-size: 0.9rem;
            background: #fff;
            border: 1px solid var(--line);
            border-radius: var(--radius);
        }}
        .loading {{ padding: 24px; text-align: center; color: var(--muted); }}
        .error {{ padding: 24px; color: #dc2626; }}
        pre:not(.loading):not(.error):hover {{ background: var(--accent-soft); }}
    </style>
</head>
<body>
    <div class=\"container\">
        <div class=\"hero\">
            <img class=\"logo\" src=\"/static/logo-small.png\" alt=\"i3X logo\" />
            <h2 class="hero-title">{safe_title}</h2>
        </div>
        <div class=\"header\">
            <h1 id="viewer-title">Loading...</h1>
            <a class=\"back-link\" href=\"/\">&larr; Back</a>
        </div>
        <div class=\"code-block\"><pre id=\"result\" class=\"loading\">Loading...</pre></div>
    </div>
    <script>
        const knownViewTargets = {{
            '/v1/info': 'i3X Server Info',
            '/ua/state': 'OPC UA State',
            '/ua/connection': 'OPC UA Connection',
            '/ua/limits': 'OPC UA Limits',
            '/ua/metrics': 'OPC UA Metrics',
        }};
        const params = new URLSearchParams(window.location.search);
        const requested = params.get('endpoint') || '/v1/info';
        const endpoint = Object.prototype.hasOwnProperty.call(knownViewTargets, requested)
            ? requested
            : '/v1/info';
        document.getElementById('viewer-title').textContent = knownViewTargets[endpoint] || 'API Result';

        fetch(endpoint)
            .then((r) => r.json())
            .then((d) => {{
                const p = document.getElementById('result');
                p.textContent = JSON.stringify(d, null, 2);
                p.className = '';
            }})
            .catch((e) => {{
                const p = document.getElementById('result');
                p.textContent = 'Error: ' + e.message;
                p.className = 'error';
            }});
    </script>
</body>
</html>
"""
        return HTMLResponse(content=html)

    @app.get("/mcp-tools-viewer", response_class=HTMLResponse, include_in_schema=False)
    async def mcp_tools_viewer() -> HTMLResponse:
        openapi_spec = custom_openapi()
        title = "i3X API Gateway for OPC UA"
        if isinstance(openapi_spec, dict):
            info = openapi_spec.get("info")
            if isinstance(info, dict):
                title = str(info.get("title", title))
        safe_title = escape(title)

        html = f"""
<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>MCP Tools</title>
    <style>
        :root {{
            --bg-a: #f7f9fc;
            --bg-b: #e9eef7;
            --panel: #ffffffcc;
            --text: #162033;
            --muted: #5e6b80;
            --line: #d7e0ef;
            --accent: #0b6ef3;
            --accent-soft: #e8f1ff;
            --radius: 14px;
            --shadow: 0 14px 40px rgba(16, 29, 56, 0.08);
        }}
        * {{ box-sizing: border-box; }}
        html, body {{ height: 100%; margin: 0; }}
        body {{
            font-family: "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
            color: var(--text);
            background: radial-gradient(circle at 15% 10%, #fff, var(--bg-a) 40%, var(--bg-b) 100%);
            padding: 24px;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        .hero, .header, .table-wrap {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: calc(var(--radius) + 4px);
            box-shadow: var(--shadow);
            backdrop-filter: blur(6px);
        }}
        .hero {{
            padding: 24px;
            margin-bottom: 16px;
            display: grid;
            place-items: center;
            gap: 10px;
            text-align: center;
        }}
        .logo {{ width: min(110px, 30vw); height: auto; }}
        .hero-title {{ margin: 0; font-weight: 650; font-size: 1.2rem; }}
        .header {{
            padding: 20px 24px;
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        h1 {{ margin: 0; font-size: 1.5rem; font-weight: 650; }}
        .back-link {{ text-decoration: none; color: var(--accent); font-weight: 500; }}
        table {{
            width: 100%;
            border-collapse: separate;
            border-spacing: 0 10px;
            padding: 0 12px 12px;
        }}
        th {{
            text-align: left;
            vertical-align: top;
            padding: 10px 12px;
            font-size: 0.9rem;
            color: #334155;
            font-weight: 650;
            border-bottom: 1px solid var(--line);
        }}
        td {{
            text-align: left;
            vertical-align: top;
            padding: 12px 14px;
            font-size: 0.95rem;
            background: #fff;
            border-top: 1px solid var(--line);
            border-bottom: 1px solid var(--line);
        }}
        tbody tr td:first-child {{
            border-left: 1px solid var(--line);
            border-top-left-radius: 12px;
            border-bottom-left-radius: 12px;
        }}
        tbody tr td:last-child {{
            border-right: 1px solid var(--line);
            border-top-right-radius: 12px;
            border-bottom-right-radius: 12px;
        }}
        tbody tr:hover td {{ background: var(--accent-soft); }}
        .tool-name {{ font-family: "Consolas", "Monaco", monospace; color: #0b6ef3; font-weight: 600; }}
        .schema {{
            margin: 0;
            white-space: pre-wrap;
            font-family: "Consolas", "Monaco", monospace;
            font-size: 0.82rem;
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 10px;
            max-height: 160px;
            overflow: auto;
        }}
        .loading, .error {{ padding: 20px; }}
        .error {{ color: #b91c1c; }}
    </style>
</head>
<body>
    <div class=\"container\">
        <div class=\"hero\">
            <img class=\"logo\" src=\"/static/logo-small.png\" alt=\"i3X logo\" />
            <h2 class=\"hero-title\">{safe_title}</h2>
        </div>
        <div class=\"header\">
            <h1>MCP Tools</h1>
            <a class=\"back-link\" href=\"/\">&larr; Back</a>
        </div>
        <div class=\"table-wrap\"><div id=\"tools-content\" class=\"loading\">Loading tools...</div></div>
    </div>
    <script>
        function esc(v) {{
            return String(v)
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }}
        fetch('/mcp/tools')
            .then((r) => r.json())
            .then((d) => {{
                const m = document.getElementById('tools-content');
                const t = d.tools || {{}};
                const ns = Object.keys(t);
                if (ns.length === 0) {{
                    m.className = 'error';
                    m.textContent = 'No MCP tools available.';
                    return;
                }}
                let rows = '';
                for (const n of ns) {{
                    const it = t[n] || {{}};
                    const schema = it.inputSchema || it.input_schema || {{}};
                    rows += `<tr>`
                        + `<td class=\"tool-name\">${{esc(n)}}</td>`
                        + `<td>${{esc(it.description || 'No description available')}}</td>`
                        + `<td><pre class=\"schema\">${{esc(JSON.stringify(schema, null, 2))}}</pre></td>`
                        + `</tr>`;
                }}
                m.className = '';
                m.innerHTML = `<table>`
                    + `<thead><tr>`
                    + `<th style=\"width:24%\">Tool</th>`
                    + `<th style=\"width:36%\">Description</th>`
                    + `<th style=\"width:40%\">Input Schema</th>`
                    + `</tr></thead>`
                    + `<tbody>${{rows}}</tbody>`
                    + `</table>`;
            }})
            .catch((e) => {{
                const m = document.getElementById('tools-content');
                m.className = 'error';
                m.textContent = 'Error loading tools: ' + e.message;
            }});
    </script>
</body>
</html>
"""
        return HTMLResponse(content=html)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request, exc
        message = "Invalid request payload or parameters"
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": 400,
                    "message": message,
                },
                "responseDetail": {
                    "title": _status_title(400),
                    "status": 400,
                    "detail": message,
                },
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        detail = exc.detail
        response_detail: dict[str, object] | None = None
        if isinstance(detail, dict):
            error = detail.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", "Request failed"))
            else:
                message = str(detail.get("message", "Request failed"))
            raw_response_detail = detail.get("responseDetail")
            if isinstance(raw_response_detail, dict):
                response_detail = {
                    "title": str(raw_response_detail.get("title", "Error")),
                    "status": int(raw_response_detail.get("status", exc.status_code)),
                    "detail": str(raw_response_detail.get("detail", message)),
                }
        else:
            message = str(detail) if detail else "Request failed"
        if response_detail is None:
            response_detail = {
                "title": _status_title(int(exc.status_code)),
                "status": int(exc.status_code),
                "detail": message,
            }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": int(exc.status_code), "message": message},
                "responseDetail": response_detail,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception method=%s path=%s", request.method, request.url.path)
        message = "Internal server error"
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {"code": 500, "message": message},
                "responseDetail": {
                    "title": _status_title(500),
                    "status": 500,
                    "detail": message,
                },
            },
        )

    @app.middleware("http")
    async def add_security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if response.headers.get("content-type", "").startswith("text/html"):
            is_docs_route = request.url.path.startswith("/docs") or request.url.path.startswith(
                "/redoc"
            )
            csp_value = (
                "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                if is_docs_route
                else "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            response.headers.setdefault(
                "Content-Security-Policy",
                csp_value,
            )
        return response

    @app.middleware("http")
    async def log_http_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started = perf_counter()
        try:
            response = await call_next(request)
        except RuntimeError as exc:
            if str(exc) == "No response returned." and await request.is_disconnected():
                logger.info(
                    "HTTP request aborted by client method=%s path=%s duration_s=%.3f",
                    request.method,
                    request.url.path,
                    perf_counter() - started,
                )
                return Response(status_code=499)
            raise
        logger.info(
            "HTTP request method=%s path=%s status=%s duration_s=%.3f",
            request.method,
            request.url.path,
            response.status_code,
            perf_counter() - started,
        )
        return response

    app.include_router(v1_router)
    app.include_router(ua_router)
    if mcp_enabled:
        app.include_router(mcp_router)
    return app


app = create_app()
