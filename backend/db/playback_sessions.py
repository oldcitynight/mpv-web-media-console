from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from backend.events.envelope import EventEnvelope

from .migration_runner import MigrationRunner


class PlaybackSessionError(ValueError):
    """Raised when an event cannot safely update a durable playback session."""


EndReason = Literal[
    "clean_shutdown",
    "crash_recovery",
    "eof",
    "stopped",
    "error",
]
_END_REASONS = frozenset({"clean_shutdown", "crash_recovery", "eof", "stopped", "error"})
_ACTIVE_STATES = ("loading", "playing", "paused", "recovering")
_SESSION_SELECT = (
    "SELECT id,player_id,media_id,state,position_s,duration_s,selected_aid,selected_sid,"
    "selected_vid,profile_set_json,state_json,started_at,updated_at,ended_at,end_reason "
    "FROM playback_sessions"
)


@dataclass(frozen=True, slots=True)
class PlaybackSession:
    id: str
    player_id: str
    media_id: int | None
    state: str
    position_s: float
    duration_s: float | None
    selected_aid: int | None
    selected_sid: int | None
    selected_vid: int | None
    profile_set: dict[str, Any]
    state_data: dict[str, Any]
    started_at: str
    updated_at: str
    ended_at: str | None
    end_reason: str | None

    @property
    def active(self) -> bool:
        return self.ended_at is None


class PlaybackSessionStore:
    """Persist session transitions and make shutdown/crash endings explicit."""

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

    def record_transition(
        self,
        event: EventEnvelope,
        *,
        media_id: int | None = None,
        end_reason: EndReason | None = None,
    ) -> PlaybackSession:
        if not isinstance(event, EventEnvelope):
            raise TypeError("event must be an EventEnvelope")
        state_data = event.payload.get("state")
        if not isinstance(state_data, Mapping):
            raise PlaybackSessionError("durable event state must be an object")
        properties = state_data.get("properties", {})
        if not isinstance(properties, Mapping):
            raise PlaybackSessionError("durable event properties must be an object")

        state = state_data.get("status", "error")
        if not isinstance(state, str):
            raise PlaybackSessionError("session state must be a string")
        state = {"ended": "stopped"}.get(state, state)
        if state not in {"idle", "loading", "playing", "paused", "stopped", "error", "recovering"}:
            raise PlaybackSessionError("session state is unsupported")

        if media_id is not None and (
            isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 1
        ):
            raise PlaybackSessionError("media_id must be a positive integer or null")
        timestamp = _event_time(event.timestamp)
        position = _finite_nonnegative(properties.get("time_pos"), "time_pos", default=0.0)
        duration = _finite_nonnegative(properties.get("duration"), "duration", default=None)
        selected_aid = _optional_track_id(properties.get("aid"), "aid")
        selected_sid = _optional_track_id(properties.get("sid"), "sid")
        selected_vid = _optional_track_id(properties.get("vid"), "vid")
        state_json = _json_object(dict(state_data), "session state")
        final_reason = end_reason
        if final_reason is not None and final_reason not in _END_REASONS:
            raise PlaybackSessionError("end_reason is unsupported")
        if state in {"stopped", "error"} and final_reason is None:
            final_reason = "error" if state == "error" else "stopped"

        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO playback_sessions(id,player_id,media_id,state,position_s,duration_s,"
                    "selected_aid,selected_sid,selected_vid,state_json,started_at,updated_at,ended_at,end_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET player_id=excluded.player_id,"
                    "media_id=COALESCE(excluded.media_id,playback_sessions.media_id),"
                    "state=excluded.state,position_s=excluded.position_s,duration_s=excluded.duration_s,"
                    "selected_aid=excluded.selected_aid,selected_sid=excluded.selected_sid,"
                    "selected_vid=excluded.selected_vid,state_json=excluded.state_json,"
                    "updated_at=excluded.updated_at,"
                    "ended_at=CASE WHEN excluded.ended_at IS NOT NULL THEN excluded.ended_at "
                    "ELSE playback_sessions.ended_at END,"
                    "end_reason=CASE WHEN excluded.end_reason IS NOT NULL THEN excluded.end_reason "
                    "ELSE playback_sessions.end_reason END "
                    "WHERE playback_sessions.ended_at IS NULL",
                    (
                        event.session_id,
                        event.player_id,
                        media_id,
                        state,
                        position,
                        duration,
                        selected_aid,
                        selected_sid,
                        selected_vid,
                        state_json,
                        timestamp,
                        timestamp,
                        timestamp if final_reason is not None else None,
                        final_reason,
                    ),
                )
                row = connection.execute(
                    f"{_SESSION_SELECT} WHERE id=?", (event.session_id,)
                ).fetchone()
                if row is None:
                    raise sqlite3.IntegrityError("playback session was not persisted")
            return _session(row)
        finally:
            connection.close()

    def checkpoint_selected_tracks(self, session_id: str, properties: Mapping[str, Any]) -> None:
        """Save fresh selection at clean shutdown without inventing a host event."""
        observed = {"selected_" + name: properties[name] for name in ("aid", "sid", "vid")
                    if type(properties.get(name)) is int and properties[name] > 0}
        if not observed:
            return  # Missing/unavailable readbacks are not new selection intent.
        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute("UPDATE playback_sessions SET " + ",".join(name + "=?" for name in observed)
                    + " WHERE id=? AND ended_at IS NULL", (*observed.values(), session_id))
        finally:
            connection.close()

    def get(self, session_id: str) -> PlaybackSession | None:
        _validate_session_id(session_id)
        connection = self.migrations.connect()
        try:
            row = connection.execute(f"{_SESSION_SELECT} WHERE id=?", (session_id,)).fetchone()
            return _session(row) if row is not None else None
        finally:
            connection.close()

    def end_session(
        self,
        session_id: str,
        *,
        reason: EndReason,
        ended_at: str | None = None,
    ) -> bool:
        _validate_session_id(session_id)
        _validate_reason(reason)
        timestamp = ended_at if ended_at is not None else self._clock()
        if not isinstance(timestamp, str) or not timestamp:
            raise PlaybackSessionError("ended_at must be a non-empty timestamp string")
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE playback_sessions SET state='stopped',ended_at=?,updated_at=?,end_reason=? "
                    "WHERE id=? AND ended_at IS NULL",
                    (timestamp, timestamp, reason, session_id),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def end_open_sessions(
        self,
        player_id: str,
        *,
        reason: Literal["clean_shutdown", "crash_recovery"],
        ended_at: str | None = None,
    ) -> int:
        if not isinstance(player_id, str) or not player_id:
            raise PlaybackSessionError("player_id must be a non-empty string")
        _validate_reason(reason)
        if reason not in {"clean_shutdown", "crash_recovery"}:
            raise PlaybackSessionError("bulk termination requires a shutdown or recovery reason")
        timestamp = ended_at if ended_at is not None else self._clock()
        if not isinstance(timestamp, str) or not timestamp:
            raise PlaybackSessionError("ended_at must be a non-empty timestamp string")
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    f"UPDATE playback_sessions SET state='stopped',ended_at=?,updated_at=?,end_reason=? "
                    f"WHERE player_id=? AND ended_at IS NULL AND state IN ({placeholders})",
                    (timestamp, timestamp, reason, player_id, *_ACTIVE_STATES),
                )
                return cursor.rowcount
        finally:
            connection.close()


