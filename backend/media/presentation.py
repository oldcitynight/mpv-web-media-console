"""Bounded, evidence-backed media presentation labels.

This module is the only place that turns probe/player codec metadata into
labels shown by the browser and player views. Unknown or unsupported metadata
is deliberately represented as ``Unknown`` rather than exposing raw probe
descriptions or guessing an enhancement layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


UNKNOWN_PRESENTATION = "Unknown"

_HDR10_PRIMARIES = frozenset({"bt2020", "bt.2020", "bt_2020"})
_HDR10_TRANSFERS = frozenset({"pq", "smpte2084", "smpte st 2084"})
_HLG_TRANSFERS = frozenset({"hlg", "arib-std-b67", "arib std b67"})
_SDR_TRANSFERS = frozenset({"bt709", "bt.709", "bt 709", "bt1886", "bt.1886", "gamma2.2", "gamma2.4", "srgb", "iec61966-2-1"})
_EAC3_ATMOS_PROFILES = frozenset({
    "dolby digital plus dolby atmos",
    "e ac 3 joc",
})
_TRUEHD_ATMOS_PROFILES = frozenset({
    "dolby truehd dolby atmos",
    "truehd atmos",
})
_DTS_HD_PROFILES = frozenset({
    "dts hd",
    "dts hd hra",
})
_DTS_HD_MA_PROFILES = frozenset({
    "dts hd ma",
    "dts hd ma xll",
    "dts xll",
    "dts hd master audio",
    "dts hd ma dts x",
    "dts hd ma dts x imax",
    "xll x",
    "dts xll x",
    "dts hd ma xll x",
})
_DTS_X_PROFILES = frozenset({
    "dts x",
    "xll x",
    "dts xll x",
    "dts hd ma dts x",
    "dts hd ma dts x imax",
    "dts hd ma xll x",
})


def _fold(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(
        value.casefold()
        .replace("_", " ")
        .replace("-", " ")
        .replace(":", " ")
        .split()
    )


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if not value or len(value) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return ""
    return value


def _profile(value: Any) -> str:
    """Normalize one authoritative codec profile, ignoring player suffix facts."""

    folded = _fold(value).replace("+", " ")
    base = folded.split(" · ", 1)[0]
    return " ".join(base.split())


def _side_data_evidence(stream: Mapping[str, Any]) -> str:
    values: list[str] = []
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data[:16]:
            if isinstance(item, Mapping):
                for key in ("side_data_type", "metadata_type", "type"):
                    value = _text(item.get(key))
                    if value:
                        values.append(value)
    return " ".join(values)


def _has_marker(evidence: str, *markers: str) -> bool:
    folded = _fold(evidence)
    return any(_fold(marker) in folded for marker in markers)


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def normalize_audio_formats(stream: Mapping[str, Any]) -> tuple[str, ...]:
    """Return short audio labels backed by codec and exact profile evidence."""

    codec = _fold(stream.get("codec_name"))
    profile = _profile(stream.get("profile"))
    labels: list[str] = []

    if codec in {"eac3", "ec 3"}:
        labels.append("Dolby Digital Plus")
    elif codec in {"ac3", "ac 3"}:
        labels.append("Dolby Digital")
    elif codec in {"truehd", "mlp"}:
        labels.append("Dolby TrueHD")
    elif codec in {"dts", "dts hd", "dtshd"}:
        if profile in _DTS_HD_MA_PROFILES:
            labels.append("DTS-HD MA")
        elif profile in _DTS_HD_PROFILES:
            labels.append("DTS-HD")
        else:
            labels.append("DTS")
    elif codec in {"aac", "aac latm"}:
        labels.append("AAC")
    elif codec == "flac":
        labels.append("FLAC")
    elif codec == "ape":
        labels.append("APE")
    elif codec in {"opus"}:
        labels.append("Opus")
    elif codec in {"vorbis"}:
        labels.append("Vorbis")
    elif codec in {"mp3", "mp2"}:
        labels.append("MP3" if codec == "mp3" else "MPEG Audio")
    elif codec in {"pcm s16le", "pcm s24le", "pcm s32le", "pcm f32le", "pcm f64le"}:
        labels.append("PCM")

    # Atmos and DTS:X require an exact codec profile. Base E-AC-3, TrueHD,
    # DTS-HD, and unrelated stream tags are never upgraded implicitly.
    if codec in {"eac3", "ec 3"} and profile in _EAC3_ATMOS_PROFILES:
        labels.append("Dolby Atmos")
    if codec in {"truehd", "mlp"} and profile in _TRUEHD_ATMOS_PROFILES:
        labels.append("Dolby Atmos")
    if codec in {"dts", "dts hd", "dtshd"} and profile in _DTS_X_PROFILES:
        labels.append("DTS:X")
    return _dedupe(labels) or (UNKNOWN_PRESENTATION,)


def normalize_video_codec(stream: Mapping[str, Any]) -> str:
    codec = _fold(stream.get("codec_name"))
    return {
        "h264": "H.264",
        "avc": "H.264",
        "hevc": "HEVC",
        "h265": "HEVC",
        "av1": "AV1",
        "vp9": "VP9",
        "vp8": "VP8",
        "mpeg2video": "MPEG-2",
        "mpeg4": "MPEG-4",
        "vc1": "VC-1",
    }.get(codec, UNKNOWN_PRESENTATION)


def normalize_subtitle_codec(stream: Mapping[str, Any]) -> str:
    codec = _fold(stream.get("codec_name"))
    return {
        "subrip": "SRT",
        "srt": "SRT",
        "ass": "ASS",
        "ssa": "SSA",
        "webvtt": "WebVTT",
        "mov text": "Timed text",
        "dvd subtitle": "DVD subtitles",
        "hdmv pgs subtitle": "PGS",
    }.get(codec, UNKNOWN_PRESENTATION)


def normalize_track_codec(track_type: str, stream: Mapping[str, Any]) -> str:
    if track_type == "audio":
        return " + ".join(normalize_audio_formats(stream))
    if track_type == "video":
        return normalize_video_codec(stream)
    if track_type == "sub":
        return normalize_subtitle_codec(stream)
    return UNKNOWN_PRESENTATION


def classify_hdr(video_stream: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Classify HDR only from explicit color/evidence markers."""

    primaries = _fold(video_stream.get("color_primaries"))
    transfer = _fold(video_stream.get("color_transfer"))
    side_data_evidence = _side_data_evidence(video_stream)

    dolby_vision = (
        _has_marker(side_data_evidence, "dolby vision", "dovi configuration record")
        or _fold(video_stream.get("codec_name")) in {"dvhe", "dvh1"}
    )
    hdr10 = primaries in _HDR10_PRIMARIES and transfer in _HDR10_TRANSFERS
    # FFmpeg exposes HDR10+ as per-frame side data, not as a stream profile.
    hdr10_plus = hdr10 and _has_marker(side_data_evidence, "hdr10+", "dynamic hdr10 plus", "hdr10 plus")
    hlg = primaries in _HDR10_PRIMARIES and transfer in _HLG_TRANSFERS
    if dolby_vision:
        return "Dolby Vision", ("HDR10",) if hdr10 else ()
    if hdr10_plus:
        return "HDR10+", ("HDR10",)
    if hdr10:
        return "HDR10", ()
    if hlg:
        return "HLG", ()
    if transfer in _SDR_TRANSFERS:
        return "SDR", ()
    return UNKNOWN_PRESENTATION, ()


