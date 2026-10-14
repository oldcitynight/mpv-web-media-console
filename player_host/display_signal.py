"""Bounded observation of the current host's DRM connector signaling.

KMS is GET-only. Never open a DRM device or acquire/drop master. Duplicate an
already-owned primary FD; close only that duplicate and fresh GEM handles
allocated by GETFB2. No EDID, source label or max-bpc value is output proof.
These facts describe kernel connector state, never physical TV reception.

ABI references: libdrm xf86drmMode.h; Linux drm_mode.h / drm_fourcc.h;
https://www.kernel.org/doc/html/v6.10/gpu/drm-uapi.html#drm-ioctl-mode-getfb2
"""
from __future__ import annotations

import ctypes as C
import errno
import os
import re
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from backend.domain.player_state import (
    DISPLAY_COLORSPACES, DISPLAY_SIGNAL_FIELDS, DISPLAY_SIGNAL_REASONS, SCANOUT_FORMAT_DEPTHS,
)

_CARD = re.compile(r"/dev/dri/card(0|[1-9][0-9]{0,2})\Z")
_CONNECTOR = re.compile(r"(HDMI-A|HDMI-B|DP|eDP|DVI-I|DVI-D|DVI-A|VGA|LVDS|DSI)-([1-9][0-9]{0,2})\Z")
_CONNECTOR_TYPES = {1: "VGA", 2: "DVI-I", 3: "DVI-D", 4: "DVI-A", 7: "LVDS",
                    10: "DP", 11: "HDMI-A", 12: "HDMI-B", 14: "eDP", 16: "DSI"}
_MAX_CONNECTORS, _MAX_PROPERTIES, _MAX_ENUMS, _MAX_FDS = 64, 64, 64, 256
TTL_SECONDS = 0.5
REFRESH_INTERVAL_SECONDS = 0.25
# Bound query count/allocation sizes and reject elapsed samples. This does not
# impose a hard timeout on a driver ioctl that blocks inside the kernel.
MAX_SAMPLE_SECONDS = 0.25


class ObservationError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def unknown(reason: str) -> dict[str, Any]:
    if reason not in DISPLAY_SIGNAL_REASONS:
        reason = "invalid_readback"
    result = dict.fromkeys(DISPLAY_SIGNAL_FIELDS)
    result.update(display_signal_state="unknown", display_signal_reason=reason)
    return result


def parse_hdr_metadata(payload: bytes) -> int:
    """Linux hdr_output_metadata: u32 descriptor + 26-byte type1 + padding.

    Both descriptor IDs must be zero (CTA's type 1 is encoded as ID 0).
    Validate size, EOTF, chromaticity and luminance consistency. Zero optional
    mastering/CLL fields mean unavailable and do not invalidate the descriptor.
    """
    if not isinstance(payload, bytes) or len(payload) != 32:
        raise ObservationError("malformed_metadata")
    metadata_type, eotf, descriptor, *values = struct.unpack("=IBB12H2x", payload)
    if metadata_type != 0 or descriptor != 0 or eotf not in range(4):
        raise ObservationError("malformed_metadata")
    if any(coordinate > 50000 for coordinate in values[:8]):
        raise ObservationError("malformed_metadata")
    if any(values[i] + values[i + 1] > 50000 for i in range(0, 8, 2)):
        raise ObservationError("malformed_metadata")
    maximum, minimum, cll, fall = values[8:]
    if (maximum and minimum > maximum * 10000) or (cll and fall > cll):
        raise ObservationError("malformed_metadata")
    return eotf


# Native ABI structures. Pointer/count pairs are checked before dereferencing.
_U32, _U64, _INT = C.c_uint32, C.c_uint64, C.c_int
class _Resources(C.Structure):
    _fields_ = [("count_fbs", _INT), ("fbs", C.POINTER(_U32)),
                ("count_crtcs", _INT), ("crtcs", C.POINTER(_U32)),
                ("count_connectors", _INT), ("connectors", C.POINTER(_U32)),
                ("count_encoders", _INT), ("encoders", C.POINTER(_U32)),
                ("min_width", _U32), ("max_width", _U32), ("min_height", _U32), ("max_height", _U32)]
class _Mode(C.Structure):
    _fields_ = [("clock", _U32)] + [(n, C.c_uint16) for n in (
        "hdisplay", "hsync_start", "hsync_end", "htotal", "hskew", "vdisplay",
        "vsync_start", "vsync_end", "vtotal", "vscan")] + [
        ("vrefresh", _U32), ("flags", _U32), ("type", _U32), ("name", C.c_char * 32)]
