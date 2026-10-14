from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from .migration_runner import MigrationRunner


class ResumePointError(ValueError):
    """Raised when a resume point has invalid media/session context or values."""


class ResumeSaveResult(str, Enum):
    SAVED = "saved"
    CLEARED = "cleared"
    THROTTLED = "throttled"


@dataclass(frozen=True, slots=True)
class ResumePolicy:
    write_interval_seconds: float = 15.0
    minimum_position_seconds: float = 10.0
    end_margin_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in (
            "write_interval_seconds",
            "minimum_position_seconds",
            "end_margin_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True, slots=True)
class ResumePoint:
    media_id: int
    position_s: float
    duration_s: float | None
    session_id: str
    updated_at: str


class ResumePointStore:
    """Persist throttled positions and restore their exact media/session context."""

    def __init__(
        self,
        migrations: MigrationRunner,
        *,
        policy: ResumePolicy | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(migrations, MigrationRunner):
            raise TypeError("migrations must be a MigrationRunner")
        self.migrations = migrations
        self.policy = policy or ResumePolicy()
        self._clock = clock or _utc_now

    def save_position(
        self,
        media_id: int,
        session_id: str,
        position_s: float,
        duration_s: float | None,
        *,
        updated_at: str | None = None,
        force: bool = False,
    ) -> ResumeSaveResult:
        _positive_id(media_id, "media_id")
        if not isinstance(session_id, str) or not session_id:
            raise ResumePointError("session_id must be a non-empty string")
        position = _finite_nonnegative(position_s, "position_s")
        duration = (
            None
            if duration_s is None
            else _finite_nonnegative(duration_s, "duration_s")
        )
        if not isinstance(force, bool):
            raise ResumePointError("force must be a boolean")
        timestamp = updated_at if updated_at is not None else self._clock()
        normalized_time = _normalize_time(timestamp)

        connection = self.migrations.connect()
        try:
            with connection:
                self._validate_context(connection, media_id, session_id)
                existing = connection.execute(
                    "SELECT position_s,duration_s,session_id,updated_at "
                    "FROM resume_points WHERE media_id=?",
                    (media_id,),
                ).fetchone()

                if self._is_trivial(position, duration):
                    connection.execute("DELETE FROM resume_points WHERE media_id=?", (media_id,))
                    return ResumeSaveResult.CLEARED

                if (
                    existing is not None
                    and existing[2] == session_id
                    and not force
                    and self._is_throttled(existing[3], normalized_time)
                ):
                    return ResumeSaveResult.THROTTLED

                connection.execute(
                    "INSERT INTO resume_points(media_id,position_s,duration_s,session_id,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET "
                    "position_s=excluded.position_s,duration_s=excluded.duration_s,"
                    "session_id=excluded.session_id,updated_at=excluded.updated_at",
                    (media_id, position, duration, session_id, timestamp),
                )
                return ResumeSaveResult.SAVED
        finally:
            connection.close()

    def get(self, media_id: int) -> ResumePoint | None:
        _positive_id(media_id, "media_id")
        connection = self.migrations.connect()
        try:
            row = connection.execute(
                "SELECT media_id,position_s,duration_s,session_id,updated_at "
                "FROM resume_points WHERE media_id=?",
                (media_id,),
            ).fetchone()
            if row is None:
                return None
            return ResumePoint(
                media_id=int(row[0]),
                position_s=float(row[1]),
                duration_s=row[2],
                session_id=str(row[3]),
                updated_at=str(row[4]),
            )
        finally:
            connection.close()

    def clear(self, media_id: int) -> bool:
        _positive_id(media_id, "media_id")
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM resume_points WHERE media_id=?", (media_id,)
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def _validate_context(
        self,
        connection: sqlite3.Connection,
        media_id: int,
        session_id: str,
    ) -> None:
        media = connection.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone()
        if media is None:
            raise ResumePointError("media_id does not identify a stored media row")
        session = connection.execute(
            "SELECT media_id FROM playback_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise ResumePointError("session_id does not identify a stored playback session")
        if session[0] is not None and int(session[0]) != media_id:
            raise ResumePointError("session does not belong to the requested media")

    def _is_trivial(self, position: float, duration: float | None) -> bool:
        if position < self.policy.minimum_position_seconds:
            return True
        if duration is not None and duration - position <= self.policy.end_margin_seconds:
            return True
        return False

    def _is_throttled(self, previous: str, current: datetime) -> bool:
        try:
            previous_time = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        if previous_time.tzinfo is None:
            previous_time = previous_time.replace(tzinfo=timezone.utc)
        elapsed = (current - previous_time.astimezone(timezone.utc)).total_seconds()
        return 0 <= elapsed < self.policy.write_interval_seconds


def _positive_id(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResumePointError(f"{label} must be a positive integer")


def _finite_nonnegative(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResumePointError(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ResumePointError(f"{label} must be a finite non-negative number")
    return result


def _normalize_time(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ResumePointError("updated_at must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ResumePointError("updated_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
