"""Origin-bound native load/start/end receipts, reconciled on the request thread."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from backend.domain.player_state import PlayerProperties, PlayerOutputStatus, PlayerStatus, SessionStatus
from backend.events.position_coalescing import PositionCoalescer
from player_host.libmpv_adapter import NativeCommandRejected, OBSERVED_PROPERTY_ATTRIBUTES


def entry_id(value: Any) -> bool:
    return type(value) is int and 0 <= value < 2**63


@dataclass
class NativeEpisode:
    serial: int
    path: str
    identity: Any
    intent_id: int | None
    playlist_entry_id: int | None = None
    session_id: str | None = None
    accepted: bool = False
    start_seen: bool = False
    confirmed: bool = False
    end_seen: bool = False
    reason: int | None = None
    error: int | None = None
    consumed: bool = False
    cancelled: bool = False
    terminal_signal: bool = False
    unconfirmed_idle: bool = False
    predecessor_entry_id: int | None = None
    replay: bool = False
    metadata_invalidations: int = 0


class NativeEpisodeSupport:
    def _init_native_episodes(self):
        self._native_event_contract = False
        self._native_serial = 0
        self._native_episode = None
        self._native_events = deque(maxlen=64)
        self._native_event_loss = False
        self._native_event_revision = 0
        self._native_metadata_revision = 0
        self._applying_native_proof = False
        self._native_operation = None
        self._native_rejected_predecessor = None
        self._native_rejected_attempts = 0
        self._native_rejected_revision = None

    def _on_native_event(self, kind, identity, reason=None, error=None):
        """Copy bounded scalars only; never read MPV/SQL or assign current epoch."""
        if kind not in {'start-file', 'end-file'} or not entry_id(identity):
            return
        if reason is not None and type(reason) is not int:
            reason = None
        if error is not None and type(error) is not int:
            error = None
        with self._event_lock:
            if len(self._native_events) == self._native_events.maxlen:
                self._native_event_loss = True
                self._observer_errors.append('native start/end buffer overflow; ownership unavailable')
            else:
                self._native_events.append((kind, identity, reason, error))
            self._native_event_revision += 1

    def _begin_native_load(self, path, intent_id):
        if intent_id is not None and (type(intent_id) is not int or intent_id <= 0):
            raise ValueError('queue intent must be a positive integer')
        with self._event_lock:
            predecessor = self._native_episode
            self._native_serial += 1
            self._native_event_loss = False
            ep = NativeEpisode(self._native_serial, path, self._load_media_identity(path), intent_id)
            if self._native_episode is not None:
                ep.predecessor_entry_id = self._native_episode.playlist_entry_id
            self._native_episode = ep
            self._native_operation = ('load', ep.serial, predecessor, self.session_id)
            self._native_rejected_predecessor = None
            self._pending_position_checkpoint = None
            return ep

    def _accept_native_load(self, ep, identity):
        with self._event_lock:
            if ep is not self._native_episode or ep.cancelled:
                return
            ep.accepted = True
            operation = self._native_operation
            if operation and operation[2] is not None:
                operation[2].cancelled = True
            self._native_operation = None
            self._load_ownership_unconfirmed = False
            ep.playlist_entry_id = identity if entry_id(identity) and identity != ep.predecessor_entry_id else None
            # Every accepted load has its own immutable attempted session,
            # including a same-path replacement. Loading is not active proof.
            self._begin_playback_session()
            self._observed_properties.pop('path', None)
            ep.session_id = self.session_id
            self._state = self._state.with_properties(status=PlayerStatus.LOADING)
            self._emit_event('load.accepted', {'state': self._state.as_dict(),
                                             'native_episode': self._episode_receipt(ep)})

    def _episode_receipt(self, ep):
        return {'load_serial': ep.serial, 'playlist_entry_id': ep.playlist_entry_id,
                'queue_intent_id': ep.intent_id, 'path': ep.path,
                'player_id': self.player_id, 'session_id': ep.session_id}

    def _cancel_native_episode(self):
        with self._event_lock:
            self._native_serial += 1
            if self._native_episode is not None:
                self._native_episode.cancelled = True
            self._pending_position_checkpoint = None

    def _begin_native_stop(self):
        with self._event_lock:
            self._native_serial += 1
            self._native_operation = ('stop', self._native_serial, self._native_episode, self.session_id)
            self._native_rejected_predecessor = None
            self._pending_position_checkpoint = None

    def _begin_native_replay(self, kind):
        if not self._native_event_contract:
            return None
        with self._event_lock:
            old = self._native_episode
            if (old is None or old.cancelled or not old.consumed or old.reason != 0
                    or not self._verified_terminal_eof or not self._playback_session_ended
                    or old.session_id != self.session_id):
                return None
            before = (self._native_serial, self._native_event_revision, self.session_id, self._display_signal_epoch)
        try:
            one = self.lifecycle.adapter.episode_snapshot()
            two = self.lifecycle.adapter.episode_snapshot()
        except (RuntimeError, ValueError, AttributeError):
            return None
        with self._event_lock:
            if (old is not self._native_episode or before !=
                    (self._native_serial, self._native_event_revision, self.session_id, self._display_signal_epoch)
                    or not all(self._episode_terminal_read(row, old) and
                               self._load_media_identity(row['properties'].get('path')) == old.identity
                               for row in (one, two))):
                return None
            self._native_serial += 1
            # Same retained entry, but a new explicit command and playback cycle.
            # The old closed session and EOF action are never reopened/replayed.
            ep = NativeEpisode(self._native_serial, old.path, old.identity, None,
                               playlist_entry_id=old.playlist_entry_id,
                               session_id=self.session_id, start_seen=True, replay=True)
            self._native_episode = ep
            self._native_operation = (kind, ep.serial, old, self.session_id)
            self._native_rejected_predecessor = None
            return ep

    def _accept_native_replay(self, ep):
        if ep is None:
            return
        with self._event_lock:
            if ep is not self._native_episode or ep.cancelled:
                return
            ep.accepted = True
            self._native_operation = None
            self._load_ownership_unconfirmed = False
        self._complete_native_episode()

    def _native_command_failed(self, error):
        with self._event_lock:
            operation = self._native_operation
            if operation is None:
                return
            self._native_operation = None
            ep = self._native_episode
            if ep is not None and ep is not operation[2]:
                ep.cancelled = True
            self._pending_position_checkpoint = None
            # Keep the predecessor as evidence, never as permission to auto-next.
            self._native_rejected_predecessor = operation if isinstance(error, NativeCommandRejected) else None
            self._native_rejected_attempts = 0
            self._native_rejected_revision = None
            self._load_ownership_unconfirmed = True
            self._observed_properties = {k:v for k,v in self._observed_properties.items()
                                         if k in {'pause','volume','speed'}}
            self._state = self._state.with_properties(
                properties=PlayerProperties.from_raw({k:v for k,v in self._observed_properties.items()
                                                      if k not in self._native_status_fields}),
                status=PlayerStatus.LOADING, output_status=None)
            self._emit_event('native.command-unconfirmed', {'state':self._state.as_dict(),
                             'operation':operation[0], 'outcome':'rejected' if self._native_rejected_predecessor else 'uncertain'})
        self._recover_rejected_native_episode()

    def _recover_rejected_native_episode(self):
        with self._event_lock:
            operation = self._native_rejected_predecessor
            if operation is None:
                return
            if (self._native_rejected_attempts >= 8 or
                    self._native_rejected_revision == self._native_event_revision):
                return
            old = operation[2]
            if (old is None or old.cancelled or not old.accepted or old.identity is None
                    or old.session_id != self.session_id):
                return
            retained = deque(maxlen=64)
            while self._native_events:
                event = self._native_events.popleft()
                if event[1] == old.playlist_entry_id and event[0] == 'start-file':
                    old.start_seen = True
                elif event[0] == 'end-file' and event[1] == old.playlist_entry_id and not old.end_seen:
                    old.end_seen, old.reason, old.error = True, event[2], event[3]
                else:
                    retained.append(event)
            self._native_events = retained
            before = (self._native_serial, self._native_event_revision, self.session_id, self._display_signal_epoch)
            self._native_rejected_revision = self._native_event_revision
            self._native_rejected_attempts += 1
        try:
            one = self.lifecycle.adapter.episode_snapshot()
            two = self.lifecycle.adapter.episode_snapshot()
        except (RuntimeError, ValueError, AttributeError):
            return
        with self._event_lock:
            if (operation is not self._native_rejected_predecessor or before !=
                    (self._native_serial, self._native_event_revision, self.session_id, self._display_signal_epoch)):
                return
            active = old.start_seen and not old.consumed and all(self._episode_active_read(row, old) for row in (one,two))
            ended = old.consumed and all(self._episode_terminal_read(row, old) for row in (one,two))
            terminal = old.start_seen and old.end_seen and not old.consumed and all(self._episode_terminal_read(row, old) for row in (one,two))
            if self._native_event_loss or not (active or ended or terminal):
                return
            self._native_episode = old
            old.intent_id = None
            self._native_rejected_predecessor = None
            self._load_ownership_unconfirmed = False
            if terminal:
                self._commit_native_terminal(old, two['properties'])
            elif ended:
                reason = 'error' if type(old.error) is int and old.error < 0 else {0:'eof',2:'stopped',3:'stopped',4:'error'}.get(old.reason)
                self._native_idle_state(two['properties'],
                    {'eof':PlayerStatus.ENDED,'stopped':PlayerStatus.STOPPED,'error':PlayerStatus.ERROR}.get(reason,PlayerStatus.IDLE),
                    SessionStatus.CLOSED if reason else SessionStatus.ACTIVE)
            else:
                old.confirmed = True
                self._playback_stop_fenced = False
                self._pending_stopped_load = None
                self._verified_stopped_load_identity = None
                self._stopped_load_path_disputed = False
                self._apply_native_active(two['properties'])
            self._emit_event('native.command-rejected', {'state':self._state.as_dict(),
                             'operation':operation[0], 'predecessor_verified':True})

    def _apply_native_active(self, properties):
        self._applying_native_proof = True
        try:
            lifecycle = ('path', 'idle-active', 'eof-reached', 'seeking', 'pause', 'duration', 'time-pos')
            for name in lifecycle:
                self._on_property(name, properties.get(name))
            # Pre-confirmation callbacks are fenced because they may belong to
            # the previous load. Rehydrate the fixed metadata/output contract
            # from the same entry-bound native proof after its lifecycle/path
            # has been applied; unchanged MPV properties may never notify again.
            for name in OBSERVED_PROPERTY_ATTRIBUTES:
                if name not in lifecycle and name in properties:
                    self._on_property(name, properties[name])
        finally:
            self._applying_native_proof = False

    def _native_property_is_fenced(self, name, value):
        if not self._native_event_contract or self._applying_native_proof:
            return False
        ep = self._native_episode
        pending = (self._native_operation is not None or self._load_ownership_unconfirmed
                   or ep is not None and not ep.confirmed and not ep.cancelled and not ep.consumed)
        if pending and name in OBSERVED_PROPERTY_ATTRIBUTES and name not in {
                'path', 'time-pos', 'idle-active', 'eof-reached', 'seeking', 'core-idle'}:
            # Callbacks carry no native entry owner. Invalidate the copied
            # metadata proof without storing their values or reading MPV here.
            # The next control poll obtains a fresh entry-bound pair instead.
            self._native_metadata_revision += 1
            self._native_event_revision += 1
        if self._native_operation is not None or self._load_ownership_unconfirmed:
            if self._load_ownership_unconfirmed and name in {'path','idle-active','eof-reached','seeking','time-pos'}:
                self._native_event_revision += 1
            return name not in {'pause', 'volume', 'speed'}
        if ep is None or ep.cancelled:
            return name in {'path', 'time-pos', 'duration', 'idle-active', 'eof-reached'}
        if not ep.confirmed or ep.consumed:
            return name not in {'pause', 'volume', 'speed'}
        # Untagged old callbacks cannot close a current episode. Actual native
        # end cause is reconciled separately, including double-null properties.
        fenced = ((name == 'path' and self._load_media_identity(value) != ep.identity)
                or name == 'eof-reached' and value is True
                or name == 'idle-active' and value is True)
        if fenced:
            ep.terminal_signal = True
            self._native_event_revision += 1
        return fenced

    def _complete_native_episode(self):
        if not self._native_event_contract:
            return
        self._recover_rejected_native_episode()
        with self._event_lock:
            if self._native_operation is not None or self._load_ownership_unconfirmed:
                return
            ep = self._native_episode
            while self._native_events:
                kind, identity, reason, error = self._native_events.popleft()
                # Identity was bound from the accepted native playlist, not
                # from whichever command happens to own callback arrival.
                if ep is None or ep.cancelled or identity != ep.playlist_entry_id:
                    continue
                if kind == 'start-file':
                    ep.start_seen = True
                elif not ep.end_seen:
                    ep.end_seen, ep.reason, ep.error = True, reason, error
            if (ep is None or ep.cancelled or ep.consumed or not ep.accepted
                    or not ep.start_seen or not entry_id(ep.playlist_entry_id)
                    or ep.identity is None or self._native_event_loss
                    or ep.session_id != self.session_id):
                return
            if ep.confirmed and not ep.end_seen and not ep.terminal_signal:
                return  # Steady polls add no new native reads.
            if not ep.confirmed and ep.metadata_invalidations >= 8:
                return  # Unsettled metadata cannot spin native proof reads forever.
            before = (ep.serial, self._native_event_revision, self.session_id, self._display_signal_epoch)
            metadata_revision = self._native_metadata_revision
        try:
            one = self.lifecycle.adapter.episode_snapshot()
            two = self.lifecycle.adapter.episode_snapshot()
        except (RuntimeError, ValueError, AttributeError):
            return
        with self._event_lock:
            if (ep is self._native_episode and not ep.cancelled and not ep.consumed
                    and not ep.confirmed and metadata_revision != self._native_metadata_revision):
                ep.metadata_invalidations += 1
                if ep.metadata_invalidations == 8:
                    self._observer_errors.append('native metadata proof retry limit; ownership unavailable')
            if (ep is not self._native_episode or ep.cancelled or ep.consumed
                    or before != (ep.serial, self._native_event_revision, self.session_id, self._display_signal_epoch)):
                return
            if ep.replay and not ep.confirmed:
                # The retained entry ID still names the old terminal cycle.
                # An accepted replay command is not proof of a new active cycle;
                # pre-proof end-file receipts cannot close its old session or
                # consume the pending new episode. Establish active ownership
                # first, then begin its fresh session below.
                if not all(self._episode_active_read(row, ep) for row in (one, two)):
                    return
                ep.end_seen, ep.reason, ep.error = False, None, None
                ep.terminal_signal, ep.unconfirmed_idle = False, False
            if ep.end_seen:
                if not all(self._episode_terminal_read(row, ep) for row in (one, two)):
                    if ep.replay and all(self._episode_active_read(row, ep) for row in (one,two)):
                        ep.end_seen, ep.reason, ep.error = False, None, None
                    return
                self._commit_native_terminal(ep, two['properties'])
            elif ep.confirmed and ep.terminal_signal:
                if all(self._episode_terminal_read(row, ep) for row in (one, two)):
                    if not ep.unconfirmed_idle:
                        ep.unconfirmed_idle = True
                        self._native_idle_state(two['properties'], PlayerStatus.IDLE, SessionStatus.ACTIVE)
                        self._emit_event('native.terminal-pending', {
                            'state': self._state.as_dict(), 'native_episode': self._episode_receipt(ep),
                            'reason': 'native_end_cause_unavailable'})
                ep.terminal_signal = False
            elif all(self._episode_active_read(row, ep) for row in (one, two)):
                if ep.replay:
                    self._begin_playback_session(same_media_replay=True)
                    ep.session_id = self.session_id
                ep.confirmed = True
                self._playback_stop_fenced = False
                self._pending_stopped_load = None
                self._verified_stopped_load_identity = None
                self._stopped_load_path_disputed = False
                self._load_ownership_unconfirmed = False
                self._apply_native_active(two['properties'])
                self._emit_event('media.started', {'state': self._state.as_dict(),
                                                 'native_episode': self._episode_receipt(ep)})

    def _episode_active_read(self, row, ep):
        if (not isinstance(row, Mapping) or not entry_id(row.get('playlist_entry_id'))
                or row.get('playlist_entry_id') != ep.playlist_entry_id):
            return False
        p = row.get('properties', {})
        if not isinstance(p, Mapping):
            return False
        return (self._load_media_identity(p.get('path')) == ep.identity
                and p.get('idle-active') is False and p.get('seeking') is False
                and p.get('eof-reached') is False and type(p.get('pause')) is bool)

    def _episode_terminal_read(self, row, ep):
        if (not isinstance(row, Mapping) or (row.get('playlist_entry_id') is not None
                and (not entry_id(row.get('playlist_entry_id'))
                     or row.get('playlist_entry_id') != ep.playlist_entry_id))):
            return False
        p = row.get('properties', {})
        if not isinstance(p, Mapping):
            return False
        cleared = p.get('path') is None or p.get('path') == ''
        if cleared and p.get('idle-active') is True:
            # This proves quiescent unloading only. Cause comes from the
            # originating end-file receipt, never from idle or nullable flags.
            return ((p.get('seeking') is False or p.get('seeking') is None)
                    and (type(p.get('eof-reached')) is bool or p.get('eof-reached') is None))
        return (ep.reason == 0 and p.get('eof-reached') is True
                and p.get('seeking') is False
                and self._load_media_identity(p.get('path')) == ep.identity)

    def _commit_native_terminal(self, ep, snapshot):
        ep.consumed = True
        reason = {0: 'eof', 2: 'stopped', 3: 'stopped', 4: 'error'}.get(ep.reason)
        if type(ep.error) is int and ep.error < 0:
            reason = 'error'
        self._pending_position_checkpoint = None
        self._playback_session_had_media = True
        self._playback_session_ended = reason is not None
        self._verified_terminal_eof = reason == 'eof'
        self._playback_stop_fenced = reason in {'stopped', 'error'}
        self._pending_stopped_load = None
        self._verified_stopped_load_identity = None
        self._stopped_load_path_disputed = False
        self._load_ownership_unconfirmed = False
        self._position_coalescer = PositionCoalescer()
        self._invalidate_display_signal('no_current_frame')
        # The final payload also names the attributed media separately; a
        # cleared live path must not erase the immutable attempted owner.
        self._native_idle_state(snapshot,
            {'eof': PlayerStatus.ENDED, 'stopped': PlayerStatus.STOPPED,
             'error': PlayerStatus.ERROR}.get(reason, PlayerStatus.IDLE),
            SessionStatus.CLOSED if reason else SessionStatus.ACTIVE)
        self._emit_event('native.end-file', {'state': self._state.as_dict(),
            'native_episode': self._episode_receipt(ep), 'end_reason': reason,
            'native_reason': ep.reason, 'native_error': ep.error,
            'terminal_verified': True})

    def _native_idle_state(self, snapshot, status, session_status):
        values = {**{k: v for k, v in self._observed_properties.items()
                     if k not in self._native_media_fields}, 'path': snapshot.get('path'),
                  'time-pos': snapshot.get('time-pos'), 'duration': snapshot.get('duration'),
                  'idle-active': snapshot.get('idle-active'), 'eof-reached': status is PlayerStatus.ENDED}
        self._observed_properties = values
        self._output_identity_key = None
        self._media_status_resample = True
        self._state = self._state.with_properties(
            properties=PlayerProperties.from_raw({k: v for k, v in values.items()
                if k not in self._native_status_fields}),
            status=status, session_status=session_status,
            output_status=PlayerOutputStatus.from_playback_observation(values,
                renderer_library_loaded=self._liborender_mapped,
                output_identity=self._output_identity))
