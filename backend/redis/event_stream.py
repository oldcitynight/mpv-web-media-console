from __future__ import annotations

import json
from typing import Any

from backend.events.envelope import EventEnvelope

from .client import RedisRuntimeClient
from .namespace import RedisNamespace


class EventStreamError(RuntimeError):
    """Raised when the canonical stream rejects an event or invariant."""


class EventStreamOrderError(EventStreamError):
    """Raised when a player event sequence is not monotonic in its stream."""


class EventStreamConflict(EventStreamError):
    """Raised when a deterministic event ID is reused with different content."""


class RedisEventStream:
    """Append canonical envelopes to the bounded, per-player Redis Stream."""

    DEFAULT_MAX_LENGTH = 10_000

    def __init__(
        self,
        client: RedisRuntimeClient,
        *,
        max_length: int = DEFAULT_MAX_LENGTH,
    ) -> None:
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
            raise ValueError("max_length must be a positive integer")
        self.client = client
        self.max_length = max_length

    def append(self, event: EventEnvelope) -> str:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be an EventEnvelope")
        if event.type == "state.snapshot" or event.sequence < 1:
            raise ValueError("only canonical sequenced player events can be stored")
        stream = RedisNamespace.events_stream(event.player_id)
        entry_id = f"{event.sequence}-0"
        fields = _event_fields(event)

        def write(redis_client: Any) -> str:
            try:
                returned_id = redis_client.xadd(
                    stream,
                    fields,
                    id=entry_id,
                    maxlen=self.max_length,
                    approximate=False,
                )
            except Exception as error:
                # A timeout can occur after Redis committed XADD. Read only the
                # deterministic ID to distinguish that case from lost data.
                try:
                    existing = redis_client.xrange(
                        stream,
                        min=entry_id,
                        max=entry_id,
                        count=1,
                    )
                except Exception:
                    raise error
                if existing:
                    existing_fields = _normalize_fields(existing[0][1])
                    if existing_fields == fields:
                        return entry_id
                    raise EventStreamConflict(
                        "deterministic event ID already contains different content"
                    ) from error
                if (
                    type(error).__name__ == "ResponseError"
                    and "equal or smaller" in str(error).lower()
                ):
                    raise EventStreamOrderError(
                        "event sequence must increase within a player stream"
                    ) from error
                raise error
            result = returned_id.decode("ascii") if isinstance(returned_id, bytes) else str(returned_id)
            if result != entry_id:
                raise EventStreamError("Redis returned an unexpected deterministic event ID")
            return result

        return self.client.execute(write)


def _event_fields(event: EventEnvelope) -> dict[str, str]:
    return {
        "schema_version": str(event.schema_version),
        "type": event.type,
        "sequence": str(event.sequence),
        "timestamp": str(event.timestamp),
        "player_id": event.player_id,
        "session_id": event.session_id,
        "correlation_id": event.correlation_id,
        "payload": json.dumps(
            dict(event.payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
    }


def _normalize_fields(fields: Any) -> dict[str, str]:
    return {
        key.decode("utf-8") if isinstance(key, bytes) else str(key):
        value.decode("utf-8") if isinstance(value, bytes) else str(value)
        for key, value in fields.items()
    }
