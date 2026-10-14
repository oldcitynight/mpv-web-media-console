from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from backend.db import DurableQueueStore, MigrationRunner
from backend.db.durable_queue import QueueError
from backend.domain.queue import QueueConflict, QueueEntry
from backend.media import MediaIdentityStore


class _MonotonicUtcClock:
    """Produce strictly increasing UTC timestamps, including rapid mutations."""

    def __init__(self) -> None:
        self._last: datetime | None = None
        self._lock = threading.Lock()

    def observe(self, value: str) -> None:
        parsed = _parse_timestamp(value)
        with self._lock:
            if self._last is None or parsed > self._last:
                self._last = parsed

    def __call__(self) -> str:
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._last is not None and now <= self._last:
                now = self._last + timedelta(microseconds=1)
            self._last = now
            return now.isoformat().replace("+00:00", "Z")


class PersistentQueueAdapter:
    """Expose DurableQueueStore through PlayerService's queue-store interface."""

    def __init__(
        self,
        migrations: MigrationRunner,
        media: MediaIdentityStore,
        *,
        queue_id: str = "default",
        name: str = "Default",
    ) -> None:
        self.queue_id = queue_id
        self.name = name
        self.media = media
        self._lock = threading.RLock()
        self._clock = _MonotonicUtcClock()
        self._store = DurableQueueStore(
            migrations,
            clock=self._clock,
            id_factory=lambda: uuid.uuid4().hex,
        )
        try:
            queue = self._store.get_queue(queue_id)
        except QueueError:
            queue = self._store.create_queue(name, queue_id=queue_id)
        self._clock.observe(queue.updated_at)

    @property
    def items(self) -> tuple[QueueEntry, ...]:
        with self._lock:
            queue = self._store.get_queue(self.queue_id)
            return tuple(self._entry(item, queue.active_item_id) for item in queue.items)

    def snapshot(self, connection=None) -> dict[str, object]:
        with self._lock:
            queue = (self._store.read_queue(connection, self.queue_id) if connection is not None
                     else self._store.get_queue(self.queue_id))
            return {
                "id": queue.id,
                "name": queue.name,
                "revision": _revision(queue.updated_at),
                "items": [
                    {
                        "id": item.id,
                        "path": item.canonical_path,
                        "ordinal": item.ordinal,
                        "active": item.id == queue.active_item_id,
                    }
                    for item in queue.items
                ],
            }

    def append(
        self,
        path: str,
        *,
        ordinal: int | None = None,
        expected_revision: int | None = None,
    ) -> QueueEntry:
        with self._lock:
            queue = self._checked_queue(expected_revision)
            if ordinal is not None and (ordinal < 0 or ordinal > len(queue.items)):
                raise ValueError("ordinal is outside the queue")
            media = self.media.identify(path)
            item = self._store.add_item(
                self.queue_id,
                media.id,
                ordinal=ordinal,
                item_id=uuid.uuid4().hex,
            )
            current = self._store.get_queue(self.queue_id)
            return self._entry(item, current.active_item_id)

    def remove(self, item_id: str, *, expected_revision: int | None = None) -> None:
        with self._lock:
            self._checked_queue(expected_revision)
            if not self._store.remove_item(self.queue_id, item_id):
                raise QueueConflict("queue item does not exist")

    def move(
        self,
        item_id: str,
        ordinal: int,
        *,
        expected_revision: int | None = None,
    ) -> QueueEntry:
        with self._lock:
            queue = self._checked_queue(expected_revision)
            if ordinal < 0 or ordinal >= len(queue.items):
                raise ValueError("ordinal is outside the queue")
            try:
                current = self._store.move_item(self.queue_id, item_id, ordinal)
            except QueueError as error:
                if "does not belong" in str(error):
                    raise QueueConflict("queue item does not exist") from error
                raise
            item = next(item for item in current.items if item.id == item_id)
            return self._entry(item, current.active_item_id)

    def clear(self, *, expected_revision: int | None = None) -> None:
        with self._lock:
            queue = self._checked_queue(expected_revision)
            for item in queue.items:
                self._store.remove_item(self.queue_id, item.id)
            self._store.set_active_item(self.queue_id, None)

    def activate(self, item_id: str, *, expected_revision: int | None = None) -> QueueEntry:
        with self._lock:
            self._checked_queue(expected_revision)
            try:
                queue = self._store.set_active_item(self.queue_id, item_id)
            except QueueError as error:
                if "must belong" in str(error):
                    raise QueueConflict("queue item does not exist") from error
                raise
            item = next(item for item in queue.items if item.id == item_id)
            return self._entry(item, queue.active_item_id)

    def active_item(self) -> QueueEntry | None:
        with self._lock:
            queue = self._store.get_queue(self.queue_id)
            if queue.active_item_id is None:
                return None
            item = next(
                (item for item in queue.items if item.id == queue.active_item_id), None
            )
            return self._entry(item, queue.active_item_id) if item is not None else None

    def _checked_queue(self, expected_revision: int | None):
        queue = self._store.get_queue(self.queue_id)
        if expected_revision is not None and expected_revision != _revision(queue.updated_at):
            raise QueueConflict("queue revision is stale")
        return queue

    @staticmethod
    def _entry(item, active_item_id: str | None) -> QueueEntry:
        return QueueEntry(
            id=item.id,
            path=item.canonical_path,
            ordinal=item.ordinal,
            active=item.id == active_item_id,
        )


def _revision(value: str) -> int:
    return int(_parse_timestamp(value).timestamp() * 1_000_000)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