class _Connector(C.Structure):
    _fields_ = [("connector_id", _U32), ("encoder_id", _U32), ("connector_type", _U32),
                ("connector_type_id", _U32), ("connection", _INT), ("mmWidth", _U32),
                ("mmHeight", _U32), ("subpixel", _INT), ("count_modes", _INT),
                ("modes", C.POINTER(_Mode)), ("count_props", _INT),
                ("props", C.POINTER(_U32)), ("prop_values", C.POINTER(_U64)),
                ("count_encoders", _INT), ("encoders", C.POINTER(_U32))]
class _Encoder(C.Structure):
    _fields_ = [(name, _U32) for name in ("encoder_id", "encoder_type", "crtc_id", "possible_crtcs", "possible_clones")]
class _Crtc(C.Structure):
    _fields_ = [(name, _U32) for name in ("crtc_id", "buffer_id", "x", "y", "width", "height")] + [
        ("mode_valid", _INT), ("mode", _Mode), ("gamma_size", _INT)]
class _FB2(C.Structure):
    _fields_ = [(name, _U32) for name in ("fb_id", "width", "height", "pixel_format")] + [
        ("modifier", _U64), ("flags", _U32), ("handles", _U32 * 4),
        ("pitches", _U32 * 4), ("offsets", _U32 * 4)]
class _Enum(C.Structure):
    _fields_ = [("value", _U64), ("name", C.c_char * 32)]
class _Property(C.Structure):
    _fields_ = [("prop_id", _U32), ("flags", _U32), ("name", C.c_char * 32),
                ("count_values", _INT), ("values", C.POINTER(_U64)),
                ("count_enums", _INT), ("enums", C.POINTER(_Enum)),
                ("count_blobs", _INT), ("blob_ids", C.POINTER(_U32))]
class _ObjectProperties(C.Structure):
    _fields_ = [("count_props", _U32), ("props", C.POINTER(_U32)), ("prop_values", C.POINTER(_U64))]
class _Blob(C.Structure):
    _fields_ = [("id", _U32), ("length", _U32), ("data", C.c_void_p)]


def _check_array(count: int, pointer: Any, maximum: int) -> None:
    if not 0 <= count <= maximum or (count and not pointer):
        raise ObservationError("invalid_readback")


def _property_type(flags: int) -> int:
    # libdrm's legacy/extended type mask, excluding IMMUTABLE and ATOMIC.
    return flags & (0xFFC0 | (1 << 1) | (1 << 3) | (1 << 4) | (1 << 5))


def _label(value: bytes) -> str:
    try:
        return value.decode("ascii", "strict")
    except UnicodeError as error:
        raise ObservationError("invalid_readback") from error


@dataclass(frozen=True)
class ConnectorSample:
    connector_id: int
    crtc_id: int
    scanout_format: str
    colorspace: str
    hdr_blob_id: int
    hdr_eotf: int | None


