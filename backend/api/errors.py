from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    FORBIDDEN_MEDIA = "forbidden_media"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PLAYER_UNAVAILABLE = "player_unavailable"
    INTERNAL = "internal_error"


_SENSITIVE_KEY = re.compile(r"(?:token|secret|password|authorization|path|trace|stack)", re.I)


def _safe_details(details: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if details is None:
        return None
    return {
        key: value
        for key, value in details.items()
        if isinstance(key, str) and not _SENSITIVE_KEY.search(key)
    }


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    code: ErrorCode
    correlation_id: str
    message: str
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.correlation_id:
            raise ValueError("correlation_id must not be empty")
        if not self.message:
            raise ValueError("message must not be empty")
        object.__setattr__(self, "details", _safe_details(self.details))

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code.value,
            "correlation_id": self.correlation_id,
            "message": self.message,
        }
        if self.details:
            value["details"] = dict(self.details)
        return value


class StructuredApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        correlation_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.response = ErrorResponse(
            code=code,
            correlation_id=correlation_id or uuid.uuid4().hex,
            message=message,
            details=dict(details) if details is not None else None,
        )
        super().__init__(message)


def public_error(
    error: Exception,
    *,
    correlation_id: str | None = None,
    code: ErrorCode = ErrorCode.INTERNAL,
) -> ErrorResponse:
    """Convert an exception into a safe response without exposing its text."""

    if isinstance(error, StructuredApiError):
        return error.response
    return ErrorResponse(
        code=code,
        correlation_id=correlation_id or uuid.uuid4().hex,
        message="The request could not be completed.",
    )


def register_error_handlers(app: Any) -> None:
    """Install JSON error handlers on a FastAPI-compatible application."""

    from fastapi.responses import JSONResponse

    status_codes = {
        ErrorCode.INVALID_REQUEST: 400,
        ErrorCode.FORBIDDEN_MEDIA: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.CONFLICT: 409,
        ErrorCode.PLAYER_UNAVAILABLE: 503,
        ErrorCode.INTERNAL: 500,
    }

    @app.exception_handler(StructuredApiError)
    async def handle_structured_error(_request: Any, error: StructuredApiError) -> JSONResponse:
        return JSONResponse(
            status_code=status_codes[error.response.code],
            content=error.response.to_dict(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Any, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=public_error(error).to_dict())
