from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


class QueueOverflow(RuntimeError):
    """Raised when a non-droppable event cannot fit in the bounded queue."""


@dataclass(slots=True)
class _Entry:
    event: Any
    terminal: bool
    coalesce_key: str | None


class BoundedEventQueue:
    """Bounded queue that drops/replaces telemetry before durable transitions."""

    def __init__(self, maxsize: int) -> None:
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._items: deque[_Entry] = deque()
        self.dropped_telemetry = 0
        self.replaced_telemetry = 0
        self.preserved_transitions = 0

    def put(
        self,
        event: Any,
        *,
        terminal: bool = False,
        coalesce_key: str | None = None,
    ) -> bool:
        """Put an event; return False only when coalescible telemetry is dropped."""

        if coalesce_key is not None and not isinstance(coalesce_key, str):
            raise TypeError("coalesce_key must be a string or None")
        is_terminal = terminal or self._is_terminal(event)
        if coalesce_key is not None:
            for index, entry in enumerate(self._items):
                if entry.coalesce_key == coalesce_key:
                    # A replacement is a new arrival. Moving it to the tail
                    # prevents its newer sequence from overtaking transitions
                    # that were enqueued after the previous telemetry value.
                    del self._items[index]
                    self._items.append(_Entry(event, is_terminal, coalesce_key))
                    self.replaced_telemetry += 1
                    return True

        if len(self._items) < self.maxsize:
            self._items.append(_Entry(event, is_terminal, coalesce_key))
            return True

        if coalesce_key is not None and not is_terminal:
            self.dropped_telemetry += 1
            return False

        telemetry_index = next(
            (index for index, entry in enumerate(self._items) if entry.coalesce_key is not None),
            None,
        )
        if telemetry_index is not None:
            del self._items[telemetry_index]
            self._items.append(_Entry(event, is_terminal, coalesce_key))
            self.preserved_transitions += 1
            return True
        raise QueueOverflow("bounded event queue contains only non-droppable events")

    def put_telemetry(self, key: str, event: Any) -> bool:
        return self.put(event, coalesce_key=key)

    def put_transition(self, event: Any) -> bool:
        return self.put(event)

    def put_terminal(self, event: Any) -> bool:
        return self.put(event, terminal=True)

    def get_nowait(self) -> Any:
        if not self._items:
            raise IndexError("bounded event queue is empty")
        return self._items.popleft().event

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "size": len(self._items),
            "capacity": self.maxsize,
            "dropped_telemetry": self.dropped_telemetry,
            "replaced_telemetry": self.replaced_telemetry,
            "preserved_transitions": self.preserved_transitions,
        }

    @staticmethod
    def _is_terminal(event: Any) -> bool:
        if isinstance(event, dict):
            event_type = event.get("type")
        else:
            event_type = getattr(event, "type", None)
        if not isinstance(event_type, str):
            return False
        return event_type in {
            "native.end-file",
            "native.terminal-pending",
            "media.started",
            "error",
            "eof",
            "stopped",
            "session.closed",
            "player.stopped",
        }


EventQueue = BoundedEventQueue
