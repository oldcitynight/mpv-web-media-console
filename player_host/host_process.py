from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import socket
import stat
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.domain.player_state import (
    PlayerOutputStatus,
    PlayerProperties,
    PlayerState,
    PlayerStatus,
    SessionStatus,
)
from backend.events.backpressure import BoundedEventQueue
from backend.events.durable_transition_policy import DurableTransitionRecorder
from backend.media.identity import MediaIdentity, MediaIdentityError
from backend.playback.position_checkpoint import (
    PositionCheckpointError, native_position, native_seek_context, verified_position,
)
from backend.events.envelope import EventEnvelope
from backend.events.position_coalescing import PositionCoalescer
from backend.events.sequence import EventSequencer
from .libmpv_adapter import LibMpvAdapter, PlayerLifecycle, NativeCommandRejected
from .protocol import Envelope, PROTOCOL_VERSION, ProtocolError
from .output_identity import OutputIdentityObserver
from .native_episode import NativeEpisodeSupport
from .display_signal import AsyncDisplaySignalObserver, unknown as unknown_display_signal


SUPPORTED_REQUESTS = frozenset(
    {
        "ping",
        "create",
        "load",
        "play",
        "pause",
        "resume",
        "seek-absolute",
        "seek-relative",
        "stop",
        "set-volume",
        "set-speed",
        "select-track",
        "snapshot",
        "recovery-transition",
        "observe-restart-options",
        "observe-hwdec-current",
        "observe-audio-decoder",
        "observe-audio-chain",
        "observe-native-video-capabilities",
        "observe-drm-mode",
        "observe-display-geometry",
        "observe-performance",
        "observe-probe-boundary",
        "observe-probe-phase",
        "observe-probe-performance",
        "events-poll",
        "event-stats",
        "shutdown",
    }
)

_LIBORENDER_MAPPED_NAME = re.compile(r"liborender\.so(?:\.[0-9]+)*\Z")
_STATUS_ONLY_PROPERTIES = frozenset(
    {
        "audio-params",
        "audio-out-params",
        "video-params",
        "video-dec-params",
        "video-target-params",
        "seeking",
        "container-fps",
        "current-vo",
        "current-gpu-context",
        "current-ao",
        "drm-device",
        "drm-connector",
        "video-out-params",
        "display-names",
        "audio-device",
    }
)


_MEDIA_STATUS_PROPERTIES = frozenset({
    "audio-params", "audio-out-params", "video-params", "video-dec-params",
    "video-out-params", "video-target-params", "container-fps", "track-list",
})
_IDENTITY_PROPERTIES = (
    "current-ao", "audio-device", "current-vo", "current-gpu-context",
    "drm-device", "drm-connector", "display-names",
)


def _audio_decoder_diagnostic_log_file() -> str | None:
    if os.environ.get("MPV_WEBD_AUDIO_DECODER_DIAGNOSTICS") != "1":
        return None
    raw_path = os.environ.get("MPV_WEBD_AUDIO_DECODER_LOG_FILE")
    if not raw_path:
        return None
    path = Path(raw_path)
    if (
        not path.is_absolute()
        or path.name != "audio-decoder.log"
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or path.parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or path.exists()
        or path.is_symlink()
    ):
        raise RuntimeError("audio-decoder diagnostic log path is not a fresh private file")
    return str(path)


def _response(request: Envelope, kind: str, payload: dict) -> Envelope:
    return Envelope(PROTOCOL_VERSION, request.request_id, kind, payload)


