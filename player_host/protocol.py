from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """Raised when a player-host envelope is malformed or unsupported."""


@dataclass(frozen=True)
class Envelope:
    version: int
    request_id: str
    kind: str
    payload: Mapping[str, Any]

    def encode(self) -> bytes:
        return (
            json.dumps(
                {
                    "version": self.version,
                    "request_id": self.request_id,
                    "kind": self.kind,
                    "payload": dict(self.payload),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    @classmethod
    def decode(cls, message: bytes | str) -> "Envelope":
        try:
            value = json.loads(message.decode("utf-8") if isinstance(message, bytes) else message)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError("invalid JSON envelope") from error
        if not isinstance(value, dict):
            raise ProtocolError("envelope must be a JSON object")
        if value.get("version") != PROTOCOL_VERSION:
            raise ProtocolError("unsupported envelope version")
        if not isinstance(value.get("request_id"), str) or not value["request_id"]:
            raise ProtocolError("envelope request_id must be a non-empty string")
        if not isinstance(value.get("kind"), str) or not value["kind"]:
            raise ProtocolError("envelope kind must be a non-empty string")
        if not isinstance(value.get("payload"), dict):
            raise ProtocolError("envelope payload must be an object")
        return cls(
            version=PROTOCOL_VERSION,
            request_id=value["request_id"],
            kind=value["kind"],
            payload=value["payload"],
        )
