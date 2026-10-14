from __future__ import annotations

import importlib
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


class RedisUnavailable(RuntimeError):
    """Raised when an operation requires Redis but Redis is not ready."""


@dataclass(frozen=True, slots=True)
class RedisSettings:
    url: str | None = field(default=None, repr=False)
    connect_timeout: float = 0.25
    command_timeout: float = 0.5
    health_check_interval: int = 15
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.url is not None:
            if not isinstance(self.url, str) or not self.url:
                raise ValueError("Redis URL must be a non-empty redis:// or rediss:// URL")
            if not self.url.startswith(("redis://", "rediss://")):
                raise ValueError("Redis URL must use redis:// or rediss://")
        for name in ("connect_timeout", "command_timeout", "retry_base_seconds", "retry_max_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite positive number")
            if not math.isfinite(value) or value <= 0 or value > 30:
                raise ValueError(f"{name} must be finite and between 0 and 30 seconds")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        if isinstance(self.health_check_interval, bool) or not isinstance(self.health_check_interval, int):
            raise ValueError("health_check_interval must be a positive integer")
        if not 1 <= self.health_check_interval <= 3600:
            raise ValueError("health_check_interval must be between 1 and 3600 seconds")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "RedisSettings":
        values = os.environ if environ is None else environ
        url = values.get("MPV_WEBD_REDIS_URL")
        if url == "":
            raise ValueError("MPV_WEBD_REDIS_URL must not be empty")
        return cls(
            url=url,
            connect_timeout=_float_setting(values, "MPV_WEBD_REDIS_CONNECT_TIMEOUT", 0.25),
            command_timeout=_float_setting(values, "MPV_WEBD_REDIS_COMMAND_TIMEOUT", 0.5),
            health_check_interval=_int_setting(
                values, "MPV_WEBD_REDIS_HEALTH_CHECK_INTERVAL", 15
            ),
            retry_base_seconds=_float_setting(
                values, "MPV_WEBD_REDIS_RETRY_BASE_SECONDS", 0.25
            ),
            retry_max_seconds=_float_setting(
                values, "MPV_WEBD_REDIS_RETRY_MAX_SECONDS", 5.0
            ),
        )


@dataclass(frozen=True, slots=True)
class RedisHealth:
    state: str
    available: bool
    degraded: bool
    failure_count: int
    retry_after_seconds: float | None = None
    error_kind: str | None = None


class RedisRuntimeClient:
    """Optional Redis client with bounded I/O and a capped reconnect backoff."""

    def __init__(
        self,
        settings: RedisSettings | None = None,
        *,
        client_factory: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings or RedisSettings.from_environment()
        self._client_factory = client_factory or _redis_client_from_url
        self._monotonic = monotonic
        self._client: Any | None = None
        self._state = "disabled" if self.settings.url is None else "degraded"
        self._failure_count = 0
        self._next_retry_at: float | None = None
        self._error_kind: str | None = None

    @classmethod
    def from_environment(cls) -> "RedisRuntimeClient":
        return cls(RedisSettings.from_environment())

    @property
    def health(self) -> RedisHealth:
        retry_after = None
        if self._next_retry_at is not None:
            retry_after = max(0.0, self._next_retry_at - self._monotonic())
        return RedisHealth(
            state=self._state,
            available=self._state == "ready",
            degraded=self._state == "degraded",
            failure_count=self._failure_count,
            retry_after_seconds=retry_after,
            error_kind=self._error_kind,
        )

    @property
    def client(self) -> Any:
        if self._client is None or self._state != "ready":
            raise RedisUnavailable("Redis is not ready")
        return self._client

    def start(self) -> RedisHealth:
        return self.probe(force=True)

    def probe(self, *, force: bool = False) -> RedisHealth:
        if self.settings.url is None:
            self._state = "disabled"
            self._next_retry_at = None
            self._error_kind = None
            return self.health
        now = self._monotonic()
        if not force and self._next_retry_at is not None and now < self._next_retry_at:
            return self.health
        try:
            if self._client is None:
                self._client = self._client_factory(
                    self.settings.url,
                    socket_connect_timeout=self.settings.connect_timeout,
                    socket_timeout=self.settings.command_timeout,
                    health_check_interval=self.settings.health_check_interval,
                    decode_responses=True,
                    retry_on_timeout=False,
                )
            if self._client.ping() is not True:
                raise ConnectionError("Redis PING returned a non-true response")
        except Exception as error:
            self._mark_failure(error, now)
            return self.health
        self._state = "ready"
        self._failure_count = 0
        self._next_retry_at = None
        self._error_kind = None
        return self.health

    def execute(self, operation: Callable[[Any], Any]) -> Any:
        """Run one caller-supplied Redis operation without replaying it on failure."""

        if not callable(operation):
            raise TypeError("Redis operation must be callable")
        if self._state != "ready":
            if not self.probe().available:
                raise RedisUnavailable("Redis is not ready")
        try:
            result = operation(self.client)
        except Exception as error:
            if _is_availability_error(error):
                self._mark_failure(error, self._monotonic())
                raise RedisUnavailable("Redis operation failed") from error
            raise
        self._state = "ready"
        self._failure_count = 0
        self._next_retry_at = None
        self._error_kind = None
        return result

    def close(self) -> None:
        client, self._client = self._client, None
        self._state = "disabled" if self.settings.url is None else "degraded"
        self._next_retry_at = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def report_failure(self, error: Exception) -> None:
        """Let a long-lived Pub/Sub connection report transport loss safely."""

        if _is_availability_error(error):
            self._mark_failure(error, self._monotonic())

    def _mark_failure(self, error: Exception, now: float) -> None:
        self._state = "degraded"
        self._failure_count += 1
        self._error_kind = type(error).__name__
        delay = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2 ** min(self._failure_count - 1, 30)),
        )
        self._next_retry_at = now + delay


def _redis_client_from_url(url: str, **options: Any) -> Any:
    redis_module = importlib.import_module("redis")
    retry_module = importlib.import_module("redis.retry")
    backoff_module = importlib.import_module("redis.backoff")
    # Commands are never transparently replayed; our health probe applies the
    # capped retry schedule so non-idempotent Redis operations cannot duplicate.
    retry = retry_module.Retry(backoff_module.NoBackoff(), retries=0)
    return redis_module.Redis.from_url(url, retry=retry, **options)


def _is_availability_error(error: Exception) -> bool:
    return isinstance(error, (OSError, TimeoutError)) or type(error).__name__ in {
        "ConnectionError",
        "TimeoutError",
        "BusyLoadingError",
        "ClusterDownError",
        "MasterDownError",
        "LoadingError",
        "TryAgainError",
    }


def _float_setting(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error


def _int_setting(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
