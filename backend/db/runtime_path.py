"""Select runtime state without silently switching away from legacy data."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


RUNTIME_DB_ENV = "MPV_WEBD_RUNTIME_DB"
LEGACY_RUNTIME_DB = Path("/var/lib/mpv-webd/runtime.sqlite")


def runtime_database_path(environ: Mapping[str, str] | None = None) -> Path:
    """Explicit paths win; new installs use user state, with no data migration."""
    values = os.environ if environ is None else environ
    if RUNTIME_DB_ENV in values:
        if not values[RUNTIME_DB_ENV]:
            raise ValueError(f"{RUNTIME_DB_ENV} must not be empty")
        return Path(values[RUNTIME_DB_ENV])

    # lstat also detects dangling symlinks. Do not interpret inaccessible legacy
    # state as a new installation, or abandon surviving SQLite sidecar data.
    for suffix in ("", "-wal", "-journal", "-shm"):
        legacy = Path(str(LEGACY_RUNTIME_DB) + suffix)
        try:
            legacy.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(
                f"Cannot check legacy runtime data; set {RUNTIME_DB_ENV} explicitly."
            ) from error
        raise ValueError(
            f"Legacy runtime data exists at {LEGACY_RUNTIME_DB}; set {RUNTIME_DB_ENV} "
            "explicitly to keep using it or select another database. No data was moved."
        )

    state_home = values.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path(values.get("HOME") or Path.home()) / ".local/state"
    if not base.is_absolute():
        raise ValueError("The runtime state home must be an absolute path")
    return base / "mpv-webd/runtime.sqlite"
