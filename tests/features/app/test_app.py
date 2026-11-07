"""App bootstrap and UI tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import fastapi_app


def _csp_source_tokens(csp: str) -> set[str]:
    tokens: set[str] = set()
    for directive in csp.split(";"):
        parts = directive.strip().split()
        if len(parts) > 1:
            tokens.update(parts[1:])
    return tokens


def test_get_model(client: TestClient) -> None:
    response = client.get("/model")
    assert response.status_code == 404


def test_get_data_value(client: TestClient) -> None:
    response = client.get("/data/property-abc")
    assert response.status_code == 404


def test_invoke_action(client: TestClient) -> None:
    response = client.post("/action/action-def/invoke", json={"args": [1, "x"]})
    assert response.status_code in {404, 405}


def test_landing_page_with_mcp_enabled(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    text = response.text
    assert "/static/logo-small.png" in text
    assert "i3X API Gateway for OPC UA" in text
    assert "Turn any OPC UA server into" in text
    assert 'href="/docs"' in text
    assert 'href="/view/?endpoint=/v1/info' in text
    assert 'href="/view/?endpoint=/ua/status' in text
    assert 'href="/view/?endpoint=/ua/connection' in text
    assert 'href="/view/?endpoint=/ua/limits' in text
    assert 'href="/view/?endpoint=/ua/metrics' in text
    assert 'href="/model-builder/"' in text


def test_landing_page_with_mcp_disabled(client_without_mcp: TestClient) -> None:
    response = client_without_mcp.get("/")
    assert response.status_code == 200
    text = response.text
    assert 'href="/docs"' in text
    assert 'href="/view/?endpoint=/v1/info' in text
    assert 'href="/view/?endpoint=/ua/status' in text
    assert 'href="/view/?endpoint=/ua/connection' in text
    assert 'href="/view/?endpoint=/ua/limits' in text
    assert 'href="/view/?endpoint=/ua/metrics' in text
    assert 'href="/mcp"' not in text


def test_docs_csp_allows_swagger_cdn_assets(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    csp_sources = _csp_source_tokens(response.headers.get("Content-Security-Policy", ""))
    assert "https://cdn.jsdelivr.net" in csp_sources
    assert "https://fastapi.tiangolo.com" in csp_sources


def test_landing_page_csp_remains_strict(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp_header = response.headers.get("Content-Security-Policy", "")
    csp_sources = _csp_source_tokens(csp_header)
    assert "https://cdn.jsdelivr.net" not in csp_sources
    assert "https://fastapi.tiangolo.com" not in csp_sources
    assert "script-src 'self' 'unsafe-inline'" not in csp_header
    assert "'sha256-" in csp_header


def test_frontend_pages_csp_uses_script_hashes(client: TestClient) -> None:
    for path in ("/", "/view?endpoint=/v1/info&label=Server%20Info"):
        response = client.get(path)
        assert response.status_code == 200
        csp_header = response.headers.get("Content-Security-Policy", "")
        directives = [part.strip() for part in csp_header.split(";") if part.strip()]
        script_directive = next((part for part in directives if part.startswith("script-src ")), "")
        assert "'unsafe-inline'" not in script_directive
        assert "'sha256-" in script_directive


def test_static_logo_is_served(client: TestClient) -> None:
    response = client.get("/static/logo-small.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_api_viewer_page(client: TestClient) -> None:
    response = client.get("/view?endpoint=/v1/info&label=Server%20Info")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Loading..." in response.text
    assert "Back" in response.text
    assert "/static/logo-small.png" in response.text
    assert "i3X API Gateway for OPC UA" in response.text


def test_api_viewer_escapes_query_inputs(client: TestClient) -> None:
    response = client.get('/view?endpoint=";alert(1);//&label=%3Cscript%3Ealert(1)%3C/script%3E')
    assert response.status_code == 200
    text = response.text

    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" not in text
    assert '";alert(1);//' not in text


def test_mcp_tools_viewer_page(client: TestClient) -> None:
    response = client.get("/mcp-tools-viewer")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "MCP Tools" in response.text
    assert "Back" in response.text
    assert "/static/logo-small.png" in response.text
    assert "i3X API Gateway for OPC UA" in response.text


def test_frontend_paths_redirect_to_canonical_trailing_slash(client: TestClient) -> None:
    """Directly entered page URLs redirect to the trailing-slash form with query preserved.

    Prerendered HTML uses relative asset URLs, so `/view` (no slash) would render a page
    whose modules resolve to `/view/_app/...` and 404, leaving an inert skeleton.
    """
    no_follow = TestClient(fastapi_app(client), follow_redirects=False)

    response = no_follow.get("/view?endpoint=/v1/info&label=Server%20Info")
    assert response.status_code == 307
    assert response.headers["location"] == "/view/?endpoint=/v1/info&label=Server%20Info"

    for path, location in (("/model-builder", "/model-builder/"), ("/mcp-tools-viewer", "/mcp-tools-viewer/")):
        redirect = no_follow.get(path)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == location

    # Canonical URLs serve the page directly; the redirect chain ends at 200 HTML.
    canonical = no_follow.get("/view/?endpoint=/v1/info")
    assert canonical.status_code == 200
    assert canonical.headers["content-type"].startswith("text/html")
    followed = client.get("/view?endpoint=/v1/info&label=Server%20Info")
    assert followed.status_code == 200

    # API paths and asset files are untouched: only directories with an index.html redirect.
    assert no_follow.get("/v1/info").status_code == 200
    assert no_follow.get("/ua/status").status_code == 200
    assert no_follow.get("/_app/").status_code != 307
    assert no_follow.get("/model").status_code == 404

    # Redirects are limited to safe methods; POSTs pass through to their routes.
    assert no_follow.post("/mcp/call", json={"tool": "getServerInfoV1"}).status_code != 307