class LibDrmReader:
    """No subprocesses, device opens, master calls, or KMS mutations."""
    def __init__(self, *, library: Any = None, clock: Callable[[], float] = time.monotonic,
                 duplicate_fd: Callable[[str], int] | None = None) -> None:
        self._library = library
        self._clock = clock
        self._duplicate_fd = duplicate_fd
        self._deadline = float("inf")

    def _lib(self):
        if self._library is None:
            # Fixed system SONAME, never user-supplied paths or shell lookup.
            lib = C.CDLL("libdrm.so.2", use_errno=True)
            for name, structure, arguments in (
                ("Resources", _Resources, [_INT]),
                ("ConnectorCurrent", _Connector, [_INT, _U32]),
                ("Encoder", _Encoder, [_INT, _U32]),
                ("Crtc", _Crtc, [_INT, _U32]),
                ("FB2", _FB2, [_INT, _U32]),
                ("Property", _Property, [_INT, _U32]),
                ("PropertyBlob", _Blob, [_INT, _U32]),
            ):
                get = getattr(lib, "drmModeGet" + name)
                get.argtypes, get.restype = arguments, C.POINTER(structure)
                free = getattr(lib, "drmModeFree" + ("Connector" if name == "ConnectorCurrent" else name))
                free.argtypes, free.restype = [C.POINTER(structure)], None
            lib.drmModeObjectGetProperties.argtypes = [_INT, _U32, _U32]
            lib.drmModeObjectGetProperties.restype = C.POINTER(_ObjectProperties)
            lib.drmModeFreeObjectProperties.argtypes = [C.POINTER(_ObjectProperties)]
            lib.drmModeFreeObjectProperties.restype = None
            lib.drmIsMaster.argtypes, lib.drmIsMaster.restype = [_INT], _INT
            lib.drmCloseBufferHandle.argtypes, lib.drmCloseBufferHandle.restype = [_INT, _U32], _INT
            self._library = lib
        return self._library

    def _owned_fd(self, card: str) -> int:
        if _CARD.fullmatch(card) is None:
            raise ObservationError("unbound_route")
        info = os.stat(card, follow_symlinks=False)
        if (not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 226
                or os.minor(info.st_rdev) >= 64 or os.minor(info.st_rdev) != int(_CARD.fullmatch(card)[1])):
            raise ObservationError("unbound_route")
        names = []
        with os.scandir("/proc/self/fd") as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > _MAX_FDS:
                    raise ObservationError("unavailable")
        for name in sorted(names):
            if not name.isdecimal():
                continue
            try:
                original = int(name)
                owned = os.fstat(original)
                if not stat.S_ISCHR(owned.st_mode) or owned.st_rdev != info.st_rdev:
                    continue
                duplicate = os.dup(original)
                # Recheck after dup against close/reuse races. Never close original.
                keep = False
                try:
                    actual = os.fstat(duplicate)
                    keep = actual.st_rdev == info.st_rdev and stat.S_ISCHR(actual.st_mode) and self._lib().drmIsMaster(duplicate) == 1
                    if keep:
                        return duplicate
                finally:
                    if not keep:
                        os.close(duplicate)
            except OSError:
                continue
        raise ObservationError("unbound_route")

    @contextmanager
    def _get(self, lib: Any, kind: str, *args: int):
        if self._clock() > self._deadline:
            raise ObservationError("stale_observation")
        getter = "drmModeObjectGetProperties" if kind == "ObjectProperties" else "drmModeGet" + kind
        pointer = getattr(lib, getter)(*args)
        if not pointer:
            err = C.get_errno()
            if err in {errno.EACCES, errno.EPERM}:
                raise PermissionError(err, "DRM observation denied")
            raise ObservationError("unavailable")
        try:
            yield pointer.contents
        finally:
            getattr(lib, "drmModeFree" + ("Connector" if kind == "ConnectorCurrent" else kind))(pointer)

    def _framebuffer_format(self, lib: Any, fd: int, fb_id: int) -> str:
        with self._get(lib, "FB2", fd, fb_id) as fb:
            # GETFB2 creates fresh handles, possibly repeated for several planes.
            # Only these query-created handles are closed, never mpv's originals.
            handles = {int(handle) for handle in fb.handles if handle}
            try:
                if fb.fb_id != fb_id or not fb.width or not fb.height:
                    raise ObservationError("invalid_readback")
                fourcc = _label(int(fb.pixel_format).to_bytes(4, "little"))
                if fourcc not in SCANOUT_FORMAT_DEPTHS:
                    raise ObservationError("unknown_scanout_format")
                return fourcc
            finally:
                failed = False
                for handle in sorted(handles):
                    try:
                        failed = lib.drmCloseBufferHandle(fd, handle) != 0 or failed
                    except Exception:
                        failed = True
                if failed:
                    raise ObservationError("invalid_readback")

    def _require_active_crtc(self, lib: Any, fd: int, crtc_id: int) -> None:
        # A configured mode/framebuffer alone can survive DPMS-off. Require
        # the current atomic CRTC ACTIVE property without enabling client caps.
        with self._get(lib, "ObjectProperties", fd, crtc_id, 0xCCCCCCCC) as values:
            _check_array(values.count_props, values.props, _MAX_PROPERTIES)
            _check_array(values.count_props, values.prop_values, _MAX_PROPERTIES)
            active = []
            for index in range(values.count_props):
                with self._get(lib, "Property", fd, values.props[index]) as prop:
                    if _label(prop.name) == "ACTIVE":
                        if prop.prop_id != values.props[index] or _property_type(prop.flags) != (1 << 1):
                            raise ObservationError("invalid_readback")
                        active.append(int(values.prop_values[index]))
            if active != [1]:
                raise ObservationError("inactive_scanout")

    def _connector(self, lib: Any, fd: int, identifier: int, name: str) -> ConnectorSample:
        with self._get(lib, "ConnectorCurrent", fd, identifier) as connector:
            actual = f"{_CONNECTOR_TYPES.get(connector.connector_type, 'Unknown')}-{connector.connector_type_id}"
            if actual != name or connector.connector_id != identifier:
                raise ObservationError("connector_changed")
            if connector.connection != 1:
                raise ObservationError("connector_disconnected")
            if not connector.encoder_id:
                raise ObservationError("inactive_scanout")
            _check_array(connector.count_props, connector.props, _MAX_PROPERTIES)
            _check_array(connector.count_props, connector.prop_values, _MAX_PROPERTIES)
            properties = {}
            for index in range(connector.count_props):
                prop_id, value = connector.props[index], int(connector.prop_values[index])
                with self._get(lib, "Property", fd, prop_id) as prop:
                    label = _label(prop.name)
                    if label not in {"Colorspace", "HDR_OUTPUT_METADATA", "CRTC_ID", "link-status", "DPMS"}:
                        continue
                    if label in properties or prop.prop_id != prop_id:
                        raise ObservationError("invalid_readback")
                    if label in {"Colorspace", "link-status", "DPMS"}:
                        if _property_type(prop.flags) != (1 << 3):
                            raise ObservationError("invalid_readback")
                        _check_array(prop.count_enums, prop.enums, _MAX_ENUMS)
                        matches = [_label(prop.enums[i].name) for i in range(prop.count_enums) if prop.enums[i].value == value]
                        if len(matches) != 1:
                            raise ObservationError("invalid_readback")
                        value = matches[0]
                    elif label == "HDR_OUTPUT_METADATA" and _property_type(prop.flags) != (1 << 4):
                        raise ObservationError("invalid_readback")
                    elif label == "CRTC_ID" and _property_type(prop.flags) != (1 << 6):
                        raise ObservationError("invalid_readback")
                    properties[label] = value
            if "Colorspace" not in properties or "HDR_OUTPUT_METADATA" not in properties:
                raise ObservationError("missing_connector_properties")
            if properties["Colorspace"] not in DISPLAY_COLORSPACES:
                raise ObservationError("invalid_readback")
            if properties.get("DPMS", "On") != "On":
                raise ObservationError("inactive_scanout")
            if properties.get("link-status", "Good") != "Good":
                raise ObservationError("conflicting_evidence")
            with self._get(lib, "Encoder", fd, connector.encoder_id) as encoder:
                crtc_id = int(encoder.crtc_id)
                if not crtc_id or encoder.encoder_id != connector.encoder_id:
                    raise ObservationError("inactive_scanout")
            if properties.get("CRTC_ID", crtc_id) != crtc_id:
                raise ObservationError("connector_changed")
            self._require_active_crtc(lib, fd, crtc_id)
            with self._get(lib, "Crtc", fd, crtc_id) as crtc:
                if crtc.crtc_id != crtc_id or not crtc.mode_valid or not crtc.buffer_id:
                    raise ObservationError("inactive_scanout")
                fourcc = self._framebuffer_format(lib, fd, crtc.buffer_id)
            blob_id = properties["HDR_OUTPUT_METADATA"]
            eotf = None
            if not 0 <= blob_id <= 0xffffffff:
                raise ObservationError("malformed_metadata")
            if blob_id:
                with self._get(lib, "PropertyBlob", fd, blob_id) as blob:
                    if blob.id != blob_id or blob.length != 32 or not blob.data:
                        raise ObservationError("malformed_metadata")
                    eotf = parse_hdr_metadata(C.string_at(blob.data, blob.length))
            return ConnectorSample(identifier, crtc_id, fourcc, properties["Colorspace"], blob_id, eotf)

    def read(self, card: str, connector: str) -> ConnectorSample:
        if _CARD.fullmatch(card) is None or _CONNECTOR.fullmatch(connector) is None:
            raise ObservationError("unbound_route")
        started = self._clock()
        self._deadline = started + MAX_SAMPLE_SECONDS
        lib = self._lib()
        fd = (self._duplicate_fd or self._owned_fd)(card)
        try:
            if lib.drmIsMaster(fd) != 1:
                raise ObservationError("unbound_route")
            with self._get(lib, "Resources", fd) as resources:
                _check_array(resources.count_connectors, resources.connectors, _MAX_CONNECTORS)
                candidates = []
                for index in range(resources.count_connectors):
                    if self._clock() - started > MAX_SAMPLE_SECONDS:
                        raise ObservationError("stale_observation")
                    identifier = int(resources.connectors[index])
                    with self._get(lib, "ConnectorCurrent", fd, identifier) as item:
                        name = f"{_CONNECTOR_TYPES.get(item.connector_type, 'Unknown')}-{item.connector_type_id}"
                        if name == connector:
                            candidates.append(identifier)
                if len(candidates) != 1:
                    raise ObservationError("unbound_route")
            first = self._connector(lib, fd, candidates[0], connector)
            second = self._connector(lib, fd, candidates[0], connector)
            if first != second or lib.drmIsMaster(fd) != 1:
                raise ObservationError("connector_changed")
            if self._clock() - started > MAX_SAMPLE_SECONDS:
                raise ObservationError("stale_observation")
            return second
        finally:
            os.close(fd)


