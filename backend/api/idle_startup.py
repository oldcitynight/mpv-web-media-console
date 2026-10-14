"""Private, hash-bound idle deployment handoff for the normal mpv-webd entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from backend.profiles.options import DEFAULT_OPTION_CATALOG
from backend.profiles.output_inventory import device_options, unsafe_output_route
from backend.profiles.display_policy import (HDR_STARTUP_NAMES, device_profile_identities, display_fingerprint,
    normalize_legacy_display_defaults, validate_display_configuration, retained_display_profile_options)
from backend.profiles.restart import option_value_matches


STARTUP_NAMES = tuple(name for name, option in DEFAULT_OPTION_CATALOG.definitions.items()
                      if option.restart_required and name != "drm-mode")
MODE_STARTUP_NAMES = STARTUP_NAMES + ("drm-mode",)
RUNTIME_NAMES = ("volume", "speed", "pause")


def queue_metadata(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Retain ordering and active identity without collecting media paths."""
    return {"id": snapshot["id"], "revision": snapshot["revision"],
            "items": [{key: item[key] for key in ("id", "ordinal", "active")}
                      for item in snapshot["items"]]}


class IdleStartupHandoff:
    def __init__(self, document: Mapping[str, Any]) -> None:
        if (set(document) != {"schema_version", "mpv_options", "expected_options", "device_profile_ids", "queue"}
                or type(document["schema_version"]) is not int or document["schema_version"] not in {1, 2}):
            raise ValueError("idle startup handoff schema is invalid")
        self.startup_names = MODE_STARTUP_NAMES if document["schema_version"] == 2 else STARTUP_NAMES
        self.mpv_options = dict(document["mpv_options"])
        self.expected_options = dict(document["expected_options"])
        self.device_profile_ids = dict(document["device_profile_ids"])
        self.queue = dict(document["queue"])
        if set(self.expected_options) != set(self.startup_names + RUNTIME_NAMES):
            raise ValueError("idle startup requires complete fixed option readbacks")
        if document["schema_version"] == 2 and (
                "drm-mode" not in self.mpv_options or ":mode:" not in self.device_profile_ids.get("display", "")):
            raise ValueError("mode startup requires explicit advertised timing intent")
        if not set(self.mpv_options) <= set(self.expected_options):
            raise ValueError("idle startup contains an unsupported option")
        for name, value in self.mpv_options.items():
            DEFAULT_OPTION_CATALOG.validate(name, value)
            if value != self.expected_options[name]:
                raise ValueError("idle startup seed differs from its expected readback")
        for name in RUNTIME_NAMES:
            if name not in self.mpv_options:
                raise ValueError("idle startup requires current runtime settings")
        # Empty/default strings can be legitimate readbacks but cannot be
        # catalog-valid initialization values. Keep them as readback assertions.
        for name, value in self.expected_options.items():
            option = DEFAULT_OPTION_CATALOG.require(name)
            if value is None and name == "gpu-context" and self.expected_options["vo"] == "null":
                continue  # No render context exists with a null video output.
            if value != "":
                option.validate(value)
            elif name in RUNTIME_NAMES:
                raise ValueError("idle runtime readback is invalid")
        self.mpv_options = normalize_legacy_display_defaults(self.mpv_options)
        validate_display_configuration(self.mpv_options)
        if not set(self.device_profile_ids) <= {"audio", "display"} or any(
                not isinstance(value, str) or not value.startswith("output:")
                for value in self.device_profile_ids.values()):
            raise ValueError("idle startup output identities are invalid")
        if (set(self.queue) != {"id", "revision", "items"}
                or not isinstance(self.queue["id"], str)
                or type(self.queue["revision"]) is not int
                or not isinstance(self.queue["items"], list)
                or len(self.queue["items"]) > 4096):
            raise ValueError("idle startup queue metadata is invalid")
        for item in self.queue["items"]:
            if (not isinstance(item, dict) or set(item) != {"id", "ordinal", "active"}
                    or not isinstance(item["id"], str) or type(item["ordinal"]) is not int
                    or type(item["active"]) is not bool):
                raise ValueError("idle startup queue item metadata is invalid")

    @classmethod
    def read(cls, path: str | Path, expected_sha256: str) -> "IdleStartupHandoff":
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise ValueError("idle startup requires a SHA-256 binding")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 262144):
                raise ValueError("idle startup handoff must be a bounded private owned file")
            raw = source.read(262145)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("idle startup handoff hash does not match")
        return cls(json.loads(raw))

    def verify(self, service: Any) -> None:
        """Verify before serving requests; a failed assertion closes the child."""
        observed = service.player_host.observe_restart_options(self.startup_names)
        response = service.player_host.snapshot()
        snapshot = getattr(response, "payload", response)
        if not isinstance(observed, Mapping) or not isinstance(snapshot, Mapping) or snapshot.get("path") is not None:
            raise RuntimeError("idle startup player state did not verify")
        actual = {**observed, **{name: snapshot.get(name) for name in RUNTIME_NAMES}}
        for name, expected in self.expected_options.items():
            if expected is None and name == "gpu-context" and self.expected_options["vo"] == "null":
                if actual.get(name) is not None:
                    raise RuntimeError("idle startup effective options did not verify")
                continue
            if not option_value_matches(DEFAULT_OPTION_CATALOG.require(name), expected, actual.get(name)):
                raise RuntimeError("idle startup effective options did not verify")
        if queue_metadata(service.queue.snapshot()) != self.queue:
            raise RuntimeError("idle startup queue metadata changed")
        inventory = service.output_discovery.discover()
        _rows, devices = service._device_profiles(inventory)
        if unsafe_output_route(service._active_profile_restart_options, inventory):
            raise RuntimeError("idle startup output route is unsafe")
        identities = device_profile_identities(inventory.devices)
        for category, identity in self.device_profile_ids.items():
            device = identities.get(identity)
            options = device_options(device) if device is not None else None
            if (device is not None and device.method == "DRM" and identity.endswith(":sdr")
                    and not HDR_STARTUP_NAMES <= self.mpv_options.keys()):
                raise RuntimeError("idle startup SDR intent requires a complete managed policy")
            if category == "display" and HDR_STARTUP_NAMES & self.mpv_options.keys():
                definition = devices.get(identity)
                options = retained_display_profile_options(definition.options, self.mpv_options) if definition is not None else None
            if (device is None or ("display" if device.method == "DRM" else "audio") != category
                    or options is None or any(
                    not option_value_matches(DEFAULT_OPTION_CATALOG.require(name), value, actual.get(name))
                    for name, value in options.items())):
                raise RuntimeError("idle startup selected output identity did not verify")
        service._active_device_profile_ids = dict(self.device_profile_ids)
        service._active_display_fingerprints = {identity: display_fingerprint(identities[identity], identity)
            for category, identity in self.device_profile_ids.items() if category == "display"}
