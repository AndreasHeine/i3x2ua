from fastapi import HTTPException


def i3x_http_error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    del details
    return HTTPException(
        status_code=status_code,
        detail={
            "success": False,
            "error": {"code": status_code, "message": message},
            "responseDetail": {
                "title": code,
                "status": status_code,
                "detail": message,
            },
        },
    )
