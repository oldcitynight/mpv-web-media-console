from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable


class QueueConflict(RuntimeError):
    """Raised when a queue mutation uses a stale revision or missing item."""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    id: str
    path: str
    ordinal: int
    active: bool = False


class QueueStore:
    def __init__(self, *, queue_id: str = "default", name: str = "Default", id_factory: Callable[[], str] | None = None) -> None:
        self.queue_id = queue_id
        self.name = name
        self.revision = 0
        self._items: list[QueueEntry] = []
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    @property
    def items(self) -> tuple[QueueEntry, ...]:
        return tuple(self._items)

    def append(self, path: str, *, ordinal: int | None = None, expected_revision: int | None = None) -> QueueEntry:
        self._check_revision(expected_revision)
        self._check_path(path)
        position = len(self._items) if ordinal is None else ordinal
        if position < 0 or position > len(self._items):
            raise ValueError("ordinal is outside the queue")
        entry = QueueEntry(self._id_factory(), path, position)
        self._items.insert(position, entry)
        self._renumber()
        self.revision += 1
        return next(item for item in self._items if item.id == entry.id)

    def remove(self, item_id: str, *, expected_revision: int | None = None) -> None:
        self._check_revision(expected_revision)
        self._index(item_id)
        self._items = [item for item in self._items if item.id != item_id]
        self._renumber()
        self.revision += 1

    def move(self, item_id: str, ordinal: int, *, expected_revision: int | None = None) -> QueueEntry:
        self._check_revision(expected_revision)
        index = self._index(item_id)
        if ordinal < 0 or ordinal >= len(self._items):
            raise ValueError("ordinal is outside the queue")
        entry = self._items.pop(index)
        self._items.insert(ordinal, entry)
        self._renumber()
        self.revision += 1
        return self._items[ordinal]

    def clear(self, *, expected_revision: int | None = None) -> None:
        self._check_revision(expected_revision)
        self._items.clear()
        self.revision += 1

    def activate(self, item_id: str, *, expected_revision: int | None = None) -> QueueEntry:
        self._check_revision(expected_revision)
        index = self._index(item_id)
        self._items = [
            QueueEntry(item.id, item.path, item.ordinal, active=index == position)
            for position, item in enumerate(self._items)
        ]
        self.revision += 1
        return self._items[index]

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.queue_id,
            "name": self.name,
            "revision": self.revision,
            "items": [
                {"id": item.id, "path": item.path, "ordinal": item.ordinal, "active": item.active}
                for item in self._items
            ],
        }

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self.revision:
            raise QueueConflict("queue revision is stale")

    def _index(self, item_id: str) -> int:
        for index, item in enumerate(self._items):
            if item.id == item_id:
                return index
        raise QueueConflict("queue item does not exist")

    @staticmethod
    def _check_path(path: str) -> None:
        if not isinstance(path, str) or not path:
            raise ValueError("queue path must be a non-empty string")

    def _renumber(self) -> None:
        self._items = [QueueEntry(item.id, item.path, ordinal, item.active) for ordinal, item in enumerate(self._items)]