def _session(row: sqlite3.Row | tuple[Any, ...]) -> PlaybackSession:
    return PlaybackSession(
        id=str(row[0]),
        player_id=str(row[1]),
        media_id=row[2],
        state=str(row[3]),
        position_s=float(row[4]),
        duration_s=row[5],
        selected_aid=row[6],
        selected_sid=row[7],
        selected_vid=row[8],
        profile_set=json.loads(row[9]),
        state_data=json.loads(row[10]),
        started_at=str(row[11]),
        updated_at=str(row[12]),
        ended_at=row[13],
        end_reason=row[14],
    )


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id:
        raise PlaybackSessionError("session_id must be a non-empty string")


def _validate_reason(reason: str) -> None:
    if not isinstance(reason, str) or reason not in _END_REASONS:
        raise PlaybackSessionError("end_reason is unsupported")


def _finite_nonnegative(value: Any, label: str, *, default: float | None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlaybackSessionError(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PlaybackSessionError(f"{label} must be a finite non-negative number")
    return result


def _optional_track_id(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlaybackSessionError(f"{label} must be a non-negative integer or null")
    return value


def _json_object(value: dict[str, Any], label: str) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PlaybackSessionError(f"{label} must be finite JSON data") from error


def _event_time(value: str | int | float) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise PlaybackSessionError("event timestamp must be ISO-8601 or epoch seconds") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise PlaybackSessionError("event timestamp must be ISO-8601 or epoch seconds")
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as error:
        raise PlaybackSessionError("event timestamp is outside the supported range") from error


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
