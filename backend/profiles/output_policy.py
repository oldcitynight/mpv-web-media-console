"""Optional local output-safety adapter boundary.

Public builds provide generic device discovery and route validation. A local
installation can register a separately maintained ``mpv_web_suite.output_safety``
entry point for routes that require host-specific checks. No adapter code or
machine identity is bundled with this project.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


OUTPUT_SAFETY_ENTRY_POINT_GROUP = "mpv_web_suite.output_safety"


@dataclass(frozen=True)
class OutputDiscoveryContext:
    """Read-only inputs made available to an optional local adapter."""

    proc: Path
    drm: Path
    procfs: Path
    environ: Mapping[str, str]
    command: Callable[[list[str]], str]


class OutputSafetyAdapter(Protocol):
    def apply_inventory(self, inventory: Any) -> Any: ...
    def is_unsafe_route(self, options: Mapping[str, Any], inventory: Any) -> bool: ...


def load_output_safety_adapters(context: OutputDiscoveryContext) -> tuple[OutputSafetyAdapter, ...]:
    """Load installed adapters deterministically; import errors fail closed."""

    discovered = entry_points().select(group=OUTPUT_SAFETY_ENTRY_POINT_GROUP)
    adapters: list[OutputSafetyAdapter] = []
    for entry in sorted(discovered, key=lambda item: item.name):
        adapter = entry.load()(context)
        if not callable(getattr(adapter, "apply_inventory", None)) or not callable(
            getattr(adapter, "is_unsafe_route", None)
        ):
            raise TypeError(f"output safety adapter {entry.name!r} has an invalid interface")
        adapters.append(adapter)
    return tuple(adapters)
