from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.auth import TokenStore, bearer_token
from backend.api.errors import ErrorCode, StructuredApiError
from backend.api.idempotency import IdempotencyConflict, IdempotencyStore
from backend.api.startup_configuration import StartupConfiguration, StartupConfigurationStore
from backend.api.models import (
    Chapter,
    CommandResponse,
    HealthResponse,
    LoadRequest,
    MediaBrowserItem,
    MediaBrowserResponse,
    MediaMetadataResponse,
    MediaPresentation,
    PlayerStateResponse,
    Profile,
    OutputDevice,
    OutputInventoryResponse,
    ProfileApplyRequest,
    ProfileApplyConflict,
    ProfileApplyResponse,
    QueueAddRequest,
    QueueItem,
    QueueMoveRequest,
    QueueRevisionRequest,
    QueueResponse,
    SeekRequest,
    Track,
    TrackSelectRequest,
    ValueRequest,
)
from backend.db import (
    MigrationRunner,
    PlaybackHistoryStore,
    PlaybackSessionStore,
    ResumePointStore,
)
from backend.db.migration_runner import runtime_migrations_directory
from backend.domain.media_tracks import (
    MediaTrackCatalog,
    NormalizedTrack,
    TrackSelectionError,
)
from backend.domain.queue import QueueConflict
from backend.domain.player_state import PlayerOutputStatus
from backend.events.durable_transition_policy import DurableTransitionPolicy
from backend.playback.position_checkpoint import (
    CheckpointReceiptRejected, PositionCheckpointError, validate_checkpoint_event,
)
from backend.events.envelope import EventEnvelope
from backend.domain.queue import QueueStore
from backend.media import (
    MEDIA_LISTING_METADATA_BUDGET_SECONDS,
    MEDIA_LISTING_METADATA_MAX_ITEMS,
    MEDIA_LISTING_PROBE_TIMEOUT_SECONDS,
    MediaIdentityStore,
    MediaPathPolicyError,
    MediaPreviewProvider,
    MediaRootAllowlist,
    MediaSourcePolicy,
)
from backend.media.presentation import normalize_track_codec
from backend.playback import PersistentPlaybackState, PersistentQueueAdapter
from backend.playback.queue_playback import QueuePlaybackCoordinator
from backend.player_client.client import PlayerHostCommandRejected
from backend.playback.native_episode import NativeEpisodeProofError, validate_native_episode
from backend.playback.recovery import (
    PlayerHostRecoveryCoordinator,
    RecoveryAttemptsExceeded,
)
from backend.profiles.drm_modes import DrmModeReport, selected_mode
from backend.profiles.native_identity import matches_expected_native_identities
from backend.profiles.output_inventory import LinuxOutputDiscovery, PROFILE_ROLES, matching_output, device_options, unsafe_output_route
from backend.profiles.display_policy import (DISPLAY_STARTUP_NAMES, HDR_STARTUP_NAMES, DisplayPolicyError,
    device_profile_ids, device_profile_identities, display_fingerprint, display_policy_options, retained_display_profile_options,
    normalize_legacy_display_defaults, validate_display_configuration, validate_native_capabilities)
from backend.profiles import (
    DEFAULT_OPTION_CATALOG,
    OptionValidationError,
    ProfileDefinition,
    ProfileResolution,
    ProfileRuntimeApplier,
    ProfileResolver,
    ProfileRestartApplier,
    RestartApplicationError,
    RuntimeApplicationError,
    option_value_matches,
)
from backend.player_client import PlayerHostError, PlayerHostProcess
from backend.redis import (
    RedisEventStream,
    RedisLivePubSub,
    RedisPlayerLease,
    RedisRuntimeClient,
    RedisUnavailable,
    PlayerLeaseUnavailable,
)
from backend.ws.contract import EventHistory, HistoryUnavailable, WebSocketAuthError, WebSocketContract


_PROFILE_RESTART_LOGGER = logging.getLogger("uvicorn.error")
_DATABASE_RETRY_BASE_SECONDS = 0.05
_DATABASE_RETRY_MAX_SECONDS = 1.0
_AUDIO_DECODER_DIAGNOSTICS_ENV = "MPV_WEBD_AUDIO_DECODER_DIAGNOSTICS"


class _RestartFailure(RestartApplicationError):
    """Keep host-change fencing at the service's managed lifecycle boundary."""

    def __init__(self, message: str, *, host_changed: bool = False) -> None:
        super().__init__(message)
        self.host_changed = host_changed


class _StartupFailureTrackedHost:
    """Bind activation failure evidence to the child created by this restart."""

    def __init__(self, host):
        self.host = host
        self.startup_failed = False
        self.activation_failed = False
        self.created_pid = None
        self.predecessor_pid = None
        self.capture_validator = None

    def begin_activation(self):
        self.startup_failed = self.activation_failed = False
        self.created_pid = None
        self.predecessor_pid = self._observed_pid()

    def end_activation(self):
        self.created_pid = self.predecessor_pid = None
        self.startup_failed = self.activation_failed = False
        self.capture_validator = None

    def _observed_pid(self):
        try:
            pid = self.host.pid
            return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None
        except Exception:
            return None

    def _observed_stopped(self):
        try:
            return self.host.running is False
        except Exception:
            return False

    def created_player_exited(self):
        return (self.created_pid is not None and self.created_pid != self.predecessor_pid
                and self._observed_pid() == self.created_pid and self._observed_stopped())

    def __getattr__(self, name):
        return getattr(self.host, name)

    def snapshot(self):
        response = self.host.snapshot()
        if self.capture_validator is not None:
            self.capture_validator(getattr(response, "payload", response))
        return response

    def shutdown(self, *, force=False):
        # Validate every capture up to teardown, including the generic applier's
        # second snapshot. Asynchronous restoration observations may be partial.
        self.capture_validator = None
        return self.host.shutdown(force=force)

    def start(self):
        try:
            result = self.host.start()
            self.created_pid = self._observed_pid()
            return result
        except Exception:
            # A failed spawn/start requires positive stopped-process evidence.
            # Missing lifecycle evidence or a healthy child fails closed.
            self.startup_failed = self._observed_stopped()
            raise

    def initialize(self, options=None):
        try:
            return self.host.initialize(options)
        except Exception:
            self.startup_failed = self.created_player_exited()
            raise


class _IdleProfileRestartApplier(ProfileRestartApplier):
    """Preserve idle restart semantics with the accepted packaged applier.

    The accepted runtime retains the af5b2bc5 applier API. The service's idle
    restart hook was overlaid independently, so passing its new keyword to
    that unchanged dependency is invalid. Keep the dependency unchanged and
    enforce the idle/runtime/failure guarantees at this service boundary.
    """

    def __init__(self, player_host, **kwargs):
        self._startup_host = _StartupFailureTrackedHost(player_host)
        super().__init__(self._startup_host, **kwargs)

    def apply(self, restart_options, *, base_options=None, idle_only=False, rollback_options=None):
        self._startup_host.begin_activation()
        try:
            return self._apply(restart_options, base_options=base_options,
                               idle_only=idle_only, rollback_options=rollback_options)
        finally:
            self._startup_host.end_activation()

    def _apply(self, restart_options, *, base_options=None, idle_only=False, rollback_options=None):
        self._rollback_snapshot = None
        self._startup_host.capture_validator = self._validate_managed_capture if HDR_STARTUP_NAMES & restart_options.keys() else None
        self._idle_only = idle_only
        self._host_changed = False
        runtime = {}
        base = dict(base_options or {})
        response = self._player_host.snapshot()
        state = getattr(response, "payload", response)
        if not isinstance(state, Mapping):
            raise _RestartFailure("profile restart requires an observed player state")
        if idle_only and state.get("path") is not None:
            raise _RestartFailure("forced profile restart requires an idle player")
        for name in ("volume", "speed", "pause"):
            if name in state and (state[name] is not None or name == "pause"):
                runtime[name] = DEFAULT_OPTION_CATALOG.validate(name, state[name])
        if HDR_STARTUP_NAMES & restart_options.keys() and set(runtime) != {"volume", "speed", "pause"}:
            raise _RestartFailure("managed display restart requires complete observed volume, speed and pause")
        base.update(runtime)
        try:
            report = super().apply(restart_options, base_options=base)
            if runtime:
                response = self._player_host.snapshot()
                state = getattr(response, "payload", response)
                if not isinstance(state, Mapping) or any(
                    not option_value_matches(DEFAULT_OPTION_CATALOG.require(name), value, state.get(name))
                    for name, value in runtime.items()
                ):
                    raise _RestartFailure("runtime settings did not restore", host_changed=True)
            return report
        except Exception as error:
            if (not idle_only and (self._startup_host.startup_failed or self._startup_host.activation_failed) and rollback_options is not None
                    and self._rollback_snapshot is not None and set(runtime) == {"volume", "speed", "pause"}):
                # Exactly one same-release recovery of verified predecessor settings.
                # Only failed start or the bound new child dying during restore
                # qualifies. Later readback/UI errors never enter here.
                try:
                    super()._rebuild_and_restore({**base, **rollback_options}, self._rollback_snapshot)
                    observed = self._player_host.observe_restart_options(tuple(rollback_options))
                    response = self._player_host.snapshot()
                    restored = getattr(response, "payload", response)
                    if (not isinstance(observed, Mapping) or not isinstance(restored, Mapping)
                            or any(not option_value_matches(DEFAULT_OPTION_CATALOG.require(name), value, observed.get(name))
                                   for name, value in rollback_options.items())
                            or any(not option_value_matches(DEFAULT_OPTION_CATALOG.require(name), value, restored.get(name))
                                   for name, value in runtime.items())):
                        raise RestartApplicationError("predecessor settings did not verify")
                except Exception as recovery_error:
                    raise _RestartFailure("player startup failed; bounded predecessor recovery did not verify", host_changed=True) from recovery_error
                raise _RestartFailure("player startup failed; prior settings and playback were restored", host_changed=True) from error
            raise _RestartFailure(str(error), host_changed=self._host_changed) from error

    @staticmethod
    def _validate_managed_capture(state):
        if not isinstance(state, Mapping) or "path" not in state:
            raise _RestartFailure("managed display restart requires an explicit observed path")
        for name in ("volume", "speed", "pause"):
            if name not in state or state[name] is None:
                raise _RestartFailure("managed display restart requires complete observed volume, speed and pause")
            DEFAULT_OPTION_CATALOG.validate(name, state[name])
        if state["path"] is None:
            if state.get("idle-active") is not True:
                raise _RestartFailure("managed display restart requires positively observed idle state")
            return
        if not isinstance(state["path"], str) or not state["path"] or "\x00" in state["path"]:
            raise _RestartFailure("managed display restart requires a valid observed path")
        tracks = state.get("track-list")
        if (not isinstance(tracks, list) or not tracks or len(tracks) > 4096
                or any(not isinstance(track, Mapping) for track in tracks)
                or not any(track.get("type") in {"audio", "video"} and track.get("selected") is True for track in tracks)):
            raise _RestartFailure("managed display restart requires observed selected tracks")
        if "time-pos" not in state:
            raise _RestartFailure("managed display restart requires an observed playback position")
        position = state["time-pos"]
        if position is None and state.get("seekable") is False:
            return  # Explicit nonseekable media has no position to restore.
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) or position < 0:
            raise _RestartFailure("managed display restart requires a reliable playback position")

    def _rebuild_and_restore(self, initialization_options, snapshot):
        # This snapshot is the applier's later snapshot. A newly loaded path
        # must be rejected before teardown even if the first idle check passed.
        if self._idle_only and snapshot.path is not None:
            raise _RestartFailure("forced profile restart requires an idle player")
        self._rollback_snapshot = snapshot
        self._host_changed = True
        try:
            return super()._rebuild_and_restore(initialization_options, snapshot)
        except Exception:
            self._startup_host.activation_failed = self._startup_host.created_player_exited()
            raise


@dataclass(eq=False)
class _WebSocketSubscriber:
    websocket: Any
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[EventEnvelope]


