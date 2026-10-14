from __future__ import annotations

import importlib
import hashlib
import math
import os
import re
import stat
import threading
import time
from collections.abc import Mapping
from enum import Enum
from typing import Any

from player_host.native_video import mapped_native_identities


class LibMpvAdapterError(RuntimeError):
    """Raised when the narrow libmpv adapter contract is violated."""


class NativeCommandRejected(LibMpvAdapterError):
    """A synchronous native command returned an explicit negative reply."""


def _native_command(operation, call):
    try:
        return call()
    except Exception as error:
        # The deployed binding retains the native numeric error as args[1].
        # A timeout, generic failure or unknown binding exception is uncertain.
        if len(error.args) > 1 and type(error.args[1]) is int and error.args[1] in {-3, -4, -12, -18, -19}:
            raise NativeCommandRejected(operation + ' rejected by native command reply') from error
        raise


class LifecycleState(str, Enum):
    NEW = "new"
    INITIALIZED = "initialized"
    SHUTDOWN = "shutdown"


PRE_INITIALIZE_OPTIONS: Mapping[str, Any] = {
    "idle": True,
    "input_default_bindings": False,
    "input_vo_keyboard": False,
}

_AUDIO_DECODER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Normalize known codec-specific libavcodec decoder names for diagnostics.
_LAVC_CODEC_DECODER_ALIASES = frozenset({"pcm_s16le", "aac"})
_DIAGNOSTIC_LOG_MAX_BYTES = 8 * 1024 * 1024

OBSERVED_PROPERTY_ATTRIBUTES: Mapping[str, str] = {
    "pause": "pause",
    "idle-active": "idle_active",
    "time-pos": "time_pos",
    "duration": "duration",
    "seekable": "seekable",
    "seeking": "seeking",
    "path": "path",
    "media-title": "media_title",
    "volume": "volume",
    "speed": "speed",
    "eof-reached": "eof_reached",
    "track-list": "track_list",
    "audio-params": "audio_params",
    "audio-out-params": "audio_out_params",
    "video-params": "video_params",
    "video-dec-params": "video_dec_params",
    "video-target-params": "video_target_params",
    "container-fps": "container_fps",
    "current-vo": "current_vo",
    "current-gpu-context": "current_gpu_context",
    "current-ao": "current_ao",
    "drm-device": "drm_device",
    "drm-connector": "drm_connector",
    "video-out-params": "video_out_params",
    "display-names": "display_names",
    "audio-device": "audio_device",
    "chapter-list": "chapter_list",
    "aid": "aid",
    "sid": "sid",
    "vid": "vid",
    "core-idle": "core_idle",
}

# New telemetry is optional on older bindings. Missing observations remain
# unknown; unlike required transport properties they must not break snapshot.
_OPTIONAL_STATUS_PROPERTIES = frozenset({
    "seeking", "video-dec-params", "video-target-params", "container-fps", "current-vo",
    "current-gpu-context", "current-ao", "drm-device", "drm-connector",
})

# One private, polling-only observation. These names never enter the public
# event observer contract, and callers cannot supply property paths.
PERFORMANCE_PROPERTY_ATTRIBUTES: Mapping[str, str] = {
    "container-fps": "container_fps",
    "estimated-vf-fps": "estimated_vf_fps",
    "display-fps": "display_fps",
    "estimated-display-fps": "estimated_display_fps",
    "frame-drop-count": "frame_drop_count",
    "decoder-frame-drop-count": "decoder_frame_drop_count",
    "mistimed-frame-count": "mistimed_frame_count",
    "avsync": "avsync",
    "total-avsync-change": "total_avsync_change",
    "pause": "pause",
    "core-idle": "core_idle",
    "time-pos": "time_pos",
    "seeking": "seeking",
    "paused-for-cache": "paused_for_cache",
    "cache-buffering-state": "cache_buffering_state",
    "path": "path",
    "framedrop": "options/framedrop",
}
PERFORMANCE_CACHE_FIELDS = ("fw-bytes", "cache-duration", "reader-pts", "eof", "underrun", "idle")
_PERFORMANCE_FLAGS = frozenset({"pause", "core-idle", "seeking", "paused-for-cache", "eof", "underrun", "idle"})
_PERFORMANCE_COUNTS = frozenset({"frame-drop-count", "decoder-frame-drop-count", "mistimed-frame-count", "fw-bytes"})


