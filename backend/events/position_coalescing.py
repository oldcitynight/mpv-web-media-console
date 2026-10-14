from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class PositionUpdate:
    position: float
    timestamp: float


class PositionCoalescer:
    """Keep exact latest position while rate-limiting external emissions."""

    def __init__(self, interval_seconds: float = 0.1, *, clock: Callable[[], float] = time.monotonic) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        self.interval_seconds = float(interval_seconds)
        self._clock = clock
        self._latest: PositionUpdate | None = None
        self._last_emitted_at: float | None = None
        self._pending = False
        self.emitted_count = 0
        self.coalesced_count = 0

    @property
    def latest(self) -> PositionUpdate | None:
        return self._latest

    @property
    def pending(self) -> bool:
        return self._pending

    def update(self, position: float, *, timestamp: float | None = None) -> PositionUpdate | None:
        value = self._validate_position(position)
        now = self._clock() if timestamp is None else self._validate_timestamp(timestamp)
        self._latest = PositionUpdate(value, now)
        if self._last_emitted_at is None or now - self._last_emitted_at >= self.interval_seconds:
            self._pending = False
            self._last_emitted_at = now
            self.emitted_count += 1
            return self._latest
        self._pending = True
        self.coalesced_count += 1
        return None

    def flush(self, *, force: bool = False, timestamp: float | None = None) -> PositionUpdate | None:
        if not self._pending or self._latest is None:
            return None
        now = self._clock() if timestamp is None else self._validate_timestamp(timestamp)
        if not force and self._last_emitted_at is not None:
            if now - self._last_emitted_at < self.interval_seconds:
                return None
        self._pending = False
        self._last_emitted_at = now
        self.emitted_count += 1
        return self._latest

    @staticmethod
    def _validate_position(position: float) -> float:
        if isinstance(position, bool) or not isinstance(position, (int, float)):
            raise TypeError("position must be a number")
        value = float(position)
        if not math.isfinite(value) or value < 0:
            raise ValueError("position must be finite and non-negative")
        return value

    @staticmethod
    def _validate_timestamp(timestamp: float) -> float:
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise TypeError("timestamp must be a number")
        value = float(timestamp)
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return value


PositionUpdateCoalescer = PositionCoalescer
TimePosCoalescer = PositionCoalescer
