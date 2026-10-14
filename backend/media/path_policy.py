from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

import ipaddress
import re


MEDIA_ROOTS_ENV = "MPV_WEBD_MEDIA_ROOTS"
MEDIA_DISCOVER_MOUNTS_ENV = "MPV_WEBD_MEDIA_DISCOVER_MOUNTS"
MEDIA_MOUNT_PARENT_ENV = "MPV_WEBD_MEDIA_MOUNT_PARENT"
NETWORK_PROTOCOLS_ENV = "MPV_WEBD_ALLOWED_MEDIA_PROTOCOLS"
NETWORK_HOSTS_ENV = "MPV_WEBD_ALLOWED_MEDIA_HOSTS"
SUPPORTED_NETWORK_PROTOCOLS = frozenset({"http", "https", "rtsp"})
MAX_MEDIA_DIRECTORY_RESULTS = 200
# Keep this allowlist explicit and independent of host MIME databases. The
# browser lists common audio/video containers and excludes playlists,
# subtitles, sidecars, images, and arbitrary documents.
MEDIA_FILE_SUFFIXES = frozenset(
    {
        ".3g2", ".3gp", ".aac", ".ac3", ".aif", ".aiff", ".amr", ".ape",
        ".asf", ".avi", ".caf", ".dff", ".dsf", ".dts", ".flac", ".flv",
        ".m2t", ".m2ts", ".m2v", ".m4a", ".m4b", ".m4v", ".mka", ".mkv",
        ".mov", ".mp2", ".mp3", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts",
        ".mxf", ".nut", ".oga", ".ogg", ".ogv", ".opus", ".ra", ".rm",
        ".rmvb", ".roq", ".ts", ".tta", ".vob", ".wav", ".webm", ".wma",
        ".wmv", ".wv",
    }
)
MEDIA_AUDIO_SUFFIXES = frozenset(
    {
        ".aac", ".ac3", ".aif", ".aiff", ".amr", ".ape", ".caf", ".dff",
        ".dsf", ".dts", ".flac", ".m4a", ".m4b", ".mka", ".mp2", ".mp3",
        ".oga", ".ogg", ".opus", ".ra", ".tta", ".wav", ".wma", ".wv",
    }
)
MEDIA_PREVIEW_SUFFIXES = frozenset({".jpeg", ".jpg", ".png", ".webp"})
MAX_MEDIA_PREVIEW_BYTES = 8 * 1024 * 1024
_COVER_ART_NAMES = ("cover", "folder", "front", "poster")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class MediaPathPolicyError(ValueError):
    """Raised when a requested file is outside the configured media roots."""


@dataclass(frozen=True, slots=True)
class MediaBrowserEntry:
    name: str
    path: str
    is_directory: bool
    size_bytes: int | None = None
    modified_at: str | None = None
    media_kind: str = "directory"
    has_preview: bool = False


@dataclass(frozen=True, slots=True)
class MediaBrowserListing:
    directory: str | None
    parent_directory: str | None
    items: tuple[MediaBrowserEntry, ...]
    truncated: bool
    total: int = 0
    total_files: int = 0
    offset: int = 0
    next_offset: int | None = None
    version: str = ""


