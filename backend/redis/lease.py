from __future__ import annotations

import secrets
from collections.abc import Callable

from .client import RedisRuntimeClient
from .namespace import RedisNamespace


class PlayerLeaseUnavailable(RuntimeError):
    """Raised when a context-managed player lease is already owned elsewhere."""


_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""".strip()

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
""".strip()


class RedisPlayerLease:
    """Token-checked Redis ownership for a single player ID.

    The lease is deliberately ephemeral; callers must keep durable player state
    in SQLite. Renewal is explicit so a host can schedule it at the contract's
    five-second interval without hiding background retries in this helper.
    """

    DEFAULT_TTL_SECONDS = 15
    DEFAULT_RENEW_INTERVAL_SECONDS = 5

    def __init__(
        self,
        client: RedisRuntimeClient,
        player_id: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        renew_interval_seconds: int = DEFAULT_RENEW_INTERVAL_SECONDS,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        if not isinstance(client, RedisRuntimeClient):
            raise TypeError("client must be a RedisRuntimeClient")
        self.key = RedisNamespace.player_lease(player_id)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("ttl_seconds must be an integer from 2 to 3600")
        if not 2 <= ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be an integer from 2 to 3600")
        if isinstance(renew_interval_seconds, bool) or not isinstance(
            renew_interval_seconds, int
        ):
            raise ValueError("renew_interval_seconds must be a positive integer below the TTL")
        if not 1 <= renew_interval_seconds < ttl_seconds:
            raise ValueError("renew_interval_seconds must be a positive integer below the TTL")
        if not callable(token_factory):
            raise TypeError("token_factory must be callable")

        token = token_factory()
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("lease token factory must return at least 32 characters")

        self.client = client
        self.ttl_seconds = ttl_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self._token = token
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def acquire(self) -> bool:
        """Acquire only if free; repeated calls on this held instance are safe."""

        if self._held:
            return True
        result = self.client.execute(
            lambda redis: redis.set(self.key, self._token, nx=True, ex=self.ttl_seconds)
        )
        self._held = result is True or result in ("OK", b"OK")
        return self._held

    def renew(self) -> bool:
        """Extend expiry only while Redis still records this owner's token."""

        if not self._held:
            return False
        renewed = self.client.execute(
            lambda redis: redis.eval(
                _RENEW_SCRIPT, 1, self.key, self._token, self.ttl_seconds
            )
        )
        self._held = _redis_success(renewed)
        return self._held

    def release(self) -> bool:
        """Delete only this owner's key; never remove a replacement owner's lease."""

        if not self._held:
            return False
        try:
            released = self.client.execute(
                lambda redis: redis.eval(
                    _RELEASE_SCRIPT, 1, self.key, self._token
                )
            )
        finally:
            self._held = False
        return _redis_success(released)

    def __enter__(self) -> "RedisPlayerLease":
        if not self.acquire():
            raise PlayerLeaseUnavailable("player is already controlled by another backend")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


def _redis_success(result: object) -> bool:
    return result is True or result == 1 or result == "1" or result == b"1"
