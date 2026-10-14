from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class OptionValueType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    ENUM = "enum"


class OptionScope(str, Enum):
    PLAYBACK = "playback"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    VIDEO_OUTPUT = "video_output"
    AUDIO_OUTPUT = "audio_output"


class OptionSecurityClass(str, Enum):
    PUBLIC = "public"
    DEVICE_SELECTION = "device_selection"
    RESTRICTED = "restricted"


class OptionValidationError(ValueError):
    """Raised when a named mpv option or value is outside the catalog policy."""


@dataclass(frozen=True, slots=True)
class OptionDefinition:
    name: str
    value_type: OptionValueType
    scope: OptionScope
    mutable_runtime: bool
    restart_required: bool
    security_class: OptionSecurityClass
    allowed_values: frozenset[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _OPTION_NAME.fullmatch(self.name):
            raise ValueError("option name must be a canonical mpv option identifier")
        try:
            value_type = OptionValueType(self.value_type)
            scope = OptionScope(self.scope)
            security_class = OptionSecurityClass(self.security_class)
        except (TypeError, ValueError) as error:
            raise ValueError("option metadata contains an unsupported enum value") from error
        if not isinstance(self.mutable_runtime, bool) or not isinstance(
            self.restart_required, bool
        ):
            raise ValueError("runtime mutability metadata must be boolean")
        if self.mutable_runtime and self.restart_required:
            raise ValueError("an option cannot be both runtime-mutable and restart-required")
        values = self.allowed_values
        if values is not None:
            if not isinstance(values, (set, frozenset, tuple, list)) or not values:
                raise ValueError("allowed_values must be a non-empty set or null")
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError("allowed_values must contain non-empty strings")
            values = frozenset(values)
        if value_type is OptionValueType.ENUM and values is None:
            raise ValueError("enum options require an explicit allowed_values set")
        if value_type is not OptionValueType.ENUM and values is not None:
            raise ValueError("allowed_values are supported only for enum options")
        minimum = _bound(self.minimum, "minimum")
        maximum = _bound(self.maximum, "maximum")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum cannot exceed maximum")
        if (minimum is not None or maximum is not None) and value_type not in {
            OptionValueType.INTEGER,
            OptionValueType.NUMBER,
        }:
            raise ValueError("numeric bounds require an integer or number option")
        if self.max_length is not None and (
            isinstance(self.max_length, bool)
            or not isinstance(self.max_length, int)
            or self.max_length < 1
        ):
            raise ValueError("max_length must be a positive integer or null")
        if value_type not in {OptionValueType.STRING, OptionValueType.ENUM} and self.max_length is not None:
            raise ValueError("max_length applies only to string or enum options")
        if not isinstance(self.description, str):
            raise ValueError("description must be a string")
        object.__setattr__(self, "value_type", value_type)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "security_class", security_class)
        object.__setattr__(self, "allowed_values", values)

    def validate(self, value: Any) -> bool | int | float | str:
        if self.value_type is OptionValueType.BOOLEAN:
            if not isinstance(value, bool):
                raise OptionValidationError(f"{self.name} must be a boolean")
            return value
        if self.value_type is OptionValueType.INTEGER:
            if isinstance(value, bool) or not isinstance(value, int):
                raise OptionValidationError(f"{self.name} must be an integer")
            self._validate_range(value)
            return value
        if self.value_type is OptionValueType.NUMBER:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OptionValidationError(f"{self.name} must be a number")
            numeric = float(value)
            if not math.isfinite(numeric):
                raise OptionValidationError(f"{self.name} must be finite")
            self._validate_range(numeric)
            return numeric
        if not isinstance(value, str) or not value or "\x00" in value:
            raise OptionValidationError(f"{self.name} must be a non-empty string")
        if self.max_length is not None and len(value) > self.max_length:
            raise OptionValidationError(f"{self.name} exceeds the maximum string length")
        if self.allowed_values is not None and value not in self.allowed_values:
            raise OptionValidationError(f"{self.name} is not in the allowed value set")
        return value

    def _validate_range(self, value: int | float) -> None:
        if self.minimum is not None and value < self.minimum:
            raise OptionValidationError(f"{self.name} is below its minimum")
        if self.maximum is not None and value > self.maximum:
            raise OptionValidationError(f"{self.name} exceeds its maximum")


