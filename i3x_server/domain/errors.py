"""Domain-level exception types.

Framework-agnostic by design: the transport layer translates them into the i3X error envelope.
Infrastructure raises them when an admission rule is violated, so the rule stays expressible
without importing FastAPI into the infrastructure layer.
"""

from __future__ import annotations


class AdmissionLimitError(Exception):
    """A gateway admission limit (count cap, node budget) was exceeded.

    ``status_code``/``error_code`` carry the intent of the violated rule; the presentation
    layer renders them through the regular error envelope.
    """

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
