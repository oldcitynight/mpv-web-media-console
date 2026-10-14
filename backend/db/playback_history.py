from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal

from .migration_runner import MigrationRunner


class PlaybackHistoryError(ValueError):
    """Raised when a historical playback occurrence has invalid context."""


HistoryEndReason = Literal[
    "eof",
    "stopped",
    "error",
    "clean_shutdown",
    "crash_recovery",
]
_END_REASONS = frozenset({"eof", "stopped", "error", "clean_shutdown", "crash_recovery"})
_HISTORY_SELECT = (
    "SELECT id,session_id,media_id,started_at,ended_at,last_position_s,completed,end_reason "
    "FROM playback_history"
)


@dataclass(frozen=True, slots=True)
class PlaybackHistoryEntry:
    id: int
    session_id: str
    media_id: int
    started_at: str
    ended_at: str | None
    last_position_s: float
    completed: bool
    end_reason: str | None

    @property
    def active(self) -> bool:
        return self.ended_at is None


class PlaybackHistoryStore:
    """Keep completed playback occurrences immutable and separate from sessions."""

    def __init__(
        self,
        migrations: MigrationRunner,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(migrations, MigrationRunner):
            raise TypeError("migrations must be a MigrationRunner")
        self.migrations = migrations
        self._clock = clock or _utc_now

    def record_start(
        self,
        session_id: str,
        media_id: int,
        *,
        started_at: str | None = None,
        initial_position_s: float = 0.0,
    ) -> PlaybackHistoryEntry:
        _session_id(session_id)
        _positive_id(media_id, "media_id")
        position = _position(initial_position_s)
        timestamp = _timestamp(started_at if started_at is not None else self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                self._validate_context(connection, session_id, media_id)
                existing = connection.execute(
                    "SELECT id FROM playback_history WHERE session_id=? AND media_id=? "
                    "AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                    (session_id, media_id),
                ).fetchone()
                if existing is not None:
                    row = connection.execute(
                        f"{_HISTORY_SELECT} WHERE id=?", (existing[0],)
                    ).fetchone()
                    return _entry(row)
                cursor = connection.execute(
                    "INSERT INTO playback_history(session_id,media_id,started_at,last_position_s) "
                    "VALUES(?,?,?,?)",
                    (session_id, media_id, timestamp, position),
                )
                row = connection.execute(
                    f"{_HISTORY_SELECT} WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                if row is None:
                    raise sqlite3.IntegrityError("playback history row was not persisted")
            return _entry(row)
        finally:
            connection.close()

    def record_progress(self, history_id: int, position_s: float) -> bool:
        _positive_id(history_id, "history_id")
        position = _position(position_s)
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE playback_history SET last_position_s=? WHERE id=? AND ended_at IS NULL",
                    (position, history_id),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def record_end(
        self,
        history_id: int,
        *,
        last_position_s: float,
        completed: bool,
        end_reason: HistoryEndReason,
        ended_at: str | None = None,
    ) -> bool:
        _positive_id(history_id, "history_id")
        position = _position(last_position_s)
        if not isinstance(completed, bool):
            raise PlaybackHistoryError("completed must be a boolean")
        _end_reason(end_reason)
        timestamp = _timestamp(ended_at if ended_at is not None else self._clock())
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE playback_history SET ended_at=?,last_position_s=?,completed=?,end_reason=? "
                    "WHERE id=? AND ended_at IS NULL",
                    (timestamp, position, int(completed), end_reason, history_id),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def get(self, history_id: int) -> PlaybackHistoryEntry | None:
        _positive_id(history_id, "history_id")
        connection = self.migrations.connect()
        try:
            row = connection.execute(f"{_HISTORY_SELECT} WHERE id=?", (history_id,)).fetchone()
            return _entry(row) if row is not None else None
        finally:
            connection.close()

    def list_for_media(
        self,
        media_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[PlaybackHistoryEntry, ...]:
        _positive_id(media_id, "media_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise PlaybackHistoryError("limit must be an integer from 1 to 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise PlaybackHistoryError("offset must be a non-negative integer")
        connection = self.migrations.connect()
        try:
            rows = connection.execute(
                f"{_HISTORY_SELECT} WHERE media_id=? ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?",
                (media_id, limit, offset),
            ).fetchall()
            return tuple(_entry(row) for row in rows)
        finally:
            connection.close()

    @staticmethod
    def _validate_context(
        connection: sqlite3.Connection,
        session_id: str,
        media_id: int,
    ) -> None:
        row = connection.execute(
            "SELECT media_id FROM playback_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise PlaybackHistoryError("session_id does not identify a stored playback session")
        if row[0] is not None and int(row[0]) != media_id:
            raise PlaybackHistoryError("session does not belong to the requested media")
        if connection.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone() is None:
            raise PlaybackHistoryError("media_id does not identify a stored media row")


def _entry(row: sqlite3.Row | tuple[object, ...]) -> PlaybackHistoryEntry:
    return PlaybackHistoryEntry(
        id=int(row[0]),
        session_id=str(row[1]),
        media_id=int(row[2]),
        started_at=str(row[3]),
        ended_at=row[4],
        last_position_s=float(row[5]),
        completed=bool(row[6]),
        end_reason=row[7],
    )


def _session_id(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise PlaybackHistoryError("session_id must be a non-empty string")


def _positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlaybackHistoryError(f"{label} must be a positive integer")


def _position(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlaybackHistoryError("position must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PlaybackHistoryError("position must be a finite non-negative number")
    return result


def _end_reason(value: str) -> None:
    if not isinstance(value, str) or value not in _END_REASONS:
        raise PlaybackHistoryError("end_reason is unsupported")


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlaybackHistoryError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PlaybackHistoryError("timestamp must be an ISO-8601 string") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
