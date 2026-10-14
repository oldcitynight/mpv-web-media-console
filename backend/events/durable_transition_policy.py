from __future__ import annotations

from collections.abc import Callable
from typing import Any


TELEMETRY_EVENT_TYPES = frozenset(
    {
        "time-pos",
        "time_pos",
        "volume",
        "speed",
        "audio-bitrate",
        "video-bitrate",
        "cache-buffering-state",
    }
)


class DurableTransitionPolicy:
    """Classify event types before they reach a durable SQLite writer."""

    def should_persist(self, event: Any) -> bool:
        event_type = self.event_type(event)
        return event_type not in TELEMETRY_EVENT_TYPES

    @staticmethod
    def event_type(event: Any) -> str:
        if isinstance(event, dict):
            event_type = event.get("type")
        else:
            event_type = getattr(event, "type", None)
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event must expose a non-empty type")
        return event_type


class DurableTransitionRecorder:
    """Send only durable transitions to an injected SQLite-backed writer."""

    def __init__(
        self,
        writer: Callable[[Any], None],
        *,
        policy: DurableTransitionPolicy | None = None,
    ) -> None:
        self._writer = writer
        self._policy = policy or DurableTransitionPolicy()
        self.persisted_count = 0
        self.suppressed_telemetry_count = 0

    def record(self, event: Any) -> bool:
        if not self._policy.should_persist(event):
            self.suppressed_telemetry_count += 1
            return False
        self._writer(event)
        self.persisted_count += 1
        return True


TransitionPersistencePolicy = DurableTransitionPolicy
