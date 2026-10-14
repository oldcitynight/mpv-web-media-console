"""Explicit, bounded diagnostics. Import and construction perform no collection."""
from __future__ import annotations

import copy
import json
import hashlib
import math
import os
import stat
import threading
import time
import uuid
from pathlib import Path

from .errors import ErrorCode, StructuredApiError

_MAX_BYTES = 1024 * 1024
_MAX_REPLAY_KEYS = 128
_PHASES = frozenset({'playing', 'no_media', 'paused', 'seeking', 'not_playing', 'phase_unknown'})
_NUMBERS = frozenset({'container-fps', 'estimated-vf-fps', 'display-fps', 'estimated-display-fps',
    'frame-drop-count', 'decoder-frame-drop-count', 'mistimed-frame-count', 'avsync',
    'total-avsync-change', 'time-pos', 'cache-buffering-state'})
_FLAGS = frozenset({'pause', 'core-idle', 'seeking', 'paused-for-cache'})
_CACHE = frozenset({'fw-bytes', 'cache-duration', 'reader-pts', 'eof', 'underrun', 'idle'})
_PROCESS = frozenset({'cpu_user_ticks', 'cpu_system_ticks', 'rss_bytes', 'rchar', 'wchar',
    'syscr', 'syscw', 'read_bytes', 'write_bytes', 'cancelled_write_bytes'})
_DISABLED = {'state': 'disabled', 'run_id': None, 'in_flight': False,
             'native_completion_unconfirmed': False, 'samples': 0}


class ProbeUnavailable(RuntimeError):
    pass


def disabled_status():
    return dict(_DISABLED)


def _numeric(value):
    if type(value) in (int, float):
        try:
            if math.isfinite(value) and abs(value) <= 2**63 - 1:
                return value
        except OverflowError:
            pass
    return None


def _safe_sample(raw, process, request_ms, cycle_ms):
    metrics = raw.get('metrics', {}); cache = raw.get('cache', {})
    if not isinstance(metrics, dict) or not isinstance(cache, dict):
        raise ProbeUnavailable('invalid_sample')
    producer_start = raw.get('sample_started_monotonic_ns')
    producer_end = raw.get('sample_finished_monotonic_ns')
    if (type(producer_start) is not int or type(producer_end) is not int
            or producer_start < 0 or producer_end < producer_start):
        raise ProbeUnavailable('invalid_producer_clock')
    client_start = raw.get('probe_client_started_monotonic_ns')
    client_end = raw.get('probe_client_finished_monotonic_ns')
    if (type(client_start) is not int or type(client_end) is not int
            or not 0 <= client_start <= producer_start <= producer_end <= client_end < 2**63):
        raise ProbeUnavailable('invalid_clock_bounds')
    epoch = raw.get('playback_epoch', {})
    if (not isinstance(epoch, dict) or type(epoch.get('generation_before')) is not int
            or epoch['generation_before'] < 0 or type(epoch.get('generation_after')) is not int
            or epoch.get('generation_after') != epoch['generation_before']):
        raise ProbeUnavailable('epoch_changed')
    owner_tag = hashlib.sha256((str(epoch.get('player_id')) + ':' + str(epoch.get('session_id'))).encode()).hexdigest()[:32]
    unavailable = raw.get('unavailable', {})
    if not isinstance(unavailable, dict):
        unavailable = {}
    allowed_keys = _NUMBERS | _FLAGS | {'demuxer-cache-state/' + k for k in _CACHE}
    safe_unavailable = {k: unavailable[k] for k in sorted(allowed_keys) if k in unavailable
                        and unavailable[k] in ('unavailable', 'invalid_value', 'read_unavailable')}
    return {
        'metrics': {k: (_numeric(metrics.get(k)) if k in _NUMBERS else
                       metrics.get(k) if type(metrics.get(k)) is bool else None)
                    for k in sorted(_NUMBERS | _FLAGS)},
        'cache': {k: (_numeric(cache.get(k)) if k not in {'eof', 'underrun', 'idle'} else
                     cache.get(k) if type(cache.get(k)) is bool else None) for k in sorted(_CACHE)},
        'process': {k: _numeric(process.get(k)) for k in sorted(_PROCESS)},
        'request_ms': request_ms, 'cycle_ms': cycle_ms,
        'producer_ms': (producer_end - producer_start) / 1_000_000,
        'playback_epoch': {'owner_tag': owner_tag, 'generation': epoch['generation_before']},
        'producer_bounds_monotonic_ns': {'started': producer_start, 'finished': producer_end},
        'client_bounds_monotonic_ns': {'started': client_start, 'finished': client_end},
        'unavailable': safe_unavailable,
    }