def _performance_value(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _PERFORMANCE_FLAGS:
        return value if isinstance(value, bool) else None
    if name == "path":
        return value if isinstance(value, str) and len(value) <= 4096 and "\0" not in value else None
    if name == "framedrop":
        return value if isinstance(value, str) and value in {"no", "vo", "decoder", "decoder+vo"} else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    if name in _PERFORMANCE_COUNTS:
        return value if isinstance(value, int) and 0 <= value <= 2**63 - 1 else None
    if name not in {"avsync", "total-avsync-change", "reader-pts"} and value < 0:
        return None
    if name == "cache-buffering-state" and value > 100:
        return None
    return value

# Readback is a separate, read-only host operation. Keep every mpv option key
# literal here: callers cannot turn arbitrary request text into a property
# path, and only the profile catalog's restart-required options are exposed.
RESTART_OPTION_PROPERTY_NAMES: Mapping[str, str] = {
    "video-rotate": "video-rotate",
    "deband": "deband",
    "sub-auto": "sub-auto",
    "sub-scale": "sub-scale",
    "vo": "vo",
    "ao": "ao",
    "audio-device": "audio-device",
    "audio-exclusive": "audio-exclusive",
    "audio-fallback-to-null": "audio-fallback-to-null",
    "gpu-context": "gpu-context",
    "drm-device": "drm-device",
    "drm-connector": "drm-connector",
    "drm-mode": "drm-mode",
    "hwdec": "hwdec",
    "video-sync": "video-sync",
    "drm-format": "drm-format",
    "target-colorspace-hint": "target-colorspace-hint",
    "target-colorspace-hint-mode": "target-colorspace-hint-mode",
    "target-trc": "target-trc",
    "target-prim": "target-prim",
}


class LibMpvAdapter:
    """Keep the selected ``python-mpv`` binding behind one small boundary.

    The real binding is imported lazily so non-player tests do not require a
    system libmpv installation. Tests and process-host code may inject a
    binding module with the same ``MPV(**options)`` factory.
    """

    binding_module_name = "mpv"

    def __init__(
        self,
        binding: Any | None = None,
        *,
        options: Mapping[str, Any] | None = None,
        capture_audio_decoder_diagnostics: bool = False,
        diagnostic_log_file: str | os.PathLike[str] | None = None,
    ) -> None:
        self._binding = binding
        self._options = dict(options or {})
        self._player: Any | None = None
        if not isinstance(capture_audio_decoder_diagnostics, bool):
            raise LibMpvAdapterError("audio-decoder diagnostics switch must be boolean")
        self._capture_audio_decoder_diagnostics = capture_audio_decoder_diagnostics
        if diagnostic_log_file is not None and not capture_audio_decoder_diagnostics:
            raise LibMpvAdapterError("decoder diagnostic log requires opt-in diagnostics")
        self._diagnostic_log_file = (
            os.fspath(diagnostic_log_file) if diagnostic_log_file is not None else None
        )
        if self._diagnostic_log_file is not None and (
            not os.path.isabs(self._diagnostic_log_file)
            or "\x00" in self._diagnostic_log_file
        ):
            raise LibMpvAdapterError("decoder diagnostic log path must be absolute")
        self._diagnostic_log_offset = 0
        self._diagnostic_log_partial = ""
        self._diagnostic_log_file_state = (
            "waiting" if self._diagnostic_log_file is not None else "disabled"
        )
        self._decoder_lock = threading.Lock()
        self._media_path: str | None = None
        self._selected_audio_decoder: str | None = None
        self._unrecognized_decoder_name_sha256: str | None = None
        self._audio_decoder_log_events_seen = 0
        self._decoder_selection_marker_events_seen = 0
        self._audio_decoder_prefix_events_seen = 0

    @property
    def player(self) -> Any:
        if self._player is None:
            raise LibMpvAdapterError("libmpv player has not been created")
        return self._player

    @property
    def created(self) -> bool:
        return self._player is not None

    def create(self, *, options: Mapping[str, Any] | None = None) -> Any:
        """Create one ``python-mpv`` player using typed option mappings.

        ``python-mpv`` creates and initializes its libmpv handle in ``MPV``'s
        constructor. Later lifecycle tasks add explicit process ownership and
        typed playback operations around this adapter boundary.
        """

        if self._player is not None:
            raise LibMpvAdapterError("libmpv player has already been created")
        binding = self._binding or importlib.import_module(self.binding_module_name)
        factory = getattr(binding, "MPV", None)
        if factory is None or not callable(factory):
            raise LibMpvAdapterError("selected binding does not expose callable MPV")
        constructor_options = dict(self._options)
        constructor_options.update(options or {})
        decoder_order = constructor_options.get("ad")
        if decoder_order is not None and (
            not isinstance(decoder_order, str)
            or not decoder_order
            or len(decoder_order) > 256
            or any(not _AUDIO_DECODER_NAME.fullmatch(name)
                   for name in decoder_order.split(","))
        ):
            raise LibMpvAdapterError("configured audio decoder names are invalid")
        if "log_handler" in constructor_options:
            raise LibMpvAdapterError("python-mpv log handlers are not caller-configurable")
        if self._capture_audio_decoder_diagnostics:
            constructor_options.pop("loglevel", None)
            constructor_options.pop("log_file", None)
            constructor_options.pop("log-file", None)
            if self._diagnostic_log_file is not None:
                constructor_options["log_file"] = self._diagnostic_log_file
            player = factory(
                **constructor_options,
                loglevel="v",
                log_handler=self._capture_decoder_log,
            )
        else:
            player = factory(**constructor_options)
        self._player = player
        return self._player

    def _capture_decoder_log(self, level: str, prefix: str, message: str) -> None:
        """Retain only fixed decoder-selection facts from opt-in diagnostic logs."""

        if not isinstance(message, str):
            return
        with self._decoder_lock:
            self._audio_decoder_log_events_seen += 1
            if isinstance(prefix, str) and prefix.casefold() in {"ad", "audio", "audio-decoder"}:
                self._audio_decoder_prefix_events_seen += 1
        for line in message.splitlines():
            selected: str | None = None
            marker = "Selected decoder:"
            if marker in line:
                with self._decoder_lock:
                    self._decoder_selection_marker_events_seen += 1
                token = line.split(marker, 1)[1].strip().split(maxsplit=1)[0:1]
                name = token[0].split(":", 1)[0].lower() if token else ""
                if name.startswith("orender"):
                    selected = "orender"
                elif name.startswith("lavc") or name in _LAVC_CODEC_DECODER_ALIASES:
                    selected = "lavc"
                elif _AUDIO_DECODER_NAME.fullmatch(name):
                    with self._decoder_lock:
                        self._unrecognized_decoder_name_sha256 = hashlib.sha256(
                            name.encode("ascii")
                        ).hexdigest()
            if selected is not None:
                with self._decoder_lock:
                    self._selected_audio_decoder = selected

    def _scan_diagnostic_log_file(self) -> None:
        path = self._diagnostic_log_file
        if path is None:
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return
        except OSError:
            self._diagnostic_log_file_state = "invalid"
            return
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > _DIAGNOSTIC_LOG_MAX_BYTES
                or info.st_size < self._diagnostic_log_offset
            ):
                self._diagnostic_log_file_state = "invalid"
                return
            os.lseek(descriptor, self._diagnostic_log_offset, os.SEEK_SET)
            chunk = os.read(
                descriptor,
                min(_DIAGNOSTIC_LOG_MAX_BYTES + 1, info.st_size - self._diagnostic_log_offset),
            )
            if len(chunk) > _DIAGNOSTIC_LOG_MAX_BYTES:
                self._diagnostic_log_file_state = "invalid"
                return
            self._diagnostic_log_offset += len(chunk)
            self._diagnostic_log_file_state = "active"
        except OSError:
            self._diagnostic_log_file_state = "invalid"
            return
        finally:
            os.close(descriptor)
        decoded = self._diagnostic_log_partial + chunk.decode("utf-8", errors="replace")
        lines = decoded.splitlines(keepends=True)
        self._diagnostic_log_partial = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._diagnostic_log_partial = lines.pop()
            if len(self._diagnostic_log_partial) > 4096:
                self._diagnostic_log_partial = ""
        for line in lines:
            self._capture_decoder_log("", "audio-decoder", line)

    def observe_audio_decoder(self) -> dict[str, Any]:
        """Return bounded, parameterless audio decoder facts for diagnostics."""

        self._scan_diagnostic_log_file()
        with self._decoder_lock:
            return {
                "enabled": self._capture_audio_decoder_diagnostics,
                "log_events_seen": self._audio_decoder_log_events_seen,
                "decoder_selection_marker_events_seen": self._decoder_selection_marker_events_seen,
                "audio_decoder_prefix_events_seen": self._audio_decoder_prefix_events_seen,
                "path": self._media_path,
                "selected_decoder": (
                    self._selected_audio_decoder
                    if self._capture_audio_decoder_diagnostics else None
                ),
                "diagnostic_log_file_state": self._diagnostic_log_file_state,
                "diagnostic_log_file_bytes_seen": self._diagnostic_log_offset,
                "unrecognized_decoder_name_sha256": (
                    self._unrecognized_decoder_name_sha256
                    if self._capture_audio_decoder_diagnostics else None
                ),
            }

    def shutdown(self) -> None:
        """Terminate the owned player if it exists; repeated calls are safe."""

        if self._player is None:
            return
        terminate = getattr(self._player, "terminate", None)
        if terminate is None or not callable(terminate):
            raise LibMpvAdapterError("selected binding player does not expose terminate")
        try:
            terminate()
        finally:
            self._player = None

    def load(self, path: str | os.PathLike[str]) -> Any:
        media_path = os.fspath(path)
        if not isinstance(media_path, str) or not media_path or "\x00" in media_path:
            raise LibMpvAdapterError("media path must be a non-empty string without NUL bytes")
        with self._decoder_lock:
            self._media_path = media_path
            self._audio_decoder_log_events_seen = 0
            self._decoder_selection_marker_events_seen = 0
            self._audio_decoder_prefix_events_seen = 0
            self._selected_audio_decoder = None
            self._unrecognized_decoder_name_sha256 = None
        return _native_command('load', lambda: self.player.play(media_path))

    def play(self) -> Any:
        play = getattr(self.player, "play")
        try:
            return play()
        except TypeError as error:
            # python-mpv exposes play(filename), while the control protocol's
            # play command means resume the already loaded media.
            if "required positional argument" not in str(error):
                raise
            self.player.pause = False
            return None

    def pause(self) -> None:
        self.player.pause = True

    def resume(self) -> None:
        self.player.pause = False

    def seek_absolute(self, seconds: float) -> Any:
        value = _finite_number(seconds, "absolute seek position")
        if value < 0:
            raise LibMpvAdapterError("absolute seek position must be non-negative")
        return self.player.seek(value, reference="absolute", precision="exact")

    def seek_relative(self, seconds: float) -> Any:
        value = _finite_number(seconds, "relative seek offset")
        return self.player.seek(value, reference="relative", precision="exact")

    def stop(self) -> Any:
        return _native_command('stop', self.player.stop)

    def set_volume(self, volume: float) -> None:
        value = _finite_number(volume, "volume")
        if not 0 <= value <= 100:
            raise LibMpvAdapterError("volume must be between 0 and 100")
        self.player.volume = value

    def set_speed(self, speed: float) -> None:
        value = _finite_number(speed, "speed")
        if not 0 < value <= 16:
            raise LibMpvAdapterError("speed must be greater than 0 and at most 16")
        self.player.speed = value

    def select_track(self, track_id: int, track_type: str) -> None:
        """Select one present audio, subtitle, or video track by fixed property."""

        if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id < 0:
            raise LibMpvAdapterError("track id must be a non-negative integer")
        property_by_type = {"audio": "aid", "sub": "sid", "video": "vid"}
        property_name = property_by_type.get(track_type)
        if property_name is None:
            raise LibMpvAdapterError("track type is not selectable")
        raw_tracks = getattr(self.player, "track_list", None)
        if not isinstance(raw_tracks, (list, tuple)) or not any(
            isinstance(track, Mapping)
            and track.get("id") == track_id
            and track.get("type") == track_type
            for track in raw_tracks
        ):
            raise LibMpvAdapterError("track is not present in the current media")
        setattr(self.player, property_name, track_id)

    @staticmethod
    def _fixed_media_guard(player: Any) -> dict[str, Any]:
        """Capture identity/lifecycle before facts and compare again afterward."""
        guard = {}
        for name in ("path", "aid", "sid", "vid", "idle-active", "seeking"):
            try:
                guard[name] = getattr(player, OBSERVED_PROPERTY_ATTRIBUTES[name])
            except AttributeError as error:
                if name in _OPTIONAL_STATUS_PROPERTIES:
                    guard[name] = None  # Unknown stays unknown; resampling requires False.
                else:
                    raise LibMpvAdapterError("selected binding does not expose the fixed media guard") from error
        return guard

    def snapshot(self) -> dict[str, Any]:
        """Read the fixed observed-property contract from the owned player."""

        player = self.player
        before = self._fixed_media_guard(player)
        values: dict[str, Any] = dict(before)
        for property_name, attribute_name in OBSERVED_PROPERTY_ATTRIBUTES.items():
            if property_name in before:
                continue
            try:
                values[property_name] = getattr(player, attribute_name)
            except AttributeError as error:
                if property_name in _OPTIONAL_STATUS_PROPERTIES:
                    values[property_name] = None
                    continue
                raise LibMpvAdapterError(
                    f"selected binding does not expose {property_name}"
                ) from error
        if self._fixed_media_guard(player) != before:
            raise LibMpvAdapterError("media or track changed during fixed snapshot")
        return values

    def observe_restart_options(self, option_names: list[str] | tuple[str, ...]) -> dict[str, Any]:
        """Read fixed restart options and normalize libmpv's structured values."""

        if not isinstance(option_names, (list, tuple)):
            raise LibMpvAdapterError("restart-option names must be a list")
        if len(option_names) > len(RESTART_OPTION_PROPERTY_NAMES):
            raise LibMpvAdapterError("too many restart-option names")
        if any(not isinstance(name, str) for name in option_names):
            raise LibMpvAdapterError("restart-option names must be strings")
        if len(set(option_names)) != len(option_names):
            raise LibMpvAdapterError("restart-option names must be unique")
        observed: dict[str, Any] = {}
        player = self.player
        for name in option_names:
            property_name = RESTART_OPTION_PROPERTY_NAMES.get(name)
            if property_name is None:
                raise LibMpvAdapterError("restart option is not in the readback allowlist")
            try:
                value = player[property_name]
            except Exception as error:
                raise LibMpvAdapterError("restart option could not be observed") from error
            value = _normalize_restart_option_value(name, value)
            if value is not None and not isinstance(value, (bool, int, float, str)):
                raise LibMpvAdapterError("restart option readback has an unsupported type")
            if isinstance(value, float) and not math.isfinite(value):
                raise LibMpvAdapterError("restart option readback is not finite")
            observed[name] = value
        return observed

    def observe_audio_chain(self) -> dict[str, Any]:
        """Bounded fixed GETs; no commands, option writes or background probe.

        User af settings exclude automatically inserted conversion filters.
        Values are sequential and owner-guarded, not an atomic PCM snapshot.
        Raw af params may contain private paths/URLs: keep raw receipts local
        and sanitize any exported summary; bounded copying is not redaction.
        """
        try:
            before = self.episode_snapshot()
        except Exception:
            raise LibMpvAdapterError("audio-chain observation failed") from None
        started = time.monotonic_ns()
        fixed_properties = {
            "af": "af",
            "volume": "volume", "volume-gain": "options/volume-gain",
            "mute": "mute", "speed": "speed", "pitch": "pitch",
            "replaygain": "options/replaygain",
            "replaygain-preamp": "options/replaygain-preamp",
            "replaygain-clip": "options/replaygain-clip",
            "replaygain-fallback": "options/replaygain-fallback",
            "audio-pitch-correction": "options/audio-pitch-correction",
            "audio-speed-correction": "audio-speed-correction",
            "audio-samplerate": "options/audio-samplerate",
            "audio-format": "options/audio-format",
            "audio-channels": "options/audio-channels",
            "audio-normalize-downmix": "options/audio-normalize-downmix",
            "audio-spdif": "options/audio-spdif",
            "gapless-audio": "options/gapless-audio",
            "ad": "options/ad",
            "ad-lavc-ac3drc": "options/ad-lavc-ac3drc",
            "demuxer": "demuxer",
        }
        values, unavailable = {}, {}
        for name, property_name in fixed_properties.items():
            try:
                raw = self.player[property_name]
                if name == "af" and not isinstance(raw, (list, tuple)):
                    raise ValueError("invalid native af list")
                if name in {"mute", "replaygain-clip", "audio-pitch-correction",
                            "audio-normalize-downmix"} and type(raw) is not bool:
                    raise ValueError("invalid native boolean")
                if name in {"volume", "volume-gain", "speed", "pitch",
                            "replaygain-preamp", "replaygain-fallback",
                            "audio-speed-correction", "audio-samplerate",
                            "ad-lavc-ac3drc"} and type(raw) not in (int, float):
                    raise ValueError("invalid native number")
                values[name] = _bounded_audio_chain_value(raw)
            except Exception:
                # Never substitute a default or expose exception/property text.
                unavailable[name] = "native_read_unavailable_or_invalid"
        try:
            after = self.episode_snapshot()
        except Exception:
            raise LibMpvAdapterError("audio-chain observation failed") from None
        guard_fields = ("path", "aid", "sid", "vid", "pause", "speed", "volume",
                        "seeking", "idle-active", "audio-params", "audio-out-params",
                        "current-ao", "audio-device")
        if (before["playlist_entry_id"] != after["playlist_entry_id"] or any(
                before["properties"].get(n) != after["properties"].get(n)
                for n in guard_fields)):
            raise LibMpvAdapterError("audio-chain owner or settings changed during read")
        return {"values": values, "unavailable": unavailable,
                "playlist_entry_id": after["playlist_entry_id"],
                "properties": after["properties"],
                "started_monotonic_ns": started,
                "finished_monotonic_ns": time.monotonic_ns(),
                "user_af_excludes_automatic_filters": True,
                "sequential_read_not_atomic_PCM_proof": True}

    def observe_performance(self) -> dict[str, Any]:
        """Read a small fixed set of nullable metrics without altering playback.

        Sampling times bound sequential GETs; this is not an atomic native
        snapshot. Unavailable/invalid optional values cannot fail playback.
        """
        player = self.player
        started = time.monotonic_ns()
        wall = time.time_ns()
        metrics: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        for name, attribute in PERFORMANCE_PROPERTY_ATTRIBUTES.items():
            try:
                raw = getattr(player, attribute)
                metrics[name] = _performance_value(name, raw)
                if metrics[name] is None:
                    unavailable[name] = "unavailable" if raw is None else "invalid_value"
            except Exception:
                metrics[name] = None
                unavailable[name] = "read_unavailable"
        cache = {name: None for name in PERFORMANCE_CACHE_FIELDS}
        try:
            # This node does not support slash-key GETs. The binding decodes
            # the whole map, including ranges; only fixed scalars leave here.
            raw_cache = getattr(player, "demuxer-cache-state")
            cache_reason = "unavailable" if raw_cache is None else "invalid_value"
        except Exception:
            raw_cache = None
            cache_reason = "read_unavailable"
        for name in PERFORMANCE_CACHE_FIELDS:
            if not isinstance(raw_cache, Mapping):
                unavailable["demuxer-cache-state/" + name] = cache_reason
                continue
            try:
                raw = raw_cache.get(name)
                cache[name] = _performance_value(name, raw)
                if cache[name] is None:
                    unavailable["demuxer-cache-state/" + name] = "unavailable" if raw is None else "invalid_value"
            except Exception:
                unavailable["demuxer-cache-state/" + name] = "read_unavailable"
        return {"producer_pid": os.getpid(), "sample_started_monotonic_ns": started,
                "sample_finished_monotonic_ns": time.monotonic_ns(), "sample_utc_epoch_ns": wall,
                "metrics": metrics, "cache": cache, "unavailable": unavailable}

    def observe_display_geometry(self) -> dict[str, Any]:
        """Fixed GET-only scaling and destination margins for owned video.

        Sequential samples are guarded by current media/track identity and lifecycle;
        they are not a physical TV measurement or an arbitrary property API.
        """
        player = self.player
        started = time.monotonic_ns()
        try:
            before = self._fixed_media_guard(player)
            path = before["path"]
            if (not isinstance(path, str) or not path or before["idle-active"] is not False
                    or before["seeking"] is not False or type(before["vid"]) is not int or before["vid"] <= 0):
                raise ValueError("no stable current frame")
            identities = mapped_native_identities()
            scaling = {name: getattr(player, name.replace("-", "_")) for name in (
                "keepaspect", "panscan", "video-zoom", "video-pan-x", "video-pan-y",
                "video-scale-x", "video-scale-y", "video-unscaled",
            )}
            if type(scaling["keepaspect"]) is not bool or scaling["video-unscaled"] not in {False, True, "no", "yes", "downscale-big"}:
                raise ValueError("invalid scaling observation")
            for name in ("panscan", "video-zoom", "video-pan-x", "video-pan-y", "video-scale-x", "video-scale-y"):
                value = scaling[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 100:
                    raise ValueError("invalid scaling observation")
            color_mapping = {name: getattr(player, name.replace("-", "_"))
                             for name in ("hdr-reference-white", "target-peak")}
            for value in color_mapping.values():
                if value == "auto":
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1000000:
                    raise ValueError("invalid luminance observation")
            raw = getattr(player, "osd-dimensions")
            if not isinstance(raw, Mapping):
                raise ValueError("destination margins unavailable")
            dimensions = {name: raw.get(name) for name in ("w", "h", "ml", "mr", "mt", "mb")}
            if (any(type(value) is not int or abs(value) > 65536 for value in dimensions.values())
                    or dimensions["w"] <= 0 or dimensions["h"] <= 0):
                raise ValueError("invalid destination margins")
            if self._fixed_media_guard(player) != before:
                raise ValueError("media or track changed during geometry read")
            return {**identities, "path": path, "scaling": scaling, "color_mapping": color_mapping, "osd_dimensions": dimensions,
                    "sample_started_monotonic_ns": started, "sample_finished_monotonic_ns": time.monotonic_ns()}
        except Exception as error:
            raise LibMpvAdapterError("owned-host display geometry is unavailable") from error

    def observe_drm_mode(self) -> dict[str, Any]:
        """Fixed GET-only current-host KMS timing, bound to mapped native libraries."""
        from player_host.drm_modes import DrmModeReader
        try:
            identities = mapped_native_identities()
            # Compiled type readback is separate from the configured option.
            kind = getattr(self.player, "option-info/drm-mode/type")
            if kind != "String":
                raise ValueError("native DRM mode option type is unavailable")
            report = DrmModeReader().read_modes(self.player["drm-device"], self.player["drm-connector"], owned=True)
            return {**identities, "drm_mode_type": kind, "report": report.record()}
        except Exception as error:
            raise LibMpvAdapterError("owned-host DRM timing is unavailable") from error

    def observe_native_video_capabilities(self) -> dict[str, Any]:
        """Fixed GET-only compiled choices from this host's pinned native build.

        These are compilation evidence, never renderer initialization or output
        signaling. python-mpv's mapping API addresses options, so runtime
        option-info properties use attribute GET with literal paths instead.
        """
        try:
            identities = mapped_native_identities()
            player = self.player
            choices = {
                "vo": getattr(player, "option-info/vo/choices"),
                "gpu-context": getattr(player, "option-info/gpu-context/choices"),
                "drm-format": getattr(player, "option-info/drm-format/choices"),
                "target-colorspace-hint": getattr(player, "option-info/target-colorspace-hint/choices"),
                "target-colorspace-hint-mode": getattr(player, "option-info/target-colorspace-hint-mode/choices"),
                "target-trc": getattr(player, "option-info/target-trc/choices"),
                "target-prim": getattr(player, "option-info/target-prim/choices"),
            }
            for values in choices.values():
                if (not isinstance(values, (list, tuple)) or not values or len(values) > 256
                        or any(not isinstance(value, str) or not value or len(value) > 64
                               or any(ord(character) < 32 for character in value) for value in values)):
                    raise ValueError("native option choices are malformed")
            return {**identities, "choices": {name: list(values) for name, values in choices.items()}}
        except Exception as error:
            raise LibMpvAdapterError("qualified native video capabilities are unavailable") from error

    def observe_hwdec_current(self) -> str | None:
        """Read only libmpv's fixed active-decoder property."""

        try:
            # python-mpv's mapping API addresses options/<name>, not runtime
            # properties. Attribute access maps this one fixed name to the
            # libmpv hwdec-current property.
            value = self.player.hwdec_current
        except Exception as error:
            raise LibMpvAdapterError("active decoder could not be observed") from error
        if value is not None and not isinstance(value, str):
            raise LibMpvAdapterError("active decoder readback must be a string or None")
        return value

    def observe_properties(self, callback) -> None:
        """Register callbacks for only the fixed observed-property contract."""

        player = self.player
        register = getattr(player, "property_observer", None)
        if register is None or not callable(register):
            raise LibMpvAdapterError("selected binding does not expose property_observer")
        for property_name in OBSERVED_PROPERTY_ATTRIBUTES:
            decorator = register(property_name)

            def on_change(_name, value, *, name=property_name):
                callback(name, value)

            decorator(on_change)

    def observe_native_events(self, callback) -> None:
        """Fixed start/end scalars; never retain the C event pointer."""
        register = getattr(self.player, 'event_callback', None)
        if not callable(register):
            raise LibMpvAdapterError('selected binding does not expose native events')
        for kind in ('start-file', 'end-file'):
            def observed(event, *, name=kind):
                data = event.data
                identity = getattr(data, 'playlist_entry_id', None)
                reason = getattr(data, 'reason', None) if name == 'end-file' else None
                error = getattr(data, 'error', None) if name == 'end-file' else None
                callback(name, identity, reason, error)
            register(kind)(observed)

    def loaded_playlist_entry(self, path: str) -> int | None:
        """Replacement-command result, not playback proof or array identity."""
        playlist = self.player.playlist
        if not isinstance(playlist, list) or len(playlist) != 1:
            return None
        row = playlist[0]
        if (not isinstance(row, Mapping) or row.get('filename') != path
                or type(row.get('id')) is not int or not 0 <= row['id'] < 2**63):
            return None
        return row['id']

    def episode_snapshot(self) -> dict[str, Any]:
        """Fixed playlist identity around the existing native property read."""
        def identity():
            rows = self.player.playlist
            if not isinstance(rows, list) or len(rows) > 1:
                raise LibMpvAdapterError('replacement playlist identity is ambiguous')
            if not rows:
                return None
            row = rows[0]
            value = row.get('id') if isinstance(row, Mapping) else None
            if type(value) is not int or not 0 <= value < 2**63:
                raise LibMpvAdapterError('native playlist entry is unavailable')
            return value
        before = identity()
        values = self.snapshot()
        after = identity()
        if before != after:
            raise LibMpvAdapterError('native playlist entry changed during snapshot')
        return {'playlist_entry_id': after, 'properties': values}


def _bounded_audio_chain_value(value: Any) -> Any:
    """Copy JSON-like native data with a shared node and string budget."""
    budget = [256, 32768]
    def copy(item, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 6:
            raise ValueError("audio-chain node bound exceeded")
        if item is None or type(item) is bool:
            return item
        if type(item) in (int, float):
            if not math.isfinite(item):
                raise ValueError("nonfinite native value")
            return item
        if isinstance(item, str):
            budget[1] -= len(item.encode("utf-8"))
            if len(item) > 4096 or budget[1] < 0:
                raise ValueError("native string bound exceeded")
            return item
        if isinstance(item, (list, tuple)) and len(item) <= 64:
            return [copy(v, depth + 1) for v in item]
        if isinstance(item, Mapping) and len(item) <= 64:
            result = {}
            for key, v in item.items():
                if not isinstance(key, str):
                    raise ValueError("native mapping key is invalid")
                result[copy(key, depth + 1)] = copy(v, depth + 1)
            return result
        raise ValueError("unsupported native audio-chain node")
    return copy(value)


def _normalize_restart_option_value(name: str, value: Any) -> Any:
    """Project known libmpv collection readbacks to one observed scalar.

    Output backends are reported as records; only one explicitly enabled
    backend is observable as the catalog option. List-valued renderer and
    decoder settings are observable only when they resolve to one string.
    Ambiguous or unavailable state stays ``None`` so callers report it pending.
    """

    # mpv choice_get represents the enum choices yes/no as MPV_FORMAT_FLAG.
    # Preserve the public typed enum contract without widening other readbacks.
    if name == "target-colorspace-hint" and isinstance(value, bool):
        return "yes" if value else "no"
    if name in {"vo", "ao"} and isinstance(value, (list, tuple)):
        if any(
            not isinstance(output, Mapping)
            or not isinstance(output.get("name"), str)
            or not isinstance(output.get("enabled"), bool)
            for output in value
        ):
            return None
        active = [output["name"] for output in value if output["enabled"] is True]
        return active[0] if len(active) == 1 else None
    # mpv 0.41 changed gpu-context from a string to an object settings list.
    # An empty list is the unset/default autoprobe state, read as "" by 0.37.
    # Do not equate unavailable or ambiguous contexts with that empty state.
    if name == "gpu-context" and isinstance(value, (list, tuple)):
        if not value:
            return ""
        if len(value) == 1 and isinstance(value[0], Mapping):
            context = value[0]
            if (
                set(context) == {"name", "enabled", "params"}
                and isinstance(context["name"], str)
                and context["name"]
                and context["enabled"] is True
                and context["params"] == {}
            ):
                return context["name"]
            return None
    if name in {"gpu-context", "hwdec"} and isinstance(value, (list, tuple)):
        if len(value) == 1 and isinstance(value[0], str):
            return value[0]
        return None
    return value


class PlayerLifecycle:
    """Enforce one create/initialize and idempotent shutdown per host lifetime."""

    def __init__(
        self,
        adapter: LibMpvAdapter | None = None,
        *,
        pre_initialize_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.adapter = adapter or LibMpvAdapter()
        self.pre_initialize_options = dict(
            pre_initialize_options or PRE_INITIALIZE_OPTIONS
        )
        self.state = LifecycleState.NEW

    @property
    def player(self) -> Any:
        if self.state is not LifecycleState.INITIALIZED:
            raise LibMpvAdapterError(f"player lifecycle is {self.state.value}")
        return self.adapter.player

    def initialize(self, *, options: Mapping[str, Any] | None = None) -> Any:
        if self.state is not LifecycleState.NEW:
            raise LibMpvAdapterError(
                f"player lifecycle cannot initialize from {self.state.value}"
            )
        constructor_options = dict(self.pre_initialize_options)
        constructor_options.update(options or {})
        player = self.adapter.create(options=constructor_options)
        self.state = LifecycleState.INITIALIZED
        return player

    def shutdown(self) -> None:
        if self.state is LifecycleState.SHUTDOWN:
            return
        self.adapter.shutdown()
        self.state = LifecycleState.SHUTDOWN


LibmpvAdapter = LibMpvAdapter


def _finite_number(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LibMpvAdapterError(f"{label} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise LibMpvAdapterError(f"{label} must be a finite number")
    return converted

__all__ = [
    "LifecycleState",
    "LibMpvAdapter",
    "LibmpvAdapter",
    "LibMpvAdapterError",
    "OBSERVED_PROPERTY_ATTRIBUTES",
    "PRE_INITIALIZE_OPTIONS",
    "PlayerLifecycle",
]
