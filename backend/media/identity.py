from __future__ import annotations

import json
import math
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.db import MigrationRunner


class MediaIdentityError(ValueError):
    """Raised when a path cannot identify a readable local regular file."""


@dataclass(frozen=True, slots=True)
class MediaIdentity:
    canonical_path: str
    size_bytes: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> "MediaIdentity":
        if not isinstance(path, (str, os.PathLike)):
            raise MediaIdentityError("media path must be a local filesystem path")
        try:
            canonical = Path(path).expanduser().resolve(strict=True)
            stat = canonical.stat()
        except (OSError, RuntimeError, ValueError) as error:
            raise MediaIdentityError("media path must resolve to an existing local file") from error
        if not canonical.is_file():
            raise MediaIdentityError("media path must resolve to a regular file")
        return cls(
            canonical_path=str(canonical),
            size_bytes=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
        )


@dataclass(frozen=True, slots=True)
class MediaRecord:
    id: int
    identity: MediaIdentity
    probe_data: dict[str, Any] | None
    probed_at: str | None
    duration_s: float | None
    container: str | None
    video_codec: str | None
    audio_codec: str | None
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    hdr_kind: str | None

    @property
    def probe_cache_valid(self) -> bool:
        return self.probe_data is not None and self.probed_at is not None


class MediaIdentityStore:
    """Persist canonical local-file identity and invalidate stale probe data."""

    _SELECT = (
        "SELECT id,canonical_path,size_bytes,mtime_ns,duration_s,container,video_codec,"
        "audio_codec,width,height,fps_num,fps_den,hdr_kind,probe_json,probed_at "
        "FROM media WHERE canonical_path=?"
    )

    def __init__(
        self,
        migrations: MigrationRunner,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(migrations, MigrationRunner):
            raise TypeError("migrations must be a MigrationRunner")
        self.migrations = migrations
        self._clock = clock or _utc_now

    def identify(self, path: str | os.PathLike[str]) -> MediaRecord:
        identity = MediaIdentity.from_path(path)
        now = self._clock()
        connection = self.migrations.connect()
        try:
            with connection:
                row = connection.execute(self._SELECT, (identity.canonical_path,)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO media(canonical_path,size_bytes,mtime_ns,created_at,updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (
                            identity.canonical_path,
                            identity.size_bytes,
                            identity.mtime_ns,
                            now,
                            now,
                        ),
                    )
                elif (row[2], row[3]) != (identity.size_bytes, identity.mtime_ns):
                    connection.execute(
                        "UPDATE media SET size_bytes=?,mtime_ns=?,duration_s=NULL,container=NULL,"
                        "video_codec=NULL,audio_codec=NULL,width=NULL,height=NULL,fps_num=NULL,"
                        "fps_den=NULL,hdr_kind=NULL,probe_json=NULL,probed_at=NULL,updated_at=? "
                        "WHERE id=?",
                        (
                            identity.size_bytes,
                            identity.mtime_ns,
                            now,
                            row[0],
                        ),
                    )
                current = connection.execute(self._SELECT, (identity.canonical_path,)).fetchone()
                if current is None:
                    raise sqlite3.IntegrityError("media identity was not persisted")
            return _record(current)
        finally:
            connection.close()

    def cache_probe(
        self,
        identity: MediaIdentity,
        probe_data: Mapping[str, Any],
        *,
        probed_at: str | None = None,
    ) -> bool:
        """Cache probe data only if the file and stored fingerprint still match."""

        if not isinstance(identity, MediaIdentity):
            raise TypeError("identity must be a MediaIdentity")
        if not isinstance(probe_data, Mapping) or any(
            not isinstance(key, str) for key in probe_data
        ):
            raise ValueError("probe data must be a JSON object with string keys")
        try:
            encoded = json.dumps(
                dict(probe_data),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("probe data must contain only finite JSON values") from error
        timestamp = probed_at if probed_at is not None else self._clock()
        if not isinstance(timestamp, str) or not timestamp:
            raise ValueError("probed_at must be a non-empty timestamp string")

        try:
            current_identity = MediaIdentity.from_path(identity.canonical_path)
        except MediaIdentityError:
            return False
        if current_identity != identity:
            self.identify(identity.canonical_path)
            return False

        fields = _probe_columns(probe_data)
        connection = self.migrations.connect()
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE media SET duration_s=?,container=?,video_codec=?,audio_codec=?,"
                    "width=?,height=?,fps_num=?,fps_den=?,hdr_kind=?,probe_json=?,probed_at=?,"
                    "updated_at=? WHERE canonical_path=? AND size_bytes=? AND mtime_ns=?",
                    (
                        fields["duration_s"],
                        fields["container"],
                        fields["video_codec"],
                        fields["audio_codec"],
                        fields["width"],
                        fields["height"],
                        fields["fps_num"],
                        fields["fps_den"],
                        fields["hdr_kind"],
                        encoded,
                        timestamp,
                        timestamp,
                        identity.canonical_path,
                        identity.size_bytes,
                        identity.mtime_ns,
                    ),
                )
                return cursor.rowcount == 1
        finally:
            connection.close()

    def get_probe(self, path: str | os.PathLike[str]) -> dict[str, Any] | None:
        """Return cached probe data only after refreshing the file fingerprint."""

        return self.identify(path).probe_data


def _record(row: sqlite3.Row | tuple[Any, ...]) -> MediaRecord:
    probe_data = json.loads(row[13]) if row[13] is not None else None
    return MediaRecord(
        id=int(row[0]),
        identity=MediaIdentity(str(row[1]), int(row[2]), int(row[3])),
        duration_s=row[4],
        container=row[5],
        video_codec=row[6],
        audio_codec=row[7],
        width=row[8],
        height=row[9],
        fps_num=row[10],
        fps_den=row[11],
        hdr_kind=row[12],
        probe_data=probe_data,
        probed_at=row[14],
    )


def _probe_columns(probe_data: Mapping[str, Any]) -> dict[str, Any]:
    columns: dict[str, Any] = {}
    duration = probe_data.get("duration_s")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError("duration_s must be a finite non-negative number")
        duration = float(duration)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration_s must be a finite non-negative number")
    columns["duration_s"] = duration

    for name in ("container", "video_codec", "audio_codec", "hdr_kind"):
        value = probe_data.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be a non-empty string or null")
        columns[name] = value

    for name in ("width", "height", "fps_num", "fps_den"):
        value = probe_data.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer or null")
        columns[name] = value
    return columns


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