class OwnedProbeSource:
    """Only the service's existing child; caller input never selects a process."""
    def __init__(self, service):
        self.service = service

    @staticmethod
    def _read(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            raw = os.read(fd, 16385)
        finally:
            os.close(fd)
        if len(raw) > 16384:
            raise ProbeUnavailable('process_read_limit')
        return raw.decode('ascii', errors='strict')

    def _process(self, host):
        # /proc/stat names may contain spaces/parentheses. No command name or
        # raw proc text leaves the collector, and no directory is scanned.
        fields = self._read(Path('/proc') / str(host.pid) / 'stat').rsplit(')', 1)[1].split()
        birth = int(fields[19])
        values = {'cpu_user_ticks': int(fields[11]), 'cpu_system_ticks': int(fields[12]),
                  'rss_bytes': max(0, int(fields[21])) * os.sysconf('SC_PAGE_SIZE')}
        for line in self._read(Path('/proc') / str(host.pid) / 'io').splitlines():
            key, separator, value = line.partition(':')
            if separator and key in _PROCESS:
                values[key] = int(value.strip())
        return birth, values

    def _phase(self, host, expected=None):
        raw = host.observe_probe_phase().payload
        epoch = raw.get('epoch', {})
        if (type(raw.get('phase')) is not str or raw.get('phase') not in _PHASES or raw.get('phase') != 'playing'):
            raise ProbeUnavailable('media_not_playing')
        if (not isinstance(epoch, dict) or type(epoch.get('generation_before')) is not int
                or epoch['generation_before'] < 0 or type(raw.get('media_token')) is not str
                or len(raw['media_token']) != 64
                or (epoch.get('player_id'), epoch.get('session_id')) !=
                   (self.service._player_id, self.service._session_id)):
            raise ProbeUnavailable('phase_unavailable')
        identity = (epoch['player_id'], epoch['session_id'], epoch['generation_before'], raw['media_token'])
        if expected is not None and identity != expected:
            raise ProbeUnavailable('epoch_changed')
        return identity

    def bind(self):
        service = self.service; host = service.player_host
        if (not service._started or not host.running or host._process is None
                or not service._player_id or not service._session_id):
            raise ProbeUnavailable('owner_unavailable')
        phase = self._phase(host)  # Cached admission before process or native metric reads.
        sock = host.socket_path.stat(follow_symlinks=False)
        if not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.getuid():
            raise ProbeUnavailable('socket_owner_unavailable')
        birth, _ = self._process(host)
        return (host, host._process, host.pid, sock.st_dev, sock.st_ino, birth,
                service._player_id, service._session_id, phase)

    def check(self, owner):
        host, process, pid, dev, ino, birth, player, session, phase = owner
        s = self.service
        if (not s._started or s.player_host is not host or host._process is not process
                or not host.running or host.pid != pid
                or (s._player_id, s._session_id) != (player, session)):
            raise ProbeUnavailable('owner_changed')
        self._phase(host, phase)
        sock = host.socket_path.stat(follow_symlinks=False)
        if not stat.S_ISSOCK(sock.st_mode) or (sock.st_dev, sock.st_ino, sock.st_uid) != (dev, ino, os.getuid()):
            raise ProbeUnavailable('socket_changed')
        current_birth, values = self._process(host)
        if current_birth != birth:
            raise ProbeUnavailable('process_birth_changed')
        return values

    def _request(self, owner, permit, *, boundary):
        # Bound waiting behind ordinary lifecycle/control work. Cancellation
        # remains independent of this lock, and is rechecked before submission.
        lock = self.service._host_recovery_lock
        if not lock.acquire(timeout=0.25):
            raise ProbeUnavailable('control_busy')
        try:
            permit()
            self.check(owner)
            permit()
            return (owner[0].observe_probe_boundary(before_send=lambda: permit(submit=True)) if boundary else
                    owner[0].observe_probe_performance(before_send=lambda: permit(submit=True))).payload
        finally:
            lock.release()

    def performance(self, owner, permit):
        return self._request(owner, permit, boundary=False)

    def boundary(self, owner, permit):
        return self._request(owner, permit, boundary=True)

    def verify_epoch(self, owner, raw):
        epoch = raw.get('playback_epoch', {})
        if (not isinstance(epoch, dict) or type(raw.get('producer_pid')) is not int
                or raw.get('producer_pid') != owner[2]
                or (epoch.get('player_id'), epoch.get('session_id')) != owner[6:8]
                or type(epoch.get('generation_before')) is not int
                or epoch['generation_before'] < 0
                or type(epoch.get('generation_after')) is not int
                or epoch.get('generation_after') != epoch['generation_before']
                or epoch.get('owner_stable', True) is not True):
            raise ProbeUnavailable('epoch_changed')
        self.verify_phase(owner, raw)
        return epoch['generation_after']

    def verify_phase(self, owner, raw):
        expected = owner[8]
        for name in ('probe_phase_before', 'probe_phase_after'):
            phase = raw.get(name, {})
            epoch = phase.get('epoch', {}) if isinstance(phase, dict) else {}
            if (not isinstance(phase, dict) or not isinstance(epoch, dict)
                    or phase.get('phase') != 'playing' or phase.get('media_token') != expected[3]
                    or (epoch.get('player_id'), epoch.get('session_id'), epoch.get('generation_before')) != expected[:3]):
                raise ProbeUnavailable('media_not_playing')
        metrics = raw.get('metrics', {})
        path = metrics.get('path') if isinstance(metrics, dict) else None
        if (type(path) is not str or not path or hashlib.sha256(path.encode()).hexdigest() != expected[3]
                or metrics.get('pause') is not False or metrics.get('seeking') is not False
                or metrics.get('core-idle') is not False):
            raise ProbeUnavailable('media_not_playing')


class PerformanceProbeManager:
    """One active run and latest terminal result, memory only, explicit start."""
    def __init__(self, source, *, clock=time.monotonic, thread_factory=threading.Thread):
        self.source = source; self._clock = clock; self._thread_factory = thread_factory
        self._lock = threading.RLock(); self._run = None; self._closed = False
        self._seen_keys = {}

    def _status(self):
        if self._run is None:
            return disabled_status()
        r = self._run
        return {'state': r['state'], 'run_id': r['id'], 'duration_s': r['duration'],
                'in_flight': r['in_flight'], 'native_completion_unconfirmed': r['unconfirmed'],
                'samples': len(r['samples']), 'native_requests_submitted': r['requests'],
                'cache_node_reads_submitted': r['cache_reads'], 'hwdec_reads_submitted': r['hwdec_reads'],
                'reason': r['reason'], 'worker_busy': self._worker_busy(r)}

    @staticmethod
    def _worker_busy(r):
        thread = r.get('thread')
        return r['worker_busy'] or bool(thread and callable(getattr(thread, 'is_alive', None)) and thread.is_alive())

    def status(self):
        with self._lock:
            return self._status()

    def result(self):
        with self._lock:
            r = self._run
            return {'status': self._status(), 'samples': copy.deepcopy(r['samples']) if r else [],
                    'hwdec_current': r['hwdec'] if r else None}

    def start(self, duration=30, request_key=None):
        if type(duration) is not int or not 5 <= duration <= 60:
            raise StructuredApiError(ErrorCode.INVALID_REQUEST, 'Probe duration must be 5–60 seconds.')
        if request_key is not None and (type(request_key) is not str or not 1 <= len(request_key) <= 256):
            raise StructuredApiError(ErrorCode.INVALID_REQUEST, 'Invalid probe request key.')
        with self._lock:
            if self._closed:
                raise StructuredApiError(ErrorCode.PLAYER_UNAVAILABLE, 'Probe manager is closed.')
            key = hashlib.sha256(request_key.encode()).digest() if request_key is not None else None
            if self._run and key is not None and self._run['key'] == key:
                if self._run['duration'] != duration:
                    raise StructuredApiError(ErrorCode.CONFLICT, 'Probe request key conflicts with its previous duration.')
                return self._status()
            if key is not None and key in self._seen_keys:
                raise StructuredApiError(ErrorCode.CONFLICT, 'Probe request is completed and no longer retained; replay rejected.')
            if key is not None and len(self._seen_keys) >= _MAX_REPLAY_KEYS:
                raise StructuredApiError(ErrorCode.CONFLICT, 'Probe replay capacity is exhausted for this service lifetime.')
            if self._run and (self._worker_busy(self._run) or self._run['state'] == 'running' or self._run['in_flight'] or self._run['unconfirmed']):
                raise StructuredApiError(ErrorCode.CONFLICT, 'A probe is active or native completion is unconfirmed.')
            r = {'id': uuid.uuid4().hex, 'key': key, 'duration': duration,
                 'state': 'running', 'reason': None, 'in_flight': False, 'unconfirmed': False,
                 'cancel': threading.Event(), 'samples': [], 'requests': 0, 'cache_reads': 0,
                 'hwdec_reads': 0, 'hwdec': None, 'started': self._clock(), 'worker_busy': True}
            r['deadline'] = r['started'] + duration
            self._run = r
            if key is not None:
                self._seen_keys[key] = r['id']
            thread = self._thread_factory(target=self._collect, args=(r,), name='mpv-performance-probe', daemon=True)
            r['thread'] = thread
            try:
                thread.start()
            except Exception:
                r['state'] = 'failed'; r['reason'] = 'worker_start_failed'; r['worker_busy'] = False
                raise
            return self._status()

    def cancel(self, run_id):
        with self._lock:
            r = self._run
            if not r or run_id != r['id']:
                raise StructuredApiError(ErrorCode.NOT_FOUND, 'Probe run is not retained.')
            if r['state'] == 'running':
                r['state'] = 'cancelled'; r['reason'] = 'explicit_cancel'; r['cancel'].set()
            return self._status()

    def close(self):
        # Never wait on or kill the player/native GET during service teardown.
        with self._lock:
            self._closed = True
            if self._run and self._run['state'] == 'running':
                self._run['state'] = 'cancelled'; self._run['reason'] = 'service_shutdown'
                self._run['cancel'].set()

    def _read(self, r, owner, kind):
        submitted = False
        def permit(*, submit=False):
            nonlocal submitted
            with self._lock:
                if r['cancel'].is_set():
                    raise ProbeUnavailable('cancelled')
                if self._clock() >= r['deadline']:
                    raise ProbeUnavailable('deadline')
                if submit:
                    submitted = True
                    r['in_flight'] = True; r['requests'] += 1; r['cache_reads'] += 1
                    if kind == 'boundary':
                        r['hwdec_reads'] += 1
        try:
            permit()
            return (self.source.performance(owner, permit) if kind == 'performance'
                    else self.source.boundary(owner, permit))
        except Exception as error:
            if submitted and getattr(error, 'submitted', True):
                # A failed transport cannot prove the submitted native GET finished.
                with self._lock:
                    r['unconfirmed'] = True
            raise
        finally:
            if submitted:
                with self._lock:
                    r['in_flight'] = False

    def _collect(self, r):
        try:
            self._collect_run(r)
        finally:
            with self._lock:
                r['worker_busy'] = False

    def _collect_run(self, r):
        with self._lock:
            if r['cancel'].is_set():
                return
            if self._clock() >= r['deadline']:
                r['state'] = 'completed'
                return
        start = r['started']; deadline = r['deadline']; due = start
        bad_cycles = 0; baseline_epoch = None
        try:
            owner = self.source.bind()
            while not r['cancel'].is_set():
                now = self._clock()
                if now >= deadline or len(r['samples']) >= 61:
                    break
                if r['cancel'].wait(max(0., due - now)):
                    break
                cycle_start = self._clock()
                if cycle_start >= deadline:
                    break
                self.source.check(owner)
                request_start = self._clock()
                raw = self._read(r, owner, 'boundary' if not r['samples'] else 'performance')
                request_ms = (self._clock() - request_start) * 1000
                if r['cancel'].is_set():
                    break
                epoch = self.source.verify_epoch(owner, raw)
                if baseline_epoch is not None and epoch != baseline_epoch:
                    raise ProbeUnavailable('epoch_changed')
                baseline_epoch = epoch
                if not r['samples']:
                    hwdec = raw.get('hwdec_current')
                    # Fixed enum only. Binding strings, paths or arbitrary native
                    # text are not accepted as diagnostic results.
                    if r['cancel'].is_set():
                        break
                    r['hwdec'] = hwdec if type(hwdec) is str and hwdec in {'no', 'vaapi', 'vaapi-copy', 'nvdec', 'nvdec-copy',
                        'vdpau', 'vdpau-copy', 'videotoolbox', 'videotoolbox-copy', 'd3d11va', 'd3d11va-copy',
                        'drm', 'drm-copy', 'mediacodec', 'mediacodec-copy', 'vulkan'} else None
                process = self.source.check(owner)
                cycle_ms = (self._clock() - cycle_start) * 1000
                if self._clock() > deadline:
                    break
                sample = _safe_sample(raw, process, request_ms, cycle_ms)
                sample['offset_s'] = cycle_start - start
                with self._lock:
                    if r['cancel'].is_set():
                        break
                    # Cap the complete retained result, including envelope reserve.
                    if len(json.dumps(r['samples'] + [sample], allow_nan=False).encode()) > _MAX_BYTES - 4096:
                        raise ProbeUnavailable('result_byte_limit')
                    sample['cycle_ms'] = (self._clock() - cycle_start) * 1000
                    r['samples'].append(sample)
                bad = request_ms > 50 or sample['producer_ms'] > 10 or sample['cycle_ms'] > 100
                bad_cycles = bad_cycles + 1 if bad else 0
                if bad_cycles >= 2:
                    raise ProbeUnavailable('latency_budget')
                # Minimum1s between sample starts; missed ticks are skipped,
                # never compressed into catch-up bursts.
                due = max(due + 1., cycle_start + 1., self._clock())
            if not r['cancel'].is_set():
                self.source.check(owner)  # Ownership, phase and epoch through completion.
            with self._lock:
                if r['state'] == 'running':
                    r['state'] = 'completed'
        except Exception as error:
            with self._lock:
                if r['state'] == 'running':
                    if isinstance(error, ProbeUnavailable) and str(error) == 'deadline':
                        r['state'] = 'completed'
                        return
                    r['state'] = 'failed'
                    r['reason'] = str(error) if isinstance(error, ProbeUnavailable) and str(error) in {
                        'owner_unavailable', 'socket_owner_unavailable', 'process_read_limit', 'owner_changed',
                        'socket_changed', 'process_birth_changed', 'invalid_sample', 'invalid_producer_clock',
                        'epoch_changed', 'result_byte_limit', 'latency_budget', 'control_busy',
                        'media_not_playing', 'phase_unavailable', 'invalid_clock_bounds'} else 'collection_failed'
                    r['error_type'] = type(error).__name__  # private; no message/trace/path retained
