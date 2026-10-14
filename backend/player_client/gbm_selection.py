"""Generic player child environment policy.

The public baseline does not select distribution- or device-specific graphics
loader stacks. Such profiles belong in a separately maintained local adapter.
"""
from __future__ import annotations

from typing import Mapping


PROFILE_ENV = "MPV_WEBD_PLAYER_GBM_PROFILE"


class GBMSelectionError(ValueError):
    """A private hardware profile was requested from the generic baseline."""


def child_environment(inherited: Mapping[str, str]) -> dict[str, str]:
    """Preserve the caller environment; fail closed on a local-only profile."""
    environment = dict(inherited)
    if PROFILE_ENV in environment:
        raise GBMSelectionError(
            "device-specific graphics profiles require a local integration"
        )
    return environment