def _observation_context(properties: Mapping[str, Any], epoch: tuple[str, int]) -> tuple:
    target, frame = properties.get("video-target-params"), properties.get("video-out-params")
    if (not properties.get("path") or properties.get("idle-active") is not False
            or properties.get("seeking") is True or properties.get("eof-reached") is True
            or not isinstance(frame, Mapping) or not frame
            or any(isinstance(frame.get(name), bool) or not isinstance(frame.get(name), int)
                   or not 1 <= frame[name] <= 65535 for name in ("w", "h"))
            or not isinstance(frame.get("pixelformat"), str)
            or re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", frame["pixelformat"]) is None
            or not isinstance(target, Mapping) or not target):
        raise ObservationError("no_current_frame")
    if properties.get("current-vo") != "gpu-next" or properties.get("current-gpu-context") != "drm":
        raise ObservationError("unsupported_renderer")
    card, connector = properties.get("drm-device"), properties.get("drm-connector")
    if not isinstance(card, str) or _CARD.fullmatch(card) is None or not isinstance(connector, str) or _CONNECTOR.fullmatch(connector) is None:
        raise ObservationError("unbound_route")
    gamma, primaries = target.get("gamma"), target.get("primaries")
    if not isinstance(gamma, str) or not isinstance(primaries, str):
        raise ObservationError("no_current_frame")
    return (epoch, card, connector, gamma, primaries, frame.get("pixelformat"), frame.get("w"), frame.get("h"), properties.get("path"))


