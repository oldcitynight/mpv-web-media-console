from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar


T = TypeVar("T")
MAX_RECOVERY_ATTEMPTS = 5


class RecoveryAttemptsExceeded(RuntimeError):
    """Raised after a bounded sequence of playback-host recovery attempts."""


@dataclass(frozen=True, slots=True)
class RecoveryRetryPolicy:
    """Finite exponential-backoff schedule for one recovery incident."""

    max_attempts: int = 3
    initial_delay_s: float = 0.05
    max_delay_s: float = 0.4

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
            or self.max_attempts > MAX_RECOVERY_ATTEMPTS
        ):
            raise ValueError(
                f"max_attempts must be an integer from 1 to {MAX_RECOVERY_ATTEMPTS}"
            )
        for name in ("initial_delay_s", "max_delay_s"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError("max_delay_s cannot be less than initial_delay_s")

    def delay_before_retry(self, retry_number: int) -> float:
        """Return the capped delay before retry 1..max_attempts-1."""

        if (
            isinstance(retry_number, bool)
            or not isinstance(retry_number, int)
            or not 1 <= retry_number < self.max_attempts
        ):
            raise ValueError("retry_number must identify a retry within the policy")
        delay = float(self.initial_delay_s)
        for _ in range(retry_number - 1):
            delay = min(delay * 2.0, float(self.max_delay_s))
        return delay


def run_with_bounded_recovery(
    operation: Callable[[int], T],
    *,
    policy: RecoveryRetryPolicy = RecoveryRetryPolicy(),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run one recovery operation with a fixed attempt limit and capped delay."""

    if not callable(operation):
        raise TypeError("operation must be callable")
    if not isinstance(policy, RecoveryRetryPolicy):
        raise TypeError("policy must be a RecoveryRetryPolicy")
    if not callable(sleep):
        raise TypeError("sleep must be callable")

    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation(attempt)
        except Exception as error:
            if attempt == policy.max_attempts:
                raise RecoveryAttemptsExceeded(
                    f"player-host recovery failed after {policy.max_attempts} attempts"
                ) from error
            sleep(policy.delay_before_retry(attempt))
    raise AssertionError("unreachable recovery retry state")


class PlayerHostRecoveryCoordinator:
    """Restart a failed player-host and run its durable-state restoration."""

    def __init__(
        self,
        player_host: object,
        options: Mapping[str, object],
        *,
        policy: RecoveryRetryPolicy = RecoveryRetryPolicy(),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        for name in ("shutdown", "start", "initialize", "request"):
            if not callable(getattr(player_host, name, None)):
                raise TypeError(f"player_host must expose callable {name}")
        if not isinstance(options, Mapping):
            raise TypeError("options must be a mapping")
        if not isinstance(policy, RecoveryRetryPolicy):
            raise TypeError("policy must be a RecoveryRetryPolicy")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        self._player_host = player_host
        self._options = dict(options)
        self._policy = policy
        self._sleep = sleep

    def recover(self, restore: Callable[[], T]) -> T:
        """Retry child startup and restoration as one bounded operation."""

        if not callable(restore):
            raise TypeError("restore must be callable")

        def attempt(_attempt_number: int) -> T:
            self._player_host.shutdown(force=True)
            self._player_host.start()
            self._player_host.initialize(self._options)
            self._player_host.request(
                "recovery-transition",
                {"phase": "recovering"},
            )
            result = restore()
            self._player_host.request(
                "recovery-transition",
                {"phase": "recovered"},
            )
            return result

        return run_with_bounded_recovery(
            attempt,
            policy=self._policy,
            sleep=self._sleep,
        )
