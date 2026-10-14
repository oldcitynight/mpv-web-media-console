"""Bounded libdrm GET-only mode observation; no master changes or modesets.

Discovery opens a primary card read-only. Activation duplicates only the
current host's already-owned master FD and requires active scanout. No EDID
advertisement or option echo is an activation claim.
"""
from __future__ import annotations

import os
import stat
from backend.profiles.drm_modes import DrmMode, DrmModeReport
from player_host.display_signal import (LibDrmReader, ObservationError, _CARD, _CONNECTOR,
    _CONNECTOR_TYPES, _MAX_CONNECTORS, _MAX_PROPERTIES, MAX_SAMPLE_SECONDS, _check_array, _label, _property_type)


def _mode(value):
    return DrmMode(**{name: int(getattr(value, name)) for name in DrmMode.__dataclass_fields__})


class DrmModeReader(LibDrmReader):
    def _read_fd(self, card):
        fd = os.open(card, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 226
                    or os.minor(info.st_rdev) != int(_CARD.fullmatch(card)[1])):
                raise ObservationError("unbound_route")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _sample(self, lib, fd, card, name, *, owned):
        with self._get(lib, "Resources", fd) as resources:
            _check_array(resources.count_connectors, resources.connectors, _MAX_CONNECTORS)
            ids = [int(resources.connectors[i]) for i in range(resources.count_connectors)]
        reports = []
        for identifier in ids:
            with self._get(lib, "ConnectorCurrent", fd, identifier) as connector:
                actual = f"{_CONNECTOR_TYPES.get(connector.connector_type, 'Unknown')}-{connector.connector_type_id}"
                if actual != name:
                    continue
                if connector.connector_id != identifier or connector.connection != 1:
                    raise ObservationError("connector_changed")
                _check_array(connector.count_modes, connector.modes, 256)
                modes = tuple(_mode(connector.modes[i]) for i in range(connector.count_modes))
                encoder_id = int(connector.encoder_id)
                _check_array(connector.count_props, connector.props, _MAX_PROPERTIES)
                _check_array(connector.count_props, connector.prop_values, _MAX_PROPERTIES)
                properties = {}
                for i in range(connector.count_props):
                    with self._get(lib, "Property", fd, connector.props[i]) as prop:
                        if prop.prop_id != connector.props[i]:
                            raise ObservationError("invalid_readback")
                        label = _label(prop.name)
                        if label in {"DPMS", "link-status"}:
                            if _property_type(prop.flags) != (1 << 3):
                                raise ObservationError("invalid_readback")
                            _check_array(prop.count_enums, prop.enums, 64)
                            matches = [_label(prop.enums[j].name) for j in range(prop.count_enums)
                                       if prop.enums[j].value == connector.prop_values[i]]
                            if len(matches) != 1 or label in properties:
                                raise ObservationError("invalid_readback")
                            properties[label] = matches[0]
                        elif label == "CRTC_ID":
                            if _property_type(prop.flags) != (1 << 6):
                                raise ObservationError("invalid_readback")
                            if label in properties:
                                raise ObservationError("invalid_readback")
                            properties[label] = int(connector.prop_values[i])
            active = None
            if encoder_id:
                with self._get(lib, "Encoder", fd, encoder_id) as encoder:
                    if encoder.encoder_id != encoder_id:
                        raise ObservationError("connector_changed")
                    crtc_id = int(encoder.crtc_id)
                if crtc_id and properties.get("CRTC_ID", crtc_id) == crtc_id:
                    with self._get(lib, "Crtc", fd, crtc_id) as crtc:
                        if crtc.crtc_id == crtc_id and crtc.mode_valid and crtc.buffer_id:
                            active = _mode(crtc.mode)
                    if owned:
                        self._require_active_crtc(lib, fd, crtc_id)
            if owned and (active is None or properties.get("DPMS", "On") != "On"
                          or properties.get("link-status", "Good") != "Good"):
                raise ObservationError("inactive_scanout")
            reports.append(DrmModeReport(card, name, identifier, modes, active))
        if len(reports) != 1:
            raise ObservationError("unbound_route")
        return reports[0]

    def read_modes(self, card, connector, *, owned=False):
        if _CARD.fullmatch(card) is None or _CONNECTOR.fullmatch(connector) is None:
            raise ObservationError("unbound_route")
        started = self._clock()
        self._deadline = started + MAX_SAMPLE_SECONDS
        lib = self._lib()
        fd = ((self._duplicate_fd or self._owned_fd)(card) if owned else self._read_fd(card))
        try:
            if owned and lib.drmIsMaster(fd) != 1:
                raise ObservationError("unbound_route")
            first = self._sample(lib, fd, card, connector, owned=owned)
            second = self._sample(lib, fd, card, connector, owned=owned)
            if first != second or (owned and lib.drmIsMaster(fd) != 1):
                raise ObservationError("connector_changed")
            if self._clock() > self._deadline:
                raise ObservationError("stale_observation")
            return second
        finally:
            os.close(fd)


def discover_modes(card, connector):
    return DrmModeReader().read_modes(card, connector)
