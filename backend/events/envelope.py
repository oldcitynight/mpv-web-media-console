from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Mapping


class EnvelopeError(ValueError):
    """Raised when an internal event envelope is malformed."""


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    schema_version: int
    type: str
    sequence: int
    timestamp: str | int | float
    player_id: str
    session_id: str
    correlation_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise EnvelopeError("schema_version must be an integer")
        if self.schema_version < 1:
            raise EnvelopeError("schema_version must be positive")
        if not isinstance(self.type, str) or not self.type:
            raise EnvelopeError("type must be a non-empty string")
        minimum_sequence = 0 if self.type == "state.snapshot" else 1
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < minimum_sequence
        ):
            raise EnvelopeError("sequence must be non-negative for snapshots and positive for events")
        if isinstance(self.timestamp, str):
            if not self.timestamp:
                raise EnvelopeError("timestamp must not be empty")
        elif isinstance(self.timestamp, (int, float)) and not isinstance(self.timestamp, bool):
            if not math.isfinite(float(self.timestamp)):
                raise EnvelopeError("timestamp must be finite")
        else:
            raise EnvelopeError("timestamp must be an ISO string or finite number")
        for name in ("player_id", "session_id", "correlation_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise EnvelopeError(f"{name} must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise EnvelopeError("payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))

    @property
    def event_type(self) -> str:
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "type": self.type,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "player_id": self.player_id,
            "session_id": self.session_id,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }

    def encode(self) -> bytes:
        try:
            return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise EnvelopeError(f"payload is not JSON serializable: {error}") from error

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(value, Mapping):
            raise EnvelopeError("envelope must be a mapping")
        required = {
            "schema_version",
            "type",
            "sequence",
            "timestamp",
            "player_id",
            "session_id",
            "correlation_id",
            "payload",
        }
        missing = sorted(required - set(value))
        if missing:
            raise EnvelopeError(f"missing envelope fields: {', '.join(missing)}")
        return cls(**{name: value[name] for name in required})

    @classmethod
    def decode(cls, value: bytes | str) -> "EventEnvelope":
        try:
            decoded = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EnvelopeError(f"invalid envelope JSON: {error}") from error
        return cls.from_dict(decoded)


InternalEventEnvelope = EventEnvelope
