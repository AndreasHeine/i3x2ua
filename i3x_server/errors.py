"""The shared error shape: ``i3x_http_error`` builds the documented error envelope for a status
code and detail payload."""

import http

from fastapi import HTTPException


def format_error_details(details: dict[str, object] | None) -> str | None:
    """Render optional diagnostic details as a compact `key=value` suffix.

    Producers (``_raise_invalid_argument``, ``_raise_opcua_error``, service
    errors) pass field/value pairs that used to be dropped; the i3X envelope
    carries them in ``responseDetail.detail`` so callers can see *which* field
    failed instead of guessing.
    """
    if not details:
        return None
    return ", ".join(f"{key}={value}" for key, value in details.items())


def i3x_http_error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    try:
        title = http.HTTPStatus(status_code).phrase
    except ValueError:
        title = code
    detail = message
    diagnostics = format_error_details(details)
    if diagnostics is not None:
        detail = f"{message} ({diagnostics})"
    return HTTPException(
        status_code=status_code,
        detail={
            "success": False,
            "error": {"code": status_code, "message": message},
            "responseDetail": {
                "title": title,
                "status": status_code,
                "detail": detail,
            },
        },
    )
