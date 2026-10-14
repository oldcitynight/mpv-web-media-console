from __future__ import annotations

from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthResponse(_StrictModel):
    status: str
    service: str = "mpv-webd"


class CommandResponse(_StrictModel):
    accepted: bool = True
    request_id: str | None = None


class LoadRequest(_StrictModel):
    path: str = Field(min_length=1)
    request_key: str | None = Field(default=None, min_length=1)


class SeekRequest(_StrictModel):
    seconds: float = Field(
        description=(
            "Finite seek value: non-negative target when relative is false, "
            "signed offset when relative is true."
        )
    )
    relative: bool = False
    request_key: str | None = Field(default=None, min_length=1)

    @field_validator("seconds")
    @classmethod
    def finite_seconds(cls, value: float) -> float:
        if not isfinite(value):
            raise ValueError("seek seconds must be finite")
        return value

    @model_validator(mode="after")
    def nonnegative_absolute_target(self) -> "SeekRequest":
        if not self.relative and self.seconds < 0:
            raise ValueError("absolute seek position must be non-negative")
        return self


class ValueRequest(_StrictModel):
    value: float
    request_key: str | None = Field(default=None, min_length=1)


class PlayerStateResponse(_StrictModel):
    player_id: str
    session_id: str | None = None
    status: str
    position: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, ge=0)
    seekable: bool | None = None
    path: str | None = None
    volume: float | None = Field(default=None, ge=0, le=100)
    speed: float | None = Field(default=None, gt=0)
    pause: bool = False


class MediaPresentation(_StrictModel):
    """Short, shared labels safe for every visible media-information surface."""

    hdr_format: str = "Unknown"
    hdr_compatibility: list[str] = Field(default_factory=list)
    audio_formats: list[str] = Field(default_factory=list)
    video_codec: str = "Unknown"


class MediaBrowserItem(_StrictModel):
    name: str
    path: str
    is_directory: bool
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: str | None = None
    media_kind: Literal["directory", "audio", "video"] = "directory"
    has_preview: bool = False
    preview_kind: Literal["embedded_art", "sibling_art", "video_frame", "placeholder", "none"] = "none"
    presentation: MediaPresentation = Field(default_factory=MediaPresentation)


class MediaMetadataResponse(_StrictModel):
    path: str
    preview_kind: Literal["embedded_art", "sibling_art", "video_frame", "placeholder", "none"] = "none"
    has_preview: bool = True
    presentation: MediaPresentation = Field(default_factory=MediaPresentation)


class MediaBrowserResponse(_StrictModel):
    directory: str | None = None
    parent_directory: str | None = None
    items: list[MediaBrowserItem] = Field(default_factory=list)
    truncated: bool = False
    total: int = 0
    total_files: int = 0
    offset: int = 0
    next_offset: int | None = None
    version: str = ""


class Track(_StrictModel):
    id: int
    type: str
    title: str | None = None
    language: str | None = None
    codec: str | None = None
    selected: bool = False


class Chapter(_StrictModel):
    id: int
    title: str | None = None
    time: float = Field(ge=0)


class QueueItem(_StrictModel):
    id: str
    path: str
    ordinal: int = Field(ge=0)
    active: bool = False


class QueuePlaybackStatus(_StrictModel):
    intent_id: int = Field(gt=0)
    item_id: str | None
    phase: Literal['prepared','submitted','confirmed','failed','uncertain','superseded','removed','cancelled']
    detail: str | None


class QueueResponse(_StrictModel):
    id: str
    name: str
    revision: int = Field(ge=0)
    items: list[QueueItem] = Field(default_factory=list)
    playback: QueuePlaybackStatus | None = None


class QueueAddRequest(_StrictModel):
    path: str = Field(min_length=1)
    ordinal: int | None = Field(default=None, ge=0)
    expected_revision: int | None = Field(default=None, ge=0)


class QueueRevisionRequest(_StrictModel):
    expected_revision: int | None = Field(default=None, ge=0)


class QueueMoveRequest(QueueRevisionRequest):
    ordinal: int = Field(ge=0)


class TrackSelectRequest(_StrictModel):
    type: Literal["audio", "sub", "video"]
    request_key: str | None = Field(default=None, min_length=1)


class OutputDevice(_StrictModel):
    id: str
    name: str | None = None
    method: Literal["ALSA", "PipeWire", "DRM"]
    route: str
    kind: Literal["audio_device", "audio_sink", "display_connector"]
    profile_ids: list[str] = Field(default_factory=list)


class OutputInventoryResponse(_StrictModel):
    devices: list[OutputDevice]
    unavailable: list[str] = Field(default_factory=list)


class Profile(_StrictModel):
    id: str
    name: str
    category: str
    enabled: bool = True
    kind: Literal["profile", "backend", "device", "no_output", "decoder", "render_context"] = "profile"
    route: str | None = None
    device_reported: bool | None = None


class ProfileApplyRequest(_StrictModel):
    """A profile-ID selection plus JSON scalar overrides, never raw mpv input."""

    profile_ids: list[StrictStr] = Field(
        min_length=1,
        max_length=5,
        json_schema_extra={"uniqueItems": True},
    )
    overrides: dict[StrictStr, Any] = Field(default_factory=dict, max_length=32)
    request_key: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    force_restart: StrictBool = Field(
        default=False,
        description="Explicitly rebuild the managed host while idle, preserving its live options and snapshot.",
    )

    @field_validator("profile_ids")
    @classmethod
    def _validate_profile_ids(cls, values: list[str]) -> list[str]:
        if any(not value or value.strip() != value for value in values):
            raise ValueError("profile IDs must be non-empty and contain no surrounding whitespace")
        if len(set(values)) != len(values):
            raise ValueError("profile IDs must be unique")
        return values


class ProfileApplyConflict(_StrictModel):
    option: str
    previous_profile_id: str
    winning_profile_id: str
    previous_category: str
    winning_category: str
    previous_value: Any
    winning_value: Any


class ProfileApplyResponse(_StrictModel):
    """Effective options, observed runtime values, and restart-pending settings."""

    accepted: bool = True
    applied: bool = False
    profile_ids: list[str]
    effective_options: dict[str, Any]
    sources: dict[str, str]
    conflicts: list[ProfileApplyConflict]
    observed_options: dict[str, Any]
    pending_restart_options: dict[str, Any]


def model_dump(value: BaseModel) -> dict[str, Any]:
    """Support both Pydantic v1 and v2 without leaking version details."""

    dump = getattr(value, "model_dump", None)
    return dump() if dump is not None else value.dict()


class PerformanceProbeStartRequest(_StrictModel):
    duration_s: StrictInt = Field(default=30, ge=5, le=60)
    request_key: StrictStr | None = Field(default=None, min_length=1, max_length=256)


class PerformanceProbeCancelRequest(_StrictModel):
    run_id: StrictStr = Field(min_length=32, max_length=32, pattern="^[0-9a-f]{32}$")
