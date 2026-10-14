"""Read-only hardware identities tied to the current libmpv output.

These names identify an observed route; they are not proof of HDMI signal,
HDR, audio audibility, or display presentation. In particular, mpv's
``audio-device`` option is never by itself evidence of the opened device.
No device is opened, command run, or route changed by this observer.
"""
from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from backend.profiles.output_inventory import (
    LinuxOutputDiscovery,
    ReportedOutput,
    OutputInventory,
    alsa_outputs,
    edid_name,
)


_CONNECTOR = re.compile(r"card\d+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*-\d+")
_DRM_DEVICE = re.compile(r"/dev/dri/(card\d+)")
_PCM_ROUTE = re.compile(r"hw:CARD=[A-Za-z0-9_-]+,DEV=(\d+)")
_PLAYBACK_PCM = re.compile(r"^(\d+)-(\d+):\s*.*?:\s*.*?\bplayback\s+[1-9]\d*\b", re.MULTILINE)
_SUBSTREAM = re.compile(r"sub\d+")
_OWNER = re.compile(r"^owner_pid\s*:\s*(\d+)\s*$", re.MULTILINE)
_DISPLAY_VOS = frozenset({"drm", "gpu", "gpu-next", "x11", "xv", "wlshm", "sdl"})
_KEYS = (
    "display_sink", "audio_sink", "display_sink_evidence",
    "audio_sink_evidence", "display_route", "audio_route",
)
_MAX_ENTRIES = 256
_MAX_OUTPUTS = 128
_MAX_STATUS_READS = 256