class RuntimeRepository:
    """Own runtime schema initialization and read-only runtime repositories."""

    def __init__(
        self,
        database_path: str | Path,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self.migrations = MigrationRunner(database_path, migrations_dir)
        self.tokens = TokenStore(self.migrations.connect)
        self.ready = False

    @classmethod
    def from_environment(cls) -> "RuntimeRepository":
        from backend.db.runtime_path import runtime_database_path

        return cls(runtime_database_path(), runtime_migrations_directory())

    def start(self) -> None:
        self.migrations.apply()
        self.ready = True

    def close(self) -> None:
        self.ready = False

    def profiles(self) -> list[Profile]:
        if not self.ready:
            raise StructuredApiError(
                ErrorCode.PLAYER_UNAVAILABLE,
                "The runtime repository is not ready.",
            )
        connection = self.migrations.connect()
        try:
            rows = connection.execute(
                "SELECT id,name,category,enabled FROM profiles ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
        return [
            Profile(id=row[0], name=row[1], category=row[2], enabled=bool(row[3]))
            for row in rows
        ]

    def profile_definitions(self, profile_ids: list[str]) -> list[ProfileDefinition]:
        if not isinstance(profile_ids, list) or not profile_ids:
            raise ValueError("profile_ids must be a non-empty list")
        if any(not isinstance(value, str) or not value for value in profile_ids):
            raise ValueError("profile_ids must contain non-empty strings")
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("profile_ids must be unique")
        placeholders = ",".join("?" for _ in profile_ids)
        connection = self.migrations.connect()
        try:
            profile_rows = connection.execute(
                f"SELECT id,name,category FROM profiles WHERE enabled=1 AND id IN ({placeholders})",
                profile_ids,
            ).fetchall()
            by_id = {str(row[0]): row for row in profile_rows}
            if len(by_id) != len(profile_ids):
                raise KeyError("one or more requested profiles are unavailable")
            option_rows = connection.execute(
                f"SELECT profile_id,option_name,value_json FROM profile_options "
                f"WHERE profile_id IN ({placeholders}) ORDER BY profile_id,precedence,option_name",
                profile_ids,
            ).fetchall()
        finally:
            connection.close()
        options_by_id: dict[str, dict[str, Any]] = {value: {} for value in profile_ids}
        for profile_id, name, encoded in option_rows:
            options_by_id[str(profile_id)][str(name)] = json.loads(str(encoded))
        return [
            ProfileDefinition(
                id=profile_id,
                name=str(by_id[profile_id][1]),
                category=str(by_id[profile_id][2]),
                options=options_by_id[profile_id],
            )
            for profile_id in profile_ids
        ]

    def authenticate_token(self, token: str | None) -> bool:
        if not self.ready:
            return False
        return self.tokens.authenticate(token)

    def record_transition(self, event: EventEnvelope) -> None:
        state = event.payload.get("state", {})
        if not isinstance(state, Mapping):
            raise ValueError("durable event state must be an object")
        properties = state.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError("durable event properties must be an object")
        state_name = str(state.get("status", "error"))
        state_name = {"ended": "stopped"}.get(state_name, state_name)
        if state_name not in {"idle", "loading", "playing", "paused", "stopped", "error", "recovering"}:
            raise ValueError("durable event has an unsupported player state")
        position = _finite_nonnegative(properties.get("time_pos"), default=0.0)
        duration = _finite_nonnegative(properties.get("duration"), default=None)
        selected = {
            name: value if isinstance(value, int) and not isinstance(value, bool) else None
            for name, value in (
                ("selected_aid", properties.get("aid")),
                ("selected_sid", properties.get("sid")),
                ("selected_vid", properties.get("vid")),
            )
        }
        now = _utc_now()
        occurred_at = _event_time(event.timestamp)
        payload_json = json.dumps(dict(event.payload), sort_keys=True, separators=(",", ":"))
        state_json = json.dumps(dict(state), sort_keys=True, separators=(",", ":"))
        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO playback_sessions "
                    "(id,player_id,state,position_s,duration_s,selected_aid,selected_sid,selected_vid,"
                    "state_json,started_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET player_id=excluded.player_id,state=excluded.state,"
                    "position_s=excluded.position_s,duration_s=excluded.duration_s,"
                    "selected_aid=excluded.selected_aid,selected_sid=excluded.selected_sid,"
                    "selected_vid=excluded.selected_vid,state_json=excluded.state_json,"
                    "updated_at=excluded.updated_at WHERE playback_sessions.ended_at IS NULL",
                    (
                        event.session_id,
                        event.player_id,
                        state_name,
                        position,
                        duration,
                        selected["selected_aid"],
                        selected["selected_sid"],
                        selected["selected_vid"],
                        state_json,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO player_events "
                    "(player_id,session_id,sequence,event_type,occurred_at,correlation_id,payload_json) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        event.player_id,
                        event.session_id,
                        event.sequence,
                        event.type,
                        occurred_at,
                        event.correlation_id,
                        payload_json,
                    ),
                )
        finally:
            connection.close()

    def recent_events(
        self,
        player_id: str,
        session_id: str,
        *,
        limit: int = 256,
    ) -> tuple[EventEnvelope, ...]:
        if not self.ready:
            return ()
        connection = self.migrations.connect()
        try:
            rows = connection.execute(
                "SELECT sequence,event_type,occurred_at,correlation_id,payload_json "
                "FROM player_events WHERE player_id=? AND session_id=? "
                "ORDER BY sequence DESC LIMIT ?",
                (player_id, session_id, limit),
            ).fetchall()
        finally:
            connection.close()
        rows.reverse()
        return tuple(
            EventEnvelope(
                schema_version=1,
                type=row[1],
                sequence=row[0],
                timestamp=row[2],
                player_id=player_id,
                session_id=session_id,
                correlation_id=row[3],
                payload=json.loads(row[4]),
            )
            for row in rows
        )


class PlayerService:
    """Compose the owned player-host process and runtime repositories."""

    def __init__(
        self,
        *,
        player_host: PlayerHostProcess | None = None,
        runtime_repository: RuntimeRepository | None = None,
        redis_client: RedisRuntimeClient | None = None,
        mpv_options: Mapping[str, Any] | None = None,
        event_history_size: int = 256,
        persistent_state: bool = False,
        startup_idle: bool = False,
        startup_handoff: Any | None = None,
        require_startup_configuration: bool = False,
        player_lease_enabled: bool = False,
        media_root_policy: MediaRootAllowlist | MediaSourcePolicy | None = None,
        media_preview_provider: MediaPreviewProvider | None = None,
        output_discovery: LinuxOutputDiscovery | None = None,
        native_video_capability_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self.player_host = player_host or PlayerHostProcess()
        self.runtime_repository = runtime_repository or RuntimeRepository.from_environment()
        self.redis_client = redis_client or RedisRuntimeClient.from_environment()
        self.redis_event_stream = RedisEventStream(self.redis_client)
        self.redis_live_pubsub = RedisLivePubSub(self.redis_client)
        self.output_discovery = output_discovery or LinuxOutputDiscovery()
        self._native_video_capability_provider = native_video_capability_provider
        self._display_policy_errors: dict[str, str] = {}
        self._active_display_fingerprints: dict[str, str] = {}
        self.mpv_options = normalize_legacy_display_defaults(mpv_options or {})
        self._base_restart_options = _catalog_restart_options(self.mpv_options)
        self._active_profile_restart_options = dict(self._base_restart_options)
        self._profile_apply_lock = threading.Lock()
        self._output_inventory_lock = threading.Lock()
        self._device_profile_cache: dict[str, Profile] = {}
        self._active_device_profile_ids: dict[str, str] = {}
        self.idempotency = IdempotencyStore()
        self.queue = QueueStore()
        self._persistent_state_enabled = bool(persistent_state)
        if (not isinstance(require_startup_configuration, bool)
                or (require_startup_configuration and (not persistent_state or startup_idle))):
            raise ValueError("required durable startup configuration needs normal persistent startup")
        self._require_startup_configuration = require_startup_configuration
        self._startup_configuration_store = StartupConfigurationStore(self.runtime_repository.migrations)
        self._durable_startup_configuration = None
        self._startup_configuration_error = None
        if not isinstance(startup_idle, bool):
            raise ValueError("startup_idle must be a boolean")
        self._startup_idle = startup_idle
        if startup_handoff is not None and not startup_idle:
            raise ValueError("startup handoff requires explicit idle startup")
        self._startup_handoff = startup_handoff
        self._player_lease_enabled = bool(player_lease_enabled)
        self._lease_player_id = _runtime_lease_identity(
            self.runtime_repository.migrations.db_path
        )
        self._player_lease: RedisPlayerLease | None = None
        self._next_lease_maintenance_at = 0.0
        self.media_root_policy = media_root_policy
        self.media_preview_provider = media_preview_provider or MediaPreviewProvider()
        self._playback_state: PersistentPlaybackState | None = None
        self._started = False
        self._transition_policy = DurableTransitionPolicy()
        self._pipeline_lock = threading.RLock()
        self._history = EventHistory(maxsize=event_history_size)
        self._subscribers: set[_WebSocketSubscriber] = set()
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._pending_host_events: deque[EventEnvelope] = deque()
        self._checkpoint_rejections: deque[dict[str, Any]] = deque(maxlen=32)
        self._database_pipeline_error: str | None = None
        self._database_retry_delay = 0.0
        self._database_retry_after = 0.0
        self._host_recovery_lock = threading.RLock()
        self._event_drain_lock = threading.RLock()
        self._host_recovery_exhausted = False
        self._event_sequence_offset = 0
        self._pipeline_error: str | None = None
        self._redis_pipeline_error: str | None = None
        self._player_id: str | None = None
        self._session_id: str | None = None
        self._performance_probe = None
        self._queue_playback = None

    @classmethod
    def from_environment(
        cls, *, mpv_options: Mapping[str, Any] | None = None, startup_idle: bool = False,
        startup_handoff: Any | None = None,
        require_startup_configuration: bool = False,
    ) -> "PlayerService":
        diagnostics_setting = os.environ.get(_AUDIO_DECODER_DIAGNOSTICS_ENV, "0")
        if diagnostics_setting not in {"0", "1"}:
            raise ValueError(f"{_AUDIO_DECODER_DIAGNOSTICS_ENV} must be 0 or 1")
        return cls(
            player_host=PlayerHostProcess(
                capture_audio_decoder_diagnostics=diagnostics_setting == "1"
            ),
            runtime_repository=RuntimeRepository.from_environment(),
            persistent_state=True,
            startup_idle=startup_idle,
            startup_handoff=startup_handoff,
            require_startup_configuration=require_startup_configuration,
            mpv_options=mpv_options,
            player_lease_enabled=True,
            media_root_policy=MediaSourcePolicy.from_environment(),
        )

    def start(self) -> None:
        if self._started:
            return
        self.runtime_repository.start()
        try:
            # Acquire the optional ownership lease before touching durable
            # recovery state, so a competing backend cannot end the active
            # owner's SQLite session before being rejected.
            self.redis_client.start()
            self._acquire_player_lease()
            if self._persistent_state_enabled and not self._startup_idle:
                self._durable_startup_configuration = self._startup_configuration_store.read()
                if self._durable_startup_configuration is None and self._require_startup_configuration:
                    raise RuntimeError("required durable startup configuration is missing")
                if self._durable_startup_configuration is not None:
                    self.mpv_options = self._durable_startup_configuration.mpv_options
                    self._base_restart_options = _catalog_restart_options(self.mpv_options)
                    self._active_profile_restart_options = dict(self._base_restart_options)
            recovery = None
            if self._persistent_state_enabled:
                migrations = self.runtime_repository.migrations
                media = MediaIdentityStore(migrations)
                queue = PersistentQueueAdapter(migrations, media)
                self.queue = queue
                self._queue_playback = QueuePlaybackCoordinator(queue)
                self._queue_playback.abandon_claimed()
                self._playback_state = PersistentPlaybackState(
                    migrations,
                    media,
                    PlaybackSessionStore(migrations),
                    ResumePointStore(migrations),
                    PlaybackHistoryStore(migrations),
                    queue,
                )
            self._event_sequence_offset = self._history.latest_sequence or 0
            self._host_recovery_exhausted = False
            self.player_host.start()
            self.player_host.initialize(self.mpv_options)
            if self._startup_handoff is not None:
                self._startup_handoff.verify(self)
                if self._persistent_state_enabled:
                    self._checkpoint_startup_configuration()
            elif self._durable_startup_configuration is not None:
                self._durable_startup_configuration.verify(self)
            if self._playback_state is not None and not self._startup_idle:
                # Validate output before recovery can end abandoned sessions.
                recovery = self._playback_state.recover()
            self._restore_playback_recovery(recovery)
            self._started = True
            self._poll_stop.clear()
            state = self._event_stats().get("state", {})
            self._player_id = str(state.get("player_id", "")) or None
            self._session_id = str(state.get("session_id", "")) or None
            if self._player_id and self._session_id:
                for event in self.runtime_repository.recent_events(
                    self._player_id, self._session_id
                ):
                    self._history.append(event)
            self._drain_host_events()
            self._poll_thread = threading.Thread(
                target=self._poll_events,
                name="mpv-player-event-poller",
                daemon=True,
            )
            self._poll_thread.start()
        except Exception:
            self._started = False
            self.player_host.shutdown(force=True)
            self._release_player_lease()
            self.runtime_repository.close()
            self.redis_client.close()
            raise

    def close(self) -> None:
        if self._performance_probe is not None:
            self._performance_probe.close()
        self._poll_stop.set()
        poll_thread = self._poll_thread
        if poll_thread is not None and poll_thread.is_alive():
            poll_thread.join(timeout=3.0)
        self._poll_thread = None
        try:
            with self._host_recovery_lock:
                if self._playback_state is not None and self._started:
                    if self._durable_startup_configuration is not None:
                        try:
                            self._checkpoint_startup_configuration()
                        except (PlayerHostError, ValueError, RuntimeError, sqlite3.Error):
                            # Settings persistence must not suppress the media
                            # checkpoint when fresh state is still available.
                            pass
                    try:
                        state = self._event_stats().get("state", {})
                        if isinstance(state, Mapping):
                            self._playback_state.clean_shutdown(state)
                    except (StructuredApiError, PlayerHostError, ValueError, RuntimeError, sqlite3.Error) as error:
                        # Still tear down the host and local runtime if the final
                        # best-effort checkpoint cannot be obtained.
                        _PROFILE_RESTART_LOGGER.warning("Final playback checkpoint unavailable (%s); continuing teardown", type(error).__name__)
                self.player_host.shutdown()
                self._release_player_lease()
        finally:
            self.runtime_repository.close()
            self.redis_client.close()
            self._started = False

    def performance_probe_start(self, request, idempotency_key=None):
        # Construct lazily only on explicit POST. GET, startup and playback never
        # create the manager or a collector, and this path never uses recovery.
        from .performance_probes import OwnedProbeSource, PerformanceProbeManager
        with self._host_recovery_lock:
            if not self._started:
                raise StructuredApiError(ErrorCode.PLAYER_UNAVAILABLE, "Playback service is not ready.")
            if self._performance_probe is None:
                self._performance_probe = PerformanceProbeManager(OwnedProbeSource(self))
            key = idempotency_key if idempotency_key is not None else request.request_key
            return self._performance_probe.start(request.duration_s, key)

    def performance_probe_cancel(self, request):
        if self._performance_probe is None:
            raise StructuredApiError(ErrorCode.NOT_FOUND, "Probe run is not retained.")
        return self._performance_probe.cancel(request.run_id)

    def performance_probe_status(self):
        if self._performance_probe is None:
            return {"state": "disabled", "run_id": None, "in_flight": False,
                    "native_completion_unconfirmed": False, "samples": 0}
        return self._performance_probe.status()

    def performance_probe_result(self):
        if self._performance_probe is None:
            return {"status": self.performance_probe_status(), "samples": [], "hwdec_current": None}
        return self._performance_probe.result()

    def _recovered_snapshot(self, deadline: float):
        """Retry only the fixed media/track transition rejection within its budget.

        The native fence remains strict. A rejected sample supplies no facts;
        restoration must obtain a new stable sample or fail by the existing
        deadline. Other host errors retain their immediate failure behavior.
        """
        while True:
            try:
                return self.player_host.snapshot()
            except PlayerHostError as error:
                if str(error) != "media or track changed during fixed snapshot":
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PlayerHostError("durable recovery snapshot did not settle") from error
                time.sleep(min(0.025, remaining))

    def _restore_playback_recovery(self, recovery) -> None:
        if recovery is None or recovery.path is None:
            return
        if (self._durable_startup_configuration is not None
                and not self._durable_startup_configuration.document["recover_media"]):
            return  # Latest explicit idle/stop intent is not historical media.
        recovered_path = self._validated_media_path(
            recovery.path,
            allow_network=False,
        )
        if getattr(self, '_queue_playback', None) is not None:
            intent = self._queue_playback.prepare_recovery(recovered_path, recovery.session_id)
            self._submit_queue_intent(intent)
        else:
            self.player_host.load(recovered_path)
        seek_required = recovery.position_s is not None and recovery.position_s > 0
        self._wait_for_recovered_media(recovered_path, seekable=seek_required)
        self.player_host.pause()
        self._wait_for_recovered_media(recovered_path, paused=True)
        if seek_required:
            self.player_host.seek_absolute(recovery.position_s)
        for track_type, track_id in getattr(recovery, "selected_tracks", ()):
            # A coherent early sample can precede demux track announcements.
            # Observe availability within the existing stage budget; never
            # select or accept a track that has not been seen in a fenced sample.
            deadline = time.monotonic() + 2.0
            while True:
                response = self._recovered_snapshot(deadline)
                snapshot = getattr(response, "payload", response)
                tracks = snapshot.get("track-list", []) if isinstance(snapshot, Mapping) else []
                if any(track.get("type") == track_type and track.get("id") == track_id
                       for track in tracks if isinstance(track, Mapping)):
                    break
                if time.monotonic() >= deadline:
                    raise PlayerHostError("durable selected track is unavailable in recovered media")
                time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
            self.player_host.select_track(track_id, track_type)
            property_name = {"audio": "aid", "sub": "sid", "video": "vid"}[track_type]
            deadline = time.monotonic() + 2.0
            while True:
                response = self._recovered_snapshot(deadline)
                observed = getattr(response, "payload", response)
                if isinstance(observed, Mapping) and observed.get(property_name) == track_id:
                    break
                if time.monotonic() >= deadline:
                    raise PlayerHostError("durable selected track did not verify after recovery")
                time.sleep(0.025)
        if self._durable_startup_configuration is not None and seek_required:
            deadline = time.monotonic() + 2.0
            while True:
                response = self._recovered_snapshot(deadline)
                observed = getattr(response, "payload", response)
                position = observed.get("time-pos") if isinstance(observed, Mapping) else None
                if (type(position) in (int, float) and math.isfinite(position)
                        and abs(position - recovery.position_s) <= 0.5
                        and observed.get("seeking") is False):
                    break
                if time.monotonic() >= deadline:
                    raise PlayerHostError("durable resume position did not verify after recovery")
                time.sleep(0.025)
        # Legacy recovery stays paused. A retained configuration follows an
        # explicitly persisted play/pause intent, rather than the old capture.
        if (self._durable_startup_configuration is not None
                and self._durable_startup_configuration.document["expected_options"]["pause"] is False):
            self.player_host.resume()
        if self._durable_startup_configuration is not None:
            deadline = time.monotonic() + 2.0
            expected = self._durable_startup_configuration.document["expected_options"]
            while True:
                response = self._recovered_snapshot(deadline)
                observed = getattr(response, "payload", response)
                if isinstance(observed, Mapping) and all(option_value_matches(
                        DEFAULT_OPTION_CATALOG.require(name), expected[name], observed.get(name))
                        for name in ("volume", "speed", "pause")):
                    break
                if time.monotonic() >= deadline:
                    raise PlayerHostError("durable runtime settings did not verify after recovery")
                time.sleep(0.025)
        if self._durable_startup_configuration is not None and HDR_STARTUP_NAMES & self._active_profile_restart_options.keys():
            response = self._recovered_snapshot(time.monotonic() + 2.0)
            snapshot = getattr(response, "payload", response)
            expected_video = isinstance(snapshot, Mapping) and any(
                track.get("type") == "video" for track in snapshot.get("track-list", []) if isinstance(track, Mapping))
            if self._managed_renderer_pending(self._active_profile_restart_options, wait=True, expected_video=expected_video):
                raise PlayerHostError("durable startup managed renderer did not activate")

    def _checkpoint_startup_configuration(self) -> None:
        with self._host_recovery_lock:
            try:
                configuration = StartupConfiguration.capture(self)
                self._startup_configuration_store.save(configuration)
            except (PlayerHostError, ValueError, RuntimeError, sqlite3.Error) as error:
                self._startup_configuration_error = type(error).__name__
                raise
            self._durable_startup_configuration = configuration
            self._startup_configuration_error = None

    def _restore_latest_durable_playback(self) -> None:
        # The explicit administrative idle bootstrap must remain idle if its
        # child dies during the deployment, not reload the last historical file.
        if self._startup_idle or self._playback_state is None:
            return
        recovery = self._playback_state.recover()
        self._restore_playback_recovery(recovery)

    def _acquire_player_lease(self) -> None:
        if not self._player_lease_enabled or self.redis_client.settings.url is None:
            return
        if not self.redis_client.health.available:
            self._next_lease_maintenance_at = (
                time.monotonic() + RedisPlayerLease.DEFAULT_RENEW_INTERVAL_SECONDS
            )
            return
        lease = RedisPlayerLease(self.redis_client, self._lease_player_id)
        try:
            acquired = lease.acquire()
        except RedisUnavailable:
            self._next_lease_maintenance_at = (
                time.monotonic() + RedisPlayerLease.DEFAULT_RENEW_INTERVAL_SECONDS
            )
            return
        if not acquired:
            raise PlayerLeaseUnavailable(
                "the runtime database is already controlled by another backend"
            )
        self._player_lease = lease
        self._next_lease_maintenance_at = (
            time.monotonic() + lease.renew_interval_seconds
        )

    def _maintain_player_lease(self) -> None:
        if not self._player_lease_enabled or self.redis_client.settings.url is None:
            return
        now = time.monotonic()
        if now < self._next_lease_maintenance_at:
            return
        self._next_lease_maintenance_at = (
            now + RedisPlayerLease.DEFAULT_RENEW_INTERVAL_SECONDS
        )

        lease = self._player_lease
        if lease is None:
            if not self.redis_client.probe().available:
                return
            lease = RedisPlayerLease(self.redis_client, self._lease_player_id)
            try:
                acquired = lease.acquire()
            except RedisUnavailable:
                return
            if not acquired:
                self._stop_after_player_lease_loss()
                return
            self._player_lease = lease
            self._next_lease_maintenance_at = now + lease.renew_interval_seconds
            return

        try:
            renewed = lease.renew()
        except RedisUnavailable:
            # Redis is an optional projection/coordination layer; continue
            # local playback during outages and fail closed if ownership is
            # demonstrably lost after Redis becomes reachable again.
            return
        if not renewed:
            self._stop_after_player_lease_loss()

    def _stop_after_player_lease_loss(self) -> None:
        self._player_lease = None
        self._host_recovery_exhausted = True
        self._pipeline_error = "player lease ownership was lost"
        self._poll_stop.set()
        with self._host_recovery_lock:
            try:
                self.player_host.shutdown(force=True)
            except Exception:
                pass
        self._started = False
        self.runtime_repository.close()

    def _release_player_lease(self) -> None:
        lease, self._player_lease = self._player_lease, None
        self._next_lease_maintenance_at = 0.0
        if lease is None:
            return
        try:
            lease.release()
        except RedisUnavailable:
            pass

    def _recover_player_host(self) -> None:
        if self._host_recovery_exhausted or self._poll_stop.is_set():
            return
        self._event_sequence_offset = self._history.latest_sequence or 0
        try:
            with self._host_recovery_lock:
                # The poller may have waited behind a user mutation while an
                # explicit restart requested its exit. Never rebuild twice.
                if self._poll_stop.is_set():
                    return
                if self._durable_startup_configuration is not None and not self._startup_idle:
                    try:
                        configuration = self._startup_configuration_store.read()
                        if configuration is None:
                            raise ValueError("durable startup configuration disappeared")
                    except (TypeError, ValueError, RuntimeError, sqlite3.Error) as error:
                        self._startup_configuration_error = type(error).__name__
                        self._pipeline_error = "durable startup configuration could not be read"
                        self._host_recovery_exhausted = True
                        return
                    self._durable_startup_configuration = configuration
                    self.mpv_options = configuration.mpv_options
                    self._active_profile_restart_options = _catalog_restart_options(self.mpv_options)
                coordinator = PlayerHostRecoveryCoordinator(
                    self.player_host,
                    ({**self.mpv_options, **self._active_profile_restart_options}
                     if self._startup_idle else self.mpv_options),
                )
                def restore_verified_current_state():
                    if self._durable_startup_configuration is not None and not self._startup_idle:
                        self._durable_startup_configuration.verify(self)
                    self._restore_latest_durable_playback()
                coordinator.recover(restore_verified_current_state)
        except RecoveryAttemptsExceeded:
            self._host_recovery_exhausted = True
            self._pipeline_error = "player-host recovery exhausted its retry budget"
            return
        self._pipeline_error = None

    def health_live(self) -> HealthResponse:
        self._require_started()
        return HealthResponse(status="ok")

    def health_ready(self) -> HealthResponse:
        self._require_started()
        if not self.runtime_repository.ready:
            raise StructuredApiError(
                ErrorCode.PLAYER_UNAVAILABLE,
                "The runtime repository is not ready.",
            )
        if (self._pipeline_error is not None or self._database_pipeline_error is not None
                or self._startup_configuration_error is not None):
            raise self._player_unavailable()
        redis_health = self.redis_client.probe()
        redis_required_but_unavailable = (
            self.redis_client.settings.url is not None and not redis_health.available
        )
        if redis_required_but_unavailable or self._redis_pipeline_error is not None:
            return HealthResponse(status="degraded")
        return HealthResponse(status="ready")

    def player_state(self) -> PlayerStateResponse:
        state = self._event_stats().get("state", {})
        properties = state.get("properties", {})
        return PlayerStateResponse(
            player_id=state.get("player_id", ""),
            session_id=state.get("session_id"),
            status=state.get("status", "error"),
            position=properties.get("time_pos"),
            duration=properties.get("duration"),
            seekable=properties.get("seekable"),
            path=properties.get("path"),
            volume=properties.get("volume"),
            speed=properties.get("speed"),
            pause=properties.get("pause", False),
        )

    def media_browse(
        self,
        *,
        directory: str | None = None,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
        version: str | None = None,
        sort: str = "name",
        details: bool = True,
    ) -> MediaBrowserResponse:
        self._require_started()
        policy = self.media_root_policy
        roots = (
            policy
            if isinstance(policy, MediaRootAllowlist)
            else policy.local_files
            if isinstance(policy, MediaSourcePolicy)
            else None
        )
        if roots is None or not roots.roots:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "No configured media roots are available for browsing.",
            )
        from backend.media.directory_index import DirectoryChanged
        try:
            listing = roots.browse(directory, search=search, limit=limit, offset=offset, version=version, sort=sort)
        except DirectoryChanged as error:
            raise StructuredApiError(ErrorCode.CONFLICT, str(error)) from error
        except MediaPathPolicyError as error:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "The requested media directory is not allowed.",
            ) from error
        metadata_started = time.monotonic()
        metadata_attempts = 0
        items: list[MediaBrowserItem] = []
        for item in listing.items:
            probe_metadata = details and (item.is_directory or (
                metadata_attempts < MEDIA_LISTING_METADATA_MAX_ITEMS
                and time.monotonic() - metadata_started < MEDIA_LISTING_METADATA_BUDGET_SECONDS
            ))
            metadata_timeout = None
            if probe_metadata and not item.is_directory:
                metadata_attempts += 1
                metadata_timeout = min(
                    MEDIA_LISTING_PROBE_TIMEOUT_SECONDS,
                    max(
                        0.1,
                        MEDIA_LISTING_METADATA_BUDGET_SECONDS
                        - (time.monotonic() - metadata_started),
                    ),
                )
            items.append(
                self._media_browser_item(
                    roots,
                    item,
                    metadata_timeout_seconds=metadata_timeout,
                    probe_metadata=probe_metadata,
                )
            )
        return MediaBrowserResponse(
            directory=listing.directory,
            parent_directory=listing.parent_directory,
            items=items,
            truncated=False, total=listing.total, total_files=listing.total_files, offset=listing.offset, next_offset=listing.next_offset, version=listing.version,
        )

    def _media_browser_item(
        self,
        roots: MediaRootAllowlist,
        item: Any,
        *,
        metadata_timeout_seconds: float | None = None,
        probe_metadata: bool = True,
    ) -> MediaBrowserItem:
        if item.is_directory:
            return MediaBrowserItem(
                name=item.name,
                path=item.path,
                is_directory=True,
                size_bytes=None,
                modified_at=item.modified_at,
                media_kind="directory",
            )
        presentation = MediaPresentation()
        preview_kind = "placeholder"
        has_preview = True
        try:
            sibling_art = roots.preview_for(item.path)
            description = (
                self.media_preview_provider.describe(
                    item.path,
                    sibling_art,
                    timeout_seconds=metadata_timeout_seconds,
                )
                if probe_metadata
                else None
            )
            if description is not None:
                presentation = MediaPresentation(**description.presentation.as_dict())
                preview_kind = description.preview_kind
                has_preview = description.has_preview
        except (OSError, RuntimeError, ValueError):
            pass
        return MediaBrowserItem(
            name=item.name,
            path=item.path,
            is_directory=False,
            size_bytes=item.size_bytes,
            modified_at=item.modified_at,
            media_kind=item.media_kind,
            has_preview=has_preview,
            preview_kind=preview_kind,
            presentation=presentation,
        )

    def media_metadata(self, path: str) -> MediaMetadataResponse:
        self._require_started()
        policy = self.media_root_policy
        roots = (
            policy
            if isinstance(policy, MediaRootAllowlist)
            else policy.local_files
            if isinstance(policy, MediaSourcePolicy)
            else None
        )
        if roots is None or not roots.roots:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "No configured media roots are available for metadata.",
            )
        try:
            canonical = roots.resolve(path)
            sibling_art = roots.preview_for(canonical)
            description = self.media_preview_provider.describe(canonical, sibling_art)
        except MediaPathPolicyError as error:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "The requested media metadata is not allowed.",
            ) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise StructuredApiError(
                ErrorCode.NOT_FOUND,
                "The requested media metadata is unavailable.",
            ) from error
        return MediaMetadataResponse(
            path=canonical,
            preview_kind=description.preview_kind,
            has_preview=description.has_preview,
            presentation=MediaPresentation(**description.presentation.as_dict()),
        )

    def media_preview(self, path: str) -> Any:
        self._require_started()
        policy = self.media_root_policy
        roots = (
            policy
            if isinstance(policy, MediaRootAllowlist)
            else policy.local_files
            if isinstance(policy, MediaSourcePolicy)
            else None
        )
        if roots is None or not roots.roots:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "No configured media roots are available for previews.",
            )
        try:
            canonical = roots.resolve(path)
            sibling_art = roots.preview_for(canonical)
        except MediaPathPolicyError as error:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "The requested media preview is not allowed.",
            ) from error
        try:
            asset = self.media_preview_provider.render(canonical, sibling_art)
        except (OSError, RuntimeError, ValueError) as error:
            raise StructuredApiError(
                ErrorCode.NOT_FOUND,
                "No bounded preview is available for this media item.",
            ) from error
        from starlette.responses import Response

        return Response(
            content=asset.body,
            media_type=asset.media_type,
            headers={"Cache-Control": "private, max-age=60"},
        )

    def player_load(
        self,
        request: LoadRequest,
        idempotency_key: str | None = None,
    ) -> CommandResponse:
        path = self._validated_media_path(request.path)
        key = idempotency_key if idempotency_key is not None else request.request_key
        intent = []

        def prepare_load():
            if self._queue_playback is not None:
                intent.append(self._queue_playback.prepare(path=path, source='direct'))

        def load_after_revalidation() -> None:
            # Re-check immediately before handing the path to MPV. This narrows
            # the validation/open window without pretending to eliminate a
            # trusted-local-filesystem race after this check.
            revalidated_path = self._validated_media_path(request.path)
            if revalidated_path != path:
                raise StructuredApiError(
                    ErrorCode.FORBIDDEN_MEDIA,
                    "The requested media path changed during validation.",
                )
            if intent:
                return self._submit_queue_intent(intent[0])
            else:
                self.player_host.load(revalidated_path)

        return self._mutate(
            "load",
            key,
            {"path": path},
            load_after_revalidation,
            before_host=prepare_load,
        )

    def player_play(self, idempotency_key: str | None = None) -> CommandResponse:
        return self._mutate("play", idempotency_key, {}, self.player_host.play,
                            before_host=self._queue_playback.cancel_claimed_replay if self._queue_playback else None)

    def player_pause(self, idempotency_key: str | None = None) -> CommandResponse:
        return self._mutate("pause", idempotency_key, {}, self.player_host.pause)

    def player_seek(
        self,
        request: SeekRequest,
        idempotency_key: str | None = None,
    ) -> CommandResponse:
        key = idempotency_key if idempotency_key is not None else request.request_key
        if request.relative:
            operation = lambda: self.player_host.seek_relative(request.seconds)
        else:
            operation = lambda: self.player_host.seek_absolute(request.seconds)
        return self._mutate(
            "seek-relative" if request.relative else "seek-absolute",
            key,
            {"seconds": request.seconds, "relative": request.relative},
            operation,
            before_host=self._queue_playback.cancel_claimed_replay if self._queue_playback else None,
        )

    def player_stop(self, idempotency_key: str | None = None) -> CommandResponse:
        intent = []
        def prepare_stop():
            if self._queue_playback is not None:
                intent.append(self._queue_playback.prepare(source='stop'))
        def stop():
            if intent:
                if self._queue_playback.submit(intent[0]) is None:
                    return False
            try:
                self.player_host.stop()
            except Exception as error:
                if intent:
                    phase = 'failed' if isinstance(error, PlayerHostCommandRejected) or not isinstance(error, (PlayerHostError,OSError)) else 'uncertain'
                    self._queue_playback.outcome(intent[0], phase, type(error).__name__)
                raise
            if intent:
                self._queue_playback.outcome(intent[0], 'confirmed')
        return self._mutate("stop", idempotency_key, {}, stop, before_host=prepare_stop)

    def player_volume(
        self,
        request: ValueRequest,
        idempotency_key: str | None = None,
    ) -> CommandResponse:
        key = idempotency_key if idempotency_key is not None else request.request_key
        return self._mutate(
            "volume",
            key,
            {"value": request.value},
            lambda: self.player_host.set_volume(request.value),
        )

    def player_speed(
        self,
        request: ValueRequest,
        idempotency_key: str | None = None,
    ) -> CommandResponse:
        key = idempotency_key if idempotency_key is not None else request.request_key
        return self._mutate(
            "speed",
            key,
            {"value": request.value},
            lambda: self.player_host.set_speed(request.value),
        )

    def player_tracks(self) -> list[Track]:
        raw_tracks = self._event_stats().get("state", {}).get("properties", {}).get("track_list", [])
        normalized: list[Track] = []
        for track in raw_tracks:
            if not isinstance(track, Mapping) or not isinstance(track.get("id"), int):
                continue
            track_type = str(track.get("type", "unknown"))
            stream = dict(track)
            if isinstance(track.get("codec"), str):
                stream["codec_name"] = track["codec"]
            if isinstance(track.get("codec-profile"), str):
                stream["profile"] = track["codec-profile"]
            codec = normalize_track_codec(track_type, stream)
            normalized.append(
                Track(
                    id=int(track["id"]),
                    type=track_type,
                    title=track.get("title"),
                    language=track.get("lang"),
                    codec=codec,
                    selected=bool(track.get("selected", False)),
                )
            )
        return normalized

    def player_select_track(
        self,
        track_id: int,
        request: TrackSelectRequest,
    ) -> CommandResponse:
        catalog = MediaTrackCatalog()
        tracks = self.player_tracks()
        catalog.replace(
            [
                NormalizedTrack(
                    id=track.id,
                    type=track.type,
                    title=track.title,
                    language=track.language,
                    codec=track.codec,
                    selected=track.selected,
                )
                for track in tracks
            ],
            (),
        )
        try:
            catalog.select_track(track_id, request.type)
        except TrackSelectionError as error:
            raise StructuredApiError(
                ErrorCode.NOT_FOUND,
                "The requested track is not selectable in the current media.",
            ) from error
        selector = getattr(self.player_host, "select_track", None)
        if not callable(selector):
            raise self._player_unavailable()
        return self._mutate(
            "select-track",
            request.request_key,
            {"track_id": track_id, "type": request.type},
            lambda: selector(track_id, request.type),
        )

    def player_chapters(self) -> list[Chapter]:
        raw_chapters = self._event_stats().get("state", {}).get("properties", {}).get("chapter_list", [])
        chapters: list[Chapter] = []
        for index, chapter in enumerate(raw_chapters):
            if not isinstance(chapter, Mapping):
                continue
            time_value = chapter.get("time")
            if isinstance(time_value, bool) or not isinstance(time_value, (int, float)):
                continue
            chapter_id = chapter.get("id", index)
            if isinstance(chapter_id, bool) or not isinstance(chapter_id, int):
                chapter_id = index
            chapters.append(
                Chapter(
                    id=chapter_id,
                    title=chapter.get("title"),
                    time=max(0.0, float(time_value)),
                )
            )
        return chapters

    def queue_get(self) -> QueueResponse:
        snapshot = self._queue_playback.snapshot() if self._queue_playback else self.queue.snapshot()
        return QueueResponse(
            id=str(snapshot["id"]),
            name=str(snapshot["name"]),
            revision=int(snapshot["revision"]),
            items=[QueueItem(**item) for item in snapshot["items"]],
            playback=snapshot.get('playback'),
        )

    def queue_add(self, request: QueueAddRequest) -> QueueItem:
        path = self._validated_media_path(request.path, allow_network=False)
        try:
            entry = self.queue.append(
                path,
                ordinal=request.ordinal,
                expected_revision=request.expected_revision,
            )
        except QueueConflict as error:
            raise StructuredApiError(ErrorCode.CONFLICT, str(error)) from error
        except ValueError as error:
            raise StructuredApiError(ErrorCode.INVALID_REQUEST, str(error)) from error
        return QueueItem(
            id=entry.id,
            path=entry.path,
            ordinal=entry.ordinal,
            active=entry.active,
        )

    def queue_remove(
        self,
        item_id: str,
        expected_revision: int | None,
    ) -> QueueResponse:
        try:
            self.queue.remove(item_id, expected_revision=expected_revision)
        except QueueConflict as error:
            code = ErrorCode.NOT_FOUND if "does not exist" in str(error) else ErrorCode.CONFLICT
            raise StructuredApiError(code, str(error)) from error
        return self.queue_get()

    def queue_move(
        self,
        item_id: str,
        request: QueueMoveRequest,
    ) -> QueueResponse:
        try:
            self.queue.move(
                item_id,
                request.ordinal,
                expected_revision=request.expected_revision,
            )
        except QueueConflict as error:
            code = ErrorCode.NOT_FOUND if "does not exist" in str(error) else ErrorCode.CONFLICT
            raise StructuredApiError(code, str(error)) from error
        except ValueError as error:
            raise StructuredApiError(ErrorCode.INVALID_REQUEST, str(error)) from error
        return self.queue_get()

    def queue_activate(
        self,
        item_id: str,
        request: QueueRevisionRequest,
    ) -> QueueResponse:
        try:
            if self._queue_playback is not None:
                self._require_started()
                intent = self._queue_playback.prepare(item_id=item_id, expected_revision=request.expected_revision)
                self._submit_queue_intent(intent)
                self._drain_host_events()
            else:
                self.queue.activate(item_id, expected_revision=request.expected_revision)
        except QueueConflict as error:
            code = ErrorCode.NOT_FOUND if "does not exist" in str(error) else ErrorCode.CONFLICT
            raise StructuredApiError(code, str(error)) from error
        return self.queue_get()

    def _submit_queue_intent(self, identity):
        with self._host_recovery_lock:
            command = self._queue_playback.submit(identity)
            if command is None:
                return False
            try:
                path = self._validated_media_path(command['path'])
                if getattr(self.player_host, 'supports_native_episode', False) or command['source'] in ('manual','auto'):
                    self.player_host.request('load', {'path': path, 'queue_intent_id': identity})
                else:
                    self.player_host.load(path)
            except PlayerHostCommandRejected as error:
                self._queue_playback.outcome(identity, 'failed', type(error).__name__)
                raise self._player_unavailable() from error
            except (PlayerHostError, OSError) as error:
                self._queue_playback.outcome(identity, 'uncertain', type(error).__name__)
                raise self._player_unavailable() from error
            except Exception as error:
                self._queue_playback.outcome(identity, 'failed', type(error).__name__)
                raise
            if self._durable_startup_configuration is not None:
                self._startup_configuration_store.update_media_intent(True)
                self._durable_startup_configuration = self._startup_configuration_store.read()
            return True

    def queue_clear(self, expected_revision: int | None) -> QueueResponse:
        try:
            self.queue.clear(expected_revision=expected_revision)
        except QueueConflict as error:
            raise StructuredApiError(ErrorCode.CONFLICT, str(error)) from error
        return self.queue_get()

    def _native_video_capabilities(self):
        provider = self._native_video_capability_provider or getattr(self.player_host, "observe_native_video_capabilities", None)
        if not callable(provider):
            raise DisplayPolicyError("Native GPU-next/DRM capability evidence is unavailable.")
        try:
            result = provider()
            validate_native_capabilities(result)
            return result
        except DisplayPolicyError:
            raise
        except Exception as error:
            raise DisplayPolicyError("The qualified native GPU-next/DRM capabilities could not be verified.") from error

    def _device_profiles(self, inventory):
        definitions = {}
        current = {}
        errors = {}
        native = None
        native_error = None
        if any(device.method == "DRM" for device in inventory.devices):
            try:
                native = self._native_video_capabilities()
            except DisplayPolicyError as error:
                native_error = str(error)
        for profile_id, device in device_profile_identities(inventory.devices).items():
            options = device_options(device)
            category = "display" if device.method == "DRM" else "audio"
            name = f"{device.name or device.route} · {device.method}"
            explicit_sdr = profile_id.endswith(":sdr")
            mode = selected_mode(device, profile_id)
            if mode is not None:
                name += " · " + mode.label
            if explicit_sdr:
                name += " · SDR"
            identity_options = options
            if options and device.method == "DRM":
                try:
                    if native_error:
                        raise DisplayPolicyError(native_error)
                    options = {**options, **display_policy_options(device, native, intent="sdr" if explicit_sdr else "auto")}
                    if mode is not None:
                        options["drm-mode"] = mode.selector
                except DisplayPolicyError as error:
                    errors[profile_id] = str(error)
                    options = None
            route = (f"{identity_options['drm-device']} / {identity_options['drm-connector']}" if identity_options and device.method == "DRM"
                     else options["audio-device"] if options else device.route)
            current[profile_id] = Profile(id=profile_id, name=name, category=category,
                kind="device", route=route, enabled=options is not None, device_reported=True)
            if options:
                definitions[profile_id] = ProfileDefinition(profile_id, name, category, options)
        with self._output_inventory_lock:
            self._display_policy_errors = errors
            for profile_id, profile in list(self._device_profile_cache.items()):
                if profile_id not in current:
                    self._device_profile_cache[profile_id] = profile.model_copy(update={"enabled": False, "device_reported": False})
            self._device_profile_cache.update(current)
            # Bound metadata retained across hotplug. Active identities are kept;
            # clients independently retain any selected unavailable identity.
            while len(self._device_profile_cache) > 1024:
                removable = next((key for key in self._device_profile_cache
                    if key not in current and key not in self._active_device_profile_ids.values()), None)
                if removable is None:
                    break
                del self._device_profile_cache[removable]
            rows = list(self._device_profile_cache.values())
        return rows, definitions

    def _reported_outputs(self):
        inventory = self.output_discovery.discover()
        profiles = self.runtime_repository.profiles()
        enabled = [profile.id for profile in profiles if profile.enabled]
        definitions = self.runtime_repository.profile_definitions(enabled) if enabled else []
        matches = {
            definition.id: matching_output({**self.mpv_options, **definition.options}, inventory)
            for definition in definitions
            if definition.category == "audio" and ("ao" in definition.options or "audio-device" in definition.options)
        }
        audio_routes = {}
        for definition in definitions:
            if definition.category != "audio" or not ({"ao", "audio-device"} & definition.options.keys()):
                continue
            options = {**self.mpv_options, **definition.options}
            route = options.get("audio-device")
            method = {"alsa": "ALSA", "pipewire": "PipeWire"}.get(options.get("ao"))
            if method and isinstance(route, str) and route != "auto":
                audio_routes[definition.id] = (route, method)
        return inventory, profiles, matches, audio_routes

    def output_inventory(self) -> OutputInventoryResponse:
        inventory, _profiles, matches, _routes = self._reported_outputs()
        return OutputInventoryResponse(
            devices=[OutputDevice(
                id=device.id, name=device.name, method=device.method, route=device.route,
                kind=device.kind,
                profile_ids=list(device_profile_ids(device)) + [profile_id for profile_id, match in matches.items() if match == device],
            ) for device in inventory.devices],
            unavailable=list(inventory.unavailable),
        )

    def profiles(self) -> list[Profile]:
        inventory, profiles, matches, routes = self._reported_outputs()
        result, _definitions = self._device_profiles(inventory)
        for profile in profiles:
            name, kind = PROFILE_ROLES.get(profile.id, (profile.name, "profile"))
            device = matches.get(profile.id)
            route = None
            reported = None
            if device is not None:
                name, kind = f"{device.name or device.route} · {device.method}", "device"
                route, reported = routes[profile.id][0], True
            elif profile.id in routes:
                route, method = routes[profile.id]
                name, kind = f"{route} · {method}", "device"
                # ALSA plugin aliases are not hardware enumeration rows. Unknown
                # aliases and unavailable discovery cannot prove device absence.
                enumerable = route.startswith("alsa/hw:") or route.startswith("pipewire/")
                reported = False if enumerable and method not in inventory.unavailable else None
            enabled = profile.enabled
            if profile.id in routes:
                selected_route, method = routes[profile.id]
                if unsafe_output_route({"ao": method.lower(), "audio-device": selected_route}, inventory):
                    enabled = False
            result.append(profile.model_copy(update={"name": name, "kind": kind, "route": route, "device_reported": reported, "enabled": enabled}))
        return result

    def resolve_profile_selection(
        self,
        request: ProfileApplyRequest,
    ) -> ProfileResolution:
        """Validate profile IDs and typed overrides without forwarding raw options."""

        self._require_started()
        try:
            legacy_ids = [key for key in request.profile_ids if not key.startswith("output:")]
            profiles = self.runtime_repository.profile_definitions(legacy_ids) if legacy_ids else []
            inventory = self.output_discovery.discover()
            _rows, devices = self._device_profiles(inventory)
            for key in request.profile_ids:
                if key.startswith("output:"):
                    if key not in devices and key in self._display_policy_errors:
                        raise StructuredApiError(ErrorCode.PLAYER_UNAVAILABLE, self._display_policy_errors[key])
                    profiles.append(devices[key])
        except KeyError as error:
            raise StructuredApiError(
                ErrorCode.NOT_FOUND,
                "One or more selected profiles are unavailable.",
            ) from error
        try:
            for profile in profiles:
                for name, value in profile.options.items():
                    DEFAULT_OPTION_CATALOG.validate(name, value)
            overrides = {
                name: DEFAULT_OPTION_CATALOG.validate_request_override(name, value)
                for name, value in request.overrides.items()
            }
            resolution = ProfileResolver().resolve(profiles, request_overrides=overrides)
            validate_display_configuration(resolution.options)
            fingerprints = {identity: display_fingerprint(device, identity)
                            for identity, device in device_profile_identities(inventory.devices).items()
                            if device.method == "DRM" and identity in request.profile_ids}
            return replace(resolution, output_fingerprints=fingerprints)
        except (OptionValidationError, ValueError) as error:
            raise StructuredApiError(
                ErrorCode.INVALID_REQUEST,
                "The selected profile or override is not permitted.",
            ) from error

    def profile_apply(self, request: ProfileApplyRequest) -> ProfileApplyResponse:
        """Apply profile settings, restarting once for validated startup options."""

        with self._profile_apply_lock:
            # Fresh discovery and ID validation happen under the apply lock.
            # Unknown/stale IDs never become raw client-supplied device options.
            resolution = self.resolve_profile_selection(request)
            requested_restart_options = {
                name: value for name, value in resolution.options.items()
                if DEFAULT_OPTION_CATALOG.require(name).restart_required
            }
            if "drm-mode" in requested_restart_options:
                # An explicit comparison needs real video activation. This
                # check precedes teardown; normal retained idle startup remains
                # configuration-only until its existing media recovery loads.
                response = self.player_host.snapshot()
                state = getattr(response, "payload", response)
                if (request.force_restart or not isinstance(state, Mapping) or not state.get("path")
                        or not any(isinstance(track, Mapping) and track.get("type") == "video"
                                   and track.get("selected") is True for track in state.get("track-list", []))):
                    raise StructuredApiError(ErrorCode.INVALID_REQUEST, "Load a video before applying an advertised display timing.")
            next_device_ids = dict(self._active_device_profile_ids)
            groups = {
                "audio": {"ao", "audio-device", "audio-exclusive", "audio-fallback-to-null"},
                "display": DISPLAY_STARTUP_NAMES,
            }
            selected_groups = {category for category, keys in groups.items()
                               if keys & requested_restart_options.keys()}
            for category in selected_groups:
                next_device_ids.pop(category, None)
            fresh_inventory = self.output_discovery.discover()
            _rows, fresh_devices = self._device_profiles(fresh_inventory)
            fresh_identities = {identity: device for identity, device in device_profile_identities(fresh_inventory.devices).items()
                                if device_options(device) is not None}
            for key, fingerprint in resolution.output_fingerprints.items():
                device = fresh_identities.get(key)
                if (device is None or display_fingerprint(device, key) != fingerprint or key not in fresh_devices
                        or any(resolution.options.get(name) != value for name, value in fresh_devices[key].options.items())):
                    raise StructuredApiError(ErrorCode.INVALID_REQUEST, "The selected display capabilities changed; review and apply again.")
            for key in request.profile_ids:
                if key.startswith("output:"):
                    if key not in fresh_devices:
                        raise StructuredApiError(ErrorCode.NOT_FOUND, "A selected output is unavailable.")
                    next_device_ids[fresh_devices[key].category.value] = key
            if any(key not in fresh_identities for key in next_device_ids.values()):
                raise StructuredApiError(ErrorCode.NOT_FOUND, "A current output is unavailable; select its replacement explicitly.")
            retained_display = next_device_ids.get("display")
            if retained_display and "display" not in selected_groups:
                device = fresh_identities[retained_display]
                previous_fingerprint = self._active_display_fingerprints.get(retained_display)
                if previous_fingerprint and display_fingerprint(device, retained_display) != previous_fingerprint:
                    raise StructuredApiError(ErrorCode.INVALID_REQUEST, "The current display capabilities changed; select its replacement explicitly.")
                if HDR_STARTUP_NAMES & self._active_profile_restart_options.keys():
                    definition = fresh_devices.get(retained_display)
                    retained_options = (retained_display_profile_options(definition.options, self._active_profile_restart_options)
                                        if definition is not None else None)
                    if retained_options is None or any(self._active_profile_restart_options.get(name) != value
                                                       for name, value in retained_options.items()):
                        raise StructuredApiError(ErrorCode.PLAYER_UNAVAILABLE, "The current managed display policy could not be revalidated.")
            preserve = bool(request.force_restart or self._active_device_profile_ids or any(key.startswith("output:") for key in request.profile_ids))
            restart_target = dict(self._active_profile_restart_options if preserve else self._base_restart_options)
            if preserve:
                for category in selected_groups:
                    for key in groups[category]:
                        restart_target.pop(key, None)
                        if category != "display" and key in self._base_restart_options:
                            restart_target[key] = self._base_restart_options[key]
            restart_target.update(requested_restart_options)
            if "display" in selected_groups and requested_restart_options.get("vo") != "drm" and requested_restart_options.get("gpu-context") != "drm":
                restart_target.pop("drm-device", None)
                restart_target.pop("drm-connector", None)
            try:
                validate_display_configuration(restart_target)
                if HDR_STARTUP_NAMES & restart_target.keys():
                    self._native_video_capabilities()
            except DisplayPolicyError as error:
                raise StructuredApiError(ErrorCode.INVALID_REQUEST, str(error)) from error
            if unsafe_output_route(restart_target, fresh_inventory):
                raise StructuredApiError(ErrorCode.INVALID_REQUEST, "The selected output requires a verified managed route.")
            if request.force_restart and (
                restart_target != self._active_profile_restart_options
                or any(DEFAULT_OPTION_CATALOG.require(name).mutable_runtime for name in resolution.options)
            ):
                raise StructuredApiError(
                    ErrorCode.INVALID_REQUEST,
                    "A forced restart must reapply current startup settings without runtime changes.",
                )
            restart_report = None
            activation_pending: dict[str, Any] = {}
            current_restart_observations: Mapping[str, Any] = {}
            restart_needed = request.force_restart or restart_target != self._active_profile_restart_options
            if not restart_needed and restart_target:
                try:
                    current_restart_observations = self._observe_restart_options(
                        tuple(restart_target)
                    )
                    restart_needed = any(
                        not option_value_matches(
                            DEFAULT_OPTION_CATALOG.require(name),
                            expected,
                            current_restart_observations.get(name),
                        )
                        for name, expected in restart_target.items()
                    )
                except Exception:
                    # A missing readback is not evidence of application. One
                    # controlled restart may try to establish and verify it.
                    restart_needed = True

            display_id = next_device_ids.get("display")
            display_proof = (display_id, display_fingerprint(fresh_identities[display_id], display_id)) if display_id else None
            if restart_needed:
                try:
                    if request.force_restart:
                        restart_report = self._restart_profile_options(restart_target, idle_only=True)
                    else:
                        restart_report = self._restart_profile_options(restart_target)
                except Exception as error:
                    failure = StructuredApiError(
                        ErrorCode.PLAYER_UNAVAILABLE,
                        "The player could not restart and verify profile settings.",
                    )
                    _PROFILE_RESTART_LOGGER.exception(
                        "profile apply restart failed (correlation_id=%s, options=%s)",
                        failure.response.correlation_id,
                        sorted(restart_target),
                    )
                    raise failure from error
                if HDR_STARTUP_NAMES & restart_target.keys():
                    activation_pending = self._managed_renderer_pending(restart_target, wait=True,
                        expected_video=any(track.track_type == "video" for track in restart_report.playback_snapshot.selected_tracks),
                        expected_display=display_proof)
                if not restart_report.pending_restart_options and not activation_pending:
                    self._active_profile_restart_options = dict(restart_target)
                    self._active_device_profile_ids = next_device_ids
                    self._active_display_fingerprints = {key: display_fingerprint(fresh_identities[key], key)
                        for key in next_device_ids.values() if fresh_identities[key].method == "DRM"}
                current_restart_observations = restart_report.observed_options
            if not restart_needed:
                if HDR_STARTUP_NAMES & restart_target.keys():
                    activation_pending = self._managed_renderer_pending(restart_target, expected_display=display_proof)
                if not activation_pending:
                    self._active_device_profile_ids = next_device_ids
                    self._active_display_fingerprints = {key: display_fingerprint(fresh_identities[key], key)
                        for key in next_device_ids.values() if fresh_identities[key].method == "DRM"}
            try:
                report = ProfileRuntimeApplier(
                    {
                        "volume": lambda value: self.player_host.set_volume(value),
                        "speed": lambda value: self.player_host.set_speed(value),
                        "pause": lambda value: (
                            self.player_host.pause()
                            if value
                            else self.player_host.resume()
                        ),
                    },
                    self._observe_profile_options,
                ).apply(resolution)
            except RuntimeApplicationError as error:
                raise StructuredApiError(
                    ErrorCode.PLAYER_UNAVAILABLE,
                    "The player could not confirm runtime profile settings.",
                ) from error

        observed_options = dict(report.observed_options)
        pending_restart_options: dict[str, Any] = {}
        verification_options = restart_target if preserve else requested_restart_options
        for name, expected in verification_options.items():
            actual = current_restart_observations.get(name)
            definition = DEFAULT_OPTION_CATALOG.require(name)
            if option_value_matches(definition, expected, actual):
                observed_options[name] = actual
            else:
                pending_restart_options[name] = expected
        for name, expected in activation_pending.items():
            pending_restart_options[name] = expected
            observed_options.pop(name, None)
        if (self._persistent_state_enabled and not pending_restart_options
                and (self._durable_startup_configuration is not None
                     or (HDR_STARTUP_NAMES & self._active_profile_restart_options.keys()
                         and self._active_device_profile_ids.get("display")))):
            with self._profile_apply_lock:
                self._checkpoint_startup_configuration()
        if resolution.options.get("hwdec") == "vaapi":
            # This is a fixed read-only runtime observation, not a configurable
            # option. Null/unavailable values remain visible and cannot satisfy
            # the hardware gate's exact active-decoder check.
            observed_options["hwdec-current"] = self._observe_hwdec_current()
        return ProfileApplyResponse(
            applied=not pending_restart_options,
            profile_ids=list(resolution.profile_ids),
            effective_options=dict(resolution.options),
            sources=dict(resolution.sources),
            conflicts=[
                ProfileApplyConflict(
                    option=conflict.option,
                    previous_profile_id=conflict.previous_profile_id,
                    winning_profile_id=conflict.winning_profile_id,
                    previous_category=conflict.previous_category,
                    winning_category=conflict.winning_category,
                    previous_value=conflict.previous_value,
                    winning_value=conflict.winning_value,
                )
                for conflict in resolution.conflicts
            ],
            observed_options=observed_options,
            pending_restart_options=pending_restart_options,
        )

    def _managed_renderer_pending(self, options: Mapping[str, Any], *, wait: bool = False, expected_video: bool | None = None, expected_display=None) -> dict[str, Any]:
        """Requested options cannot prove that a loaded video's GPU VO initialized.

        Missing/delayed activation is pending, never a reason to roll back a
        healthy player. Idle and known audio-only playback verify configuration
        only; their separate output telemetry remains unqualified.
        """
        deadline = time.monotonic() + (1.0 if wait else 0.0)
        pending = {}
        while True:
            try:
                response = self.player_host.snapshot()
                state = getattr(response, "payload", response)
                if not isinstance(state, Mapping) or "path" not in state:
                    raise ValueError("snapshot unavailable")
                if state["path"] is None:
                    if state.get("idle-active") is True and expected_video is not True:
                        return {}
                    raise ValueError("idle or restored-video state is unverified")
                tracks = state.get("track-list")
                if not isinstance(tracks, list) or not tracks:
                    raise ValueError("selected tracks are unverified")
                has_video = any(isinstance(track, Mapping) and track.get("type") == "video" for track in tracks)
                selected_video = any(isinstance(track, Mapping) and track.get("type") == "video"
                                     and track.get("selected") is True for track in tracks)
                if not selected_video:
                    if (expected_video is not True and not has_video and any(isinstance(track, Mapping)
                            and track.get("type") == "audio" and track.get("selected") is True for track in tracks)):
                        return {}
                    raise ValueError("selected video disappeared or is unverified")
                pending = {}
                if "drm-mode" in options and not self._verified_drm_mode(options, expected_display):
                    pending["drm-mode"] = options["drm-mode"]
                if state.get("current-vo") != "gpu-next":
                    pending["vo"] = options["vo"]
                if state.get("current-gpu-context") != "drm":
                    pending["gpu-context"] = options["gpu-context"]
                target = state.get("video-target-params")
                if (not isinstance(target, Mapping) or not isinstance(target.get("gamma"), str)
                        or target.get("gamma") in {"", "unknown", "auto"}):
                    pending["target-trc"] = options["target-trc"]
                if (not isinstance(target, Mapping) or not isinstance(target.get("primaries"), str)
                        or target.get("primaries") in {"", "unknown", "auto"}):
                    pending["target-prim"] = options["target-prim"]
                if isinstance(target, Mapping) and options["target-colorspace-hint-mode"] == "target":
                    if str(target.get("gamma", "")).lower().replace(".", "") != str(options["target-trc"]).lower().replace(".", ""):
                        pending["target-trc"] = options["target-trc"]
                    if str(target.get("primaries", "")).lower().replace(".", "") != str(options["target-prim"]).lower().replace(".", ""):
                        pending["target-prim"] = options["target-prim"]
            except Exception:
                pending = {name: options[name] for name in ("vo", "gpu-context", "target-trc", "target-prim")}
                if "drm-mode" in options:
                    pending["drm-mode"] = options["drm-mode"]
            if not pending or time.monotonic() >= deadline:
                return pending
            time.sleep(0.025)

    def _verified_drm_mode(self, options, expected_display=None):
        """Require exact advertised timing on the owned active CRTC, beyond echoes."""
        identity = self._active_device_profile_ids.get("display")
        proof = expected_display or (identity, self._active_display_fingerprints.get(identity))
        if not proof[0] or not proof[1]:
            return False
        inventory = self.output_discovery.discover()
        device = device_profile_identities(inventory.devices).get(proof[0])
        if device is None or display_fingerprint(device, proof[0]) != proof[1]:
            return False
        mode = selected_mode(device, proof[0])
        if mode is None or mode.selector != options["drm-mode"]:
            return False
        observation = self.player_host.observe_drm_mode()
        if (not isinstance(observation, Mapping) or set(observation) !=
                {"native_library_sha256", "libplacebo_sha256", "drm_mode_type", "report"}
                or not matches_expected_native_identities(observation)
                or observation["drm_mode_type"] != "String"):
            return False
        report = DrmModeReport.from_record(observation["report"])
        return (report.card == options["drm-device"] == device.drm_modes.card
                and report.connector == options["drm-connector"] == device.drm_modes.connector
                and report.connector_id == device.drm_modes.connector_id
                and {value.identity for value in report.modes} == {value.identity for value in device.drm_modes.modes}
                and mode.same_timing(report.active))

    def _observe_restart_options(self, option_names: tuple[str, ...]) -> Mapping[str, Any]:
        if not option_names:
            return {}
        observer = getattr(self.player_host, "observe_restart_options", None)
        if not callable(observer):
            raise RestartApplicationError("player host does not expose fixed option readback")
        values = observer(option_names)
        if not isinstance(values, Mapping):
            raise RestartApplicationError("player-host restart-option readback is invalid")
        return values

    def _observe_hwdec_current(self) -> str | None:
        observer = getattr(self.player_host, "observe_hwdec_current", None)
        if not callable(observer):
            return None
        try:
            value = observer()
        except Exception:
            return None
        return value if value is None or isinstance(value, str) else None

    def _restart_profile_options(self, restart_options: Mapping[str, Any], *, idle_only: bool = False):
        self._poll_stop.set()
        poll_thread = self._poll_thread
        if poll_thread is not None and poll_thread.is_alive():
            poll_thread.join(timeout=3.0)
            if poll_thread.is_alive():
                self._poll_stop.clear()
                raise RestartApplicationError("player event poller did not stop")
        self._poll_thread = None
        try:
            self._drain_host_events()
        except Exception as error:
            self._resume_event_pipeline_after_restart_failure()
            raise RestartApplicationError("could not drain events before player restart") from error
        try:
            with self._host_recovery_lock:
                if idle_only:
                    observed = self.player_host.snapshot()
                    snapshot = getattr(observed, "payload", observed)
                    if not isinstance(snapshot, Mapping) or snapshot.get("path") is not None:
                        raise RestartApplicationError("forced profile restart requires an idle player")
                rollback_options = None
                if not idle_only and HDR_STARTUP_NAMES & restart_options.keys():
                    prior = dict(self._active_profile_restart_options)
                    try:
                        readback = self._observe_restart_options(tuple(prior))
                        if prior and all(option_value_matches(DEFAULT_OPTION_CATALOG.require(name), value, readback.get(name))
                                         for name, value in prior.items()):
                            rollback_options = prior
                    except Exception:
                        pass  # Unverified predecessor settings cannot authorize recovery.
                self._close_websocket_subscribers_for_restart()
                report = _IdleProfileRestartApplier(self.player_host).apply(
                    restart_options,
                    base_options={key: value for key, value in self.mpv_options.items()
                                  if key not in self._base_restart_options},
                    idle_only=idle_only,
                    rollback_options=rollback_options,
                )
                if idle_only and report.pending_restart_options:
                    raise _RestartFailure("forced restart options did not verify", host_changed=True)
                try:
                    self._reset_event_pipeline_after_restart()
                except Exception as error:
                    raise _RestartFailure("restarted event pipeline failed", host_changed=True) from error
                return report
        except Exception as error:
            if idle_only and getattr(error, "host_changed", False):
                # Avoid historical-media recovery after an explicit idle
                # restart fails. Cleanup belongs to the managed supervisor.
                with self._host_recovery_lock:
                    try:
                        self.player_host.shutdown(force=True)
                    finally:
                        self._poll_stop.set()
                        self._pipeline_error = "forced idle player restart failed"
            else:
                self._resume_event_pipeline_after_restart_failure()
            if isinstance(error, RestartApplicationError):
                raise
            raise RestartApplicationError("profile restart failed") from error

    def _close_websocket_subscribers_for_restart(self) -> None:
        with self._pipeline_lock:
            subscribers = tuple(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            async def close_for_restart(websocket=subscriber.websocket):
                try:
                    await websocket.close(code=1012, reason="player restarting")
                except Exception:
                    pass

            try:
                subscriber.loop.call_soon_threadsafe(
                    lambda close=close_for_restart: asyncio.create_task(close())
                )
            except RuntimeError:
                continue

    def _reset_event_pipeline_after_restart(self) -> None:
        state = self._event_stats().get("state", {})
        player_id = str(state.get("player_id", ""))
        session_id = str(state.get("session_id", ""))
        if not player_id or not session_id:
            raise RestartApplicationError("restarted player session identity is unavailable")
        self._history = EventHistory(maxsize=self._history.maxsize)
        self._player_id = player_id
        self._session_id = session_id
        self._drain_host_events()
        self._pipeline_error = None
        self._start_event_poller()

    def _resume_event_pipeline_after_restart_failure(self) -> None:
        try:
            state = self._event_stats().get("state", {})
            player_id = str(state.get("player_id", ""))
            session_id = str(state.get("session_id", ""))
            if not player_id or not session_id:
                raise RestartApplicationError("player session identity is unavailable")
            if (player_id, session_id) != (self._player_id, self._session_id):
                self._history = EventHistory(maxsize=self._history.maxsize)
            self._player_id = player_id
            self._session_id = session_id
            self._drain_host_events()
            self._start_event_poller()
            self._pipeline_error = "profile restart or verification failed"
        except Exception:
            self._pipeline_error = "player restart failed"

    def _start_event_poller(self) -> None:
        self._poll_stop.clear()
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._poll_thread = threading.Thread(
            target=self._poll_events,
            name="mpv-player-event-poller",
            daemon=True,
        )
        self._poll_thread.start()

    def _observe_profile_options(self) -> Mapping[str, Any]:
        response = self.player_host.snapshot()
        payload = getattr(response, "payload", None)
        if not isinstance(payload, Mapping):
            raise ValueError("player-host snapshot is invalid")
        return payload

    async def websocket(
        self, websocket: Any, *, allow_unauthenticated: bool = False
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        token, accepted_subprotocol = _websocket_authentication(websocket)
        if allow_unauthenticated and token is None:
            if not _websocket_allows_tokenless_trusted_lan(websocket):
                await websocket.close(code=1008, reason="authentication required")
                return
            accepted_subprotocol = _WEBSOCKET_APPLICATION_PROTOCOL
        elif not self.runtime_repository.authenticate_token(token):
            await websocket.close(code=1008, reason="authentication required")
            return
        try:
            last_sequence = _parse_last_sequence(
                websocket.query_params.get("last_sequence")
            )
        except ValueError:
            await websocket.close(code=1008, reason="invalid last_sequence")
            return

        state = self._event_stats().get("state", {})
        player_id = str(state.get("player_id", ""))
        session_id = str(state.get("session_id", ""))
        if not player_id or not session_id:
            await websocket.close(code=1013, reason="player state unavailable")
            return
        contract = WebSocketContract(
            player_id=player_id,
            session_id=session_id,
            history=self._history,
            snapshot=self._snapshot_payload,
            token_validator=lambda candidate: candidate == token,
        )
        subscriber = _WebSocketSubscriber(
            websocket=websocket,
            loop=asyncio.get_running_loop(),
            queue=asyncio.Queue(maxsize=256),
        )
        try:
            with self._pipeline_lock:
                initial = contract.connect(token, last_sequence=last_sequence)
                cursor = self._history.latest_sequence or 0
                if initial:
                    cursor = max(cursor, max(event.sequence for event in initial))
                self._subscribers.add(subscriber)
        except HistoryUnavailable:
            await websocket.close(
                code=4009,
                reason="requested event history is no longer retained",
            )
            return
        except (ValueError, WebSocketAuthError):
            await websocket.close(code=1008, reason="invalid WebSocket resume request")
            return

        receive_task = event_task = None
        try:
            await websocket.accept(subprotocol=accepted_subprotocol)
            for event in initial:
                await websocket.send_json(event.to_dict())
            # Keep one receive pending across events. Cancelling it on every
            # telemetry update races disconnect and can consume queued data.
            receive_task = asyncio.create_task(websocket.receive())
            event_task = asyncio.create_task(subscriber.queue.get())
            while True:
                done, _pending = await asyncio.wait(
                    {receive_task, event_task}, return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    incoming = receive_task.result()
                    if incoming.get("type") == "websocket.disconnect":
                        break
                    receive_task = asyncio.create_task(websocket.receive())
                if event_task in done:
                    event = event_task.result()
                    event_task = asyncio.create_task(subscriber.queue.get())
                    if event.sequence > cursor:
                        await websocket.send_json(event.to_dict())
                        cursor = event.sequence
        except (WebSocketDisconnect, asyncio.CancelledError):
            # ASGI server cancellation is connection teardown, not playback
            # success; process supervision and client reconnect remain intact.
            pass
        finally:
            with self._pipeline_lock:
                self._subscribers.discard(subscriber)
            tasks = [task for task in (receive_task, event_task) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                from anyio import CancelScope
                with CancelScope(shield=True):
                    await asyncio.gather(*tasks, return_exceptions=True)

    def _event_stats(self) -> Mapping[str, Any]:
        self._require_started()
        try:
            response = self.player_host.request("event-stats")
        except PlayerHostError as error:
            raise self._player_unavailable() from error
        payload = getattr(response, "payload", None)
        if not isinstance(payload, Mapping):
            raise self._player_unavailable()
        return payload

    def _wait_for_recovered_media(
        self,
        path: str,
        *,
        paused: bool = False,
        seekable: bool = False,
    ) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            response = self.player_host.request("event-stats")
            payload = getattr(response, "payload", None)
            state = payload.get("state") if isinstance(payload, Mapping) else None
            properties = state.get("properties") if isinstance(state, Mapping) else None
            duration = properties.get("duration") if isinstance(properties, Mapping) else None
            position = properties.get("time_pos") if isinstance(properties, Mapping) else None
            seek_ready = (
                not seekable
                or (
                    isinstance(duration, (int, float))
                    and not isinstance(duration, bool)
                    and duration > 0
                )
                or (
                    isinstance(position, (int, float))
                    and not isinstance(position, bool)
                    and position > 0
                )
            )
            if (
                isinstance(properties, Mapping)
                and properties.get("path") == path
                and properties.get("idle_active") is False
                and (not paused or properties.get("pause") is True)
                and seek_ready
            ):
                return
            time.sleep(0.025)
        raise PlayerHostError("recovered media did not become ready before the startup deadline")

    def _snapshot_payload(self) -> dict[str, Any]:
        state = self._event_stats().get("state", {})
        properties = state.get("properties", {})
        return {
            "player_id": state.get("player_id"),
            "session_id": state.get("session_id"),
            "status": state.get("status"),
            "position": properties.get("time_pos"),
            "duration": properties.get("duration"),
            "seekable": properties.get("seekable"),
            "path": properties.get("path"),
            "volume": properties.get("volume"),
            "speed": properties.get("speed"),
            "pause": properties.get("pause", False),
            "output_status": _normalized_output_status(state.get("output_status")),
        }

    def _poll_events(self) -> None:
        while not self._poll_stop.is_set():
            self._maintain_player_lease()
            if self._poll_stop.is_set():
                break
            try:
                self._drain_host_events()
            except PlayerHostError:
                if self._host_recovery_exhausted:
                    self._pipeline_error = "player-host recovery exhausted its retry budget"
                else:
                    self._pipeline_error = "player event ingestion failed"
                    self._recover_player_host()
            except Exception:
                self._pipeline_error = "player event ingestion failed"
            else:
                if not self._host_recovery_exhausted:
                    self._pipeline_error = None
            self._poll_stop.wait(0.025)

    def _drain_host_events(self) -> None:
        with self._event_drain_lock:
            self._drain_host_events_locked()
        # Native side effects execute without the event, queue or SQL locks.
        if self._database_pipeline_error is None and self._queue_playback is not None:
            for identity in self._queue_playback.claimed():
                self._submit_queue_intent(identity)

    def _drain_host_events_locked(self) -> None:
        if not self._pending_host_events:
            response = self.player_host.request("events-poll", {"limit": 256})
            payload = getattr(response, "payload", None)
            if not isinstance(payload, Mapping):
                raise ValueError("player-host event response is invalid")
            events = payload.get("events", [])
            if not isinstance(events, list):
                raise ValueError("player-host events must be a list")
            self._pending_host_events.extend(
                EventEnvelope.from_dict(raw_event) for raw_event in events
            )

        if time.monotonic() < self._database_retry_after:
            return

        while self._pending_host_events:
            event = self._pending_host_events[0]
            try:
                self._ingest_event(event)
            except (CheckpointReceiptRejected, NativeEpisodeProofError) as error:
                # Only a known pre-write receipt rejection can be discarded.
                # Preserve a bounded diagnostic and leave its stream gap
                # visible; never publish the invalid proof/state as committed.
                self._checkpoint_rejections.append({
                    "sequence": event.sequence, "player_id": event.player_id,
                    "session_id": event.session_id, "reason": str(error),
                })
                _PROFILE_RESTART_LOGGER.warning(
                    "Rejected resume checkpoint at sequence %s: %s", event.sequence, error)
            except sqlite3.Error as error:
                # events-poll consumes from the child outbox, so retain the
                # event here until SQLite commits it. Commands still go
                # directly to the player host while the durable pipeline is
                # degraded.
                self._database_pipeline_error = type(error).__name__
                delay = max(self._database_retry_delay, _DATABASE_RETRY_BASE_SECONDS)
                self._database_retry_delay = min(
                    delay * 2,
                    _DATABASE_RETRY_MAX_SECONDS,
                )
                self._database_retry_after = time.monotonic() + delay
                return
            self._pending_host_events.popleft()

        self._database_pipeline_error = None
        self._database_retry_delay = 0.0
        self._database_retry_after = 0.0

    def _ingest_event(self, event: EventEnvelope) -> None:
        if event.type in {'media.started', 'native.end-file', 'native.terminal-pending'}:
            validate_native_episode(event)  # Validate before every durable projection.
        predecessor = event.payload.get("stream_previous_sequence", event.sequence - 1)
        if isinstance(predecessor, bool) or not isinstance(predecessor, int) or not 0 <= predecessor < event.sequence:
            if event.type == "resume.checkpoint":
                raise CheckpointReceiptRejected("invalid checkpoint stream predecessor")
            raise ValueError("invalid player-host stream predecessor")
        if self._event_sequence_offset:
            event = replace(
                event,
                sequence=event.sequence + self._event_sequence_offset,
            )
        event = replace(event, payload={
            **event.payload,
            "stream_previous_sequence": predecessor + self._event_sequence_offset,
        })
        with self._pipeline_lock:
            latest = self._history.latest_sequence
            if latest is not None and event.sequence <= latest:
                if event.type == "resume.checkpoint":
                    raise CheckpointReceiptRejected("checkpoint event sequence is not increasing")
                raise ValueError("player-host event sequence is not increasing")
            if event.type == "resume.checkpoint":
                try:
                    validate_checkpoint_event(event)
                    if self._playback_state is not None:
                        self._playback_state.validate_position_checkpoint(event)
                except PositionCheckpointError as error:
                    raise CheckpointReceiptRejected(str(error)) from error
                if self._playback_state is not None:
                    # Check the existing durable owner before the generic
                    # session projection can change its player/state fields.
                    self._playback_state.record_event(event)
                event = replace(event, payload={key: value for key, value in event.payload.items()
                                               if key != "position_checkpoint"})
                self.runtime_repository.record_transition(event)
            else:
                if self._transition_policy.should_persist(event):
                    self.runtime_repository.record_transition(event)
                if self._playback_state is not None:
                    self._playback_state.record_event(event)
            if self._queue_playback is not None:
                self._queue_playback.on_event(event)
            # Preserve the producer link, including a mismatch caused by real
            # loss before ingestion. Browser resynchronization remains visible.
            self._history.append(event)
            self._player_id = event.player_id
            self._session_id = event.session_id
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.loop.call_soon_threadsafe(
                        self._enqueue_event,
                        subscriber,
                        event,
                    )
                except RuntimeError:
                    self._subscribers.discard(subscriber)
        self._publish_redis_event(event)

    def _publish_redis_event(self, event: EventEnvelope) -> None:
        """Mirror an already-durable event to Redis without coupling local delivery."""

        if self.redis_client.settings.url is None:
            return
        try:
            self.redis_event_stream.append(event)
            self.redis_live_pubsub.publish(event)
            self._redis_pipeline_error = None
        except RedisUnavailable:
            # SQLite and in-process WebSocket delivery have already completed.
            # Redis is a bounded, recoverable fanout/cache and never truth.
            return
        except Exception as error:
            self._redis_pipeline_error = type(error).__name__
            self.redis_client.report_failure(error)

    @staticmethod
    def _enqueue_event(
        subscriber: _WebSocketSubscriber,
        event: EventEnvelope,
    ) -> None:
        if subscriber.queue.full():
            asyncio.create_task(
                subscriber.websocket.close(
                    code=1013,
                    reason="event consumer is too slow",
                )
            )
            return
        subscriber.queue.put_nowait(event)

    def _mutate(
        self,
        operation_name: str,
        request_key: str | None,
        fingerprint_data: Mapping[str, Any],
        operation: Callable[[], Any],
        *, before_host: Callable[[], Any] | None = None,
    ) -> CommandResponse:
        fingerprint = json.dumps(
            {"operation": operation_name, "payload": fingerprint_data},
            sort_keys=True,
            separators=(",", ":"),
        )

        def execute() -> CommandResponse:
            self._require_started()
            if before_host is not None:
                before_host()
            try:
                with self._host_recovery_lock:
                    result = operation()
                    if result is False:
                        return CommandResponse(accepted=False, request_id=request_key)
                    runtime_values = {
                        "volume": {"volume": fingerprint_data.get("value")},
                        "speed": {"speed": fingerprint_data.get("value")},
                        "play": {"pause": False}, "pause": {"pause": True},
                    }.get(operation_name)
                    if self._durable_startup_configuration is not None and runtime_values is not None:
                        deadline = time.monotonic() + 1.0
                        while True:
                            response = self.player_host.snapshot()
                            snapshot = getattr(response, "payload", response)
                            if isinstance(snapshot, Mapping) and all(option_value_matches(
                                DEFAULT_OPTION_CATALOG.require(name), expected, snapshot.get(name))
                                for name, expected in runtime_values.items()):
                                break
                            if time.monotonic() >= deadline:
                                self._startup_configuration_error = "runtime_readback_unavailable"
                                raise self._player_unavailable()
                            time.sleep(0.025)
                        try:
                            self._startup_configuration_store.update_runtime(runtime_values)
                            self._durable_startup_configuration = self._startup_configuration_store.read()
                        except (ValueError, sqlite3.Error) as error:
                            self._startup_configuration_error = type(error).__name__
                            raise self._player_unavailable() from error
                        self._startup_configuration_error = None
                    if self._durable_startup_configuration is not None and operation_name in {"load", "stop"}:
                        try:
                            self._startup_configuration_store.update_media_intent(operation_name == "load")
                            self._durable_startup_configuration = self._startup_configuration_store.read()
                        except (ValueError, sqlite3.Error) as error:
                            self._startup_configuration_error = type(error).__name__
                            raise self._player_unavailable() from error
                        self._startup_configuration_error = None
            except PlayerHostError as error:
                raise self._player_unavailable() from error
            return CommandResponse(accepted=True, request_id=request_key)

        try:
            return self.idempotency.execute(request_key, fingerprint, execute).value
        except IdempotencyConflict as error:
            raise StructuredApiError(
                ErrorCode.CONFLICT,
                "The idempotency key was already used for a different operation.",
            ) from error
        except ValueError as error:
            raise StructuredApiError(
                ErrorCode.INVALID_REQUEST,
                "The idempotency key is invalid.",
            ) from error

    def _validated_media_path(self, path: str, *, allow_network: bool = True) -> str:
        if self.media_root_policy is None:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "Media access is unavailable because no media policy is configured.",
            )
        try:
            if isinstance(self.media_root_policy, MediaSourcePolicy) and not allow_network:
                return self.media_root_policy.resolve_local(path)
            return self.media_root_policy.resolve(path)
        except MediaPathPolicyError as error:
            raise StructuredApiError(
                ErrorCode.FORBIDDEN_MEDIA,
                "The requested media path is not allowed.",
            ) from error

    def _require_started(self) -> None:
        if not self._started or not getattr(self.player_host, "running", False):
            raise self._player_unavailable()

    @staticmethod
    def _player_unavailable() -> StructuredApiError:
        return StructuredApiError(
            ErrorCode.PLAYER_UNAVAILABLE,
            "The playback service is unavailable.",
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _runtime_lease_identity(database_path: str | Path) -> str:
    raw_path = os.fspath(database_path)
    if raw_path == ":memory:":
        return "runtime-memory"
    canonical_path = str(Path(raw_path).expanduser().resolve())
    digest = hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:24]
    return f"runtime-{digest}"


def _catalog_restart_options(options: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        raise TypeError("mpv initialization options must be a mapping")
    selected: dict[str, Any] = {}
    for name, value in options.items():
        definition = DEFAULT_OPTION_CATALOG.get(name)
        if definition is None or not definition.restart_required:
            continue
        try:
            selected[name] = definition.validate(value)
        except OptionValidationError as error:
            raise ValueError("restart-required base option is not catalog-valid") from error
    return selected


def _event_time(value: str | int | float) -> str:
    if isinstance(value, str):
        return value
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _finite_nonnegative(value: Any, *, default: float | None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("player state numeric field is invalid")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise ValueError("player state numeric field is invalid")
    return converted


def _normalized_output_status(value: Any) -> dict[str, Any] | None:
    """Expose only the fixed canonical output-status fields; malformed data is omitted."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        _PROFILE_RESTART_LOGGER.warning("omitting malformed canonical player output status")
        return None
    try:
        return PlayerOutputStatus.from_mapping(value).as_dict()
    except (TypeError, ValueError):
        _PROFILE_RESTART_LOGGER.warning("omitting malformed canonical player output status")
        return None


_WEBSOCKET_APPLICATION_PROTOCOL = "mpv-webd.v1"
_WEBSOCKET_AUTH_PROTOCOL_PREFIX = "mpv-auth."
_WEBSOCKET_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")


def _websocket_allows_tokenless_trusted_lan(websocket: Any) -> bool:
    """Require the tokenless browser contract and reject supplied credentials."""

    scope = getattr(websocket, "scope", None)
    if not isinstance(scope, Mapping):
        return False
    if scope.get("subprotocols") != [_WEBSOCKET_APPLICATION_PROTOCOL]:
        return False
    raw_headers = scope.get("headers", ())
    if not isinstance(raw_headers, (list, tuple)):
        return False
    authorization_count = 0
    protocol_values: list[bytes] = []
    for header in raw_headers:
        if not isinstance(header, (list, tuple)) or len(header) != 2:
            return False
        name, value = header
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            return False
        if name.lower() == b"authorization":
            authorization_count += 1
        elif name.lower() == b"sec-websocket-protocol":
            protocol_values.append(value)
    return (
        authorization_count == 0
        and protocol_values == [_WEBSOCKET_APPLICATION_PROTOCOL.encode("ascii")]
    )


def _websocket_authentication(websocket: Any) -> tuple[str | None, str | None]:
    """Return one credential and only the non-secret protocol to negotiate.

    Native browser WebSocket clients cannot set Authorization headers, so the
    browser offers its bearer token in an application-specific subprotocol.
    Header-authenticated clients remain supported. Multiple or conflicting
    credential channels are rejected rather than resolved by precedence.
    """
    scope = getattr(websocket, "scope", None)
    if not isinstance(scope, Mapping):
        return None, None
    raw_headers = scope.get("headers", ())
    if not isinstance(raw_headers, (list, tuple)):
        return None, None

    authorization_values: list[str] = []
    protocol_header_count = 0
    for header in raw_headers:
        if not isinstance(header, (list, tuple)) or len(header) != 2:
            return None, None
        name, value = header
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            return None, None
        lowered_name = name.lower()
        if lowered_name == b"authorization":
            authorization_values.append(value.decode("latin-1"))
        elif lowered_name == b"sec-websocket-protocol":
            protocol_header_count += 1
    if len(authorization_values) > 1 or protocol_header_count > 1:
        return None, None

    raw_authorization = authorization_values[0] if authorization_values else None
    header_token = bearer_token(raw_authorization)
    if raw_authorization is not None and header_token is None:
        return None, None

    offered = scope.get("subprotocols", ())
    if not isinstance(offered, (list, tuple)) or any(
        not isinstance(protocol, str) for protocol in offered
    ):
        return None, None
    if len(set(offered)) != len(offered):
        return None, None

    auth_values = [
        protocol[len(_WEBSOCKET_AUTH_PROTOCOL_PREFIX) :]
        for protocol in offered
        if protocol.startswith(_WEBSOCKET_AUTH_PROTOCOL_PREFIX)
    ]
    if len(auth_values) > 1:
        return None, None
    auth_token: str | None = None
    if auth_values:
        candidate = auth_values[0]
        if not _WEBSOCKET_TOKEN_PATTERN.fullmatch(candidate):
            return None, None
        auth_token = candidate

    if header_token is not None and auth_token is not None:
        return None, None
    if auth_token is not None:
        if _WEBSOCKET_APPLICATION_PROTOCOL not in offered:
            return None, None
        return auth_token, _WEBSOCKET_APPLICATION_PROTOCOL
    if header_token is not None:
        selected = (
            _WEBSOCKET_APPLICATION_PROTOCOL
            if _WEBSOCKET_APPLICATION_PROTOCOL in offered
            else None
        )
        return header_token, selected
    return None, None


def _parse_last_sequence(raw: str | None) -> int | None:
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit():
        raise ValueError("last_sequence must be a non-negative integer")
    return int(raw)
