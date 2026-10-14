from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable


class IdempotencyConflict(ValueError):
    """Raised when a key is reused for a different mutation."""


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    value: Any
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _Record:
    fingerprint: str
    value: Any


class IdempotencyStore:
    """Thread-safe request-key cache for mutation results."""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}
        self._lock = RLock()

    def execute(
        self,
        key: str | None,
        fingerprint: str,
        action: Callable[[], Any],
    ) -> IdempotencyResult:
        if key is None:
            return IdempotencyResult(action())
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError("idempotency key must be 1-256 characters")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("fingerprint must be a non-empty string")
        with self._lock:
            record = self._records.get(key)
            if record is not None:
                if record.fingerprint != fingerprint:
                    raise IdempotencyConflict("idempotency key conflicts with an earlier request")
                return IdempotencyResult(record.value, replayed=True)
            value = action()
            self._records[key] = _Record(fingerprint, value)
            return IdempotencyResult(value)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        return len(self._records)


ControlIdempotencyStore = IdempotencyStore