class DisplaySignalObserver:
    """Cache for <=500 ms, keyed by player/media epoch and observed route/target."""
    def __init__(self, *, reader: Any = None, clock: Callable[[], float] = time.monotonic):
        self._reader = reader if reader is not None else LibDrmReader(clock=clock)
        self._clock = clock
        self._key = None
        self._sampled_at = float("-inf")
        self._cached = unknown("not_observed")

    def invalidate(self, reason: str = "epoch_invalidated") -> dict[str, Any]:
        self._key = None
        self._sampled_at = float("-inf")
        self._cached = unknown(reason)
        return dict(self._cached)

    def close(self) -> None:
        self.invalidate()

    def observe(self, properties: Mapping[str, Any], *, epoch: tuple[str, int]) -> dict[str, Any]:
        try:
            key = _observation_context(properties, epoch)
        except ObservationError as error:
            return self.invalidate(error.reason)
        _epoch, card, connector, gamma, primaries, *_frame = key
        now = self._clock()
        if key == self._key and 0 <= now - self._sampled_at < TTL_SECONDS:
            return dict(self._cached)
        try:
            sample = self._reader.read(card, connector)
            if self._clock() - now > MAX_SAMPLE_SECONDS:
                raise ObservationError("stale_observation")
            if (not isinstance(sample, ConnectorSample) or sample.colorspace not in DISPLAY_COLORSPACES
                    or isinstance(sample.hdr_blob_id, bool) or not isinstance(sample.hdr_blob_id, int)
                    or not 0 <= sample.hdr_blob_id <= 0xffffffff
                    or (sample.hdr_eotf is not None and (isinstance(sample.hdr_eotf, bool)
                        or not isinstance(sample.hdr_eotf, int) or not 0 <= sample.hdr_eotf <= 3))
                    or (sample.hdr_blob_id == 0) != (sample.hdr_eotf is None)):
                raise ObservationError("invalid_readback")
            depth = SCANOUT_FORMAT_DEPTHS.get(sample.scanout_format)
            if depth is None:
                raise ObservationError("unknown_scanout_format")
            result = unknown("conflicting_evidence")
            result.update(scanout_format=sample.scanout_format, scanout_bit_depth=depth,
                          connector_colorspace=sample.colorspace, connector_hdr_eotf=sample.hdr_eotf)
            state = None
            if depth == 10 and primaries == "bt.2020" and sample.colorspace == "BT2020_RGB" and sample.hdr_blob_id:
                if gamma == "pq" and sample.hdr_eotf == 2:
                    state = "hdr10"
                elif gamma == "hlg" and sample.hdr_eotf == 3:
                    state = "hlg"
            elif (primaries == "bt.709" and gamma in {"bt.1886", "bt.709", "srgb", "gamma2.2"}
                  and sample.colorspace in {"Default", "BT709_YCC"}
                  and ((sample.hdr_blob_id == 0 and sample.hdr_eotf is None) or (sample.hdr_blob_id > 0 and sample.hdr_eotf == 0))):
                state = "sdr"
            if state:
                result.update(display_signal_state=state, display_signal_reason="verified_" + state,
                              display_signal_evidence="drm_connector_state")
        except PermissionError:
            result = unknown("permission_denied")
        except ObservationError as error:
            result = unknown(error.reason)
        except Exception:
            result = unknown("unavailable")
        self._key, self._sampled_at, self._cached = key, now, result
        return dict(result)


