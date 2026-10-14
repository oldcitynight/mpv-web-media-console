from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .migration_runner import MigrationRunner


class QueueError(ValueError):
    """Raised for invalid durable-queue operations or identity references."""


class QueueReconciliationError(QueueError):
    """Raised when runtime player state cannot identify one queue item safely."""


@dataclass(frozen=True, slots=True)
class DurableQueueItem:
    id: str
    queue_id: str
    media_id: int
    ordinal: int
    canonical_path: str
    added_at: str


@dataclass(frozen=True, slots=True)
class DurableQueue:
    id: str
    name: str
    active_item_id: str | None
    items: tuple[DurableQueueItem, ...]
    created_at: str
    updated_at: str


_ITEM_SELECT = (
    "SELECT qi.id,qi.queue_id,qi.media_id,qi.ordinal,m.canonical_path,qi.added_at "
    "FROM queue_items qi JOIN media m ON m.id=qi.media_id"
)


class DurableQueueStore:
    """Persist queues using stable application IDs, never player playlist indexes."""

    def __init__(
        self,
        migrations: MigrationRunner,
        *,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(migrations, MigrationRunner):
            raise TypeError("migrations must be a MigrationRunner")
        self.migrations = migrations
        self._clock = clock or _utc_now
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def create_queue(self, name: str, *, queue_id: str | None = None) -> DurableQueue:
        if not isinstance(name, str) or not name.strip():
            raise QueueError("queue name must be a non-empty string")
        identity = queue_id if queue_id is not None else self._new_id()
        _nonempty(identity, "queue_id")
        timestamp = _timestamp(self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO queues(id,name,created_at,updated_at,active_item_id) "
                    "VALUES(?,?,?,?,NULL)",
                    (identity, name.strip(), timestamp, timestamp),
                )
            return self.get_queue(identity)
        finally:
            connection.close()

    def add_item(
        self,
        queue_id: str,
        media_id: int,
        *,
        ordinal: int | None = None,
        item_id: str | None = None,
    ) -> DurableQueueItem:
        _nonempty(queue_id, "queue_id")
        _positive_id(media_id, "media_id")
        item_identity = item_id if item_id is not None else self._new_id()
        _nonempty(item_identity, "item_id")
        timestamp = _timestamp(self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                self._require_queue(connection, queue_id)
                items = self._ordered_ids(connection, queue_id)
                position = len(items) if ordinal is None else _ordinal(ordinal)
                if position > len(items):
                    raise QueueError("ordinal must be between zero and the queue length")
                offset = len(items) + 1
                connection.execute(
                    "INSERT INTO queue_items(id,queue_id,ordinal,media_id,added_at) "
                    "VALUES(?,?,?,?,?)",
                    (item_identity, queue_id, offset, media_id, timestamp),
                )
                items.insert(position, item_identity)
                self._write_order(connection, queue_id, items)
                self._touch_queue(connection, queue_id, timestamp)
            item = self.get_item(item_identity)
            if item is None:
                raise sqlite3.IntegrityError("queue item was not persisted")
            return item
        finally:
            connection.close()

    def move_item(self, queue_id: str, item_id: str, ordinal: int) -> DurableQueue:
        _nonempty(queue_id, "queue_id")
        _nonempty(item_id, "item_id")
        position = _ordinal(ordinal)
        timestamp = _timestamp(self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                self._require_queue(connection, queue_id)
                items = self._ordered_ids(connection, queue_id)
                if item_id not in items:
                    raise QueueError("item_id does not belong to this queue")
                if position >= len(items):
                    raise QueueError("ordinal must identify an existing queue position")
                items.remove(item_id)
                items.insert(position, item_id)
                self._write_order(connection, queue_id, items)
                self._touch_queue(connection, queue_id, timestamp)
            return self.get_queue(queue_id)
        finally:
            connection.close()

    def remove_item(self, queue_id: str, item_id: str) -> bool:
        _nonempty(queue_id, "queue_id")
        _nonempty(item_id, "item_id")
        timestamp = _timestamp(self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                self._require_queue(connection, queue_id)
                items = self._ordered_ids(connection, queue_id)
                if item_id not in items:
                    return False
                items.remove(item_id)
                connection.execute(
                    "DELETE FROM queue_items WHERE id=? AND queue_id=?", (item_id, queue_id)
                )
                self._write_order(connection, queue_id, items)
                self._touch_queue(connection, queue_id, timestamp)
                return True
        finally:
            connection.close()

    def set_active_item(self, queue_id: str, item_id: str | None) -> DurableQueue:
        _nonempty(queue_id, "queue_id")
        if item_id is not None:
            _nonempty(item_id, "item_id")
        timestamp = _timestamp(self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                self._require_queue(connection, queue_id)
                if item_id is not None and connection.execute(
                    "SELECT 1 FROM queue_items WHERE id=? AND queue_id=?",
                    (item_id, queue_id),
                ).fetchone() is None:
                    raise QueueError("active item must belong to the selected queue")
                connection.execute(
                    "UPDATE queues SET active_item_id=?,updated_at=? WHERE id=?",
                    (item_id, timestamp, queue_id),
                )
            return self.get_queue(queue_id)
        finally:
            connection.close()

    def reconcile_active_item(
        self,
        queue_id: str,
        *,
        queue_item_id: str | None = None,
        media_id: int | None = None,
    ) -> DurableQueueItem | None:
        """Reconcile observed player state via stable item/media IDs only.

        When only media_id is available, a unique queue match is accepted. A
        duplicate-media queue is ambiguous and must supply queue_item_id; item
        order or player playlist indexes are never used as identities.
        """

        _nonempty(queue_id, "queue_id")
        if queue_item_id is None and media_id is None:
            raise QueueReconciliationError("queue_item_id or media_id is required")
        if queue_item_id is not None:
            _nonempty(queue_item_id, "queue_item_id")
        if media_id is not None:
            _positive_id(media_id, "media_id")

        connection = self.migrations.connect()
        try:
            with connection:
                self._require_queue(connection, queue_id)
                if queue_item_id is not None:
                    row = connection.execute(
                        f"{_ITEM_SELECT} WHERE qi.queue_id=? AND qi.id=?",
                        (queue_id, queue_item_id),
                    ).fetchone()
                    if row is None:
                        raise QueueReconciliationError(
                            "queue_item_id does not identify an item in this queue"
                        )
                    if media_id is not None and int(row[2]) != media_id:
                        raise QueueReconciliationError(
                            "observed media_id does not match queue_item_id"
                        )
                else:
                    rows = connection.execute(
                        f"{_ITEM_SELECT} WHERE qi.queue_id=? AND qi.media_id=? "
                        "ORDER BY qi.ordinal",
                        (queue_id, media_id),
                    ).fetchall()
                    if len(rows) > 1:
                        raise QueueReconciliationError(
                            "media occurs more than once; a stable queue_item_id is required"
                        )
                    row = rows[0] if rows else None

                selected_id = str(row[0]) if row is not None else None
                connection.execute(
                    "UPDATE queues SET active_item_id=?,updated_at=? WHERE id=?",
                    (selected_id, _timestamp(self._clock()), queue_id),
                )
            return _item(row) if row is not None else None
        finally:
            connection.close()

    def get_item(self, item_id: str) -> DurableQueueItem | None:
        _nonempty(item_id, "item_id")
        connection = self.migrations.connect()
        try:
            row = connection.execute(f"{_ITEM_SELECT} WHERE qi.id=?", (item_id,)).fetchone()
            return _item(row) if row is not None else None
        finally:
            connection.close()

    def get_queue(self, queue_id: str) -> DurableQueue:
        _nonempty(queue_id, "queue_id")
        connection = self.migrations.connect()
        try:
            return self.read_queue(connection, queue_id)
        finally:
            connection.close()

    def read_queue(self, connection: sqlite3.Connection, queue_id: str) -> DurableQueue:
        """Read an ordered queue within the caller's existing SQL snapshot."""
        _nonempty(queue_id, "queue_id")
        row = connection.execute(
            "SELECT id,name,active_item_id,created_at,updated_at FROM queues WHERE id=?",
            (queue_id,),
        ).fetchone()
        if row is None:
            raise QueueError("queue_id does not identify a stored queue")
        items = connection.execute(
            f"{_ITEM_SELECT} WHERE qi.queue_id=? ORDER BY qi.ordinal,qi.id", (queue_id,)
        ).fetchall()
        return DurableQueue(
            id=str(row[0]),
            name=str(row[1]),
            active_item_id=row[2],
            items=tuple(_item(item) for item in items),
            created_at=str(row[3]),
            updated_at=str(row[4]),
        )

    def _new_id(self) -> str:
        value = self._id_factory()
        _nonempty(value, "generated ID")
        return value

    @staticmethod
    def _require_queue(connection: sqlite3.Connection, queue_id: str) -> None:
        if connection.execute("SELECT 1 FROM queues WHERE id=?", (queue_id,)).fetchone() is None:
            raise QueueError("queue_id does not identify a stored queue")

    @staticmethod
    def _ordered_ids(connection: sqlite3.Connection, queue_id: str) -> list[str]:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM queue_items WHERE queue_id=? ORDER BY ordinal,id", (queue_id,)
            ).fetchall()
        ]

    @staticmethod
    def _write_order(
        connection: sqlite3.Connection,
        queue_id: str,
        item_ids: list[str],
    ) -> None:
        existing = len(item_ids)
        offset = existing * 2 + 10
        connection.execute(
            "UPDATE queue_items SET ordinal=ordinal+? WHERE queue_id=?", (offset, queue_id)
        )
        for ordinal, item_id in enumerate(item_ids):
            connection.execute(
                "UPDATE queue_items SET ordinal=? WHERE id=? AND queue_id=?",
                (ordinal, item_id, queue_id),
            )

    @staticmethod
    def _touch_queue(connection: sqlite3.Connection, queue_id: str, timestamp: str) -> None:
        connection.execute(
            "UPDATE queues SET updated_at=? WHERE id=?", (timestamp, queue_id)
        )


def _item(row: sqlite3.Row | tuple[object, ...]) -> DurableQueueItem:
    return DurableQueueItem(
        id=str(row[0]),
        queue_id=str(row[1]),
        media_id=int(row[2]),
        ordinal=int(row[3]),
        canonical_path=str(row[4]),
        added_at=str(row[5]),
    )


def _nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise QueueError(f"{label} must be a non-empty string")


def _positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise QueueError(f"{label} must be a positive integer")


def _ordinal(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueueError("ordinal must be a non-negative integer")
    return value


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise QueueError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise QueueError("timestamp must be an ISO-8601 string") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