class HostRuntime(NativeEpisodeSupport):
    """The child-owned runtime; the parent only sees protocol envelopes."""

    def __init__(
        self,
        *,
        transition_writer: Callable[[EventEnvelope], None] | None = None,
        event_queue_size: int = 256,
    ) -> None:
        self.lifecycle = PlayerLifecycle(
            LibMpvAdapter(
                capture_audio_decoder_diagnostics=(
                    os.environ.get("MPV_WEBD_AUDIO_DECODER_DIAGNOSTICS") == "1"
                ),
                diagnostic_log_file=_audio_decoder_diagnostic_log_file(),
            )
        )
        self.player_id = f"player-{uuid.uuid4().hex}"
        self.session_id = uuid.uuid4().hex
        self._event_lock = threading.RLock()
        self._init_native_episodes()
        self._native_media_fields = _MEDIA_STATUS_PROPERTIES
        self._native_status_fields = _STATUS_ONLY_PROPERTIES
        self._sequencer = EventSequencer()
        self._position_coalescer = PositionCoalescer()
        self._events = BoundedEventQueue(event_queue_size)
        self._last_delivered_sequence = 0
        # This bounded transition outbox is the handoff point for the runtime's
        # durable writer. A deployment can inject its persistent sink without
        # coupling libmpv callbacks to a particular database implementation.
        self._transition_outbox: deque[EventEnvelope] = deque(maxlen=512)
        self._transition_writer = transition_writer
        self._recorder = DurableTransitionRecorder(self._record_transition)
        self._observed_properties: dict[str, Any] = {}
        self._state = PlayerState.initial(self.player_id, self.session_id)
        self._playback_session_ended = False
        self._playback_session_had_media = False
        self._playback_stop_fenced = False
        self._pending_stopped_load: dict[str, Any] | None = None
        self._verified_stopped_load_identity: MediaIdentity | None = None
        self._stopped_load_path_disputed = False
        self._load_ownership_revision = 0
        self._verified_terminal_eof = False
        self._load_ownership_unconfirmed = False
        # One latest accepted semantic seek, driven only by existing request
        # polling. No timer, observer or background native reads are installed.
        self._pending_position_checkpoint: dict[str, Any] | None = None
        self._position_command_serial = 0
        self._pending_checkpoint_now = lambda: time.monotonic_ns() / 1_000_000_000
        self._observer_errors: deque[str] = deque(maxlen=32)
        self._liborender_mapped: bool | None = None
        self._output_identity_observer = OutputIdentityObserver()
        self._output_identity: dict[str, str | None] = {}
        self._output_identity_key: tuple[Any, ...] | None = None
        self._output_identity_checked_at = 0.0
        self._display_signal_observer = AsyncDisplaySignalObserver()
        self._display_signal_epoch = 0
        self._display_signal_pending = False
        self._display_signal_seen: set[str] = set()
        self._display_signal_resample = False
        self._media_status_resample = False
        self._display_signal_seek_seen = False
        self._display_signal_reason = "not_observed"

    def handle(self, request: Envelope) -> tuple[Envelope, bool]:
        if request.kind == "ping":
            return (
                _response(
                    request,
                    "pong",
                    {"pid": os.getpid(), "owner": "player-host", "state": self.lifecycle.state.value},
                ),
                True,
            )
        if request.kind == "create":
            self.lifecycle.initialize(options=request.payload)
            try:
                # python-mpv dispatches these callbacks from its libmpv event
                # worker. They are the production event-drain boundary: the
                # callback updates canonical state and feeds the same bounded
                # stream consumed by events-poll.
                self.lifecycle.adapter.observe_properties(self._on_property)
                self.lifecycle.adapter.observe_native_events(self._on_native_event)
                self._native_event_contract = True
            except Exception:
                self.lifecycle.shutdown()
                raise
            return (
                _response(
                    request,
                    "initialized",
                    {"pid": os.getpid(), "state": self.lifecycle.state.value},
                ),
                True,
            )
        if request.kind == "shutdown":
            with self._event_lock:
                self._invalidate_display_signal()
                self._state = self._state.with_properties(
                    status=PlayerStatus.STOPPED,
                    session_status=SessionStatus.CLOSED,
                )
                self._emit_event("session.closed", {"state": self._state.as_dict()})
            self._display_signal_observer.close()
            self.lifecycle.shutdown()
            return (_response(request, "shutdown-ack", {"pid": os.getpid()}), False)
        if self.lifecycle.state.value != "initialized":
            raise ProtocolError("player is not initialized")
        if request.kind == "recovery-transition":
            phase = request.payload.get("phase")
            if not isinstance(phase, str) or phase not in {"recovering", "recovered"}:
                raise ProtocolError("recovery phase must be recovering or recovered")
            state = self._state.as_dict()
            if phase == "recovering":
                with self._event_lock:
                    self._invalidate_display_signal()
                    state = self._state.as_dict()
                state["status"] = "recovering"
            event = self._emit_event(
                f"player.{phase}",
                {
                    "recovery_phase": phase,
                    "state": state,
                },
            )
            return (
                _response(
                    request,
                    "recovery-ack",
                    {"phase": phase, "sequence": event.sequence},
                ),
                True,
            )
        adapter = self.lifecycle.adapter
        if request.kind == "observe-probe-phase":
            if request.payload:
                raise ProtocolError("probe phase accepts no parameters")
            with self._event_lock:
                phase = self._probe_phase()
            return (_response(request, "probe-phase", phase), True)
        if request.kind in {"observe-probe-boundary", "observe-probe-performance"}:
            if request.payload:
                raise ProtocolError("probe observation accepts no parameters")
            with self._event_lock:
                phase_before = self._probe_phase()
            if phase_before["phase"] != "playing":
                return (_response(request, "performance", {"probe_phase_before": phase_before,
                        "probe_phase_after": phase_before}), True)
            result = adapter.observe_performance()
            if request.kind == "observe-probe-boundary":
                result["hwdec_current"] = adapter.observe_hwdec_current()
                result["sample_finished_monotonic_ns"] = time.monotonic_ns()
            with self._event_lock:
                phase_after = self._probe_phase()
            result["probe_phase_before"] = phase_before
            result["probe_phase_after"] = phase_after
            result["playback_epoch"] = {
                **phase_before["epoch"], "generation_after": phase_after["epoch"]["generation_before"],
                "owner_stable": phase_before["epoch"]["player_id"] == phase_after["epoch"]["player_id"]
                    and phase_before["epoch"]["session_id"] == phase_after["epoch"]["session_id"],
            }
            return (_response(request, "performance", result), True)
        if request.kind == "observe-performance":
            if request.payload:
                raise ProtocolError("performance observation accepts no parameters")
            with self._event_lock:
                before = self._display_signal_epoch
            result = adapter.observe_performance()
            with self._event_lock:
                after = self._display_signal_epoch
            # Reuse the existing conservative media/seek/output invalidation
            # epoch. Changed epochs invalidate counter deltas, including a
            # same-file reload/seek, without adding another event observer.
            result["playback_epoch"] = {"player_id": self.player_id, "session_id": self.session_id,
                                        "generation_before": before, "generation_after": after}
            return (_response(request, "performance", result), True)
        if request.kind == "snapshot":
            snapshot = adapter.snapshot()
            with self._event_lock:
                self._resample_display_signal_frame(snapshot)
                self._refresh_output_status()
            return (_response(request, "snapshot", snapshot), True)
        if request.kind == "observe-drm-mode":
            if request.payload:
                raise ProtocolError("DRM mode observation accepts no parameters")
            return (_response(request, "drm-mode", {"observation": adapter.observe_drm_mode()}), True)
        if request.kind == "observe-native-video-capabilities":
            if request.payload:
                raise ProtocolError("native-video-capabilities observation accepts no parameters")
            return (_response(request, "native-video-capabilities", {"capabilities": adapter.observe_native_video_capabilities()}), True)
        if request.kind == "observe-hwdec-current":
            if request.payload:
                raise ProtocolError("hwdec-current observation accepts no parameters")
            return (
                _response(
                    request,
                    "hwdec-current",
                    {"value": adapter.observe_hwdec_current()},
                ),
                True,
            )
        if request.kind == "observe-audio-chain":
            if request.payload:
                raise ProtocolError("audio-chain observation accepts no parameters")
            try:
                with self._event_lock:
                    owner = (self.player_id, self.session_id, self._load_ownership_revision)
                observation = adapter.observe_audio_chain()
                with self._event_lock:
                    if owner != (self.player_id, self.session_id, self._load_ownership_revision):
                        raise ProtocolError("audio-chain host owner changed during read")
                observation.update(player_id=owner[0], session_id=owner[1],
                                   load_ownership_revision=owner[2])
            except Exception:
                # Scope native-detail suppression to this diagnostic operation.
                raise ProtocolError("audio-chain observation failed") from None
            return (_response(request, "audio-chain", observation), True)
        if request.kind == "observe-audio-decoder":
            if request.payload:
                raise ProtocolError("audio-decoder observation accepts no parameters")
            return (
                _response(
                    request,
                    "audio-decoder",
                    adapter.observe_audio_decoder(),
                ),
                True,
            )
        if request.kind == "observe-display-geometry":
            if request.payload:
                raise ProtocolError("display geometry observation takes no arguments")
            return (_response(request, "display-geometry", {"observation": adapter.observe_display_geometry()}), True)
        if request.kind == "observe-restart-options":
            option_names = request.payload.get("option_names")
            if (
                not isinstance(option_names, list)
                or any(not isinstance(name, str) for name in option_names)
            ):
                raise ProtocolError("restart option names must be a string list")
            return (
                _response(
                    request,
                    "restart-options",
                    {"options": adapter.observe_restart_options(option_names)},
                ),
                True,
            )
        if request.kind == "events-poll":
            limit = request.payload.get("limit", 128)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 256:
                raise ProtocolError("event poll limit must be an integer between 1 and 256")
            self._complete_native_episode()
            if not self._native_event_contract:
                self._complete_stopped_load()
            self._complete_pending_position_checkpoint()
            with self._event_lock:
                self._refresh_output_status()
                pending = self._position_coalescer.flush(force=True)
                if pending is not None:
                    self._emit_event(
                        "time-pos",
                        {
                            "property": "time-pos",
                            "value": pending.position,
                            "state": self._state.as_dict(),
                        },
                    )
                events = []
                while len(events) < limit and len(self._events):
                    event = self._events.get_nowait()
                    # Only this boundary knows which telemetry was intentionally
                    # removed. A consumer must never infer continuity after loss.
                    event = replace(event, payload={
                        **event.payload,
                        "stream_previous_sequence": self._last_delivered_sequence,
                    })
                    events.append(event.to_dict())
                    self._last_delivered_sequence = event.sequence
                return (
                    _response(
                        request,
                        "events",
                        {
                            "events": events,
                            "queue": self._events.stats,
                            "last_sequence": self._sequencer.last(self.player_id),
                        },
                    ),
                    True,
                )
        if request.kind == "event-stats":
            self._complete_native_episode()
            with self._event_lock:
                self._refresh_output_status()
                return (
                    _response(
                        request,
                        "event-stats",
                        {
                            "state": self._state.as_dict(),
                            "queue": self._events.stats,
                            "transition_policy": {
                                "selected": self._recorder.persisted_count,
                                "suppressed_telemetry": self._recorder.suppressed_telemetry_count,
                                "writer_configured": self._transition_writer is not None,
                                "outbox_types": [event.type for event in self._transition_outbox],
                                "sequences": [event.sequence for event in self._transition_outbox],
                            },
                            "observer_errors": list(self._observer_errors),
                        },
                    ),
                    True,
                )
        if request.kind == 'load' and self._native_event_contract:
            path = request.payload.get('path', '')
            ep = self._begin_native_load(path, request.payload.get('queue_intent_id'))
            self._invalidate_display_signal()
            try:
                adapter.load(path)
            except Exception as error:
                self._native_command_failed(error)
                raise
            try:
                identity = adapter.loaded_playlist_entry(path)
            except (RuntimeError, AttributeError, ValueError):
                identity = None
            self._accept_native_load(ep, identity)
            self._complete_native_episode()
            return (_response(request, 'command-ack', {
                'state': self.lifecycle.state.value,
                'native_episode': self._episode_receipt(ep)}), True)
        if request.kind == 'stop' and self._native_event_contract:
            self._begin_native_stop()
            self._invalidate_display_signal()
            try:
                adapter.stop()
            except Exception as error:
                self._native_command_failed(error)
                raise
            with self._event_lock:
                self._native_operation = None
                self._cancel_native_episode()
                self._playback_stop_fenced = True
                self._pending_stopped_load = None
                self._verified_stopped_load_identity = None
                self._stopped_load_path_disputed = False
                self._load_ownership_unconfirmed = False
                self._verified_terminal_eof = False
                announce_closed_stop = (self._playback_session_ended and
                                        self._state.status is not PlayerStatus.STOPPED)
                self._end_playback_session()
                # Stop also supersedes a pending replay of an already closed
                # EOF session. Present the accepted Stop without ending that
                # historical session a second time or changing its EOF cause.
                self._state = self._state.with_properties(
                    status=PlayerStatus.STOPPED, session_status=SessionStatus.CLOSED)
                if announce_closed_stop:
                    self._emit_event('stopped', {'state':self._state.as_dict()})
            return (_response(request, 'command-ack', {'state':self.lifecycle.state.value}), True)
        replay = self._begin_native_replay(request.kind) if request.kind in {
            'play', 'resume', 'seek-absolute', 'seek-relative'} else None
        checkpoint_before = None
        seek_intent = None
        if request.kind in {"load", "stop", "pause", "play", "resume",
                            "seek-absolute", "seek-relative"}:
            with self._event_lock:
                self._pending_position_checkpoint = None
        if request.kind in {"pause", "seek-absolute", "seek-relative"}:
            try:
                checkpoint_before = self._position_snapshot(
                    allow_seeking=True) if request.kind.startswith("seek-") else self._position_snapshot()
            except (PositionCheckpointError, RuntimeError, AttributeError):
                pass
        if request.kind.startswith("seek-") and checkpoint_before is not None:
            with self._event_lock:
                self._position_command_serial += 1
                seek_intent = {
                    "command_id": self._position_command_serial,
                    "origin": request.kind, "requested": request.payload.get("seconds"),
                    "before": checkpoint_before, "adapter": adapter,
                    "identity": self._load_media_identity(checkpoint_before[0].get("path")),
                    "generation": checkpoint_before[1], "accepted": False,
                    "seek_seen": False, "seek_finished": False, "attempts": 0,
                    "expires": self._pending_checkpoint_now() + 15.0,
                    "next_attempt": 0.0,
                }
                if (self._display_signal_epoch != checkpoint_before[1]
                        or (self.player_id, self.session_id) != checkpoint_before[2:]
                        or self._playback_session_ended or self._playback_stop_fenced
                        or self._pending_stopped_load is not None
                        or self._load_ownership_unconfirmed or self._stopped_load_path_disputed
                        or self._state.properties.path != checkpoint_before[0].get("path")):
                    seek_intent = None
                    checkpoint_before = None
                self._pending_position_checkpoint = seek_intent
        if request.kind in {"load", "seek-absolute", "seek-relative", "stop"}:
            with self._event_lock:
                self._invalidate_display_signal(allow_pending_seek=request.kind.startswith("seek-"))
        if request.kind == "load":
            path = request.payload.get("path", "")
            with self._event_lock:
                pending = None
                previous_fence = self._playback_stop_fenced
                if previous_fence or self._verified_stopped_load_identity is not None:
                    self._playback_stop_fenced = True
                    pending = {"session_id": self.session_id, "accepted": False,
                               "active_seen": False, "path": path, "properties": {},
                               "identity": self._load_media_identity(path),
                               "position_signal": None}
                    self._pending_stopped_load = pending
                    self._stopped_load_path_disputed = False
                    self._load_ownership_revision += 1
            try:
                adapter.load(path)
            except Exception:
                with self._event_lock:
                    if pending is not None and self._pending_stopped_load is pending:
                        self._pending_stopped_load = None
                        self._playback_stop_fenced = previous_fence
                raise
            with self._event_lock:
                if pending is not None and self._pending_stopped_load is pending:
                    pending["accepted"] = True
            self._complete_stopped_load()
        elif request.kind == "play":
            try:
                adapter.play()
            except Exception as error:
                if replay is not None: self._native_command_failed(error)
                raise
        elif request.kind == "pause":
            adapter.pause()
        elif request.kind == "resume":
            try:
                adapter.resume()
            except Exception as error:
                if replay is not None: self._native_command_failed(error)
                raise
        elif request.kind in {"seek-absolute", "seek-relative"}:
            try:
                if request.kind == "seek-absolute":
                    adapter.seek_absolute(request.payload.get("seconds"))
                else:
                    adapter.seek_relative(request.payload.get("seconds"))
            except Exception as error:
                with self._event_lock:
                    self._pending_position_checkpoint = None
                if replay is not None: self._native_command_failed(error)
                raise
            with self._event_lock:
                if self._pending_position_checkpoint is seek_intent and seek_intent is not None:
                    seek_intent["accepted"] = True
        elif request.kind == "stop":
            with self._event_lock:
                previous_fence = self._playback_stop_fenced
                previous_identity = self._verified_stopped_load_identity
                previous_terminal_eof = self._verified_terminal_eof
                previous_dispute = self._stopped_load_path_disputed
                self._playback_stop_fenced = True
                # A later Stop cancels even an accepted load awaiting callbacks.
                self._pending_stopped_load = None
                self._verified_stopped_load_identity = None
                self._stopped_load_path_disputed = False
                self._load_ownership_revision += 1
                self._verified_terminal_eof = False
            try:
                adapter.stop()
            except Exception:
                with self._event_lock:
                    self._playback_stop_fenced = previous_fence
                    self._verified_stopped_load_identity = previous_identity
                    self._verified_terminal_eof = previous_terminal_eof
                    self._stopped_load_path_disputed = previous_dispute
                raise
            with self._event_lock:
                self._end_playback_session()
        elif request.kind == "set-volume":
            adapter.set_volume(request.payload.get("volume"))
        elif request.kind == "set-speed":
            adapter.set_speed(request.payload.get("speed"))
        elif request.kind == "select-track":
            track_id = request.payload.get("track_id")
            track_type = request.payload.get("type")
            if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id < 0:
                raise ProtocolError("track_id must be a non-negative integer")
            if track_type not in {"audio", "sub", "video"}:
                raise ProtocolError("track type is not selectable")
            adapter.select_track(track_id, track_type)
        else:
            raise ProtocolError(f"unsupported request kind: {request.kind}")
        checkpoint = None
        self._accept_native_replay(replay)
        if request.kind in {"pause", "seek-absolute", "seek-relative"}:
            checkpoint = self._checkpoint_position(request.kind, checkpoint_before,
                                                   request.payload.get("seconds"), seek_intent)
        payload = {"state": self.lifecycle.state.value}
        if checkpoint is not None:
            payload["resume_checkpoint"] = checkpoint
        return (_response(request, "command-ack", payload), True)

    def _probe_phase(self):
        """Cached admission only; never a native GET or filesystem read."""
        values = self._observed_properties
        path = values.get("path")
        if type(path) is not str or not path:
            phase = "no_media"
        elif values.get("pause") is True:
            phase = "paused"
        elif values.get("seeking") is True:
            phase = "seeking"
        elif (self._playback_session_ended or self._playback_stop_fenced
              or self._pending_stopped_load is not None or self._load_ownership_unconfirmed
              or self._stopped_load_path_disputed):
            phase = "not_playing"
        elif (self._state.status is not PlayerStatus.PLAYING or values.get("pause") is not False
              or values.get("seeking") is not False or values.get("idle-active") is not False
              or values.get("eof-reached") is True):
            phase = "phase_unknown"
        else:
            phase = "playing"
        return {"phase": phase, "media_token": hashlib.sha256(path.encode()).hexdigest()
                if type(path) is str and path else None,
                "epoch": {"player_id": self.player_id, "session_id": self.session_id,
                          "generation_before": self._display_signal_epoch}}

    def _position_snapshot(self, *, allow_seeking=False):
        with self._event_lock:
            before = self._display_signal_epoch
            before_owner = self.player_id, self.session_id
        snapshot = self.lifecycle.adapter.snapshot()
        with self._event_lock:
            after = self._display_signal_epoch
            owner = self.player_id, self.session_id
        if before != after or before_owner != owner:
            raise PositionCheckpointError("native generation changed during read")
        (native_seek_context if allow_seeking else native_position)(snapshot)
        return snapshot, after, *owner

    def _pending_checkpoint_valid(self, pending, *, deferred=False):
        # Called under the event lock. A pending pointer identifies the latest
        # command; ownership and generation fences cover asynchronous callbacks.
        if (pending is None or self._pending_position_checkpoint is not pending
                or not pending["accepted"] or self.lifecycle.adapter is not pending["adapter"]
                or (self.player_id, self.session_id) != pending["before"][2:]
                or self._display_signal_epoch != pending["generation"]
                or self._playback_session_ended or self._playback_stop_fenced
                or self._pending_stopped_load is not None
                or self._load_ownership_unconfirmed or self._stopped_load_path_disputed
                or self._pending_checkpoint_now() >= pending["expires"]):
            if self._pending_position_checkpoint is pending:
                self._pending_position_checkpoint = None
            return False
        identity = pending["identity"]
        if ((deferred and identity is None) or (identity is not None and
                self._load_media_identity(pending["before"][0].get("path")) != identity)):
            self._pending_position_checkpoint = None
            return False
        return True

    def _produce_position_checkpoint(self, origin, before, requested_seconds, pending=None):
        if pending is not None:
            with self._event_lock:
                if not self._pending_checkpoint_valid(pending):
                    raise PositionCheckpointError("seek command ownership changed")
        first_read = self._position_snapshot()
        second_read = self._position_snapshot()
        if first_read[2:] != before[2:] or second_read[2:] != before[2:]:
            raise PositionCheckpointError("native session changed")
        first, generation = first_read[:2]
        second, confirmed_generation = second_read[:2]
        values = verified_position(
            before[0], first, second, origin=origin,
            before_generation=before[1], first_generation=generation,
            second_generation=confirmed_generation, requested_seconds=requested_seconds,
        )
        with self._event_lock:
            if (self._display_signal_epoch != confirmed_generation
                    or (self.player_id, self.session_id) != before[2:]
                    or pending is not None and not self._pending_checkpoint_valid(pending)):
                raise PositionCheckpointError("native generation changed before receipt")
            state = self._state.as_dict()
            cached = state["properties"]
            cached_position = cached.get("time_pos")
            if (cached.get("path") != values["path"]
                    or cached.get("pause") is not values["pause"]
                    or cached.get("duration") != values["duration"]
                    or type(cached_position) not in (int, float)
                    or not math.isfinite(cached_position)
                    or abs(cached_position - values["position"]) > (0.05 if values["pause"] else 0.5)):
                raise PositionCheckpointError("position callbacks have not settled")
            state["status"] = "paused" if values["pause"] else "playing"
            state["properties"] = {**state["properties"], "path": values["path"],
                                   "time_pos": values["position"], "duration": values["duration"],
                                   "pause": values["pause"], "idle_active": False}
            proof = {**values, "origin": origin, "player_id": self.player_id,
                     "session_id": self.session_id, "seeking": False,
                     "generation_before": confirmed_generation,
                     "generation_after": confirmed_generation}
            event = self._emit_event("resume.checkpoint", {"state": state,
                                     "position_checkpoint": proof})
            if pending is not None:
                self._pending_position_checkpoint = None
            return {"status": "queued", "sequence": event.sequence}

    def _checkpoint_position(self, origin, before, requested_seconds, pending=None):
        if before is None:
            return {"status": "unavailable", "reason": "pre_read_unavailable"}
        deadline = time.monotonic() + 0.5
        while True:
            try:
                return self._produce_position_checkpoint(origin, before, requested_seconds, pending)
            except (PositionCheckpointError, RuntimeError, AttributeError) as error:
                if time.monotonic() >= deadline:
                    with self._event_lock:
                        retained = pending is not None and self._pending_checkpoint_valid(pending, deferred=True)
                        if retained:
                            pending["next_attempt"] = self._pending_checkpoint_now() + 0.1
                    return {"status": "unavailable", "reason": "post_read_unsettled",
                            "detail": str(error), "pending": retained}
                time.sleep(0.025)

    def _complete_pending_position_checkpoint(self):
        # Existing serialized events-poll is the only deferred execution point.
        # No native GET occurs in callbacks, under the event lock, or after the
        # intent resolves, expires, is superseded or loses its owner.
        with self._event_lock:
            pending = self._pending_position_checkpoint
            if pending is None or not self._pending_checkpoint_valid(pending, deferred=True):
                return
            now = self._pending_checkpoint_now()
            if now < pending["next_attempt"]:
                return
            if pending["attempts"] >= 150:
                self._pending_position_checkpoint = None
                return
            pending["attempts"] += 1
            pending["next_attempt"] = now + 0.1
        try:
            self._produce_position_checkpoint(pending["origin"], pending["before"],
                                              pending["requested"], pending)
        except (PositionCheckpointError, RuntimeError, AttributeError):
            # Native proof is unavailable. SQL failures are handled separately
            # by the existing event ingestion retry and cannot trigger resampling.
            pass

    def _end_playback_session(self) -> None:
        self._pending_position_checkpoint = None
        if self._playback_session_ended or not self._playback_session_had_media:
            return
        self._playback_session_ended = True
        self._state = self._state.with_properties(
            status=PlayerStatus.STOPPED, session_status=SessionStatus.CLOSED)
        self._emit_event("stopped", {"state": self._state.as_dict()})

    def _begin_playback_session(self, *, same_media_replay: bool = False) -> None:
        """A new observed load/replay owns a new immutable durable session."""
        if not self._playback_session_ended:
            self._end_playback_session()
        self.session_id = uuid.uuid4().hex
        self._playback_session_ended = False
        self._playback_session_had_media = False
        self._verified_terminal_eof = False
        # Preserve existing observed controls (including the seeking edge),
        # while invalidating media facts and deferred position telemetry.
        # Same-file EOF replay still verifies retained position against native
        # readback; a different path starts without the prior position/duration.
        reset_fields = _MEDIA_STATUS_PROPERTIES | {"eof-reached"}
        if not same_media_replay:
            reset_fields |= {"time-pos", "duration",
                             "chapter-list", "chapter", "media-title"}
        self._observed_properties = {
            key: item for key, item in self._observed_properties.items()
            if key not in reset_fields
        }
        self._position_coalescer = PositionCoalescer()
        self._state = PlayerState.initial(self.player_id, self.session_id)
        self._output_identity_key = None
        self._media_status_resample = True
        self._invalidate_display_signal()

    @staticmethod
    def _load_media_identity(path: Any) -> MediaIdentity | None:
        # Reuse the durable store's local regular-file identity contract:
        # relative/dot/symlink forms resolve to the same canonical media.
        try:
            return MediaIdentity.from_path(path)
        except MediaIdentityError:
            return None

    def _terminal_native_reason(self, snapshot: Any, expected: MediaIdentity) -> str | None:
        if (not isinstance(snapshot, Mapping) or snapshot.get("seeking") is not False
                or type(snapshot.get("eof-reached")) is not bool):
            return None
        path = snapshot.get("path")
        cleared = path is None or type(path) is str and not path
        if cleared and snapshot.get("idle-active") is True:
            return "eof" if snapshot["eof-reached"] else "stopped"
        if (snapshot["eof-reached"] is True
                and type(snapshot.get("idle-active")) is bool
                and self._load_media_identity(path) == expected):
            return "eof"
        return None

    def _apply_owned_terminal(self, snapshot: Mapping[str, Any], reason: str,
                              expected: MediaIdentity) -> None:
        if self._playback_session_ended:
            self._stopped_load_path_disputed = False
            return
        # Attribute the end to the verified owner before clearing its path.
        # EOF and non-EOF unload have distinct immutable durable end reasons.
        self._verified_stopped_load_identity = None
        self._stopped_load_path_disputed = False
        if reason == "eof":
            self._on_property("eof-reached", True)
        else:
            self._end_playback_session()
        self._on_property("idle-active", snapshot["idle-active"])
        if not snapshot.get("path"):
            self._on_property("path", None)
            self._on_property("time-pos", None)
            self._on_property("duration", None)
        self._position_coalescer = PositionCoalescer()
        self._verified_terminal_eof = reason == "eof"
        self._verified_stopped_load_identity = expected if reason == "eof" else None
        self._playback_stop_fenced = reason != "eof"
        self._load_ownership_unconfirmed = False

    def _complete_stopped_load(self) -> bool:
        """Verify load ownership on the host request thread, never a callback."""
        with self._event_lock:
            pending = self._pending_stopped_load
            refresh = (pending is None and not self._playback_stop_fenced
                       and self._stopped_load_path_disputed)
            if refresh:
                expected = self._verified_stopped_load_identity
            elif (pending is not None and pending["accepted"]
                  and (pending["active_seen"] or pending["position_signal"] is not None)
                  and pending["session_id"] == self.session_id):
                expected = pending["identity"]
                observed = pending["properties"].get(
                    "path", self._observed_properties.get("path"))
                if expected is None or self._load_media_identity(observed) != expected:
                    return False
            else:
                return False
            if expected is None:
                return False
            before = (self.player_id, self.session_id, self._display_signal_epoch,
                      self._load_ownership_revision)
        # Native GET must not hold the event lock or run on libmpv's callback
        # thread. Read both active and terminal facts before interpreting them.
        try:
            first_raw = self.lifecycle.adapter.snapshot()
            second_raw = self.lifecycle.adapter.snapshot()
        except (RuntimeError, AttributeError):
            return False
        reasons = [self._terminal_native_reason(row, expected) for row in (first_raw, second_raw)]
        foreign = any(isinstance(row, Mapping) and isinstance(row.get("path"), str)
                      and row["path"] and self._load_media_identity(row["path"]) != expected
                      for row in (first_raw, second_raw))
        with self._event_lock:
            after = (self.player_id, self.session_id, self._display_signal_epoch,
                     self._load_ownership_revision)
            if before != after or self._pending_stopped_load is not pending:
                return False
            if foreign:
                if refresh:
                    self._load_ownership_unconfirmed = True
                return False
            if refresh and reasons[0] is not None and reasons[0] == reasons[1]:
                explicit_owner = all(isinstance(row, Mapping)
                                     and self._load_media_identity(row.get("path")) == expected
                                     for row in (first_raw, second_raw))
                if self._load_ownership_unconfirmed and not explicit_owner:
                    return False
                self._apply_owned_terminal(second_raw, reasons[1], expected)
                return True
        try:
            first, second = native_position(first_raw), native_position(second_raw)
        except PositionCheckpointError:
            return False
        if (self._load_media_identity(first["path"]) != expected
                or self._load_media_identity(second["path"]) != expected):
            return False
        if (pending is not None and not pending["active_seen"]
                and any(abs(row["position"] - pending["position_signal"]) > 0.5
                        for row in (first, second))):
            return False
        with self._event_lock:
            after = (self.player_id, self.session_id, self._display_signal_epoch,
                     self._load_ownership_revision)
            if (before != after or self._pending_stopped_load is not pending
                    or (refresh and not self._stopped_load_path_disputed)):
                return False
            self._pending_stopped_load = None
            self._playback_stop_fenced = False
            self._stopped_load_path_disputed = False
            self._verified_stopped_load_identity = expected
            self._load_ownership_unconfirmed = False
            if not refresh or self._verified_terminal_eof and self._playback_session_ended:
                self._begin_playback_session()
                self._observed_properties.pop("path", None)
            # Old callback buffers never supply position or source facts for
            # the new owner. Hydrate from the verified current native readback.
            for name, value in (("path", expected.canonical_path),
                                ("idle-active", False), ("eof-reached", False),
                                ("seeking", False), ("pause", second["pause"]),
                                ("duration", second["duration"]),
                                ("time-pos", second["position"])):
                self._on_property(name, value)
            return True

    def _on_property(self, name: str, value: Any) -> None:
        """Normalize one observed libmpv property into state and event flow."""

        with self._event_lock:
            try:
                if self._native_property_is_fenced(name, value):
                    return
                previous = self._observed_properties.get(name, _MISSING)
                previous_output_status = self._state.output_status
                json_value = _json_safe(_normalize_observed_property(name, value))
                if self._playback_stop_fenced:
                    pending = self._pending_stopped_load
                    if pending is not None and pending["session_id"] == self.session_id:
                        self._load_ownership_revision += 1
                        if name == "idle-active":
                            pending["active_seen"] = json_value is False
                        elif (name == "path" and pending["identity"] is not None
                              and self._load_media_identity(json_value) == pending["identity"]):
                            pending["active_seen"] = True
                        elif (name == "time-pos" and type(json_value) in (int, float)
                              and json_value != previous):
                            try:
                                signal = float(json_value)
                            except OverflowError:
                                signal = None
                            if signal is not None and math.isfinite(signal) and signal >= 0:
                                pending["position_signal"] = signal
                        # Before a fresh active edge, position/duration callbacks
                        # may still belong to the stopped media. Do not retain them.
                        if name in {"path", "idle-active"} or pending["active_seen"]:
                            pending["properties"][name] = json_value
                    if name in {"path", "time-pos", "duration", "eof-reached",
                                "chapter-list", "chapter", "media-title"} or (
                            name == "idle-active" and json_value is False
                    ):
                        # Stop is not EOF replay. Delayed callbacks cannot create
                        # a new owner or rewrite resume after the Stop boundary.
                        return
                elif self._verified_stopped_load_identity is not None:
                    expected = self._verified_stopped_load_identity
                    if (self._verified_terminal_eof and self._playback_session_ended
                            or name == "eof-reached" and json_value is True
                            or name == "idle-active" and json_value is True):
                        self._stopped_load_path_disputed = True
                    if name == "path":
                        if self._load_media_identity(json_value) != expected:
                            self._stopped_load_path_disputed = True
                        else:
                            json_value = expected.canonical_path
                    if self._stopped_load_path_disputed:
                        self._load_ownership_revision += 1
                        if name in {"path", "time-pos", "duration", "eof-reached",
                                    "idle-active", "chapter-list", "chapter", "media-title"}:
                            return
                path = self._observed_properties.get("path")
                rotated = False
                if (name == "path" and isinstance(json_value, str) and json_value
                        and (self._playback_session_ended
                             or (isinstance(path, str) and path and path != json_value))):
                    self._begin_playback_session()
                    rotated = True
                elif (name == "eof-reached" and json_value is False
                      and self._playback_session_ended and path
                      and self._observed_properties.get("idle-active") is False):
                    # Native EOF reset can also mean a seek/play replay without
                    # any path notification. Retained values still must match
                    # fresh settled native readback before a forced checkpoint.
                    self._begin_playback_session(same_media_replay=True)
                    rotated = True
                elif (name == "time-pos" and isinstance(json_value, (int, float))
                      and not isinstance(json_value, bool) and self._playback_session_ended
                      and path and self._observed_properties.get("eof-reached") is False
                      and self._observed_properties.get("idle-active") is False):
                    # A seek away from EOF can replay without a path change.
                    self._begin_playback_session(same_media_replay=True)
                    rotated = True
                if (name == "idle-active" and json_value is True
                        and self._playback_session_had_media):
                    self._end_playback_session()
                retained_properties = self._observed_properties
                if (name == "path" and previous != json_value) or (name == "idle-active" and json_value is True):
                    # Do not carry source/frame/target facts from a previous
                    # media epoch into the next title or the idle snapshot.
                    retained_properties = {key: item for key, item in retained_properties.items()
                                           if key not in _MEDIA_STATUS_PROPERTIES}
                    self._output_identity_key = None
                    self._media_status_resample = True
                    self._invalidate_display_signal()
                elif name == "eof-reached" and json_value is True:
                    self._invalidate_display_signal("no_current_frame")
                elif name in {"current-vo", "current-gpu-context", "drm-device", "drm-connector"} and previous != json_value:
                    self._invalidate_display_signal("connector_changed")
                    self._display_signal_resample = True
                elif name == "seeking" and json_value is True:
                    pending_seek = self._pending_position_checkpoint
                    allowed = pending_seek is not None and not pending_seek["seek_finished"]
                    self._invalidate_display_signal(allow_pending_seek=allowed)
                    if allowed:
                        pending_seek["seek_seen"] = True
                    self._display_signal_seek_seen = True
                elif name == "seeking" and json_value is False and self._display_signal_seek_seen:
                    if self._pending_position_checkpoint is not None:
                        self._pending_position_checkpoint["seek_finished"] = True
                    self._display_signal_resample = True
                    self._display_signal_seek_seen = False
                if name in {"video-out-params", "video-target-params"}:
                    if isinstance(json_value, Mapping) and json_value:
                        self._display_signal_seen.add(name)
                        if len(self._display_signal_seen) == 2:
                            self._display_signal_pending = False
                    else:
                        self._invalidate_display_signal("no_current_frame", allow_pending_seek=True)
                if (name == "pause" and previous != json_value
                        and self._pending_position_checkpoint is not None
                        and json_value is not self._pending_position_checkpoint["before"][0].get("pause")):
                    self._pending_position_checkpoint = None
                next_observed_properties = {
                    **retained_properties,
                    name: json_value,
                }
                player_property_values = {
                    property_name: property_value
                    for property_name, property_value in next_observed_properties.items()
                    if property_name not in _STATUS_ONLY_PROPERTIES
                }
                properties = PlayerProperties.from_raw(player_property_values)
                self._refresh_liborender_mapping()
                output_status = PlayerOutputStatus.from_playback_observation(
                    next_observed_properties,
                    renderer_library_loaded=self._liborender_mapped,
                    output_identity=self._observed_output_identity(next_observed_properties),
                    display_signal=self._observed_display_signal(next_observed_properties),
                )
                self._observed_properties = next_observed_properties
                if properties.path is not None and not properties.idle_active:
                    self._playback_session_had_media = True
                if name == "eof-reached" and json_value is True and properties.path is not None:
                    self._playback_session_ended = True
                status = (PlayerStatus.STOPPED if self._playback_stop_fenced
                          and self._playback_session_ended else _status_for(properties))
                session_status = (
                    SessionStatus.CLOSED if self._playback_session_ended else SessionStatus.ACTIVE
                    if properties.path is not None
                    else SessionStatus.NEW
                )
                self._state = self._state.with_properties(
                    properties=properties,
                    status=status,
                    session_status=session_status,
                    output_status=output_status,
                )
                if not rotated and previous == json_value and output_status == previous_output_status:
                    return
                if name in _STATUS_ONLY_PROPERTIES:
                    # Fixed adapter inputs remain private; emit only the
                    # normalized status DTO when media/output facts change.
                    if output_status != previous_output_status:
                        self._emit_event(
                            "output.status", {"state": self._state.as_dict()}
                        )
                    return
                if name == "time-pos":
                    if value is None:
                        return
                    update = self._position_coalescer.update(value)
                    if update is None:
                        return
                    event_value = update.position
                else:
                    event_value = json_value
                self._emit_event(
                    name,
                    {
                        "property": name,
                        "value": event_value,
                        "state": self._state.as_dict(),
                    },
                )
            except Exception as error:
                self._observer_errors.append(f"{name}: {error}")

    def _invalidate_display_signal(self, reason: str = "epoch_invalidated", *,
                                   allow_pending_seek: bool = False) -> None:
        self._display_signal_epoch += 1
        if allow_pending_seek and self._pending_position_checkpoint is not None:
            self._pending_position_checkpoint["generation"] = self._display_signal_epoch
        else:
            self._pending_position_checkpoint = None
        self._display_signal_pending = True
        self._display_signal_seen.clear()
        self._display_signal_resample = False
        # Seeking is an independent lifecycle edge. Intermediate null target
        # or route callbacks must not erase the pending True -> False edge.
        self._display_signal_reason = reason
        fields = self._display_signal_observer.invalidate(reason)
        if self._state.output_status is not None:
            status = replace(self._state.output_status, **fields)
            if status != self._state.output_status:
                self._state = self._state.with_properties(output_status=status)
                self._emit_event("output.status", {"state": self._state.as_dict()})

    def _observed_display_signal(self, properties: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._display_signal_pending:
            return unknown_display_signal(self._display_signal_reason)
        return self._display_signal_observer.observe(
            properties, epoch=(self.player_id, self._display_signal_epoch),
        )

    def _resample_display_signal_frame(self, snapshot: Mapping[str, Any] | None = None) -> None:
        # Unchanged target properties need not generate callbacks after a seek.
        # Only re-sample after seeking True -> False, or an observed route change.
        # Missing seeking support keeps the signal unknown until fresh callbacks.
        if not self._display_signal_resample:
            return
        try:
            values = snapshot if snapshot is not None else self.lifecycle.adapter.snapshot()
        except Exception:
            return
        if (values.get("path") != self._observed_properties.get("path")
                or not values.get("path") or values.get("idle-active") is not False
                or values.get("seeking") is not False):
            return
        names = ("video-out-params", "video-target-params")
        if any(not isinstance(values.get(name), Mapping) or not values[name] for name in names):
            return
        if any(values.get(name) != self._observed_properties.get(name) for name in (
            "current-vo", "current-gpu-context", "drm-device", "drm-connector",
        )):
            return
        self._observed_properties.update({name: _json_safe(values[name]) for name in names})
        self._display_signal_seen.update(names)
        self._display_signal_pending = False
        self._display_signal_resample = False

    def _observed_output_identity(self, properties: Mapping[str, Any]) -> Mapping[str, Any]:
        # Device/route changes invalidate immediately. Poll fresh bounded
        # reports at most once a second otherwise, so time-pos/frame callbacks
        # do not repeatedly scan procfs/sysfs. Failed refresh clears old proof.
        key = tuple(properties.get(name) for name in _IDENTITY_PROPERTIES) + (
            bool(properties.get("audio-out-params")), bool(properties.get("video-out-params")),
        )
        now = time.monotonic()
        if key != self._output_identity_key or now - self._output_identity_checked_at >= 1.0:
            self._output_identity = self._output_identity_observer.observe(properties)
            self._output_identity_key = key
            self._output_identity_checked_at = now
        return self._output_identity

    def _resample_media_status(self) -> None:
        # A new media epoch clears prior facts. Unchanged mpv properties may
        # never produce new callbacks (notably stereo audio-out-params).
        # Read only the existing fixed snapshot, once playback is established;
        # never carry facts across a different path, idle or seek boundary.
        if not self._media_status_resample:
            return
        try:
            values = self.lifecycle.adapter.snapshot()
        except Exception:
            return
        if (not values.get("path") or values.get("path") != self._observed_properties.get("path")
                or values.get("idle-active") is not False or values.get("seeking") is not False
                or self._observed_properties.get("idle-active") is not False
                or self._observed_properties.get("seeking") is not False):
            return
        # Identity and output facts must come from the same fixed read. An
        # AO/route callback may also be pending while a media load completes.
        route_names = ("current-vo", "current-gpu-context", "drm-device", "drm-connector")
        route_changed = any(values.get(name) != self._observed_properties.get(name) for name in route_names)
        self._observed_properties.update({name: _json_safe(values.get(name))
                                         for name in _MEDIA_STATUS_PROPERTIES | frozenset(_IDENTITY_PROPERTIES)})
        if route_changed:
            self._invalidate_display_signal("connector_changed")
        self._media_status_resample = False
        self._output_identity_key = None
        # Re-read frame evidence independently in the same current epoch.
        self._display_signal_resample = True
        self._resample_display_signal_frame(values)

    def _refresh_output_status(self) -> None:
        """Refresh fixed playback facts without exposing arbitrary mpv properties."""

        self._resample_media_status()
        self._resample_display_signal_frame()
        self._refresh_liborender_mapping()
        status = PlayerOutputStatus.from_playback_observation(
            self._observed_properties,
            renderer_library_loaded=self._liborender_mapped,
            output_identity=self._observed_output_identity(self._observed_properties),
            display_signal=self._observed_display_signal(self._observed_properties),
        )
        if status == self._state.output_status:
            return
        self._state = self._state.with_properties(output_status=status)
        self._emit_event("output.status", {"state": self._state.as_dict()})

    def _refresh_liborender_mapping(self) -> None:
        """Observe whether the original upstream renderer library is mapped."""
        try:
            raw = Path("/proc/self/maps").read_bytes()
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("native mapping report is too large")
            maps = raw.decode("utf-8", errors="strict")
        except (OSError, UnicodeError, ValueError):
            self._liborender_mapped = None
            return
        self._liborender_mapped = any(
            len(fields := line.split(maxsplit=5)) == 6
            and not fields[5].endswith(" (deleted)")
            and _LIBORENDER_MAPPED_NAME.fullmatch(Path(fields[5]).name)
            for line in maps.splitlines()
        )

    def _emit_event(self, event_type: str, payload: Mapping[str, Any]) -> EventEnvelope:
        sequence = self._sequencer.next(self.player_id)
        event = EventEnvelope(
            schema_version=1,
            type=event_type,
            sequence=sequence,
            timestamp=time.time(),
            player_id=self.player_id,
            session_id=self.session_id,
            correlation_id=uuid.uuid4().hex,
            payload=payload,
        )
        durable = self._recorder.record(event)
        terminal = event_type in {"error", "eof", "stopped", "session.closed", "player.stopped",
                                 "native.end-file", "native.terminal-pending", "media.started"}
        if terminal:
            self._events.put_terminal(event)
        elif durable:
            self._events.put_transition(event)
        else:
            self._events.put_telemetry(event_type, event)
        return event

    def _record_transition(self, event: EventEnvelope) -> None:
        self._transition_outbox.append(event)
        if self._transition_writer is not None:
            self._transition_writer(event)


_MISSING = object()


def _normalize_observed_property(name: str, value: Any) -> Any:
    """Map python-mpv's documented unset/automatic sentinels to domain values."""

    if name == "eof-reached" and value is None:
        return False
    if name in {"aid", "sid", "vid"}:
        if value is False or (isinstance(value, str) and value in {"auto", "no"}):
            return None
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise TypeError(f"{name} must be an integer, None, or an unset libmpv sentinel")
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _status_for(properties: PlayerProperties) -> PlayerStatus:
    if properties.eof_reached:
        return PlayerStatus.ENDED
    if properties.idle_active:
        return PlayerStatus.IDLE
    return PlayerStatus.PAUSED if properties.pause else PlayerStatus.PLAYING


def serve(socket_path: Path) -> None:
    """Serve the private one-request-per-connection player-host protocol."""

    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    socket_path.chmod(0o600)
    server.listen(16)
    server.settimeout(0.2)
    runtime = HostRuntime()
    running = True
    try:
        while running:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                try:
                    with connection.makefile("rb") as connection_file:
                        line = connection_file.readline()
                except ConnectionError:
                    # The private client may time out or disconnect. Its
                    # connection does not own the lifetime of the player.
                    continue
                request = None
                try:
                    request = Envelope.decode(line)
                    if request.kind not in SUPPORTED_REQUESTS:
                        raise ProtocolError(f"unsupported request kind: {request.kind}")
                    reply, running = runtime.handle(request)
                except Exception as error:
                    request_id = request.request_id if request is not None else "invalid"
                    reply = Envelope(
                        PROTOCOL_VERSION,
                        request_id,
                        "error",
                        {"message": str(error), **({'native_outcome':'rejected'}
                            if isinstance(error, NativeCommandRejected) else {})},
                    )
                try:
                    connection.sendall(reply.encode())
                except ConnectionError:
                    # Keep the completed command's lifecycle result (including
                    # shutdown) even when its acknowledgement cannot be sent.
                    continue
    finally:
        runtime._display_signal_observer.close()
        server.close()
        if socket_path.exists():
            socket_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the private mpv player-host transport")
    parser.add_argument("--socket", required=True, type=Path)
    args = parser.parse_args()
    serve(args.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
