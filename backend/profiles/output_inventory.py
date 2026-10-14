"""Read reported names without opening audio/video devices or changing routes."""
from __future__ import annotations

import json
import os
import shlex
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from backend.profiles.display_policy import DrmDisplayCapabilities, edid_capabilities
from backend.profiles.drm_modes import DrmModeReport
from player_host.drm_modes import discover_modes
from player_host.display_signal import ObservationError
from backend.profiles.output_policy import OutputDiscoveryContext, OutputSafetyAdapter, load_output_safety_adapters


@dataclass(frozen=True)
class ReportedOutput:
    id: str
    name: str | None
    method: str
    route: str
    kind: str
    selection_route: str | None = None
    selectable: bool = True
    requires_policy: bool = False
    alsa_card_index: int | None = None
    drm_capabilities: DrmDisplayCapabilities | None = None
    drm_modes: DrmModeReport | None = None


@dataclass(frozen=True)
class OutputInventory:
    devices: tuple[ReportedOutput, ...]
    unavailable: tuple[str, ...] = ()
    policies: tuple[OutputSafetyAdapter, ...] = ()


def _name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned[:256] or None


def alsa_outputs(cards: str, pcm: str, hints: str = "") -> list[ReportedOutput]:
    card_names = {}
    for line in cards.splitlines():
        match = re.match(r"\s*(\d+)\s+\[([^]]+)\]\s*:\s*.*? - (.+)$", line)
        if match:
            card_names[int(match[1])] = (match[2].strip(), _name(match[3]))
    hint_names = {}
    lines = hints.splitlines()
    for index, line in enumerate(lines[:-1]):
        if re.fullmatch(r"hw:CARD=[^,\s]+,DEV=\d+", line):
            description = lines[index + 1].strip()
            card_name, _, port_name = description.partition(",")
            # USB Audio is a PCM role, not a more useful hardware identity.
            hint_names[line] = _name(card_name if port_name.strip() in {"", "USB Audio"} else description)
    result = []
    for line in pcm.splitlines():
        match = re.match(r"(\d+)-(\d+):\s*(.*?)\s*:\s*.*?\bplayback\s+[1-9]\d*\b", line)
        if not match or int(match[1]) not in card_names:
            continue
        card_id, card_name = card_names[int(match[1])]
        route = f"hw:CARD={card_id},DEV={int(match[2])}"
        port = _name(match[3])
        name = hint_names.get(route) or card_name
        if route not in hint_names and port and port != "USB Audio" and port != card_name:
            name = f"{card_name} · {port}" if card_name else port
        result.append(ReportedOutput(f"alsa/{route}", name, "ALSA", route, "audio_device", alsa_card_index=int(match[1])))
    return result


def pipewire_outputs(document: str) -> list[ReportedOutput]:
    try:
        objects = json.loads(document)
    except (ValueError, TypeError):
        raise ValueError("invalid PipeWire report") from None
    if not isinstance(objects, list):
        raise ValueError("invalid PipeWire report")
    result = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict) or props.get("media.class") != "Audio/Sink":
            continue
        route = props.get("node.name")
        if not isinstance(route, str) or not route or len(route) > 256 or any(ord(c) < 32 for c in route):
            continue
        name = _name(props.get("node.description")) or _name(props.get("node.nick"))
        result.append(ReportedOutput(f"pipewire/{route}", name, "PipeWire", route, "audio_sink"))
    return result


def edid_name(data: bytes) -> str | None:
    if len(data) < 128 or data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00" or sum(data[:128]) % 256:
        return None
    for offset in range(54, 126, 18):
        descriptor = data[offset:offset + 18]
        if descriptor[:5] == b"\x00\x00\x00\xfc\x00":
            try:
                name = _name(descriptor[5:18].decode("ascii"))
            except UnicodeDecodeError:
                name = None
            if name:
                return name
    manufacturer = int.from_bytes(data[8:10], "big")
    letters = [(manufacturer >> shift) & 31 for shift in (10, 5, 0)]
    if all(1 <= letter <= 26 for letter in letters):
        return "".join(chr(64 + letter) for letter in letters) + f" {int.from_bytes(data[10:12], 'little'):04X}"
    return None


def _read(path: Path, limit: int = 65536) -> bytes:
    with path.open("rb") as source:
        return source.read(limit)


def _command(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=1.5, check=False)
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("report unavailable")
    return result.stdout


