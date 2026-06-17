"""Application-layer exception types.

These exceptions are framework-agnostic and translated to HTTP at the
presentation/bootstrap boundary.
"""

from __future__ import annotations


class ApplicationServiceError(Exception):
    """Framework-agnostic application service error."""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details or {}
