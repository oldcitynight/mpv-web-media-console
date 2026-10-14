from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from backend.media.presentation import classify_hdr, normalize_audio_formats


class PlayerStatus(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"
    ENDED = "ended"
    ERROR = "error"


class SessionStatus(str, Enum):
    NEW = "new"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


def _optional_float(value: Any, name: str, *, non_negative: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number or None")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    if non_negative and converted < 0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer or None")
    return value


def _copy_sequence(value: Any, name: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        raise TypeError(f"{name} must be a sequence")
    try:
        values = tuple(value)
    except TypeError as error:
        raise TypeError(f"{name} must be a sequence") from error
    return tuple(dict(item) if isinstance(item, Mapping) else item for item in values)


def _optional_status_label(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")
    if not value or value.strip() != value or len(value) > 120:
        raise ValueError(f"{name} must be a non-empty bounded label or None")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


_SINK_EVIDENCE = {
    "display_sink_evidence": frozenset({"drm-active-connector-edid", "mpv-display-name-edid"}),
    "audio_sink_evidence": frozenset({"alsa-owned-pcm", "alsa-managed-policy"}),
}
_FRAME_RATE_BASES = frozenset({"container", "selected_track", "video_params"})


DISPLAY_SIGNAL_STATES = frozenset({"unknown", "sdr", "hdr10", "hlg"})
DISPLAY_SIGNAL_REASONS = frozenset({
    "not_observed", "no_current_frame", "unsupported_renderer", "unbound_route",
    "permission_denied", "unavailable", "invalid_readback", "connector_disconnected",
    "inactive_scanout", "missing_connector_properties", "malformed_metadata",
    "unknown_scanout_format", "conflicting_evidence", "stale_observation",
    "epoch_invalidated", "connector_changed", "verified_hdr10", "verified_hlg", "verified_sdr",
})
SCANOUT_FORMAT_DEPTHS = {
    "XR24": 8, "AR24": 8, "XB24": 8, "AB24": 8,
    "XR30": 10, "AR30": 10, "XB30": 10, "AB30": 10,
}
DISPLAY_COLORSPACES = frozenset({
    "Default", "SMPTE_170M_YCC", "BT709_YCC", "XVYCC_601", "XVYCC_709", "SYCC_601",
    "opYCC_601", "opRGB", "BT2020_CYCC", "BT2020_RGB", "BT2020_YCC",
    "DCI-P3_RGB_D65", "DCI-P3_RGB_Theater", "RGB_WIDE_FIXED", "RGB_WIDE_FLOAT", "BT601_YCC",
})
DISPLAY_SIGNAL_FIELDS = (
    "display_signal_state", "display_signal_reason", "display_signal_evidence",
    "scanout_format", "scanout_bit_depth", "connector_colorspace", "connector_hdr_eotf",
)


def _selected_track(properties: Mapping[str, Any], kind: str) -> Mapping[str, Any] | None:
    tracks = properties.get("track-list")
    selected = [track for track in tracks if isinstance(track, Mapping)
                and track.get("type") == kind and track.get("selected") is True
                and track.get("albumart") is not True] if isinstance(tracks, (list, tuple)) else []
    return selected[0] if len(selected) == 1 else None


def _positive_rate(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    return None


def _video_dynamic_range(value: Any, *, track: Mapping[str, Any] | None = None,
                         source: bool = True) -> str | None:
    """Use the same evidence-backed HDR labels as browser metadata."""

    if not isinstance(value, Mapping):
        if not source or track is None:
            return None
        value = {}
    evidence = dict(value)
    evidence["color_primaries"] = value.get("primaries", value.get("color_primaries"))
    evidence["color_transfer"] = value.get("gamma", value.get("transfer", value.get("color_transfer")))
    if source:
        # mpv v0.41 reports the demuxed Dolby Vision configuration on the
        # selected track. A decoded PQ base layer must not erase that fact.
        dv_profile = track.get("dolby-vision-profile") if track else None
        if isinstance(dv_profile, int) and not isinstance(dv_profile, bool) and 1 <= dv_profile <= 10:
            evidence["side_data_list"] = [{"side_data_type": "DOVI configuration record"}]
        elif track and track.get("codec") in {"dvhe", "dvh1"}:
            evidence["codec_name"] = track["codec"]
        else:
            # The native property only exposes these three fields when
            # libplacebo identifies HDR10+ metadata (mpv v0.41 command.c).
            scene = [value.get(f"scene-max-{component}") for component in "rgb"]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(float(v)) and v >= 0 for v in scene) and any(v > 0 for v in scene):
                side_data = evidence.get("side_data_list")
                evidence["side_data_list"] = ([{"side_data_type": "HDR10+ metadata"}]
                    + (side_data[:15] if isinstance(side_data, list) else []))
    else:
        # Target primaries/transfer do not prove dynamic HDMI signaling.
        evidence = {key: evidence[key] for key in ("color_primaries", "color_transfer")}
    hdr_format, _compatibility = classify_hdr(evidence)
    return hdr_format


def _optional_output_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 120:
        return None
    if value.strip() != value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _display_names(value: Any) -> str | None:
    if isinstance(value, str):
        return _optional_output_label(value)
    if not isinstance(value, (list, tuple)) or not value or len(value) > 4:
        return None
    names = [_optional_output_label(item) for item in value]
    if any(name is None for name in names) or len(set(names)) != len(names):
        return None
    combined = ", ".join(name for name in names if name is not None)
    return combined if len(combined) <= 120 else None


@dataclass(frozen=True, slots=True)
class PlayerProperties:
    """Normalized player properties independent of libmpv naming/types."""

    pause: bool = False
    idle_active: bool = True
    time_pos: float | None = None
    duration: float | None = None
    seekable: bool | None = None
    path: str | None = None
    media_title: str | None = None
    volume: float | None = None
    speed: float | None = None
    eof_reached: bool = False
    track_list: tuple[Any, ...] = ()
    chapter_list: tuple[Any, ...] = ()
    aid: int | None = None
    sid: int | None = None
    vid: int | None = None
    core_idle: bool = True

    def __post_init__(self) -> None:
        if self.seekable is not None and not isinstance(self.seekable,bool):
            raise TypeError("seekable must be boolean or None")
        for name in ("pause", "idle_active", "eof_reached", "core_idle"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        for name in ("path", "media_title"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        for name in ("time_pos", "duration"):
            value = _optional_float(getattr(self, name), name, non_negative=True)
            object.__setattr__(self, name, value)
        for name in ("volume", "speed"):
            value = _optional_float(getattr(self, name), name)
            object.__setattr__(self, name, value)
        for name in ("aid", "sid", "vid"):
            object.__setattr__(self, name, _optional_int(getattr(self, name), name))
        object.__setattr__(self, "track_list", _copy_sequence(self.track_list, "track_list"))
        object.__setattr__(self, "chapter_list", _copy_sequence(self.chapter_list, "chapter_list"))

    @classmethod
    def from_raw(cls, values: Mapping[str, Any]) -> "PlayerProperties":
        """Normalize dashed libmpv property names into domain field names."""

        names = {
            "idle-active": "idle_active",
            "time-pos": "time_pos",
            "media-title": "media_title",
            "eof-reached": "eof_reached",
            "track-list": "track_list",
            "chapter-list": "chapter_list",
            "core-idle": "core_idle",
        }
        normalized = {names.get(key, key): value for key, value in values.items()}
        allowed = {field_name for field_name in cls.__dataclass_fields__}
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise ValueError(f"unknown player properties: {', '.join(unknown)}")
        return cls(**normalized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pause": self.pause,
            "idle_active": self.idle_active,
            "time_pos": self.time_pos,
            "duration": self.duration,
            "seekable": self.seekable,
            "path": self.path,
            "media_title": self.media_title,
            "volume": self.volume,
            "speed": self.speed,
            "eof_reached": self.eof_reached,
            "track_list": list(self.track_list),
            "chapter_list": list(self.chapter_list),
            "aid": self.aid,
            "sid": self.sid,
            "vid": self.vid,
            "core_idle": self.core_idle,
        }


@dataclass(frozen=True, slots=True)
class PlayerOutputStatus:
    """Fixed selected-media and observed output facts."""

    source_dynamic_range: str | None = None
    source_width: int | None = None
    source_height: int | None = None
    source_frame_rate: float | None = None
    source_frame_rate_basis: str | None = None
    source_audio_format: str | None = None
    target_dynamic_range: str | None = None
    display_signal_state: str | None = None
    display_signal_reason: str | None = None
    display_signal_evidence: str | None = None
    scanout_format: str | None = None
    scanout_bit_depth: int | None = None
    connector_colorspace: str | None = None
    connector_hdr_eotf: int | None = None
    display_route: str | None = None
    audio_route: str | None = None
    display_sink_evidence: str | None = None
    audio_sink_evidence: str | None = None
    display_sink: str | None = None
    audio_sink: str | None = None
    audio_layout: str | None = None
    renderer_library_loaded: bool | None = None

    def __post_init__(self) -> None:
        for name in (
            "source_dynamic_range",
            "source_frame_rate_basis",
            "source_audio_format",
            "target_dynamic_range",
            "display_route",
            "audio_route",
            "display_sink_evidence",
            "audio_sink_evidence",
            "display_sink",
            "audio_sink",
            "audio_layout",
        ):
            object.__setattr__(self, name, _optional_status_label(getattr(self, name), name))
        if self.source_frame_rate_basis is not None and self.source_frame_rate_basis not in _FRAME_RATE_BASES:
            raise ValueError("source_frame_rate_basis must use a fixed evidence code")
        for name in ("display_sink_evidence", "audio_sink_evidence"):
            if getattr(self, name) is not None and getattr(self, name) not in _SINK_EVIDENCE[name]:
                raise ValueError(f"{name} must use a fixed evidence code")
        if self.target_dynamic_range not in {None, "Unknown", "SDR", "HDR10", "HLG"}:
            raise ValueError("target_dynamic_range cannot claim dynamic output signaling")
        for name, allowed in (
            ("display_signal_state", DISPLAY_SIGNAL_STATES),
            ("display_signal_reason", DISPLAY_SIGNAL_REASONS),
            ("display_signal_evidence", {"drm_connector_state"}),
            ("scanout_format", SCANOUT_FORMAT_DEPTHS),
            ("connector_colorspace", DISPLAY_COLORSPACES),
        ):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or value not in allowed):
                raise ValueError(f"{name} must use a fixed signal code or None")
        for name, allowed in (("scanout_bit_depth", {8, 10}), ("connector_hdr_eotf", {0, 1, 2, 3})):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value not in allowed):
                raise ValueError(f"{name} must use a fixed integer value or None")
        if self.scanout_bit_depth is not None and SCANOUT_FORMAT_DEPTHS.get(self.scanout_format) != self.scanout_bit_depth:
            raise ValueError("scanout format/depth evidence conflicts")
        signal = self.display_signal_state
        if signal in {"sdr", "hdr10", "hlg"}:
            consistent = (
                self.display_signal_reason == "verified_" + signal
                and self.display_signal_evidence == "drm_connector_state"
                and self.scanout_bit_depth is not None
                and self.target_dynamic_range == {"sdr": "SDR", "hdr10": "HDR10", "hlg": "HLG"}[signal]
            )
            if signal in {"hdr10", "hlg"}:
                consistent = consistent and self.scanout_bit_depth == 10 and self.connector_colorspace == "BT2020_RGB" and self.connector_hdr_eotf == {"hdr10": 2, "hlg": 3}[signal]
            else:
                consistent = consistent and self.connector_colorspace in {"Default", "BT709_YCC"} and self.connector_hdr_eotf in {None, 0}
            if not consistent:
                raise ValueError("display signal claim requires consistent current connector proof")
        elif self.display_signal_reason in {"verified_hdr10", "verified_hlg", "verified_sdr"} or self.display_signal_evidence is not None:
            raise ValueError("unknown display signal cannot carry positive proof")
        for name in ("source_width", "source_height"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        frame_rate = _optional_float(self.source_frame_rate, "source_frame_rate")
        if frame_rate is not None and frame_rate <= 0:
            raise ValueError("source_frame_rate must be positive or None")
        object.__setattr__(self, "source_frame_rate", frame_rate)
        if self.renderer_library_loaded is not None and type(self.renderer_library_loaded) is not bool:
            raise TypeError("renderer_library_loaded must be a boolean or None")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlayerOutputStatus":
        if not isinstance(value, Mapping):
            raise TypeError("output status must be a mapping")
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown output status fields: {', '.join(unknown)}")
        return cls(**{name: value.get(name) for name in allowed})

    @classmethod
    def from_playback_observation(
        cls,
        properties: Mapping[str, Any],
        *,
        renderer_library_loaded: bool | None = None,
        output_identity: Mapping[str, Any] | None = None,
        display_signal: Mapping[str, Any] | None = None,
    ) -> "PlayerOutputStatus | None":
        """Project standard track metadata and fresh output observations."""

        track = _selected_track(properties, "audio")
        video_track = _selected_track(properties, "video")

        codec = track.get("codec") if isinstance(track, Mapping) else None
        if not isinstance(codec, str) or not codec or len(codec) > 64 or not re.fullmatch(r"[A-Za-z0-9_.+-]+", codec):
            codec = None
        profile = track.get("codec-profile", track.get("codec_profile")) if isinstance(track, Mapping) else None
        if (
            not isinstance(profile, str)
            or not profile
            or len(profile) > 512
            or profile.strip() != profile
            or any(ord(character) < 32 or ord(character) == 127 for character in profile)
        ):
            profile = None
        normalized_audio_stream = dict(track) if isinstance(track, Mapping) else {}
        if codec is not None:
            normalized_audio_stream["codec_name"] = codec
        if profile is not None:
            normalized_audio_stream["profile"] = profile
        audio_formats = normalize_audio_formats(normalized_audio_stream) if track else ()
        output_params = properties.get("audio-out-params")
        output_layout = None
        if isinstance(output_params, Mapping):
            for key in ("channels", "channel-layout", "channel_layout"):
                candidate = output_params.get(key)
                if (
                    isinstance(candidate, str)
                    and candidate
                    and len(candidate) <= 64
                    and re.fullmatch(r"[A-Za-z0-9_.+ (),-]+", candidate)
                ):
                    output_layout = candidate
                    break

        # Decoder parameters precede user/filter overrides. Fall back only
        # for bindings which do not expose that optional observation yet.
        video_params = properties.get("video-dec-params")
        if not isinstance(video_params, Mapping) or not video_params:
            video_params = properties.get("video-params")
        source_width = source_height = None
        source_frame_rate = None
        source_frame_rate_basis = None
        if isinstance(video_params, Mapping):
            width = video_params.get("w", video_params.get("dw"))
            height = video_params.get("h", video_params.get("dh"))
            if isinstance(width, int) and not isinstance(width, bool) and width > 0:
                source_width = width
            if isinstance(height, int) and not isinstance(height, bool) and height > 0:
                source_height = height
        for basis, candidate in (
            ("container", properties.get("container-fps")),
            ("selected_track", video_track.get("demux-fps") if video_track else None),
            ("video_params", video_params.get("fps") if isinstance(video_params, Mapping) else None),
        ):
            rate = _positive_rate(candidate)
            if rate is not None:
                source_frame_rate, source_frame_rate_basis = rate, basis
                break

        identity = output_identity or {}
        display_sink = _optional_output_label(identity.get("display_sink"))
        audio_sink = _optional_output_label(identity.get("audio_sink"))
        display_route = _optional_output_label(identity.get("display_route")) or _display_names(properties.get("display-names"))
        audio_route = _optional_output_label(identity.get("audio_route")) or _optional_output_label(properties.get("audio-device"))

        source_audio_format = next((label for label in ("Dolby Atmos", "DTS:X") if label in audio_formats),
                                   audio_formats[0] if audio_formats else None)
        signal = display_signal or {}
        if set(signal) - set(DISPLAY_SIGNAL_FIELDS):
            raise ValueError("unknown display signal observation fields")
        result = cls(
            **{name: signal.get(name) for name in DISPLAY_SIGNAL_FIELDS},
            source_dynamic_range=_video_dynamic_range(video_params, track=video_track),
            source_frame_rate_basis=source_frame_rate_basis,
            source_audio_format=source_audio_format,
            target_dynamic_range=_video_dynamic_range(properties.get("video-target-params"), source=False),
            display_route=display_route,
            audio_route=audio_route,
            display_sink_evidence=identity.get("display_sink_evidence") if display_sink else None,
            audio_sink_evidence=identity.get("audio_sink_evidence") if audio_sink else None,
            source_width=source_width,
            source_height=source_height,
            source_frame_rate=source_frame_rate,
            display_sink=display_sink,
            audio_sink=audio_sink,
            audio_layout=output_layout,
            renderer_library_loaded=renderer_library_loaded,
        )
        # Preserve the empty/idle DTO boundary. An unavailable signal alone
        # does not invent an output status when no playback/output facts exist.
        if result.display_signal_state in {None, "unknown"} and all(
            getattr(result, name) is None for name in cls.__dataclass_fields__
            if name not in DISPLAY_SIGNAL_FIELDS
        ):
            return None
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_dynamic_range": self.source_dynamic_range,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "source_frame_rate": self.source_frame_rate,
            "source_frame_rate_basis": self.source_frame_rate_basis,
            "source_audio_format": self.source_audio_format,
            "target_dynamic_range": self.target_dynamic_range,
            **{name: getattr(self, name) for name in DISPLAY_SIGNAL_FIELDS},
            "display_route": self.display_route,
            "audio_route": self.audio_route,
            "display_sink_evidence": self.display_sink_evidence,
            "audio_sink_evidence": self.audio_sink_evidence,
            "display_sink": self.display_sink,
            "audio_sink": self.audio_sink,
            "audio_layout": self.audio_layout,
            "renderer_library_loaded": self.renderer_library_loaded,
        }


@dataclass(frozen=True, slots=True)
class PlayerState:
    """Canonical state for one player session."""

    player_id: str
    session_id: str
    status: PlayerStatus = PlayerStatus.IDLE
    session_status: SessionStatus = SessionStatus.NEW
    properties: PlayerProperties = field(default_factory=PlayerProperties)
    error: str | None = None
    output_status: PlayerOutputStatus | None = None

    def __post_init__(self) -> None:
        if not self.player_id or not isinstance(self.player_id, str):
            raise ValueError("player_id must be a non-empty string")
        if not self.session_id or not isinstance(self.session_id, str):
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.status, PlayerStatus):
            raise TypeError("status must be PlayerStatus")
        if not isinstance(self.session_status, SessionStatus):
            raise TypeError("session_status must be SessionStatus")
        if not isinstance(self.properties, PlayerProperties):
            raise TypeError("properties must be PlayerProperties")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string or None")
        if self.output_status is not None and not isinstance(
            self.output_status, PlayerOutputStatus
        ):
            raise TypeError("output_status must be PlayerOutputStatus or None")

    @classmethod
    def initial(cls, player_id: str, session_id: str) -> "PlayerState":
        return cls(player_id=player_id, session_id=session_id)

    def with_properties(self, **updates: Any) -> "PlayerState":
        return PlayerState(
            player_id=self.player_id,
            session_id=self.session_id,
            status=updates.pop("status", self.status),
            session_status=updates.pop("session_status", self.session_status),
            properties=updates.pop("properties", self.properties),
            error=updates.pop("error", self.error),
            output_status=updates.pop("output_status", self.output_status),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "session_status": self.session_status.value,
            "properties": self.properties.as_dict(),
            "error": self.error,
            "output_status": (
                self.output_status.as_dict() if self.output_status is not None else None
            ),
        }


PlayerSessionState = PlayerState
CanonicalPlayerState = PlayerState