class OptionCatalog:
    """Immutable metadata for the only mpv options accepted by profile code."""

    def __init__(self, definitions: Iterable[OptionDefinition]) -> None:
        by_name: dict[str, OptionDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, OptionDefinition):
                raise TypeError("catalog entries must be OptionDefinition values")
            if definition.name in by_name:
                raise ValueError(f"duplicate mpv option metadata for {definition.name!r}")
            by_name[definition.name] = definition
        if not by_name:
            raise ValueError("option catalog must not be empty")
        self._definitions: Mapping[str, OptionDefinition] = MappingProxyType(by_name)

    @property
    def definitions(self) -> Mapping[str, OptionDefinition]:
        return self._definitions

    def get(self, name: str) -> OptionDefinition | None:
        return self._definitions.get(name)

    def require(self, name: str) -> OptionDefinition:
        definition = self.get(name)
        if definition is None:
            raise OptionValidationError(f"mpv option {name!r} is not allowlisted")
        return definition

    def validate(self, name: str, value: Any) -> bool | int | float | str:
        return self.require(name).validate(value)

    def validate_request_override(self, name: str, value: Any) -> bool | int | float | str:
        definition = self.require(name)
        if definition.security_class is not OptionSecurityClass.PUBLIC:
            raise OptionValidationError(f"{name} is not available as a public request override")
        return definition.validate(value)


_OPTION_NAME = re.compile(r"[a-z][a-z0-9-]{0,63}\Z", re.ASCII)


def _bound(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number or null")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite number or null")
    return normalized


