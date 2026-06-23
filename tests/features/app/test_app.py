"""App bootstrap and UI tests."""

from __future__ import annotations

from fastapi.testclient import TestClient


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
    assert 'href="/view?endpoint=/v1/info' in text
    assert 'href="/view?endpoint=/ua/state' in text
    assert 'href="/view?endpoint=/ua/connection' in text
    assert 'href="/view?endpoint=/ua/limits' in text
    assert 'href="/view?endpoint=/ua/metrics' in text


def test_landing_page_with_mcp_disabled(client_without_mcp: TestClient) -> None:
    response = client_without_mcp.get("/")
    assert response.status_code == 200
    text = response.text
    assert 'href="/docs"' in text
    assert 'href="/view?endpoint=/v1/info' in text
    assert 'href="/view?endpoint=/ua/state' in text
    assert 'href="/view?endpoint=/ua/connection' in text
    assert 'href="/view?endpoint=/ua/limits' in text
    assert 'href="/view?endpoint=/ua/metrics' in text
    assert 'href="/mcp"' not in text


def test_docs_csp_allows_swagger_cdn_assets(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy", "")
    assert "https://cdn.jsdelivr.net" in csp
    assert "https://fastapi.tiangolo.com" in csp


def test_landing_page_csp_remains_strict(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy", "")
    assert "https://cdn.jsdelivr.net" not in csp
    assert "https://fastapi.tiangolo.com" not in csp


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
