from __future__ import annotations

import time
from threading import RLock
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from backend.events.envelope import EventEnvelope


class WebSocketAuthError(PermissionError):
    """Raised when a WebSocket connection lacks valid authentication."""


class HistoryUnavailable(RuntimeError):
    """Raised when reconnect asks for a sequence older than retained history."""


class EventHistory:
    def __init__(self, maxsize: int = 256) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._events: deque[EventEnvelope] = deque(maxlen=maxsize)
        self._lock = RLock()

    def append(self, event: EventEnvelope) -> None:
        with self._lock:
            if self._events and event.sequence <= self._events[-1].sequence:
                raise ValueError("event sequence must be strictly increasing")
            self._events.append(event)

    def replay_after(self, sequence: int | None) -> tuple[EventEnvelope, ...]:
        with self._lock:
            if sequence is None:
                return tuple(self._events)
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
                raise ValueError("last_sequence must be a non-negative integer")
            oldest_predecessor = self._events[0].payload.get("stream_previous_sequence") if self._events else None
            retained_floor = oldest_predecessor if isinstance(oldest_predecessor,int) and not isinstance(oldest_predecessor,bool) else self._events[0].sequence - 1 if self._events else 0
            if self._events and sequence < retained_floor:
                raise HistoryUnavailable("requested event history is no longer retained")
            if sequence > (self._events[-1].sequence if self._events else 0):
                raise HistoryUnavailable("requested cursor belongs to a reset event stream")
            return tuple(event for event in self._events if event.sequence > sequence)

    @property
    def latest_sequence(self) -> int | None:
        with self._lock:
            return self._events[-1].sequence if self._events else None


class WebSocketContract:
    """Prepare authenticated snapshot-plus-ordered-event WebSocket payloads."""

    def __init__(
        self,
        *,
        player_id: str,
        session_id: str,
        history: EventHistory,
        snapshot: Callable[[], dict[str, Any]],
        token_validator: Callable[[str | None], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.player_id = player_id
        self.session_id = session_id
        self.history = history
        self._snapshot = snapshot
        self._token_validator = token_validator
        self._clock = clock

    def connect(
        self,
        token: str | None,
        *,
        last_sequence: int | None = None,
    ) -> tuple[EventEnvelope, ...]:
        if not self._token_validator(token):
            raise WebSocketAuthError("WebSocket authentication failed")
        if last_sequence is not None:
            return self.history.replay_after(last_sequence)
        snapshot_sequence = self.history.latest_sequence or 0
        snapshot = EventEnvelope(
            schema_version=1,
            type="state.snapshot",
            sequence=snapshot_sequence,
            timestamp=self._clock(),
            player_id=self.player_id,
            session_id=self.session_id,
            correlation_id="websocket-snapshot",
            payload=self._snapshot(),
        )
        return (snapshot,)

    @staticmethod
    def encode(events: Iterable[EventEnvelope]) -> tuple[bytes, ...]:
        return tuple(event.encode() for event in events)