DEFAULT_OPTION_CATALOG = OptionCatalog(
    (
        OptionDefinition(
            "volume",
            OptionValueType.NUMBER,
            OptionScope.AUDIO,
            True,
            False,
            OptionSecurityClass.PUBLIC,
            minimum=0,
            maximum=100,
            description="Software volume, normalized to the PlayerService 0-100 contract.",
        ),
        OptionDefinition(
            "speed",
            OptionValueType.NUMBER,
            OptionScope.PLAYBACK,
            True,
            False,
            OptionSecurityClass.PUBLIC,
            minimum=0.01,
            maximum=16,
            description="Playback-rate multiplier within the typed PlayerHost control range.",
        ),
        OptionDefinition(
            "pause",
            OptionValueType.BOOLEAN,
            OptionScope.PLAYBACK,
            True,
            False,
            OptionSecurityClass.PUBLIC,
            description="Paused state for the current playback session.",
        ),
        OptionDefinition(
            "video-rotate",
            OptionValueType.INTEGER,
            OptionScope.VIDEO,
            False,
            True,
            OptionSecurityClass.PUBLIC,
            minimum=0,
            maximum=359,
            description="Clockwise rotation; product policy applies it through restart reconfiguration.",
        ),
        OptionDefinition(
            "deband",
            OptionValueType.BOOLEAN,
            OptionScope.VIDEO,
            False,
            True,
            OptionSecurityClass.PUBLIC,
            description="Toggle mpv's video debanding filter at a controlled reconfiguration boundary.",
        ),
        OptionDefinition(
            "sub-auto",
            OptionValueType.ENUM,
            OptionScope.SUBTITLE,
            False,
            True,
            OptionSecurityClass.PUBLIC,
            allowed_values=frozenset({"no", "exact", "fuzzy", "all"}),
            description="Policy for loading external subtitle files for a media item.",
        ),
        OptionDefinition(
            "sub-scale",
            OptionValueType.NUMBER,
            OptionScope.SUBTITLE,
            False,
            True,
            OptionSecurityClass.PUBLIC,
            minimum=0,
            maximum=100,
            description="Subtitle scaling factor, applied at a reconfiguration boundary.",
        ),
        OptionDefinition(
            "vo",
            OptionValueType.ENUM,
            OptionScope.VIDEO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset({"auto", "gpu", "gpu-next", "drm", "null"}),
            description="Video-output backend; available drivers depend on the mpv build.",
        ),
        OptionDefinition(
            "ao",
            OptionValueType.ENUM,
            OptionScope.AUDIO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset({"auto", "alsa", "pipewire", "pulse", "null"}),
            description="Audio-output backend; available drivers depend on the mpv build.",
        ),
        OptionDefinition(
            "audio-device",
            OptionValueType.STRING,
            OptionScope.AUDIO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            max_length=256,
            description="Explicit output device identifier; it is never a public request override.",
        ),
        OptionDefinition(
            "audio-exclusive",
            OptionValueType.BOOLEAN,
            OptionScope.AUDIO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            description="Backend-dependent exclusive output request; not assumed supported by ALSA.",
        ),
        OptionDefinition(
            "audio-fallback-to-null",
            OptionValueType.BOOLEAN,
            OptionScope.AUDIO_OUTPUT,
            False,
            True,
            OptionSecurityClass.RESTRICTED,
            description="Changes fail-closed device behavior and may not be request-overridden.",
        ),
        OptionDefinition(
            "drm-device", OptionValueType.STRING, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION, max_length=256,
            description="Trusted DRM card path; never a public request override.",
        ),
        OptionDefinition(
            "drm-mode", OptionValueType.STRING, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION, max_length=32,
            description="Trusted advertised progressive timing; never a public override.",
        ),
        OptionDefinition(
            "drm-connector", OptionValueType.STRING, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION, max_length=64,
            description="Trusted reported DRM connector; never a public request override.",
        ),
        OptionDefinition(
            "gpu-context",
            OptionValueType.STRING,
            OptionScope.VIDEO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            max_length=128,
            description="Renderer context selection; actual contexts are build and platform dependent.",
        ),
        OptionDefinition(
            'drm-format', OptionValueType.ENUM, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(['xrgb8888', 'xrgb2101010']),
            description="Fixed managed DRM target policy; never a public request override.",
        ),
        OptionDefinition(
            'target-colorspace-hint', OptionValueType.ENUM, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(['auto', 'yes', 'no']),
            description="Fixed managed DRM target policy; never a public request override.",
        ),
        OptionDefinition(
            'target-colorspace-hint-mode', OptionValueType.ENUM, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(['source', 'target']),
            description="Fixed managed DRM target policy; never a public request override.",
        ),
        OptionDefinition(
            'target-trc', OptionValueType.ENUM, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(['auto', 'bt.1886', 'pq']),
            description="Fixed managed DRM target policy; never a public request override.",
        ),
        OptionDefinition(
            'target-prim', OptionValueType.ENUM, OptionScope.VIDEO_OUTPUT,
            False, True, OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(['auto', 'bt.709', 'bt.2020']),
            description="Fixed managed DRM target policy; never a public request override.",
        ),
        OptionDefinition(
            "hwdec",
            OptionValueType.ENUM,
            OptionScope.VIDEO,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(
                {
                    "no",
                    "auto",
                    "auto-safe",
                    "auto-unsafe",
                    "auto-copy",
                    "auto-copy-safe",
                    "auto-copy-unsafe",
                    "vaapi",
                    "vaapi-copy",
                    "drm",
                    "drm-copy",
                    "vulkan",
                    "vulkan-copy",
                }
            ),
            description="Hardware decoding policy; concrete decoder availability is runtime dependent.",
        ),
        OptionDefinition(
            "video-sync",
            OptionValueType.ENUM,
            OptionScope.VIDEO_OUTPUT,
            False,
            True,
            OptionSecurityClass.DEVICE_SELECTION,
            allowed_values=frozenset(
                {
                    "audio",
                    "display-resample",
                    "display-resample-vdrop",
                    "display-vdrop",
                    "display-adrop",
                    "display-tempo",
                    "desync",
                    "auto",
                }
            ),
            description="Video/audio synchronization mode selected only by trusted profiles.",
        ),
    )
)