@dataclass(frozen=True, slots=True)
class MediaRootAllowlist:
    roots: tuple[Path, ...]
    mounted_roots: frozenset[Path]
    discovery_parents: tuple[Path, ...]

    def __init__(
        self,
        roots: tuple[str | os.PathLike[str], ...] | list[str | os.PathLike[str]],
        *,
        mounted_roots: tuple[str | os.PathLike[str], ...]
        | list[str | os.PathLike[str]]
        | frozenset[str | os.PathLike[str]] = (),
        discovery_parents: tuple[str | os.PathLike[str], ...]
        | list[str | os.PathLike[str]] = (),
    ):
        canonical_roots: list[Path] = []
        for root in roots:
            if not isinstance(root, (str, os.PathLike)):
                raise MediaPathPolicyError("media roots must be filesystem paths")
            candidate = Path(root).expanduser()
            if not candidate.is_absolute():
                raise MediaPathPolicyError("media roots must be absolute paths")
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise MediaPathPolicyError("media root must resolve to an existing directory") from error
            if _has_hidden_component(canonical):
                raise MediaPathPolicyError("media root cannot contain hidden path components")
            if not canonical.is_dir():
                raise MediaPathPolicyError("media root must resolve to an existing directory")
            if canonical not in canonical_roots:
                canonical_roots.append(canonical)
        canonical_mounted: set[Path] = set()
        for root in mounted_roots:
            if not isinstance(root, (str, os.PathLike)):
                raise MediaPathPolicyError("mounted media roots must be filesystem paths")
            candidate = Path(root).expanduser()
            try:
                canonical = candidate.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as error:
                raise MediaPathPolicyError("mounted media root must resolve to an existing directory") from error
            if canonical not in canonical_roots:
                raise MediaPathPolicyError("mounted media root must be one of the configured roots")
            canonical_mounted.add(canonical)
        canonical_parents: list[Path] = []
        for parent in discovery_parents:
            if not isinstance(parent, (str, os.PathLike)):
                raise MediaPathPolicyError("media mount-discovery parents must be filesystem paths")
            candidate = Path(parent).expanduser()
            if not candidate.is_absolute():
                raise MediaPathPolicyError("media mount-discovery parents must be absolute paths")
            try:
                canonical = candidate.resolve(strict=False)
            except (OSError, RuntimeError, ValueError) as error:
                raise MediaPathPolicyError("media mount-discovery parent is invalid") from error
            if canonical not in canonical_parents:
                canonical_parents.append(canonical)
        object.__setattr__(self, "roots", tuple(canonical_roots))
        object.__setattr__(self, "mounted_roots", frozenset(canonical_mounted))
        object.__setattr__(self, "discovery_parents", tuple(canonical_parents))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "MediaRootAllowlist":
        values = os.environ if environ is None else environ
        raw_roots = values.get(MEDIA_ROOTS_ENV, "")
        if not isinstance(raw_roots, str):
            raise MediaPathPolicyError(f"{MEDIA_ROOTS_ENV} must be a path-separated string")
        if not raw_roots:
            return cls([])
        entries = raw_roots.split(os.pathsep)
        if any(not entry or entry.strip() != entry for entry in entries):
            raise MediaPathPolicyError(f"{MEDIA_ROOTS_ENV} contains an empty or whitespace-padded root")
        configured = cls(entries)
        if not _environment_bool(values, MEDIA_DISCOVER_MOUNTS_ENV, default=True):
            return configured
        parent_override = values.get(MEDIA_MOUNT_PARENT_ENV, "")
        mount_parents: list[Path] = []
        if parent_override:
            if not isinstance(parent_override, str) or parent_override.strip() != parent_override:
                raise MediaPathPolicyError(f"{MEDIA_MOUNT_PARENT_ENV} must be an absolute path")
            parent_path = Path(parent_override).expanduser()
            if not parent_path.is_absolute():
                raise MediaPathPolicyError(f"{MEDIA_MOUNT_PARENT_ENV} must be an absolute path")
            mount_parents.append(parent_path)
        else:
            for root in configured.roots:
                if root.parent in {Path("/home"), Path("/var/home")}:
                    mount_parents.extend((Path("/media") / root.name, Path("/run/media") / root.name))
        discovered: list[Path] = []
        for parent in mount_parents:
            discovered.extend(configured.discover_mounted_children(parent))
        return cls(
            [*configured.roots, *discovered],
            mounted_roots=discovered,
            discovery_parents=mount_parents,
        )

    @staticmethod
    def discover_mounted_children(parent: str | os.PathLike[str]) -> tuple[Path, ...]:
        """Return only real, immediate mountpoints below one user media directory."""

        candidate_parent = Path(parent).expanduser()
        try:
            canonical_parent = candidate_parent.resolve(strict=True)
            if not canonical_parent.is_dir():
                return ()
            children = list(canonical_parent.iterdir())
        except (OSError, RuntimeError, ValueError):
            # A missing /media/<user> parent is normal on machines without
            # removable storage; the configured home root remains usable.
            return ()
        discovered: list[Path] = []
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if not os.path.ismount(os.fspath(child)):
                    continue
                canonical = child.resolve(strict=True)
                if canonical != child or not canonical.is_dir():
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            if canonical not in discovered:
                discovered.append(canonical)
        return tuple(sorted(discovered, key=lambda path: (path.name.casefold(), path.name)))

    @staticmethod
    def root_display_name(path: str | os.PathLike[str]) -> str:
        """Use a stable human label without exposing the account name for /home roots."""

        candidate = Path(path)
        if candidate.parent.name == "home":
            return "home"
        return candidate.name

    def resolve(self, path: str | os.PathLike[str]) -> str:
        if not isinstance(path, (str, os.PathLike)):
            raise MediaPathPolicyError("media path must be a local filesystem path")
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise MediaPathPolicyError("media path is invalid")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise MediaPathPolicyError("media path must be absolute and contain no traversal segments")
        _reject_hidden_path(candidate)
        try:
            canonical = candidate.resolve(strict=True)
            metadata = canonical.stat()
        except (OSError, RuntimeError, ValueError) as error:
            raise MediaPathPolicyError("media path must resolve to an existing file") from error
        _reject_hidden_path(canonical)
        if not stat.S_ISREG(metadata.st_mode):
            raise MediaPathPolicyError("media path must resolve to a regular file")
        _require_media_suffix(candidate)
        _require_media_suffix(canonical)
        if not self.roots or not self._is_allowed(canonical):
            raise MediaPathPolicyError("media path is outside configured roots")
        return str(canonical)

    def resolve_directory(self, path: str | os.PathLike[str]) -> Path:
        if not isinstance(path, (str, os.PathLike)):
            raise MediaPathPolicyError("media directory must be a filesystem path")
        raw_path = os.fspath(path)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise MediaPathPolicyError("media directory is invalid")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise MediaPathPolicyError("media directory must be absolute and contain no traversal segments")
        _reject_hidden_path(candidate)
        try:
            canonical = candidate.resolve(strict=True)
            metadata = canonical.stat()
        except (OSError, RuntimeError, ValueError) as error:
            raise MediaPathPolicyError("media directory must resolve to an existing directory") from error
        _reject_hidden_path(canonical)
        if not stat.S_ISDIR(metadata.st_mode) or not self._is_allowed(canonical):
            raise MediaPathPolicyError("media directory is outside configured roots")
        return canonical

    def browse(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
        version: str | None = None,
        sort: str = "name",
    ) -> MediaBrowserListing:
        if not isinstance(search, str) or "\x00" in search or len(search) > 128:
            raise MediaPathPolicyError("media search is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_MEDIA_DIRECTORY_RESULTS:
            raise MediaPathPolicyError("media result limit is invalid")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or sort not in {"name", "name_desc", "modified"}:
            raise MediaPathPolicyError("media page is invalid")
        if not self.roots:
            raise MediaPathPolicyError("no media roots are configured")

        if directory is None:
            current_directory = None
            parent_directory = None
            available_roots, mounted_roots = self._effective_roots()
            candidates = iter(
                root for root in available_roots if self._root_is_available(root, mounted_roots)
            )
        else:
            current = self.resolve_directory(directory)
            current_directory = str(current)
            parent = current.parent
            parent_directory = (
                str(parent)
                if parent != current and self._is_allowed(parent)
                else None
            )
            try:
                candidates = current.iterdir()
            except OSError as error:
                raise MediaPathPolicyError("media directory cannot be read") from error

        search_folded = search.casefold()
        def entries():
            try:
                for candidate in candidates:
                    if candidate.name.startswith("."):
                        continue
                    try:
                        canonical = candidate.resolve(strict=True)
                        metadata = canonical.stat()
                    except (OSError, RuntimeError, ValueError):
                        # Ignore dangling, inaccessible, or concurrently removed entries.
                        continue
                    if (
                        _has_hidden_component(canonical)
                        or not self._is_allowed(canonical)
                    ):
                        continue
                    is_directory = stat.S_ISDIR(metadata.st_mode)
                    if not is_directory and (
                        not stat.S_ISREG(metadata.st_mode)
                        or not _has_media_suffix(candidate)
                        or not _has_media_suffix(canonical)
                    ):
                        continue
                    display_name = self.root_display_name(canonical) if directory is None else candidate.name
                    if search_folded and search_folded not in display_name.casefold():
                        continue
                    canonical_text = str(canonical)
                    media_kind = "directory" if is_directory else _media_kind(candidate)
                    yield MediaBrowserEntry(
                            name=display_name,
                            path=canonical_text,
                            is_directory=is_directory,
                            size_bytes=None if is_directory else int(metadata.st_size),
                            modified_at=_format_modified_at(metadata.st_mtime),
                            media_kind=media_kind,
                            has_preview=False,
                        )
            except OSError as error:
                raise MediaPathPolicyError("media directory cannot be read") from error

        from .directory_index import DirectoryChanged, window
        def directory_signature():
            try:
                metadata = current.stat()
            except (OSError, RuntimeError, ValueError) as error:
                raise DirectoryChanged("Directory changed during enumeration; reload.") from error
            return (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)
        available_roots, mounted_roots = self._effective_roots()
        signature = self._roots_signature(available_roots, mounted_roots) if directory is None else directory_signature()
        rows,total,total_files,snapshot_version = window((self.roots,current_directory,search.casefold(),available_roots),signature,entries,offset=offset,limit=limit,version=version,sort=sort)
        if directory is None:
            fresh_roots, fresh_mounted = self._effective_roots()
            if signature != self._roots_signature(fresh_roots, fresh_mounted):
                raise DirectoryChanged("Media roots changed during enumeration; reload.")
        if directory is not None and signature != directory_signature():
            raise DirectoryChanged("Directory changed during enumeration; reload.")
        from dataclasses import replace
        page = []
        for row in rows:
            entry = MediaBrowserEntry(row[0],row[1],bool(row[2]),row[3],row[4],row[5])
            # Revalidate page paths: cached enumeration never grants access.
            try:
                canonical = self.resolve_directory(entry.path) if entry.is_directory else self.resolve(entry.path)
            except MediaPathPolicyError as error:
                if directory is None:
                    raise DirectoryChanged("Media roots changed during enumeration; reload.") from error
                raise
            page.append(replace(entry,has_preview=not entry.is_directory and self.preview_for(str(canonical)) is not None))
        return MediaBrowserListing(directory=current_directory,parent_directory=parent_directory,items=tuple(page),truncated=False,total=total,total_files=total_files,offset=offset,next_offset=offset+limit if offset+limit<total else None,version=snapshot_version)

    def preview_for(self, path: str | os.PathLike[str]) -> Path | None:
        """Return a bounded conventional cover-art sibling for one media file."""

        canonical = Path(self.resolve(path))
        candidates = [
            canonical.with_name(f"{canonical.stem}{suffix}")
            for suffix in sorted(MEDIA_PREVIEW_SUFFIXES)
        ]
        candidates.extend(
            canonical.parent / f"{name}{suffix}"
            for name in _COVER_ART_NAMES
            for suffix in sorted(MEDIA_PREVIEW_SUFFIXES)
        )
        seen: set[Path] = set()
        for candidate in candidates:
            if candidate in seen or candidate.name.startswith("."):
                continue
            seen.add(candidate)
            try:
                preview = candidate.resolve(strict=True)
                metadata = preview.stat()
            except (OSError, RuntimeError, ValueError):
                continue
            if (
                preview.suffix.casefold() not in MEDIA_PREVIEW_SUFFIXES
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_MEDIA_PREVIEW_BYTES
                or _has_hidden_component(preview)
                or not self._is_allowed(preview)
            ):
                continue
            return preview
        return None

    def _is_allowed(self, path: Path) -> bool:
        roots, mounted_roots = self._effective_roots()
        return any(
            self._root_is_available(root, mounted_roots)
            and (path == root or root in path.parents)
            for root in roots
        )

    def _effective_roots(self) -> tuple[tuple[Path, ...], frozenset[Path]]:
        roots = list(self.roots)
        mounted_roots = set(self.mounted_roots)
        for parent in self.discovery_parents:
            for root in self.discover_mounted_children(parent):
                if root not in roots:
                    roots.append(root)
                mounted_roots.add(root)
        return tuple(roots), frozenset(mounted_roots)

    @staticmethod
    def _root_is_available(root: Path, mounted_roots: frozenset[Path]) -> bool:
        try:
            return (
                (root not in mounted_roots or os.path.ismount(os.fspath(root)))
                and root.is_dir()
                and os.access(root, os.R_OK | os.X_OK)
            )
        except (OSError, RuntimeError, ValueError):
            return False

    @classmethod
    def _roots_signature(cls, roots: tuple[Path, ...], mounted_roots: frozenset[Path]) -> tuple:
        """Sample only currently usable roots; access still requires canonical revalidation."""
        result = []
        for root in roots:
            if not cls._root_is_available(root, mounted_roots):
                continue
            try:
                metadata = root.stat()
            except (OSError, RuntimeError, ValueError):
                continue
            result.append((str(root), metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class MediaSourcePolicy:
    """Apply local-root policy and an explicit, bounded network URL policy."""

    local_files: MediaRootAllowlist
    allowed_protocols: frozenset[str]
    allowed_hosts: frozenset[str] | None

    def __init__(
        self,
        local_files: MediaRootAllowlist,
        *,
        allowed_protocols: Iterable[str] = (),
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(local_files, MediaRootAllowlist):
            raise MediaPathPolicyError("local_files must be a MediaRootAllowlist")
        protocols = frozenset(_normalize_protocol(value) for value in allowed_protocols)
        hosts = (
            None
            if allowed_hosts is None
            else frozenset(_normalize_host(value) for value in allowed_hosts)
        )
        object.__setattr__(self, "local_files", local_files)
        object.__setattr__(self, "allowed_protocols", protocols)
        object.__setattr__(self, "allowed_hosts", hosts)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "MediaSourcePolicy":
        values = os.environ if environ is None else environ
        raw_protocols = _environment_csv(values, NETWORK_PROTOCOLS_ENV, default=())
        protocols = tuple(_normalize_protocol(value) for value in raw_protocols)
        if len(protocols) != len(set(protocols)):
            raise MediaPathPolicyError(f"{NETWORK_PROTOCOLS_ENV} contains duplicate protocols")

        if NETWORK_HOSTS_ENV not in values:
            hosts = None
        else:
            raw_hosts = _environment_csv(values, NETWORK_HOSTS_ENV, default=())
            hosts = tuple(_normalize_host(value) for value in raw_hosts)
            if len(hosts) != len(set(hosts)):
                raise MediaPathPolicyError(f"{NETWORK_HOSTS_ENV} contains duplicate hosts")
        return cls(
            MediaRootAllowlist.from_environment(values),
            allowed_protocols=protocols,
            allowed_hosts=hosts,
        )

    def resolve(self, source: str | os.PathLike[str]) -> str:
        source_text = _source_text(source)
        if any(ord(character) < 32 or ord(character) == 127 for character in source_text):
            raise MediaPathPolicyError("media source contains control characters")
        if len(source_text) > 4096:
            raise MediaPathPolicyError("media source is too long")
        try:
            parsed = urlsplit(source_text)
        except ValueError as error:
            raise MediaPathPolicyError("media URL is malformed") from error
        if not parsed.scheme:
            return self.local_files.resolve(source_text)
        return self._resolve_network(source_text, parsed)

    def resolve_local(self, path: str | os.PathLike[str]) -> str:
        source_text = _source_text(path)
        try:
            parsed = urlsplit(source_text)
        except ValueError as error:
            raise MediaPathPolicyError("media path is invalid") from error
        if parsed.scheme:
            raise MediaPathPolicyError("network sources are not valid local media paths")
        return self.local_files.resolve(source_text)

    def _resolve_network(self, source: str, parsed) -> str:
        scheme = parsed.scheme.casefold()
        if scheme not in SUPPORTED_NETWORK_PROTOCOLS or scheme not in self.allowed_protocols:
            raise MediaPathPolicyError("media URL protocol is not explicitly allowed")
        if not parsed.netloc or parsed.hostname is None:
            raise MediaPathPolicyError("media URL must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise MediaPathPolicyError("media URL credentials are not allowed")
        if "\\" in source:
            raise MediaPathPolicyError("media URL contains an invalid path separator")
        try:
            port = parsed.port
        except ValueError as error:
            raise MediaPathPolicyError("media URL port is invalid") from error
        host = _normalize_host(parsed.hostname)
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise MediaPathPolicyError("media URL host is not allowed")
        display_host = f"[{host}]" if ":" in host else host
        netloc = display_host if port is None else f"{display_host}:{port}"
        return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _source_text(source: str | os.PathLike[str]) -> str:
    if not isinstance(source, (str, os.PathLike)):
        raise MediaPathPolicyError("media source must be a string or filesystem path")
    raw = os.fspath(source)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise MediaPathPolicyError("media source is invalid")
    return raw


def _normalize_protocol(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise MediaPathPolicyError("network protocols must be non-empty names without whitespace")
    normalized = value.casefold()
    if normalized not in SUPPORTED_NETWORK_PROTOCOLS:
        raise MediaPathPolicyError("network protocol is not in the supported URL catalog")
    return normalized


def _has_hidden_component(path: Path) -> bool:
    return any(
        part.startswith(".")
        for part in path.parts
        if part not in {".", "..", path.anchor}
    )


def _reject_hidden_path(path: Path) -> None:
    if _has_hidden_component(path):
        raise MediaPathPolicyError("hidden media paths are not allowed")


def _has_media_suffix(path: Path) -> bool:
    return path.suffix.casefold() in MEDIA_FILE_SUFFIXES


def _media_kind(path: Path) -> str:
    return "audio" if path.suffix.casefold() in MEDIA_AUDIO_SUFFIXES else "video"


def _format_modified_at(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _require_media_suffix(path: Path) -> None:
    if not _has_media_suffix(path):
        raise MediaPathPolicyError("media file type is not allowed")


def _normalize_host(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "*" in value
        or "%" in value
    ):
        raise MediaPathPolicyError("network host entries must be exact host names or IP addresses")
    candidate = value.rstrip(".").casefold()
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise MediaPathPolicyError("network host name is invalid") from error
    if (
        len(ascii_host) > 253
        or not ascii_host
        or any(not _HOST_LABEL.fullmatch(label) for label in ascii_host.split("."))
    ):
        raise MediaPathPolicyError(
            "network host entries must be exact host names or IP addresses"
        )
    return ascii_host


def _environment_csv(
    values: Mapping[str, str], name: str, *, default: tuple[str, ...]
) -> tuple[str, ...]:
    raw = values.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw:
        raise MediaPathPolicyError(f"{name} must be a non-empty comma-separated list")
    entries = tuple(raw.split(","))
    if any(not entry or entry.strip() != entry for entry in entries):
        raise MediaPathPolicyError(f"{name} contains an empty or whitespace-padded entry")
    return entries


def _environment_bool(values: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise MediaPathPolicyError(f"{name} must be true or false")
    normalized = raw.casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise MediaPathPolicyError(f"{name} must be true or false")