@dataclass(frozen=True, slots=True)
class MediaPresentation:
    hdr_format: str = UNKNOWN_PRESENTATION
    hdr_compatibility: tuple[str, ...] = ()
    audio_formats: tuple[str, ...] = ()
    video_codec: str = UNKNOWN_PRESENTATION

    def as_dict(self) -> dict[str, Any]:
        return {
            "hdr_format": self.hdr_format,
            "hdr_compatibility": list(self.hdr_compatibility),
            "audio_formats": list(self.audio_formats),
            "video_codec": self.video_codec,
        }

    @property
    def audio_label(self) -> str:
        return " + ".join(self.audio_formats) if self.audio_formats else UNKNOWN_PRESENTATION


def presentation_from_probe(probe: Mapping[str, Any]) -> MediaPresentation:
    streams = probe.get("streams") if isinstance(probe, Mapping) else None
    stream_list = [stream for stream in streams if isinstance(stream, Mapping)] if isinstance(streams, list) else []
    video = next((stream for stream in stream_list if stream.get("codec_type") == "video"), None)
    audio_streams = [stream for stream in stream_list if stream.get("codec_type") == "audio"]
    video_for_presentation = video
    frames = probe.get("frames") if isinstance(probe, Mapping) else None
    frame_side_data: list[Mapping[str, Any]] = []
    if isinstance(frames, list):
        for frame in frames[:8]:
            if not isinstance(frame, Mapping) or frame.get("media_type") not in {None, "video"}:
                continue
            side_data = frame.get("side_data_list")
            if isinstance(side_data, list):
                frame_side_data.extend(item for item in side_data if isinstance(item, Mapping))
    if video is not None and frame_side_data:
        video_for_presentation = dict(video)
        stream_side_data = video.get("side_data_list")
        merged_side_data = list(stream_side_data) if isinstance(stream_side_data, list) else []
        video_for_presentation["side_data_list"] = (merged_side_data + frame_side_data)[:16]
    hdr_format, compatibility = (
        classify_hdr(video_for_presentation) if video_for_presentation is not None else (UNKNOWN_PRESENTATION, ())
    )
    audio_labels: list[str] = []
    for stream in audio_streams[:16]:
        audio_labels.extend(normalize_audio_formats(stream))
    return MediaPresentation(
        hdr_format=hdr_format,
        hdr_compatibility=compatibility,
        audio_formats=_dedupe(audio_labels),
        video_codec=normalize_video_codec(video) if video is not None else UNKNOWN_PRESENTATION,
    )


