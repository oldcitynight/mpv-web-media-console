"""Durable player intent, separate from the one-shot deployment handoff.

Queue/media recovery remains owned by its existing repositories. This record
contains current player settings and selected outputs, never a queue revision.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from backend.api.idle_startup import IdleStartupHandoff, STARTUP_NAMES, MODE_STARTUP_NAMES, RUNTIME_NAMES, queue_metadata
from backend.profiles.display_policy import display_fingerprint, device_profile_identities
from backend.profiles.options import DEFAULT_OPTION_CATALOG

KEY = "player.startup_configuration.v1"
_EMPTY_QUEUE = {"id": "validation-only", "revision": 0, "items": []}


class StartupConfiguration:
    def __init__(self, document: Mapping[str, Any]):
        if (set(document) != {"schema_version", "mpv_options", "expected_options", "device_profile_ids", "display_fingerprints", "recover_media"}
                or type(document["recover_media"]) is not bool):
            raise ValueError("durable startup configuration schema is invalid")
        # Reuse the complete catalog/renderer validation without retaining the
        # administrative handoff's queue snapshot or idle-start lifecycle.
        self.document = dict(document)
        self._handoff(_EMPTY_QUEUE)
        fingerprints = document["display_fingerprints"]
        display = document["device_profile_ids"].get("display")
        if (not isinstance(fingerprints, dict) or set(fingerprints) != ({display} if display else set())
                or any(not isinstance(pin, str) or re.fullmatch(r"[0-9a-f]{64}", pin) is None
                       for pin in fingerprints.values())):
            raise ValueError("durable startup display fingerprints are invalid")

    def _handoff(self, queue):
        return IdleStartupHandoff({key: self.document[key] for key in
            ("schema_version", "mpv_options", "expected_options", "device_profile_ids")} | {"queue": queue})

    @property
    def mpv_options(self):
        return self._handoff(_EMPTY_QUEUE).mpv_options

    def verify(self, service):
        inventory = service.output_discovery.discover()
        identities = device_profile_identities(inventory.devices)
        for identity, expected in self.document["display_fingerprints"].items():
            device = identities.get(identity)
            if device is None or display_fingerprint(device, identity) != expected:
                raise RuntimeError("durable startup selected display capabilities changed")
        # Fresh queue metadata is a race guard for this startup operation only;
        # it is neither saved nor compared to a previous service invocation.
        self._handoff(queue_metadata(service.queue.snapshot())).verify(service)

    @classmethod
    def capture(cls, service):
        mode_selected = "drm-mode" in service._active_profile_restart_options
        names = MODE_STARTUP_NAMES if mode_selected else STARTUP_NAMES
        options = service.player_host.observe_restart_options(names)
        response = service.player_host.snapshot()
        snapshot = getattr(response, "payload", response)
        if not isinstance(options, Mapping) or not isinstance(snapshot, Mapping):
            raise RuntimeError("durable startup settings are unavailable")
        if "path" not in snapshot or (snapshot["path"] is not None
                and (not isinstance(snapshot["path"], str) or not snapshot["path"])):
            raise RuntimeError("durable startup media intent is unavailable")
        expected = {**options, **{name: snapshot.get(name) for name in RUNTIME_NAMES}}
        seed = {name: value for name, value in expected.items() if value is not None and value != ""}
        return cls({"schema_version": 2 if mode_selected else 1, "mpv_options": seed, "expected_options": expected,
                    "device_profile_ids": dict(service._active_device_profile_ids),
                    "display_fingerprints": dict(service._active_display_fingerprints),
                    "recover_media": snapshot["path"] is not None})


class StartupConfigurationStore:
    def __init__(self, migrations):
        self.migrations = migrations

    def read(self):
        connection = self.migrations.connect()
        try:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (KEY,)).fetchone()
            if row is None:
                return None
            if not isinstance(row[0], str) or len(row[0].encode()) > 262144:
                raise ValueError("durable startup configuration is not bounded")
            return StartupConfiguration(json.loads(row[0]))
        finally:
            connection.close()

    def save(self, configuration):
        encoded = json.dumps(configuration.document, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 262144:
            raise ValueError("durable startup configuration is not bounded")
        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (KEY, encoded))
        finally:
            connection.close()

    def update_runtime(self, values):
        if not set(values) <= set(RUNTIME_NAMES):
            raise ValueError("durable runtime setting is unsupported")
        for name, value in values.items():
            DEFAULT_OPTION_CATALOG.validate(name, value)
        self._update(values=values)

    def update_media_intent(self, recover_media):
        if type(recover_media) is not bool:
            raise ValueError("durable media intent must be a boolean")
        self._update(recover_media=recover_media)

    def _update(self, *, values=None, recover_media=None):
        connection = self.migrations.connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT value_json FROM settings WHERE key=?", (KEY,)).fetchone()
                if row is None:
                    raise ValueError("durable startup configuration disappeared")
                document = StartupConfiguration(json.loads(row[0])).document
                for name, value in (values or {}).items():
                    document["mpv_options"][name] = value
                    document["expected_options"][name] = value
                if recover_media is not None:
                    document["recover_media"] = recover_media
                encoded = json.dumps(StartupConfiguration(document).document, sort_keys=True, separators=(",", ":"), allow_nan=False)
                connection.execute("UPDATE settings SET value_json=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE key=?", (encoded, KEY))
        finally:
            connection.close()
