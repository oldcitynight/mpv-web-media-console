"""Protected per-install allowlist for mapped native rendering libraries."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping


POLICY_PATH_ENV = "MPV_WEBD_NATIVE_IDENTITY_POLICY"
DEFAULT_POLICY_PATH = Path("/etc/mpv-web-suite/native-library-identities.json")
_MAX_POLICY_BYTES = 8192
_HASH_KEYS = ("native_library_sha256", "libplacebo_sha256")


class NativeIdentityPolicyError(ValueError):
    """The install-owned native-library allowlist is absent or invalid."""


def parse_native_identity_policy(raw: bytes) -> dict[str, str]:
    if len(raw) > _MAX_POLICY_BYTES:
        raise NativeIdentityPolicyError("native identity policy is too large")
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError, TypeError) as error:
        raise NativeIdentityPolicyError("native identity policy is invalid") from error
    if (not isinstance(document, dict)
            or set(document) != {"schema_version", *_HASH_KEYS}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1):
        raise NativeIdentityPolicyError("native identity policy schema is invalid")
    result = {key: document[key] for key in _HASH_KEYS}
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in result.values()):
        raise NativeIdentityPolicyError("native identity policy hashes are invalid")
    return result


def expected_native_identity_hashes(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if environ is None else environ
    raw_path = values.get(POLICY_PATH_ENV)
    path = Path(raw_path) if raw_path is not None else DEFAULT_POLICY_PATH
    if not path.is_absolute() or path.is_symlink():
        raise NativeIdentityPolicyError("native identity policy path is unavailable")
    trusted_owners = {0, os.geteuid()}
    try:
        for directory in (path.parent, *path.parent.parents):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in trusted_owners
                    or info.st_mode & 0o022):
                raise NativeIdentityPolicyError("native identity policy directory is not install-owned")
            if directory == Path("/"):
                break
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid not in trusted_owners
                    or info.st_mode & 0o022 or info.st_size > _MAX_POLICY_BYTES):
                raise NativeIdentityPolicyError("native identity policy is not a protected regular file")
            raw = stream.read(_MAX_POLICY_BYTES + 1)
    except NativeIdentityPolicyError:
        raise
    except OSError as error:
        raise NativeIdentityPolicyError("native identity policy is unavailable") from error
    return parse_native_identity_policy(raw)


def matches_expected_native_identities(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    try:
        expected = expected_native_identity_hashes()
    except NativeIdentityPolicyError:
        return False
    return all(record.get(key) == value for key, value in expected.items())