class LinuxOutputDiscovery:
    def __init__(self, *, proc: Path = Path("/proc/asound"), drm: Path = Path("/sys/class/drm"),
                 command: Callable[[list[str]], str] = _command,
                 procfs: Path = Path("/proc"), environ=None, mode_provider=discover_modes,
                 output_policies: tuple[OutputSafetyAdapter, ...] | None = None) -> None:
        self.proc, self.drm, self.command = proc, drm, command
        self.mode_provider = mode_provider
        self.procfs = procfs
        self.environ = os.environ if environ is None else environ
        context = OutputDiscoveryContext(self.proc, self.drm, self.procfs, self.environ, self.command)
        self.output_policies = (load_output_safety_adapters(context) if output_policies is None
                                else tuple(output_policies))

    def discover(self) -> OutputInventory:
        devices = []
        unavailable = []
        try:
            cards = _read(self.proc / "cards").decode("utf-8", errors="replace")
            pcm = _read(self.proc / "pcm").decode("utf-8", errors="replace")
            try:
                hints = self.command(["aplay", "-L"])
            except (OSError, ValueError, subprocess.SubprocessError):
                hints = ""
            devices.extend(alsa_outputs(cards, pcm, hints))
        except OSError:
            unavailable.append("ALSA")
        try:
            devices.extend(pipewire_outputs(self.command(["pw-dump"])))
        except (OSError, ValueError, subprocess.SubprocessError):
            unavailable.append("PipeWire")
        try:
            if not self.drm.is_dir():
                raise FileNotFoundError
            for connector in sorted(self.drm.glob("card*-*"))[:128]:
                try:
                    if _read(connector / "status", 32).strip() != b"connected":
                        continue
                    try:
                        edid = _read(connector / "edid", 4097)
                    except OSError:
                        edid = b""
                    name = edid_name(edid)
                    capabilities = edid_capabilities(edid)
                    modes = None
                    route = re.fullmatch(r"(card(?:0|[1-9][0-9]{0,2}))-(.+)", connector.name)
                    if route and capabilities.valid and callable(self.mode_provider):
                        try:
                            report = self.mode_provider("/dev/dri/" + route[1], route[2])
                            if (isinstance(report, DrmModeReport) and report.card == "/dev/dri/" + route[1]
                                    and report.connector == route[2]
                                    and int(_read(connector / "connector_id", 32).strip()) == report.connector_id
                                    and _read(connector / "edid", 4097) == edid):
                                modes = report
                        except (OSError, ValueError, RuntimeError, ObservationError):
                            pass  # Mode discovery failure leaves legacy identities available.
                    # Recheck hotplug after reading the bounded EDID.
                    if _read(connector / "status", 32).strip() != b"connected":
                        continue
                    devices.append(ReportedOutput(f"drm/{connector.name}", name, "DRM", connector.name, "display_connector",
                                                  drm_capabilities=capabilities, drm_modes=modes))
                except OSError:
                    # A connector can disappear between the directory and status reads.
                    continue
        except OSError:
            unavailable.append("DRM")
        unique = {device.id: device for device in devices}
        inventory = OutputInventory(
            tuple(sorted(unique.values(), key=lambda d: (d.method, d.name or "", d.route)))[:128],
            tuple(unavailable), self.output_policies,
        )
        return self.apply_output_policies(inventory)

    def apply_output_policies(self, inventory: OutputInventory) -> OutputInventory:
        for policy in self.output_policies:
            try:
                updated = policy.apply_inventory(inventory)
                if not isinstance(updated, OutputInventory):
                    raise TypeError("output policy returned an invalid inventory")
                inventory = replace(updated, policies=self.output_policies)
            except Exception:
                disabled = tuple(
                    replace(device, selectable=False, selection_route=None, requires_policy=True)
                    if device.method in {"ALSA", "PipeWire"} else device
                    for device in inventory.devices
                )
                return replace(inventory, devices=disabled,
                               unavailable=tuple(dict.fromkeys((*inventory.unavailable, "output-policy"))))
        return inventory


def device_options(device: ReportedOutput) -> dict | None:
    """Translate only freshly reported routes into fixed, trusted mpv options."""
    if not device.selectable:
        return None
    if device.method in {"ALSA", "PipeWire"}:
        if device.requires_policy and device.selection_route is None:
            return None
        method = device.method.lower()
        route = device.selection_route or device.route
        if len(f"{method}/{route}") > 256:
            return None
        return {"ao": method, "audio-device": f"{method}/{route}", "audio-fallback-to-null": False}
    if device.method == "DRM":
        match = re.fullmatch(r"(card\d+)-([A-Za-z]+(?:-[A-Za-z]+)?-\d+)", device.route)
        if match:
            return {"drm-device": f"/dev/dri/{match[1]}", "drm-connector": match[2]}
    return None