class AsyncDisplaySignalObserver:
    """One lazy single-flight thread; callbacks never execute a DRM query.

    The global 4 Hz start cap survives route/epoch churn. A stalled ioctl never
    starts a replacement thread. Queued requests are latest-only, old-generation
    results are discarded, and cached proof expires 500 ms after sample start.
    The daemon *thread* does not create a process/service; shutdown never joins
    a blocked driver call. Its owned descriptors are cleaned when that call
    returns, or by the OS when the host process exits.
    """
    def __init__(self, *, reader: Any = None, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._observer = DisplaySignalObserver(reader=reader, clock=clock)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._running = False
        self._pending = None
        self._generation = 0
        self._key = None
        self._cached = unknown("not_observed")
        self._sampled_at = float("-inf")
        self._started_at = float("-inf")

    def invalidate(self, reason: str = "epoch_invalidated") -> dict[str, Any]:
        with self._condition:
            self._generation += 1
            self._key = None
            self._pending = None
            self._cached = unknown(reason)
            self._sampled_at = float("-inf")
            self._condition.notify_all()
            return dict(self._cached)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._generation += 1
            self._pending = None
            self._cached = unknown("epoch_invalidated")
            self._condition.notify_all()

    def observe(self, properties: Mapping[str, Any], *, epoch: tuple[str, int]) -> dict[str, Any]:
        try:
            key = _observation_context(properties, epoch)
        except ObservationError as error:
            return self.invalidate(error.reason)
        # Only the validated fixed fields cross the worker boundary.
        snapshot = {name: properties.get(name) for name in (
            "path", "idle-active", "seeking", "eof-reached", "current-vo",
            "current-gpu-context", "drm-device", "drm-connector",
        )}
        snapshot["video-target-params"] = {name: properties["video-target-params"].get(name) for name in ("gamma", "primaries")}
        snapshot["video-out-params"] = {name: properties["video-out-params"].get(name) for name in ("pixelformat", "w", "h")}
        with self._condition:
            if self._closed:
                return unknown("epoch_invalidated")
            if key != self._key:
                self._generation += 1
                self._key = key
                self._cached = unknown("not_observed")
                self._sampled_at = float("-inf")
                self._pending = (self._generation, key, snapshot, epoch)
            now = self._clock()
            fresh = 0 <= now - self._sampled_at < TTL_SECONDS
            if (now - self._sampled_at >= REFRESH_INTERVAL_SECONDS) and not self._running:
                self._pending = (self._generation, key, snapshot, epoch)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="display-signal-observer", daemon=True)
                self._thread.start()
            self._condition.notify_all()
            if fresh:
                return dict(self._cached)
            return unknown("stale_observation" if self._sampled_at != float("-inf") else "not_observed")

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed:
                    if self._pending is None:
                        self._condition.wait()
                        continue
                    remaining = REFRESH_INTERVAL_SECONDS - (self._clock() - self._started_at)
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    generation, key, snapshot, epoch = self._pending
                    self._pending = None
                    self._running = True
                    started = self._started_at = self._clock()
                    break
                else:
                    return
            try:
                # This worker owns the synchronous observer. Always collect a
                # new native sample: its inner timestamp can be later than the
                # outer start, so reusing its cache would renew older proof.
                self._observer.invalidate("not_observed")
                result = self._observer.observe(snapshot, epoch=epoch)
            except Exception:
                result = unknown("unavailable")
            with self._condition:
                self._running = False
                if not self._closed and generation == self._generation and key == self._key:
                    self._cached = result
                    self._sampled_at = started
                self._condition.notify_all()
