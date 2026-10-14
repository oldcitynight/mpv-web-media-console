"""Fixed direct-DRM policies; capability evidence never means observed HDR output."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from backend.profiles.native_identity import (
    NativeIdentityPolicyError,
    expected_native_identity_hashes,
)
from backend.profiles.drm_modes import selected_mode


HDR_STARTUP_NAMES = frozenset({
    "drm-format", "target-colorspace-hint", "target-colorspace-hint-mode",
    "target-trc", "target-prim",
})
LEGACY_DISPLAY_DEFAULTS = {
    "drm-format": "xrgb8888", "target-colorspace-hint": "auto",
    "target-colorspace-hint-mode": "target", "target-trc": "auto", "target-prim": "auto",
}


def normalize_legacy_display_defaults(options: Mapping[str, Any]) -> dict[str, Any]:
    """A complete handoff may read unused native defaults on a legacy VO.

    Keep those assertions in the handoff, but do not promote their mere
    presence into an opt-in managed renderer policy during initialization.
    """
    result = dict(options)
    if result.get("vo") != "gpu-next" and all(result.get(name) == value for name, value in LEGACY_DISPLAY_DEFAULTS.items()):
        for name in LEGACY_DISPLAY_DEFAULTS:
            result.pop(name)
    return result


DISPLAY_STARTUP_NAMES = frozenset({"vo", "gpu-context", "drm-device", "drm-connector", "drm-mode"}) | HDR_STARTUP_NAMES
REQUIRED_NATIVE_CHOICES = {
    "vo": frozenset({"gpu-next"}), "gpu-context": frozenset({"drm"}),
    "drm-format": frozenset({"xrgb8888", "xrgb2101010"}),
    "target-colorspace-hint": frozenset({"yes"}),
    "target-colorspace-hint-mode": frozenset({"source", "target"}),
    "target-trc": frozenset({"auto", "bt.1886", "pq"}),
    "target-prim": frozenset({"auto", "bt.709", "bt.2020"}),
}


class DisplayPolicyError(ValueError):
    """The fixed policy cannot be safely composed from current evidence."""


@dataclass(frozen=True)
class DrmDisplayCapabilities:
    edid_sha256: str | None = None
    valid: bool = False
    pq: bool = False
    hlg: bool = False
    bt2020_rgb: bool = False
    static_metadata_type1: bool = False
    reason: str = "edid_unavailable"

    @property
    def hdr10(self) -> bool:
        # The pinned mpv DRM predicate tests PQ/BT2020 and usable EDID blocks,
        # not the Type 1 descriptor bit. Keep that bit as reported evidence;
        # eligibility remains separate from observed HDR signaling/reception.
        return self.valid and self.pq and self.bt2020_rgb


def edid_capabilities(data: bytes) -> DrmDisplayCapabilities:
    """Parse at most 32 EDID blocks; no identity/name/bit-depth HDR guesses.

    Every declared block must be present and checksummed. A malformed CTA
    data-block boundary invalidates the entire HDR capability claim.
    """
    digest = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) and data else None
    def unknown(reason: str) -> DrmDisplayCapabilities:
        return DrmDisplayCapabilities(edid_sha256=digest, reason=reason)
    if (not isinstance(data, bytes) or len(data) < 128 or len(data) > 4096
            or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00"):
        return unknown("edid_invalid")
    if data[18] != 1 or data[19] not in {3, 4} or not data[20] & 0x80:
        return unknown("edid_base_unsupported")
    count = data[126] + 1
    if count > 32 or len(data) != count * 128 or any(
            sum(data[offset:offset + 128]) % 256 for offset in range(0, len(data), 128)):
        return unknown("edid_invalid")
    # DRM/EGL's target inference also requires usable base chromaticities.
    low = data[25:27]
    chroma = [data[27 + index] * 4 + ((low[index // 4] >> (6 - 2 * (index % 4))) & 3)
              for index in range(8)]
    if any(not (0 < chroma[i] < 1024 and 0 < chroma[i + 1] < 1024
                    and chroma[i] + chroma[i + 1] <= 1024) for i in range(0, 8, 2)):
        return unknown("edid_chromaticity_invalid")
    pq = hlg = bt2020_rgb = static_metadata_type1 = False
    selected_cta = False
    hdr_block = colorimetry_block = None
    for offset in range(128, len(data), 128):
        block = data[offset:offset + 128]
        if block[0] != 0x02:
            continue
        # Native mpv0.41 chooses the first CTA extension and first relevant
        # blocks, not a union of all advertised extensions.
        use_capabilities = not selected_cta
        selected_cta = True
        if block[1] < 3:
            continue
        end = block[2]
        if end == 0:
            if any(block[4:127]):
                return unknown("edid_cta_invalid")
            continue
        if not 4 <= end <= 127:
            return unknown("edid_cta_invalid")
        position = 4
        while position < end:
            header = block[position]
            length = header & 31
            position += 1
            if position + length > end:
                return unknown("edid_cta_invalid")
            payload = block[position:position + length]
            position += length
            if header >> 5 != 7:
                continue
            if not payload:
                return unknown("edid_cta_invalid")
            if payload[0] == 0x06:
                if len(payload) < 3:
                    return unknown("edid_cta_invalid")
                if use_capabilities:
                    if hdr_block is not None and hdr_block != payload:
                        return unknown("edid_cta_ambiguous")
                    hdr_block = payload
                    pq = bool(payload[1] & 4)
                    hlg = bool(payload[1] & 8)
                    static_metadata_type1 = bool(payload[2] & 1)
            elif payload[0] == 0x05:
                if len(payload) < 3:
                    return unknown("edid_cta_invalid")
                if use_capabilities:
                    if colorimetry_block is not None and colorimetry_block != payload:
                        return unknown("edid_cta_ambiguous")
                    colorimetry_block = payload
                    bt2020_rgb = bool(payload[1] & 0x80)
    return DrmDisplayCapabilities(digest, True, pq, hlg, bt2020_rgb, static_metadata_type1, "edid_valid")


def validate_native_capabilities(record: Any) -> None:
    """Accept capability evidence only when it matches the install-owned policy."""
    if not isinstance(record, Mapping) or set(record) != {"native_library_sha256", "libplacebo_sha256", "choices"}:
        raise DisplayPolicyError("Native GPU-next/DRM capability evidence is unavailable.")
    try:
        expected = expected_native_identity_hashes()
    except NativeIdentityPolicyError as error:
        raise DisplayPolicyError("The install-owned native rendering policy is unavailable.") from error
    if any(record[key] != value for key, value in expected.items()):
        raise DisplayPolicyError("The loaded native rendering libraries do not match the qualified build.")
    choices = record["choices"]
    if not isinstance(choices, Mapping) or set(choices) != set(REQUIRED_NATIVE_CHOICES):
        raise DisplayPolicyError("Native GPU-next/DRM option choices are unavailable.")
    for name, required in REQUIRED_NATIVE_CHOICES.items():
        values = choices[name]
        if (not isinstance(values, (list, tuple)) or len(values) > 256
                or any(not isinstance(value, str) or not value or len(value) > 64
                       or any(ord(character) < 32 for character in value) for value in values)
                or not required <= set(values)):
            raise DisplayPolicyError("The loaded native build does not support the fixed display policy.")


def device_profile_ids(device: Any) -> tuple[str, ...]:
    """Fixed selection identities; SDR is an intent for the same DRM device."""
    primary = "output:" + device.id
    if device.method != "DRM":
        return (primary,)
    report = getattr(device, "drm_modes", None)
    modes = report.comparison_modes if report is not None else ()
    return (primary, primary + ":sdr") + tuple(identity
        for mode in modes for identity in (primary + ":mode:" + mode.identity,
                                           primary + ":mode:" + mode.identity + ":sdr"))


def device_profile_identities(devices: Any) -> dict[str, Any]:
    return {identity: device for device in devices for identity in device_profile_ids(device)}


def display_policy_options(device: Any, native_capabilities: Any, *, intent: str = "auto") -> dict[str, str]:
    """Compose renderer intent only; initialization and connector signal need readback."""
    validate_native_capabilities(native_capabilities)
    if intent not in {"auto", "sdr"}:
        raise DisplayPolicyError("The display policy intent is unsupported.")
    match = re.fullmatch(r"(card\d+)-([A-Za-z]+(?:-[A-Za-z]+)?-\d+)", device.route)
    if device.method != "DRM" or not match or not device.selectable:
        raise DisplayPolicyError("The selected DRM connector is unavailable.")
    caps = device.drm_capabilities
    if intent == "sdr" and (not isinstance(caps, DrmDisplayCapabilities) or not caps.valid):
        raise DisplayPolicyError("Explicit SDR selection requires the current validated EDID.")
    hdr = intent == "auto" and match[2].startswith("HDMI-A-") and isinstance(caps, DrmDisplayCapabilities) and caps.hdr10
    return {
        "vo": "gpu-next", "gpu-context": "drm",
        "drm-format": "xrgb2101010" if hdr else "xrgb8888",
        "target-colorspace-hint": "yes", "target-colorspace-hint-mode": "target",
        "target-trc": "pq" if hdr else "bt.1886", "target-prim": "bt.2020" if hdr else "bt.709",
    }


def validate_display_configuration(options: Mapping[str, Any]) -> None:
    """Reject partial or inconsistent managed policy before touching the host."""
    if "drm-mode" in options:
        if (not HDR_STARTUP_NAMES <= options.keys() or re.fullmatch(
                r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}@[1-9][0-9]{0,3}\.[0-9]{2}", str(options["drm-mode"])) is None):
            raise DisplayPolicyError("Explicit mode selection requires a complete managed DRM policy and canonical timing.")
    if not HDR_STARTUP_NAMES.intersection(options):
        return  # Existing explicit software/backend profiles retain their meaning.
    if not HDR_STARTUP_NAMES <= options.keys() or options.get("vo") != "gpu-next" or options.get("gpu-context") != "drm":
        raise DisplayPolicyError("Managed DRM output requires one complete GPU-next/DRM policy.")
    if (not re.fullmatch(r"/dev/dri/card\d+", str(options.get("drm-device", "")))
            or not re.fullmatch(r"[A-Za-z]+(?:-[A-Za-z]+)?-\d+", str(options.get("drm-connector", "")))):
        raise DisplayPolicyError("Managed DRM output requires a validated physical connector.")
    actual = tuple(options[key] for key in ("drm-format", "target-colorspace-hint", "target-colorspace-hint-mode", "target-trc", "target-prim"))
    if actual not in {("xrgb2101010", "yes", "source", "auto", "auto"),
                      ("xrgb2101010", "yes", "target", "pq", "bt.2020"),
                      ("xrgb8888", "yes", "target", "bt.1886", "bt.709")}:
        raise DisplayPolicyError("The selected DRM renderer and target policy are inconsistent.")



def retained_display_profile_options(current: Mapping[str, Any], retained: Mapping[str, Any]) -> dict[str, Any]:
    """Verify retained intent against fresh qualified profile facts, without Apply.

    An existing complete source-adaptive AUTO policy is not a request for the
    new PQ container. Preserve its three target fields only when the current
    profile still proves the same qualified 10-bit HDR HDMI policy. SDR,
    unavailable outputs and changed route/mode/native evidence never qualify.
    """
    validate_display_configuration(retained)
    result = dict(current)
    names = ("drm-format", "target-colorspace-hint", "target-colorspace-hint-mode", "target-trc", "target-prim")
    if tuple(retained.get(name) for name in names) == ("xrgb2101010", "yes", "source", "auto", "auto"):
        if tuple(result.get(name) for name in names) != ("xrgb2101010", "yes", "target", "pq", "bt.2020"):
            raise DisplayPolicyError("The retained source-adaptive AUTO policy is no longer qualified.")
        result.update({"target-colorspace-hint-mode": "source", "target-trc": "auto", "target-prim": "auto"})
    return result

def display_fingerprint(device: Any, profile_id: str | None = None) -> str:
    caps = device.drm_capabilities
    evidence = vars(caps) if isinstance(caps, DrmDisplayCapabilities) else None
    payload = {"id": device.id, "route": device.route,
               "selectable": device.selectable, "capabilities": evidence}
    mode = selected_mode(device, profile_id)
    if profile_id is not None and ":mode:" in profile_id:
        if mode is None:
            raise DisplayPolicyError("Selected advertised timing is unavailable.")
        report = device.drm_modes
        payload["mode"] = {"selected": mode.identity, "connector_id": report.connector_id,
                           "advertised": sorted({value.identity for value in report.modes})}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
