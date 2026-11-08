"""Error-envelope diagnostics (L4): producers' `details` must reach the client."""

from __future__ import annotations

from i3x_server.errors import format_error_details, i3x_http_error


def test_format_error_details_renders_key_value_pairs() -> None:
    assert format_error_details(None) is None
    assert format_error_details({}) is None
    assert format_error_details({"field": "limit", "value": 0}) == "field=limit, value=0"


def test_i3x_http_error_embeds_details_in_response_detail() -> None:
    exc = i3x_http_error(400, "InvalidArgument", "Invalid value for 'limit'", {"field": "limit", "value": 0})
    assert exc.status_code == 400
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Invalid value for 'limit'"
    assert detail["responseDetail"]["detail"] == "Invalid value for 'limit' (field=limit, value=0)"


def test_i3x_http_error_without_details_keeps_plain_detail() -> None:
    exc = i3x_http_error(404, "NotFound", "Object 'asset-root' not found")
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["error"]["message"] == "Object 'asset-root' not found"
    assert detail["responseDetail"]["detail"] == "Object 'asset-root' not found"