def _alsa_hw_card(route: str) -> str | int | None:
    """Extract a hw/plughw CARD without evaluating ALSA config or opening PCM.

    ALSA accepts CARD,DEV,SUBDEV positionally, named, or as a configuration
    compound. An unresolved/default/complex CARD remains unknown and must
    fail closed when a protected physical ALSA card is present.
    """
    match = re.fullmatch(r"alsa/(?:plug)?hw(?::(.*))?", route)
    if not match or not match[1]:
        return None
    # Keep quotes: compound arguments have typed integers, whereas ordinary
    # arguments and quoted compound values use ALSA's string card lookup.
    lexer = shlex.shlex(match[1], posix=False, punctuation_chars="=,;{}")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    arguments = {}
    compound = bool(tokens and tokens[0] == "{")
    if compound:
        if tokens[-1] != "}":
            return None
        tokens = tokens[1:-1]
        while tokens:
            if tokens[0] in {",", ";"}:
                tokens.pop(0)
                continue
            key = tokens.pop(0)
            if key not in {"CARD", "DEV", "SUBDEV"}:
                return None
            if tokens and tokens[0] == "=":
                tokens.pop(0)
            if not tokens or tokens[0] in {"=", ",", ";", "{", "}"}:
                return None
            arguments[key] = tokens.pop(0)
    else:
        groups = [[]]
        for token in tokens:
            if token == ",":
                groups.append([])
            else:
                groups[-1].append(token)
        for position, group in enumerate(groups):
            if len(group) == 1 and position < 3:
                key, value = ("CARD", "DEV", "SUBDEV")[position], group[0]
            elif len(group) == 3 and group[1] == "=" and group[0] in {"CARD", "DEV", "SUBDEV"}:
                key, value = group[0], group[2]
            else:
                return None
            # ALSA's argument position advances for named arguments too;
            # later assignments override earlier values of the same key.
            arguments[key] = value
    card = arguments.get("CARD")
    if not card:
        return None
    quoted = card[0] in {'"', "'"}
    if quoted:
        if len(card) < 2 or card[-1] != card[0]:
            return None
        card = card[1:-1]
    elif compound and re.fullmatch(r"-?(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", card):
        digits = card.lstrip("-")
        base = 16 if digits.lower().startswith("0x") else 8 if digits.startswith("0") else 10
        return int(card, base)
    elif compound and re.match(r"[+-]?[0-9]", card):
        return None  # Unsupported numeric config values cannot identify a safe card.
    if re.fullmatch(r"[0-9]{1,2}", card):
        # snd_card_get_index uses sscanf("%i") for one/two-digit strings:
        # leading zero is octal, and 08/09 parse the initial zero.
        if card.startswith("0"):
            return int(card, 8) if card[-1] in "01234567" else 0
        return int(card)
    return card if re.fullmatch(r"[A-Za-z0-9_-]+", card) else None


def unsafe_output_route(options: dict, inventory: OutputInventory) -> bool:
    """Reject direct or unresolved routes to outputs managed by a local policy."""
    route = options.get("audio-device", "auto")
    if not isinstance(route, str) or len(route) > 256 or any(ord(char) < 32 for char in route):
        return True
    managed = [device for device in inventory.devices if device.requires_policy]
    for device in managed:
        prefix = device.method.lower()
        direct = f"{prefix}/{device.route}"
        selected = f"{prefix}/{device.selection_route}" if device.selection_route and device.selectable else None
        if route == direct and route != selected:
            return True
    raw_hw = re.match(r"alsa/(?:plug)?hw(?::|$)", route) is not None
    card = _alsa_hw_card(route) if raw_hw else None
    managed_alsa = [device for device in managed if device.method == "ALSA"]
    known_cards = {
        value for device in inventory.devices if device.method == "ALSA"
        for value in (_alsa_hw_card("alsa/" + device.route), device.alsa_card_index)
        if value is not None
    }
    if raw_hw and managed_alsa and (card is None or card not in known_cards or any(
        card == _alsa_hw_card("alsa/" + device.route)
        or (isinstance(card, int) and card == device.alsa_card_index)
        for device in managed_alsa
    )):
        return True
    if options.get("ao") == "alsa" and route in {"auto", "alsa/default"} and managed_alsa:
        if not any(device.selection_route == "default" and device.selectable for device in managed_alsa):
            return True
    for policy in inventory.policies:
        try:
            unsafe = policy.is_unsafe_route(options, inventory)
            if type(unsafe) is not bool or unsafe:
                return True
        except Exception:
            return True
    return False


def matching_output(options: dict, inventory: OutputInventory) -> ReportedOutput | None:
    """Match explicit routes only; a backend or GPU context never implies a monitor."""
    ao, route = options.get("ao"), options.get("audio-device")
    matches = []
    for output in inventory.devices:
        if output.method.lower() == ao and route in {
            f"{ao}/{output.route}",
            f"{ao}/{output.selection_route}" if output.selectable and output.selection_route else "",
        }:
            matches.append(output)
    return matches[0] if len(matches) == 1 else None


PROFILE_ROLES = {
    "linux.audio-alsa": ("Default audio route · ALSA", "backend"),
    "linux.audio-pipewire": ("Default audio route · PipeWire", "backend"),
    "linux.desktop": ("Automatic display · Desktop", "backend"),
    "linux.drm-direct": ("Automatic connector · DRM", "backend"),
    "linux.headless": ("No video output · Headless", "no_output"),
    "video.vaapi": ("Hardware decoding · VAAPI", "decoder"),
    "display.vaapi-drm": ("GPU render context · VAAPI / DRM", "render_context"),
}
