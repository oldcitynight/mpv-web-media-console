from __future__ import annotations

from collections import defaultdict
from threading import RLock


class SequenceError(ValueError):
    """Raised when an event sequence is not strictly newer for its player."""


class EventSequencer:
    """Allocate and validate strictly increasing sequences per player."""

    def __init__(self) -> None:
        self._last: defaultdict[str, int] = defaultdict(int)
        self._lock = RLock()

    def next(self, player_id: str) -> int:
        self._validate_player_id(player_id)
        with self._lock:
            self._last[player_id] += 1
            return self._last[player_id]

    def accept(self, player_id: str, sequence: int) -> int:
        """Record an externally supplied sequence if it is strictly newer."""

        self._validate_player_id(player_id)
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise SequenceError("sequence must be an integer")
        if sequence < 1:
            raise SequenceError("sequence must be positive")
        with self._lock:
            previous = self._last[player_id]
            if sequence <= previous:
                raise SequenceError(
                    f"sequence {sequence} is not newer than {previous} for {player_id}"
                )
            self._last[player_id] = sequence
            return sequence

    def last(self, player_id: str) -> int | None:
        self._validate_player_id(player_id)
        with self._lock:
            return self._last.get(player_id) or None

    def reset(self, player_id: str) -> None:
        self._validate_player_id(player_id)
        with self._lock:
            self._last.pop(player_id, None)

    @staticmethod
    def _validate_player_id(player_id: str) -> None:
        if not isinstance(player_id, str) or not player_id:
            raise ValueError("player_id must be a non-empty string")
