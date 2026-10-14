"""Advertised progressive timings, with the pinned mpv parser's 0.01 Hz buckets.

Selecting one is explicit user intent. Source FPS never changes a mode.
The selector is configuration; only an owned-host KMS sample proves activation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Mapping

# drm_mode.h: these four defined picture-aspect values are metadata, not
# scan timing. DRM clients without the aspect-ratio capability receive them
# stripped; the owned mpv master can receive them on connector modes while
# GETCRTC omits them. Keep every raw record intact for evidence.
_PIC_AR_MASK = 0xF << 19
_PIC_AR_VALUES = {value << 19 for value in range(5)}


@dataclass(frozen=True)
class DrmMode:
    clock: int
    hdisplay: int
    hsync_start: int
    hsync_end: int
    htotal: int
    hskew: int
    vdisplay: int
    vsync_start: int
    vsync_end: int
    vtotal: int
    vscan: int
    flags: int

    def __post_init__(self):
        if (any(type(v) is not int or v < 0 or v > 0xffffffff for v in asdict(self).values())
                or not 0 < self.clock <= 4000000
                or not 0 < self.hdisplay <= self.hsync_start <= self.hsync_end <= self.htotal <= 65535
                or not 0 < self.vdisplay <= self.vsync_start <= self.vsync_end <= self.vtotal <= 65535
                or self.hskew > 65535 or self.vscan > 65535):
            raise ValueError("invalid DRM timing")

    @property
    def progressive(self):
        # Interlace/doublescan/vscan are deliberately outside this comparison.
        return (not self.flags & ~(0xF | _PIC_AR_MASK)
                and self.flags & _PIC_AR_MASK in _PIC_AR_VALUES
                and self.vscan <= 1)

    def _timing_record(self):
        record = asdict(self)
        if self.flags & _PIC_AR_MASK in _PIC_AR_VALUES:
            record["flags"] &= ~_PIC_AR_MASK
        return record

    def same_timing(self, other):
        """Compare all effective fields, ignoring only defined aspect metadata."""
        return (isinstance(other, DrmMode) and self.progressive and other.progressive
                and self._timing_record() == other._timing_record())

    @property
    def refresh(self):
        return Fraction(self.clock * 1000, self.htotal * self.vtotal)

    @property
    def selector(self):
        if not self.progressive or not 1 <= self.refresh <= 1000:
            raise ValueError("unsupported DRM timing")
        # C round(x*100) for positive rates, without floating-point ties.
        rate = self.refresh * 100
        bucket = (rate.numerator * 2 + rate.denominator) // (rate.denominator * 2)
        return f"{self.hdisplay}x{self.vdisplay}@{bucket // 100}.{bucket % 100:02d}"

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(self._timing_record(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def label(self):
        return f"{self.hdisplay}×{self.vdisplay} · {float(self.refresh):.3f} Hz"

    @classmethod
    def from_record(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid DRM timing record")
        return cls(**value)


@dataclass(frozen=True)
class DrmModeReport:
    card: str
    connector: str
    connector_id: int
    modes: tuple[DrmMode, ...]
    active: DrmMode | None = None

    def __post_init__(self):
        if (re.fullmatch(r"/dev/dri/card(0|[1-9][0-9]{0,2})", self.card) is None
                or re.fullmatch(r"(HDMI-A|HDMI-B|DP|eDP|DVI-I|DVI-D|DVI-A|VGA|LVDS|DSI)-[1-9][0-9]{0,2}", self.connector) is None
                or type(self.connector_id) is not int or not 0 < self.connector_id <= 0xffffffff
                or not isinstance(self.modes, tuple) or not 0 < len(self.modes) <= 256
                or any(not isinstance(mode, DrmMode) for mode in self.modes)
                or (self.active is not None and not isinstance(self.active, DrmMode))):
            raise ValueError("invalid DRM mode report")

    @property
    def selectable_modes(self):
        buckets = {}
        for mode in self.modes:
            try:
                buckets.setdefault(mode.selector, {})[mode.identity] = mode
            except ValueError:
                continue
        # mpv selects the first matching bucket. Omit ambiguous buckets so a
        # selector can never stand for two different advertised timings.
        return tuple(next(iter(values.values())) for values in buckets.values() if len(values) == 1)

    @property
    def comparison_modes(self):
        # This feature is the approved 4K cinematic comparison, not a general
        # arbitrary timing editor. Keep other advertisements as evidence.
        return tuple(mode for mode in self.selectable_modes
                     if (mode.hdisplay, mode.vdisplay) in {(3840, 2160), (4096, 2160)}
                     and mode.selector.rsplit("@", 1)[1] in {"23.98", "24.00"})

    def record(self):
        return asdict(self)

    @classmethod
    def from_record(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid DRM mode report record")
        if not isinstance(value["modes"], (list, tuple)) or not 0 < len(value["modes"]) <= 256:
            raise ValueError("invalid bounded DRM mode array")
        return cls(value["card"], value["connector"], value["connector_id"],
                   tuple(DrmMode.from_record(mode) for mode in value["modes"]),
                   DrmMode.from_record(value["active"]) if value["active"] is not None else None)


def selected_mode(device, profile_id):
    report = getattr(device, "drm_modes", None)
    if not isinstance(report, DrmModeReport):
        return None
    primary = "output:" + device.id
    return next((mode for mode in report.comparison_modes
        if profile_id in {primary + ":mode:" + mode.identity, primary + ":mode:" + mode.identity + ":sdr"}), None)
