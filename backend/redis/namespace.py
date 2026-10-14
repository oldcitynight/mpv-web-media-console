from __future__ import annotations

import hashlib
import re


class RedisNamespace:
    """Build only contract-scoped Redis keys and channels."""

    PREFIX = "mpvweb:v1"
    _SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z", re.ASCII)

    @classmethod
    def player_state(cls, player_id: str) -> str:
        return f"{cls.PREFIX}:state:player:{cls._identity(player_id, 'player_id')}"

    @classmethod
    def session_state(cls, session_id: str) -> str:
        return f"{cls.PREFIX}:state:session:{cls._identity(session_id, 'session_id')}"

    @classmethod
    def events_stream(cls, player_id: str) -> str:
        return f"{cls.PREFIX}:stream:events:{cls._identity(player_id, 'player_id')}"

    @classmethod
    def live_pubsub(cls, player_id: str) -> str:
        return f"{cls.PREFIX}:pub:events:{cls._identity(player_id, 'player_id')}"

    @classmethod
    def player_lease(cls, player_id: str) -> str:
        return f"{cls.PREFIX}:lease:player:{cls._identity(player_id, 'player_id')}"

    @classmethod
    def idempotency(cls, client_id: str, key: str) -> str:
        """Hash opaque identifiers so they never appear in Redis key names."""

        client_digest = cls._digest(client_id, "client_id")
        key_digest = cls._digest(key, "key")
        return f"{cls.PREFIX}:idem:{client_digest}:{key_digest}"

    @classmethod
    def _identity(cls, value: str, label: str) -> str:
        if not isinstance(value, str) or cls._SAFE_ID.fullmatch(value) is None:
            raise ValueError(
                f"{label} must be 1-128 ASCII letters, digits, dots, underscores, or hyphens"
            )
        return value

    @staticmethod
    def _digest(value: str, label: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string")
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
