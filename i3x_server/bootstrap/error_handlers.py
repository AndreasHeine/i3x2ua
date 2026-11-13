"""Exception handlers that render the i3X JSON error envelope.

Every failure the gateway can produce - conditional-request short circuits, request validation,
HTTP exceptions raised by handlers, application service errors, and unexpected exceptions - is
converted here into the ``{success, error, responseDetail}`` shape the spec requires, so the
composition root does not carry the envelope logic.
"""

from __future__ import annotations

import http
import logging

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from i3x_server.application.errors import ApplicationServiceError
from i3x_server.errors import format_error_details
from i3x_server.http_caching import NotModifiedError

logger = logging.getLogger(__name__)
# Bound the request-validation diagnostics embedded in `responseDetail.detail` so a
# pathological payload cannot blow up the error envelope.
_MAX_VALIDATION_DIAGNOSTICS = 10


def _status_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on the app."""

    @app.exception_handler(NotModifiedError)
    async def handle_not_modified(request: Request, exc: NotModifiedError) -> Response:
        del request, exc
        # ModelRevisionMiddleware adds the ETag and Cache-Control headers.
        return Response(status_code=304)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        # Keep the field diagnostics (loc/msg) in `responseDetail.detail`: for a bulk
        # API the message alone is the difference between a fast fix and a guessing loop.
        message = "Invalid request payload or parameters"
        errors = list(exc.errors())
        diagnostics: list[str] = []
        for item in errors[:_MAX_VALIDATION_DIAGNOSTICS]:
            loc = ".".join(str(token) for token in item.get("loc", ()))
            msg = str(item.get("msg", "invalid value"))
            diagnostics.append(f"{loc}: {msg}" if loc else msg)
        detail = message
        if diagnostics:
            suffix = "; ".join(diagnostics)
            if len(errors) > len(diagnostics):
                suffix += f"; (+{len(errors) - len(diagnostics)} more)"
            detail = f"{message} ({suffix})"
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
                    "detail": detail,
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

    @app.exception_handler(ApplicationServiceError)
    async def handle_application_service_error(
        request: Request,
        exc: ApplicationServiceError,
    ) -> JSONResponse:
        del request
        # `ApplicationServiceError.details` (e.g. {"cause": ...}) must reach the client;
        # it used to be stored but never rendered.
        detail = exc.message
        diagnostics = format_error_details(exc.details)
        if diagnostics is not None:
            detail = f"{exc.message} ({diagnostics})"
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {"code": int(exc.status_code), "message": exc.message},
                "responseDetail": {
                    "title": _status_title(int(exc.status_code)),
                    "status": int(exc.status_code),
                    "detail": detail,
                },
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