def _read(path: Path, limit: int) -> bytes:
    """Bound reads and reject special files rather than opening devices/FIFOs."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("not a regular report file")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("report exceeds read bound")
    return data


def _entries(path: Path) -> list[str]:
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > _MAX_ENTRIES:
                raise ValueError("report directory exceeds entry bound")
    return sorted(names)


def _no_commands(_arguments: list[str]) -> str:
    raise RuntimeError("output identity observation never runs commands")


class OutputIdentityObserver:
    """Resolve output names from fresh, bounded procfs/sysfs reports only.

    All six result fields are always returned. Each side is wholly null when
    its identity cannot be established unambiguously. Paths, host PID and
    environment are injectable for synthetic tests; defaults observe this
    process. No results are cached across hotplug, stream end or route changes.
    """

    def __init__(
        self,
        *,
        proc: Path = Path("/proc/asound"),
        drm: Path = Path("/sys/class/drm"),
        procfs: Path = Path("/proc"),
        pid: int | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.proc, self.drm, self.procfs = Path(proc), Path(drm), Path(procfs)
        self.pid = os.getpid() if pid is None else pid
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("host PID must be a positive integer")
        self.environ = os.environ if environ is None else environ
        self._discovery = LinuxOutputDiscovery(
            proc=self.proc, drm=self.drm, procfs=self.procfs,
            environ=self.environ, command=_no_commands,
        )

    def observe(self, properties: Mapping) -> dict[str, str | None]:
        result: dict[str, str | None] = dict.fromkeys(_KEYS)
        if not isinstance(properties, Mapping):
            return result
        for kind, resolver in (("display", self._display), ("audio", self._audio)):
            try:
                resolved = resolver(properties)
            except (OSError, ValueError, UnicodeError):
                resolved = None
            if resolved is not None:
                name, evidence, route = resolved
                result[f"{kind}_sink"] = name
                result[f"{kind}_sink_evidence"] = evidence
                result[f"{kind}_route"] = route
        return result

    def _display(self, properties: Mapping) -> tuple[str, str, str] | None:
        vo = properties.get("current-vo")
        params = properties.get("video-out-params")
        if not isinstance(vo, str) or vo not in _DISPLAY_VOS or not isinstance(params, Mapping) or not params:
            return None
        connectors = [name for name in _entries(self.drm) if _CONNECTOR.fullmatch(name)]
        if len(connectors) > _MAX_OUTPUTS:
            return None

        # These options are usable only while the actual VO uses DRM. An
        # unrelated GPU/windowed backend must not inherit a configured target.
        active_drm = vo == "drm" or (
            vo in {"gpu", "gpu-next"} and properties.get("current-gpu-context") == "drm"
        )
        explicit = None
        card = properties.get("drm-device")
        connector = properties.get("drm-connector")
        if active_drm and isinstance(card, str) and isinstance(connector, str):
            match = _DRM_DEVICE.fullmatch(card)
            candidate = f"{match[1]}-{connector}" if match else ""
            if _CONNECTOR.fullmatch(candidate):
                explicit = candidate

        reported = properties.get("display-names")
        observed = None
        if isinstance(reported, (list, tuple)) and reported:
            if len(reported) > _MAX_OUTPUTS or not all(isinstance(name, str) for name in reported):
                return None
            matches = set()
            for name in reported:
                # An exact connector name or exact card-qualified name only;
                # HDMI-1 is not guessed to mean HDMI-A-1, nor is an EDID name
                # matched to whichever connected screen happens to have it.
                candidates = [item for item in connectors if name in {item, item.split("-", 1)[1]}]
                if len(candidates) != 1:
                    return None
                matches.add(candidates[0])
            if len(matches) != 1:
                return None
            observed = next(iter(matches))
        if explicit is not None and observed is not None and explicit != observed:
            return None
        selected = explicit or observed
        if selected is None or selected not in connectors:
            return None
        path = self.drm / selected
        if _read(path / "status", 32).strip() != b"connected":
            return None
        if _read(path / "enabled", 32).strip() != b"enabled":
            return None
        name = edid_name(_read(path / "edid", 4096))
        if name is None:
            return None
        evidence = "drm-active-connector-edid" if explicit else "mpv-display-name-edid"
        return name, evidence, f"drm/{selected}"

    def _audio(self, properties: Mapping) -> tuple[str, str, str] | None:
        params = properties.get("audio-out-params")
        if properties.get("current-ao") != "alsa" or not isinstance(params, Mapping) or not params:
            return None
        cards = _read(self.proc / "cards", 65536).decode("utf-8")
        pcm = _read(self.proc / "pcm", 65536).decode("utf-8")
        devices = alsa_outputs(cards, pcm)
        inventory = self._discovery.apply_output_policies(OutputInventory(tuple(devices)))
        devices = list(inventory.devices)
        if len(devices) > _MAX_OUTPUTS:
            return None
        by_pcm: dict[tuple[int, int], ReportedOutput] = {}
        for device in devices:
            match = _PCM_ROUTE.fullmatch(device.route)
            if match is None or device.alsa_card_index is None:
                return None
            by_pcm[(device.alsa_card_index, int(match[1]))] = device
        # Inspect every reported playback PCM, including a card whose friendly
        # name is unavailable. Otherwise a second, unnamed owned output could
        # be silently skipped and make the remaining output seem unambiguous.
        pcms = {(int(match[1]), int(match[2])) for match in _PLAYBACK_PCM.finditer(pcm)}
        if len(pcms) > _MAX_OUTPUTS:
            return None
        owned: set[tuple[int, int]] = set()
        status_reads = 0
        for card, device_number in sorted(pcms):
            path = self.proc / f"card{card}" / f"pcm{device_number}p"
            for substream in _entries(path):
                if not _SUBSTREAM.fullmatch(substream):
                    continue
                status_reads += 1
                if status_reads > _MAX_STATUS_READS:
                    return None
                report = _read(path / substream / "status", 4096).decode("utf-8")
                owners = _OWNER.findall(report)
                if len(owners) > 1:
                    return None
                if owners:
                    owner = int(owners[0])
                    # ALSA reports the opening task ID, which can be a worker
                    # thread rather than the process leader. Prove membership
                    # before treating it as this host's hardware output.
                    member = owner == self.pid
                    if not member:
                        try:
                            task = _read(self.procfs / str(self.pid) / "task" / str(owner) / "status", 65536).decode("utf-8")
                            member = re.search(r"^Tgid:\s*" + str(self.pid) + r"\s*$", task, re.MULTILINE) is not None
                        except (OSError, ValueError, UnicodeError):
                            member = False
                    if member:
                        owned.add((card, device_number))

        aliases = [device for device in devices
                   if device.requires_policy and device.selectable and device.selection_route
                   and properties.get("audio-device") == "alsa/" + device.selection_route]
        # An alias and independently owned hardware are ambiguous observations.
        if len(owned) + len(aliases) != 1:
            return None
        if aliases:
            device = aliases[0]
            evidence, route = "alsa-managed-policy", "alsa/" + device.selection_route
        else:
            device = by_pcm.get(next(iter(owned)))
            if device is None:
                return None
            evidence, route = "alsa-owned-pcm", f"alsa/{device.route}"
        if not device.name:
            return None
        return device.name, evidence, route
