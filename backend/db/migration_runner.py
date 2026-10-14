from __future__ import annotations

import datetime as _datetime
import hashlib
import math
import os
import re
import sqlite3
import stat
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union


PathLike = Union[str, Path]
_MIGRATION_NAME = re.compile(r"^(?P<version>\d+)_(?P<name>[A-Za-z0-9][A-Za-z0-9_-]*)\.sql$")
_DEFAULT_BUSY_TIMEOUT_SECONDS = 0.25


class MigrationError(RuntimeError):
    """Raised when the forward-only migration history is not safe to apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    checksum: str
    script: str


class MigrationRunner:
    """Apply numbered SQL migrations with checksums and per-migration transactions."""

    def __init__(
        self,
        db_path: PathLike,
        migrations_dir: PathLike | None = None,
        backup_dir: PathLike | None = None,
        *,
        busy_timeout_seconds: float = _DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(busy_timeout_seconds)
            or not 0 < busy_timeout_seconds <= 30
        ):
            raise ValueError("busy_timeout_seconds must be finite and between 0 and 30")
        self.db_path = Path(db_path)
        self.migrations_dir = Path(migrations_dir) if migrations_dir is not None else runtime_migrations_directory()
        self.backup_dir = Path(backup_dir or self.db_path.parent / "backups")
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self.last_backup_path: Path | None = None
        self.last_applied: tuple[Migration, ...] = ()

    def connect(self) -> sqlite3.Connection:
        """Open a runtime connection with foreign-key enforcement enabled."""

        if str(self.db_path) == ":memory:":
            connection = sqlite3.connect(
                ":memory:", timeout=self.busy_timeout_seconds
            )
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_sqlite_file(self.db_path, create=True)
        connection = sqlite3.connect(
            self.db_path, timeout=self.busy_timeout_seconds
        )
        try:
            _restrict_sqlite_file(self.db_path, create=False)
            _restrict_existing_sidecars(self.db_path)
            connection.execute("PRAGMA foreign_keys=ON")
        except Exception:
            connection.close()
            raise
        return connection

    def discover(self) -> tuple[Migration, ...]:
        """Return the numbered SQL migrations in deterministic order."""

        if not self.migrations_dir.is_dir():
            raise MigrationError(f"migration directory does not exist: {self.migrations_dir}")

        migrations: list[Migration] = []
        for path in sorted(self.migrations_dir.glob("*.sql")):
            match = _MIGRATION_NAME.match(path.name)
            if path.name[:1].isdigit() and match is None:
                raise MigrationError(f"invalid migration filename: {path.name}")
            if match is None:
                continue
            raw_script = path.read_bytes()
            try:
                script = raw_script.decode("utf-8")
            except UnicodeDecodeError as error:
                raise MigrationError(f"migration is not valid UTF-8: {path.name}") from error
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=match.group("name"),
                    path=path,
                    checksum=hashlib.sha256(raw_script).hexdigest(),
                    script=script,
                )
            )

        migrations.sort(key=lambda migration: migration.version)
        versions = [migration.version for migration in migrations]
        if not versions:
            raise MigrationError("migration directory contains no numbered migrations")
        if len(versions) != len(set(versions)):
            raise MigrationError("migration versions must be unique")
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError("migration versions must be contiguous starting at 001")
        return tuple(migrations)

    def apply(self) -> list[Migration]:
        """Apply all pending migrations and return the migrations applied this time."""

        migrations = self.discover()
        database_existed = self.db_path.exists() and self.db_path.stat().st_size > 0
        connection = self.connect()
        applied_now: list[Migration] = []
        self.last_backup_path = None
        self.last_applied = ()

        try:
            applied = self._read_applied(connection)
            by_version = {migration.version: migration for migration in migrations}
            for version, (name, checksum) in applied.items():
                migration = by_version.get(version)
                if migration is None:
                    raise MigrationError(
                        f"applied migration {version:03d} is missing from {self.migrations_dir}"
                    )
                if migration.name != name or migration.checksum != checksum:
                    raise MigrationError(
                        f"checksum or name mismatch for applied migration {version:03d}"
                    )

            applied_versions = sorted(applied)
            expected_prefix = [migration.version for migration in migrations[: len(applied_versions)]]
            if applied_versions != expected_prefix:
                raise MigrationError("applied migration history is not a contiguous prefix")

            self._validate_database(connection, migrations[: len(applied_versions)])
            pending = [migration for migration in migrations if migration.version not in applied]
            if not pending:
                return []

            if database_existed:
                self.last_backup_path = self._create_backup(connection)

            for migration in pending:
                self._apply_one(connection, migration)
                applied_now.append(migration)

            self._validate_database(connection, migrations)
            self.last_applied = tuple(applied_now)
            return applied_now
        finally:
            connection.close()

    # A descriptive alias keeps callers free to use either migration vocabulary.
    migrate = apply

    def _read_applied(self, connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
        if not self._table_exists(connection, "schema_migrations"):
            if _schema_objects(connection):
                raise MigrationError("database contains schema objects but migration history is missing")
            return {}
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info('schema_migrations')")
        }
        required_columns = {"version", "name", "checksum", "applied_at"}
        if not required_columns <= columns.keys() or columns["version"][5] != 1:
            raise MigrationError("schema_migrations has an incompatible shape")
        rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        if not rows:
            raise MigrationError("schema_migrations exists without a committed migration record")
        result: dict[int, tuple[str, str]] = {}
        for version, name, checksum in rows:
            try:
                normalized_version = int(version)
            except (TypeError, ValueError) as error:
                raise MigrationError("schema_migrations contains an invalid version") from error
            if (
                normalized_version <= 0
                or normalized_version in result
                or not isinstance(name, str)
                or not name
                or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            ):
                raise MigrationError("schema_migrations contains an invalid history record")
            result[normalized_version] = (name, checksum)
        return result

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        return row is not None

    def _apply_one(self, connection: sqlite3.Connection, migration: Migration) -> None:
        connection.execute("BEGIN")
        try:
            for statement in _iter_sql_statements(migration.script):
                connection.execute(statement)
            if not self._table_exists(connection, "schema_migrations"):
                raise MigrationError(
                    f"migration {migration.path.name} did not create schema_migrations"
                )
            connection.execute(
                "INSERT INTO schema_migrations(version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, _utc_now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _create_backup(self, source: sqlite3.Connection) -> Path:
        _restrict_sqlite_file(self.db_path, create=False)
        if self.backup_dir.is_symlink():
            raise MigrationError("runtime migration backup directory must not be a symbolic link")
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.backup_dir.is_dir():
            raise MigrationError("runtime migration backup path is not a directory")
        os.chmod(self.backup_dir, 0o700, follow_symlinks=False)
        timestamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backup_dir / f"{self.db_path.name}.{timestamp}.bak"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        os.close(descriptor)
        backup: sqlite3.Connection | None = None
        try:
            backup = sqlite3.connect(destination, timeout=self.busy_timeout_seconds)
            source.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise MigrationError("runtime migration backup failed its integrity check")
            if backup.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("runtime migration backup contains foreign-key violations")
            backup.close()
            backup = None
            _restrict_sqlite_file(destination, create=False)
        except Exception:
            if backup is not None:
                backup.close()
            if destination.is_file() and not destination.is_symlink():
                destination.unlink()
            raise
        return destination

    def _validate_database(
        self,
        connection: sqlite3.Connection,
        applied_migrations: tuple[Migration, ...] | list[Migration],
    ) -> None:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("runtime SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("runtime SQLite contains foreign-key violations")
        if not applied_migrations:
            return

        expected = _expected_schema_signature(applied_migrations)
        actual = _schema_signature(connection)
        if actual != expected:
            raise MigrationError("runtime SQLite schema does not match its applied migration history")


def _restrict_sqlite_file(path: Path, *, create: bool) -> None:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MigrationError(f"SQLite database is not a regular file: {path}")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _restrict_existing_sidecars(db_path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        try:
            _restrict_sqlite_file(sidecar, create=False)
        except FileNotFoundError:
            continue


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        "SELECT type,name,tbl_name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    return tuple((str(kind), str(name), str(table)) for kind, name, table in rows)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_signature(connection: sqlite3.Connection) -> tuple[object, ...]:
    """Describe persistent schema objects without comparing SQLite SQL formatting."""
    objects = _schema_objects(connection)
    tables: list[tuple[object, ...]] = []
    for kind, name, _table in objects:
        if kind != "table":
            continue
        quoted_table = _quoted_identifier(name)
        columns = tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({quoted_table})")
        )
        foreign_keys = tuple(
            (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]), str(row[7]))
            for row in connection.execute(f"PRAGMA foreign_key_list({quoted_table})")
        )
        indexes: list[tuple[object, ...]] = []
        for index in connection.execute(f"PRAGMA index_list({quoted_table})"):
            index_name = str(index[1])
            quoted_index = _quoted_identifier(index_name)
            index_columns = tuple(
                (row[1], row[2], row[3], row[4], row[5])
                for row in connection.execute(f"PRAGMA index_xinfo({quoted_index})")
            )
            indexes.append(
                (index_name, int(index[2]), str(index[3]), int(index[4]), index_columns)
            )
        tables.append((name, columns, foreign_keys, tuple(sorted(indexes))))

    non_table_sql: list[tuple[str, str]] = []
    for kind, name, _table in objects:
        if kind in {"view", "trigger"}:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
            ).fetchone()[0]
            normalized = " ".join(str(sql).split()).casefold()
            non_table_sql.append((f"{kind}:{name}", normalized))
    return (objects, tuple(sorted(tables)), tuple(sorted(non_table_sql)))


def _expected_schema_signature(migrations: tuple[Migration, ...]) -> tuple[object, ...]:
    expected = sqlite3.connect(":memory:")
    try:
        expected.execute("PRAGMA foreign_keys=ON")
        for migration in migrations:
            for statement in _iter_sql_statements(migration.script):
                expected.execute(statement)
        return _schema_signature(expected)
    except (OSError, sqlite3.Error, MigrationError) as error:
        raise MigrationError("cannot reconstruct schema from the applied migration history") from error
    finally:
        expected.close()


def runtime_migrations_directory() -> Path:
    """Locate source-tree or installed runtime SQL migration data."""

    source_directory = Path(__file__).resolve().parents[2] / "runtime_sql"
    if source_directory.is_dir():
        return source_directory
    installed_directory = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "mpv-web-suite"
        / "runtime_sql"
    )
    if installed_directory.is_dir():
        return installed_directory
    raise MigrationError("Installed runtime SQLite migrations are missing")


def _iter_sql_statements(script: str) -> Iterator[str]:
    """Yield complete SQLite statements without forfeiting the caller's transaction."""

    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise MigrationError("migration contains an unterminated SQL statement")


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")
