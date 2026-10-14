"""Read-only identity checks for the two already mapped, qualified rendering libraries."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from backend.profiles.native_identity import expected_native_identity_hashes


LIBRARY_PATTERNS = {
    "native_library_sha256": re.compile(r"libmpv\.so(?:\.\d+)*\Z"),
    "libplacebo_sha256": re.compile(r"libplacebo\.so(?:\.\d+)*\Z"),
}
_MAX_LIBRARY_BYTES = 256 * 1024 * 1024
_digest_cache: dict[tuple, str] = {}


def mapped_native_identities(*, maps_path: Path = Path("/proc/self/maps")) -> dict[str, str]:
    """No library loading, player creation, device open, shell or permissions change."""
    expected = expected_native_identity_hashes()
    with maps_path.open("rb") as source:
        raw = source.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("native mapping report is too large")
    mappings: dict[str, set[tuple[str, str, int]]] = {name: set() for name in LIBRARY_PATTERNS}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) != 6:
            continue
        path = fields[5]
        for name, pattern in LIBRARY_PATTERNS.items():
            if pattern.fullmatch(Path(path.removesuffix(" (deleted)")).name):
                if not path.startswith("/") or path.endswith(" (deleted)") or "\\" in path:
                    raise ValueError("native mapped path is unavailable")
                mappings[name].add((path, fields[3], int(fields[4])))
    result = {}
    for name in LIBRARY_PATTERNS:
        if len(mappings[name]) != 1:
            raise ValueError("native mapped library is missing or ambiguous")
        path, device, inode = next(iter(mappings[name]))
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            major, minor = (int(part, 16) for part in device.split(":"))
            if (not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _MAX_LIBRARY_BYTES
                    or before.st_ino != inode or (os.major(before.st_dev), os.minor(before.st_dev)) != (major, minor)):
                raise ValueError("native mapped library identity changed")
            identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            digest = _digest_cache.get(identity)
            if digest is None:
                hasher = hashlib.sha256()
                remaining = before.st_size
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError("native library became truncated")
                    hasher.update(block)
                    remaining -= len(block)
                if source.read(1):
                    raise ValueError("native library grew during verification")
                digest = hasher.hexdigest()
            after = os.fstat(source.fileno())
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("native library changed during verification")
            if digest != expected[name]:
                raise ValueError("native library does not match the qualified build")
            # Only successful exact-hash results are cached; metadata is rechecked.
            if len(_digest_cache) >= 8:
                _digest_cache.clear()
            _digest_cache[identity] = digest
            result[name] = digest
    return result
