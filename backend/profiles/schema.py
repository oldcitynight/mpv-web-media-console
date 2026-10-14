from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping


class ProfileCategory(str, Enum):
    BASE = "base"
    VIDEO = "video"
    AUDIO = "audio"
    DISPLAY = "display"
    MEDIA_POLICY = "media_policy"


class ProfileCompositionError(ValueError):
    """Raised when selected profile layers cannot form one deterministic stack."""


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """A named profile layer with JSON-compatible option values."""

    id: str
    name: str
    category: ProfileCategory
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("profile id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("profile name must be a non-empty string")
        try:
            category = ProfileCategory(self.category)
        except (TypeError, ValueError) as error:
            raise ValueError("profile category is unsupported") from error
        if not isinstance(self.options, Mapping):
            raise ValueError("profile options must be a mapping")
        normalized: dict[str, Any] = {}
        for key, value in self.options.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("profile option names must be non-empty strings")
            normalized[key] = _json_value(value, f"option {key!r}")
        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "options", MappingProxyType(normalized))


@dataclass(frozen=True, slots=True)
class ProfileConflict:
    """A lower-precedence value replaced by a later selected layer."""

    option: str
    previous_profile_id: str
    winning_profile_id: str
    previous_category: str
    winning_category: str
    previous_value: Any
    winning_value: Any


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    """Effective options plus the layer that supplied each value."""

    options: Mapping[str, Any]
    sources: Mapping[str, str]
    profile_ids: tuple[str, ...]
    conflicts: tuple[ProfileConflict, ...]
    output_fingerprints: Mapping[str, str] = field(default_factory=dict)


class ProfileResolver:
    """Merge one profile per category in the documented, stable order."""

    PRECEDENCE: Mapping[str, int] = MappingProxyType(
        {
            ProfileCategory.BASE.value: 10,
            ProfileCategory.VIDEO.value: 20,
            ProfileCategory.AUDIO.value: 30,
            ProfileCategory.DISPLAY.value: 40,
            ProfileCategory.MEDIA_POLICY.value: 50,
            "request_override": 60,
        }
    )

    def resolve(
        self,
        profiles: Iterable[ProfileDefinition],
        *,
        request_overrides: Mapping[str, Any] | None = None,
    ) -> ProfileResolution:
        selected = tuple(profiles)
        if any(not isinstance(profile, ProfileDefinition) for profile in selected):
            raise TypeError("profiles must contain only ProfileDefinition values")
        seen_categories: set[ProfileCategory] = set()
        seen_ids: set[str] = set()
        for profile in selected:
            if profile.category in seen_categories:
                raise ProfileCompositionError(
                    f"only one profile may be selected for category {profile.category.value!r}"
                )
            if profile.id in seen_ids:
                raise ProfileCompositionError("selected profile IDs must be unique")
            seen_categories.add(profile.category)
            seen_ids.add(profile.id)

        layers: list[tuple[int, str, str, Mapping[str, Any]]] = [
            (
                self.PRECEDENCE[profile.category.value],
                profile.category.value,
                profile.id,
                profile.options,
            )
            for profile in selected
        ]
        if request_overrides is not None:
            override_options = _normalize_options(request_overrides, "request overrides")
            layers.append(
                (
                    self.PRECEDENCE["request_override"],
                    "request_override",
                    "request",
                    MappingProxyType(override_options),
                )
            )
        layers.sort(key=lambda layer: (layer[0], layer[2]))

        effective: dict[str, Any] = {}
        sources: dict[str, str] = {}
        categories: dict[str, str] = {}
        conflicts: list[ProfileConflict] = []
        for _precedence, category, profile_id, options in layers:
            for option in sorted(options):
                value = options[option]
                if option in effective:
                    conflicts.append(
                        ProfileConflict(
                            option=option,
                            previous_profile_id=sources[option],
                            winning_profile_id=profile_id,
                            previous_category=categories[option],
                            winning_category=category,
                            previous_value=effective[option],
                            winning_value=value,
                        )
                    )
                effective[option] = value
                sources[option] = profile_id
                categories[option] = category

        return ProfileResolution(
            options=MappingProxyType(effective),
            sources=MappingProxyType(sources),
            profile_ids=tuple(
                profile_id
                for _rank, category, profile_id, _options in layers
                if category != "request_override"
            ),
            conflicts=tuple(conflicts),
        )


def _normalize_options(options: Mapping[str, Any], label: str) -> dict[str, Any]:
    if not isinstance(options, Mapping):
        raise ValueError(f"{label} must be a mapping")
    normalized: dict[str, Any] = {}
    for key, value in options.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label} option names must be non-empty strings")
        normalized[key] = _json_value(value, f"{label} option {key!r}")
    return normalized


def _json_value(value: Any, label: str) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite JSON data")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-compatible") from error
