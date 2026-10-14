"""Bounded media artwork, frame-preview, and metadata probing.

The provider never asks the player to open media. It uses allowlist-resolved
paths, trusted local ffprobe/ffmpeg subprocesses with fixed argument vectors,
small time/output limits, and an in-memory cache keyed by the source file
identity plus stat signature. Failed/corrupt probes fall back to a deterministic
placeholder instead of leaking raw tool output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .path_policy import MAX_MEDIA_PREVIEW_BYTES
from .presentation import MediaPresentation, presentation_from_probe


MAX_MEDIA_PROBE_OUTPUT_BYTES = 512 * 1024
MAX_GENERATED_PREVIEW_BYTES = 2 * 1024 * 1024
MEDIA_PROBE_TIMEOUT_SECONDS = 2.0
MEDIA_PROBE_CONCURRENCY = 2
MEDIA_LISTING_PROBE_TIMEOUT_SECONDS = 0.35
MEDIA_LISTING_METADATA_BUDGET_SECONDS = 1.5
MEDIA_LISTING_METADATA_MAX_ITEMS = 32
MEDIA_PREVIEW_CACHE_ENTRIES = 64
MEDIA_PREVIEW_CACHE_BYTES = 16 * 1024 * 1024
MEDIA_PREVIEW_KINDS = frozenset({"embedded_art", "sibling_art", "video_frame", "placeholder", "none"})

_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
    '<rect width="640" height="360" fill="#17202a"/><path d="M80 260l110-110 78 78 74-92 218 124" '
    'fill="none" stroke="#8ea3b8" stroke-width="18" stroke-linecap="round" stroke-linejoin="round"/>'
    '<text x="320" y="322" fill="#c7d2de" font-family="sans-serif" font-size="24" '
    'text-anchor="middle">Preview unavailable</text></svg>'
).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MediaPreviewDescription:
    presentation: MediaPresentation
    preview_kind: str
    has_preview: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "presentation": self.presentation.as_dict(),
            "preview_kind": self.preview_kind,
            "has_preview": self.has_preview,
        }


@dataclass(frozen=True, slots=True)
class MediaPreviewAsset:
    body: bytes
    media_type: str
    kind: str
    presentation: MediaPresentation


@dataclass(frozen=True, slots=True)
class _FileSignature:
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    payload: Mapping[str, Any] | None
    presentation: MediaPresentation
    has_video: bool
    art_stream_index: int | None


class MediaPreviewProvider:
    """Probe and preview local files without invoking playback."""

    def __init__(
        self,
        *,
        ffprobe: str | None = None,
        ffmpeg: str | None = None,
        timeout_seconds: float = MEDIA_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.ffprobe = ffprobe or shutil.which("ffprobe")
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.timeout_seconds = max(0.1, min(float(timeout_seconds), MEDIA_PROBE_TIMEOUT_SECONDS))
        self._slots = threading.BoundedSemaphore(MEDIA_PROBE_CONCURRENCY)
        self._lock = threading.RLock()
        self._probes: OrderedDict[_FileSignature, _ProbeResult] = OrderedDict()
        self._assets: OrderedDict[_FileSignature, MediaPreviewAsset] = OrderedDict()
        self._asset_bytes = 0

    def describe(
        self,
        path: str | Path,
        sibling_art: Path | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> MediaPreviewDescription:
        signature = self._signature(path)
        probe = self._probe(signature, timeout_seconds=timeout_seconds)
        kind = self._kind_for(probe, sibling_art)
        return MediaPreviewDescription(
            presentation=probe.presentation,
            preview_kind=kind,
            has_preview=kind != "none",
        )

    def render(self, path: str | Path, sibling_art: Path | None = None) -> MediaPreviewAsset:
        signature = self._signature(path)
        with self._lock:
            cached = self._assets.get(signature)
            if cached is not None:
                self._assets.move_to_end(signature)
                return cached

        probe = self._probe(signature)
        asset = self._render_uncached(signature, probe, sibling_art)
        self._cache_asset(signature, asset)
        return asset

    def _signature(self, path: str | Path) -> _FileSignature:
        candidate = Path(path)
        metadata = candidate.stat()
        if not candidate.is_file():
            raise ValueError("media preview source must be a regular file")
        return _FileSignature(
            path=str(candidate),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            modified_ns=int(metadata.st_mtime_ns),
        )

    def _probe(self, signature: _FileSignature, *, timeout_seconds: float | None = None) -> _ProbeResult:
        with self._lock:
            cached = self._probes.get(signature)
            if cached is not None:
                self._probes.move_to_end(signature)
                return cached

        payload = self._run_probe(signature.path, timeout_seconds=timeout_seconds)
        streams = payload.get("streams") if isinstance(payload, Mapping) else None
        stream_list = [stream for stream in streams if isinstance(stream, Mapping)] if isinstance(streams, list) else []
        has_video = any(stream.get("codec_type") == "video" and not self._is_art_stream(stream) for stream in stream_list)
        art_stream_index = next(
            (
                int(stream["index"])
                for stream in stream_list
                if self._is_art_stream(stream) and isinstance(stream.get("index"), int)
            ),
            None,
        )
        probe = _ProbeResult(
            payload=payload,
            presentation=presentation_from_probe(payload or {}),
            has_video=has_video,
            art_stream_index=art_stream_index,
        )
        # A bounded listing probe may time out before a direct detail request
        # gets a chance to use the full per-item budget. Do not cache that
        # failure as authoritative; valid probe payloads remain cacheable.
        if payload is None:
            return probe
        with self._lock:
            self._drop_old_path_entries(self._probes, signature.path, signature)
            self._probes[signature] = probe
            self._probes.move_to_end(signature)
            while len(self._probes) > MEDIA_PREVIEW_CACHE_ENTRIES:
                self._probes.popitem(last=False)
        return probe

    @staticmethod
    def _is_art_stream(stream: Mapping[str, Any]) -> bool:
        codec = str(stream.get("codec_name", "")).casefold()
        disposition = stream.get("disposition")
        attached = isinstance(disposition, Mapping) and disposition.get("attached_pic") == 1
        return attached or (
            stream.get("codec_type") == "attachment"
            and codec in {"mjpeg", "jpeg", "png", "webp", "bmp"}
        )

    def _kind_for(self, probe: _ProbeResult, sibling_art: Path | None) -> str:
        if probe.art_stream_index is not None and self.ffmpeg:
            return "embedded_art"
        if self._read_image(sibling_art) is not None:
            return "sibling_art"
        if probe.has_video and self.ffmpeg:
            return "video_frame"
        return "placeholder"

    def _render_uncached(
        self,
        signature: _FileSignature,
        probe: _ProbeResult,
        sibling_art: Path | None,
    ) -> MediaPreviewAsset:
        if probe.art_stream_index is not None and self.ffmpeg:
            embedded = self._render_image_stream(signature.path, probe.art_stream_index)
            if embedded is not None:
                return MediaPreviewAsset(embedded, "image/jpeg", "embedded_art", probe.presentation)

        sibling = self._read_image(sibling_art)
        if sibling is not None:
            body, media_type = sibling
            return MediaPreviewAsset(body, media_type, "sibling_art", probe.presentation)

        if probe.has_video and self.ffmpeg:
            frame = self._render_video_frame(signature.path)
            if frame is not None:
                return MediaPreviewAsset(frame, "image/jpeg", "video_frame", probe.presentation)

        return MediaPreviewAsset(_PLACEHOLDER, "image/svg+xml", "placeholder", probe.presentation)

    def _run_probe(self, path: str, *, timeout_seconds: float | None = None) -> Mapping[str, Any] | None:
        timeout = self._bounded_timeout(timeout_seconds)
        started = time.monotonic()
        payload = self._run_ffprobe(
            [self.ffprobe or "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
            timeout,
        )
        if payload is None or not self._needs_frame_side_data_probe(payload):
            return payload

        remaining = timeout - (time.monotonic() - started)
        if remaining < 0.1:
            return payload

        # HDR10+ is frame side data in FFmpeg, so request only the first video
        # frame. This is deliberately separate from the stream probe so audio
        # streams remain available to presentation_from_probe().
        frame_payload = self._run_ffprobe(
            [
                self.ffprobe or "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=media_type,side_data_list",
                "-read_intervals",
                "%+#1",
                path,
            ],
            min(remaining, 0.5),
        )
        frames = frame_payload.get("frames") if isinstance(frame_payload, Mapping) else None
        if not isinstance(frames, list):
            return payload
        enriched = dict(payload)
        enriched["frames"] = frames[:8]
        return enriched

    @staticmethod
    def _needs_frame_side_data_probe(payload: Mapping[str, Any]) -> bool:
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return False
        video = next((stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "video"), None)
        if video is None:
            return False
        primaries = str(video.get("color_primaries", "")).casefold()
        transfer = str(video.get("color_transfer", "")).casefold()
        return "2020" in primaries and ("2084" in transfer or "pq" in transfer)

    def _run_ffprobe(self, argv: list[str], timeout: float) -> Mapping[str, Any] | None:
        if not self.ffprobe or not self._acquire_slot(timeout):
            return None
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            self._slots.release()
        if result.returncode != 0 or len(result.stdout) > MAX_MEDIA_PROBE_OUTPUT_BYTES:
            return None
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def _render_image_stream(self, path: str, stream_index: int) -> bytes | None:
        return self._render_with_ffmpeg(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                path,
                "-map",
                f"0:{stream_index}",
                "-frames:v",
                "1",
                "-vf",
                "scale=640:640:force_original_aspect_ratio=decrease",
                "-f",
                "mjpeg",
                "-q:v",
                "6",
                "pipe:1",
            ]
        )

    def _render_video_frame(self, path: str) -> bytes | None:
        return self._render_with_ffmpeg(
            [
                self.ffmpeg or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                "1",
                "-i",
                path,
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=640:360:force_original_aspect_ratio=decrease",
                "-f",
                "mjpeg",
                "-q:v",
                "6",
                "pipe:1",
            ]
        )

    def _render_with_ffmpeg(self, argv: list[str]) -> bytes | None:
        if not self.ffmpeg or not self._acquire_slot():
            return None
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            self._slots.release()
        if result.returncode != 0 or not result.stdout or len(result.stdout) > MAX_GENERATED_PREVIEW_BYTES:
            return None
        return result.stdout

    def _bounded_timeout(self, timeout_seconds: float | None) -> float:
        if timeout_seconds is None:
            return self.timeout_seconds
        return max(0.1, min(float(timeout_seconds), self.timeout_seconds))

    def _acquire_slot(self, timeout_seconds: float | None = None) -> bool:
        timeout = self.timeout_seconds if timeout_seconds is None else self._bounded_timeout(timeout_seconds)
        return self._slots.acquire(timeout=min(timeout, 0.25))

    @staticmethod
    def _read_image(path: Path | None) -> tuple[bytes, str] | None:
        if path is None:
            return None
        try:
            metadata = path.stat()
            if not path.is_file() or metadata.st_size > MAX_MEDIA_PREVIEW_BYTES:
                return None
            body = path.read_bytes()
        except OSError:
            return None
        if len(body) > MAX_MEDIA_PREVIEW_BYTES:
            return None
        media_type = MediaPreviewProvider._sniff_image_type(body)
        return (body, media_type) if media_type is not None else None

    @staticmethod
    def _sniff_image_type(body: bytes) -> str | None:
        if body.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
            return "image/webp"
        return None

    def _cache_asset(self, signature: _FileSignature, asset: MediaPreviewAsset) -> None:
        with self._lock:
            self._drop_old_path_entries(self._assets, signature.path, signature)
            previous = self._assets.pop(signature, None)
            if previous is not None:
                self._asset_bytes -= len(previous.body)
            self._assets[signature] = asset
            self._assets.move_to_end(signature)
            self._asset_bytes += len(asset.body)
            while self._assets and (
                len(self._assets) > MEDIA_PREVIEW_CACHE_ENTRIES
                or self._asset_bytes > MEDIA_PREVIEW_CACHE_BYTES
            ):
                _old_signature, old_asset = self._assets.popitem(last=False)
                self._asset_bytes -= len(old_asset.body)

    @staticmethod
    def _drop_old_path_entries(cache: OrderedDict, path: str, current: _FileSignature) -> None:
        for key in list(cache):
            if getattr(key, "path", None) == path and key != current:
                cache.pop(key, None)
