from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.db import (
    PlaybackHistoryStore,
    PlaybackSessionStore,
    ResumePointStore,
)
from backend.db.migration_runner import MigrationRunner
from backend.events.durable_transition_policy import DurableTransitionPolicy
from backend.events.envelope import EventEnvelope
from backend.media import MediaIdentityError, MediaIdentityStore

from .queue import PersistentQueueAdapter
from .position_checkpoint import PositionCheckpointError, validate_checkpoint_event
from .native_episode import validate_native_episode


@dataclass(frozen=True, slots=True)
class PlaybackRecovery:
    path: str | None
    position_s: float | None
    session_id: str | None
    media_id: int | None
    selected_tracks: tuple[tuple[str, int], ...] = ()


class PersistentPlaybackState:
    """Connect host events to SQLite media, session, resume, and history stores."""

    def __init__(
        self,
        migrations: MigrationRunner,
        media: MediaIdentityStore,
        sessions: PlaybackSessionStore,
        resume: ResumePointStore,
        history: PlaybackHistoryStore,
        queue: PersistentQueueAdapter,
    ) -> None:
        self.migrations = migrations
        self.media = media
        self.sessions = sessions
        self.resume = resume
        self.history = history
        self.queue = queue
        self._policy = DurableTransitionPolicy()
        self._media_by_session: dict[str, int] = {}
        self._history_by_session: dict[str, int] = {}

    def recover(self) -> PlaybackRecovery:
        recovery = self._latest_recovery()
        self._end_abandoned_state()
        # A queue marker alone is not evidence that this media was playing.
        # Recovery follows the durable playback owner; queue identity is
        # rebound separately from its explicit session binding.
        return recovery

    def validate_position_checkpoint(self, event: EventEnvelope) -> int:
        """Validate an existing durable owner before any session/resume writes."""
        validate_checkpoint_event(event)
        properties = event.payload["state"]["properties"]
        try:
            media_id = self.media.identify(properties["path"]).id
        except MediaIdentityError as error:
            # A receipt naming unavailable/different media cannot borrow the
            # last media cached for this session.
            raise PositionCheckpointError("checkpoint media is unavailable") from error
        current_session = self.sessions.get(event.session_id)
        if (current_session is None or not current_session.active
                or current_session.player_id != event.player_id
                or media_id is None or current_session.media_id != media_id):
            raise PositionCheckpointError("checkpoint does not belong to the current durable media/session")
        return media_id

    def record_event(self, event: EventEnvelope) -> None:
        if event.type in {'media.started', 'native.end-file'}:
            proof = validate_native_episode(event)
        else:
            proof = None
        force_position = event.type == "resume.checkpoint"
        media_id = self.validate_position_checkpoint(event) if force_position else None
        state = event.payload.get("state", {})
        if not isinstance(state, Mapping):
            return
        properties = state.get("properties", {})
        if not isinstance(properties, Mapping):
            return
        if not force_position:
            media_id = self._media_for_state(properties, event.session_id)
            if media_id is None and proof is not None:
                try:
                    media_id = self.media.identify(proof['path']).id
                except MediaIdentityError:
                    pass
        durable = self._policy.should_persist(event)
        reason = _end_reason(event)
        if reason == "eof" and media_id is None:
            # libmpv may report an EOF transition while the session is still
            # idle or before its path has been associated. Closing that row
            # here would make the later media-path transition immutable and
            # lose the durable session-to-media link. A real EOF after mpv
            # clears the path remains attributable through media_by_session.
            reason = None
        if durable or event.session_id not in self._media_by_session:
            self.sessions.record_transition(
                event,
                media_id=media_id,
                end_reason=reason,
            )
        if media_id is not None:
            self._media_by_session[event.session_id] = media_id
            status = state.get("status")
            session = self.sessions.get(event.session_id)
            if status in {"loading", "playing", "paused", "recovering"} and session and session.active:
                entry = self.history.record_start(
                    event.session_id,
                    media_id,
                    initial_position_s=_position(properties.get("time_pos")),
                )
                self._history_by_session[event.session_id] = entry.id
            position = properties.get("time_pos")
            if isinstance(position, (int, float)) and not isinstance(position, bool):
                duration = properties.get("duration")
                duration_value = (
                    duration
                    if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                    else None
                )
                self.resume.save_position(
                    media_id,
                    event.session_id,
                    float(position),
                    float(duration_value) if duration_value is not None else None,
                    force=force_position,
                )
                history_id = self._history_by_session.get(event.session_id)
                if history_id is not None:
                    self.history.record_progress(history_id, float(position))

        if reason is not None:
            history_id = self._history_by_session.get(event.session_id)
            if history_id is None and media_id is not None:
                history_id = self._open_history_id(event.session_id, media_id)
            if history_id is not None:
                self.history.record_end(
                    history_id,
                    last_position_s=_position(properties.get("time_pos")),
                    completed=reason == "eof",
                    end_reason=reason,
                )

    def clean_shutdown(self, state: Mapping[str, Any]) -> None:
        player_id = state.get("player_id")
        session_id = state.get("session_id")
        properties = state.get("properties", {})
        if not isinstance(player_id, str) or not player_id:
            return
        if not isinstance(session_id, str) or not session_id:
            return
        if not isinstance(properties, Mapping):
            properties = {}
        media_id = self._media_for_state(properties, session_id)
        session = self.sessions.get(session_id)
        if session is not None and media_id is None:
            media_id = session.media_id
        if session is not None and media_id is not None:
            self.sessions.checkpoint_selected_tracks(session_id, properties)
            raw_position = properties.get("time_pos")
            position = (
                _position(raw_position)
                if isinstance(raw_position, (int, float)) and not isinstance(raw_position, bool)
                else session.position_s
            )
            duration = properties.get("duration", session.duration_s)
            duration = (
                float(duration)
                if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                else None
            )
            self.resume.save_position(
                media_id,
                session_id,
                position,
                duration,
                force=True,
            )
            history_id = self._history_by_session.get(session_id)
            if history_id is None:
                history_id = self._open_history_id(session_id, media_id)
            if history_id is not None:
                self.history.record_end(
                    history_id,
                    last_position_s=position,
                    completed=False,
                    end_reason="clean_shutdown",
                )
            self.sessions.end_session(session_id, reason="clean_shutdown")

    def _latest_recovery(self) -> PlaybackRecovery:
        connection = self.migrations.connect()
        try:
            row = connection.execute(
                "SELECT ps.id,ps.media_id,ps.position_s,m.canonical_path,ps.selected_aid,ps.selected_sid,ps.selected_vid,ps.end_reason "
                "FROM playback_sessions ps LEFT JOIN media m ON m.id=ps.media_id "
                "WHERE ps.media_id IS NOT NULL ORDER BY ps.updated_at DESC,ps.started_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[3] is None:
            return PlaybackRecovery(None, None, None, None)
        if row[7] in {'eof','stopped','error'}:
            return PlaybackRecovery(None, None, str(row[0]), int(row[1]))
        try:
            media = self.media.identify(str(row[3]))
        except MediaIdentityError:
            return PlaybackRecovery(None, None, str(row[0]), int(row[1]))
        point = self.resume.get(media.id)
        position = point.position_s if point is not None else float(row[2])
        return PlaybackRecovery(
            path=media.identity.canonical_path,
            position_s=position,
            session_id=str(row[0]),
            media_id=media.id,
            selected_tracks=tuple((kind, value) for kind, value in zip(("audio", "sub", "video"), row[4:7])
                                  if type(value) is int and value > 0),
        )

    def _end_abandoned_state(self) -> None:
        connection = self.migrations.connect()
        try:
            with connection:
                open_sessions = connection.execute(
                    "SELECT id,player_id,position_s FROM playback_sessions "
                    "WHERE ended_at IS NULL AND state IN ('loading','playing','paused','recovering')"
                ).fetchall()
                open_history = connection.execute(
                    "SELECT id,session_id,last_position_s FROM playback_history WHERE ended_at IS NULL"
                ).fetchall()
        finally:
            connection.close()
        for session_id, _player_id, _position in open_sessions:
            self.sessions.end_session(str(session_id), reason="crash_recovery")
        for history_id, _session_id, position in open_history:
            self.history.record_end(
                int(history_id),
                last_position_s=float(position),
                completed=False,
                end_reason="crash_recovery",
            )

    def _media_for_state(
        self,
        properties: Mapping[str, Any],
        session_id: str,
    ) -> int | None:
        path = properties.get("path")
        if isinstance(path, str) and path:
            try:
                return self.media.identify(path).id
            except MediaIdentityError:
                pass
        return self._media_by_session.get(session_id)

    def _open_history_id(self, session_id: str, media_id: int) -> int | None:
        connection = self.migrations.connect()
        try:
            row = connection.execute(
                "SELECT id FROM playback_history WHERE session_id=? AND media_id=? "
                "AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (session_id, media_id),
            ).fetchone()
            return int(row[0]) if row is not None else None
        finally:
            connection.close()


def _position(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


def _end_reason(event: EventEnvelope) -> str | None:
    if event.type == 'native.end-file':
        validate_native_episode(event)
        return event.payload['end_reason']
    if event.type == "eof-reached":
        # libmpv reports eof-reached=false as part of the initial property
        # snapshot. Only the affirmative transition denotes completed media.
        return "eof" if event.payload.get("value") is True else None
    if event.type == "end-file":
        return "eof"
    if event.type in {"stopped", "stop"}:
        return "stopped"
    if event.type in {"error", "playback-error"}:
        return "error"
    return None
