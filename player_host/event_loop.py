from __future__ import annotations

import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any


@dataclass(frozen=True)
class PlayerEvent:
    """A typed internal event emitted by the player-host wait worker."""

    sequence: int
    kind: str
    payload: Mapping[str, Any]


class PlayerEventLoop:
    """Drain a blocking player event source on a daemon worker thread."""

    def __init__(
        self,
        wait_event: Callable[[], Any],
        emit: Callable[[PlayerEvent], None],
        *,
        wakeup: Callable[[], None] | None = None,
        thread_name: str = "mpv-event-drain",
    ) -> None:
        self._wait_event = wait_event
        self._emit = emit
        self._wakeup = wakeup
        self._thread_name = thread_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            raise RuntimeError("player event loop is already running")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._wakeup is not None:
            self._wakeup()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise RuntimeError("player event loop did not stop after wakeup")
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw_event = self._wait_event()
            except Exception as error:
                if not self._stop.is_set():
                    self._emit(self._next_event("error", {"message": str(error)}))
                return
            if raw_event is None:
                continue
            if self._stop.is_set():
                return
            kind, payload = translate_event(raw_event)
            self._emit(self._next_event(kind, payload))

    def _next_event(self, kind: str, payload: Mapping[str, Any]) -> PlayerEvent:
        self._sequence += 1
        return PlayerEvent(self._sequence, kind, dict(payload))


def translate_event(raw_event: Any) -> tuple[str, Mapping[str, Any]]:
    """Translate binding-shaped mappings/objects into a stable event pair."""

    if isinstance(raw_event, PlayerEvent):
        return raw_event.kind, raw_event.payload
    if isinstance(raw_event, Mapping):
        kind = raw_event.get("kind") or raw_event.get("event") or "unknown"
        payload = {
            str(key): value
            for key, value in raw_event.items()
            if key not in {"kind", "event"}
        }
        return str(kind), payload
    name = getattr(raw_event, "event", None) or getattr(raw_event, "kind", None)
    if name is not None:
        payload = getattr(raw_event, "payload", None) or getattr(raw_event, "data", None)
        return str(name), payload if isinstance(payload, Mapping) else {"value": payload}
    return "unknown", {"value": raw_event}
