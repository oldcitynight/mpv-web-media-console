from __future__ import annotations

import logging
import re


_AUTHORIZATION = re.compile(
    r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)"
    r"(?:(?:bearer|basic)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+(?:\s+[^\s,;&}\]]+)?)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b((?:access[_-]?token|refresh[_-]?token|token|secret|password|passwd|"
    r"api[_-]?key|client[_-]?secret)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)
_URL_CREDENTIALS = re.compile(r"(?i)\b((?:rediss?|https?)://)[^/@\s]+@")
_WEBSOCKET_TOKEN = re.compile(r"(?i)\bmpv-auth\.[A-Za-z0-9._~-]+")
_REDACTED = "[REDACTED]"


def redact_sensitive_text(value: str) -> str:
    """Remove common bearer, key/value, URL, and WebSocket-token forms."""

    value = _URL_CREDENTIALS.sub(rf"\1{_REDACTED}@", value)
    value = _AUTHORIZATION.sub(rf"\1{_REDACTED}", value)
    value = _BEARER.sub(rf"\1{_REDACTED}", value)
    value = _SENSITIVE_ASSIGNMENT.sub(rf"\1{_REDACTED}", value)
    return _WEBSOCKET_TOKEN.sub(f"mpv-auth.{_REDACTED}", value)


class SecretRedactingFilter(logging.Filter):
    """Sanitize log messages and exception text before configured handlers emit them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            exception_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_sensitive_text(exception_text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_sensitive_text(record.stack_info)
        return True


def install_secret_redaction_filters() -> None:
    """Attach redaction to handlers already configured by the host application."""

    loggers = (
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("redis"),
    )
    visited: set[int] = set()
    for logger in loggers:
        for handler in logger.handlers:
            if id(handler) in visited:
                continue
            visited.add(id(handler))
            if not any(isinstance(item, SecretRedactingFilter) for item in handler.filters):
                handler.addFilter(SecretRedactingFilter())
