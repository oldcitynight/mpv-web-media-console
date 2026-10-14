"""Load non-secret service settings from an optional env file and process env."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Mapping


CONFIG_FILE_ENV = "MPV_WEBD_CONFIG_FILE"
MAX_CONFIG_BYTES = 64 * 1024
_SETTING_NAME = re.compile(r"MPV_WEBD_[A-Z0-9_]+\Z")
_SENSITIVE_NAME = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY|API_KEY)", re.I)


class ConfigurationError(ValueError):
    """Raised when the service configuration file is malformed or unsafe."""


def _read_env_file(path: Path, *, optional: bool) -> dict[str, str]:
    if not path.is_absolute():
        raise ConfigurationError("configuration file path must be absolute")
    if path.is_symlink():
        raise ConfigurationError("configuration file must not be a symbolic link")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        if optional:
            return {}
        raise ConfigurationError("configured service file does not exist") from error
    except OSError as error:
        raise ConfigurationError("configured service file cannot be opened safely") from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ConfigurationError("configuration file must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise ConfigurationError("configuration file must be owned by the service user")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ConfigurationError("configuration file must not be accessible by group or others")
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ConfigurationError("configuration file exceeds the 64 KiB limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)

    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("configuration file exceeds the 64 KiB limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigurationError("configuration file must be valid UTF-8") from error
    if "\x00" in text:
        raise ConfigurationError("configuration file contains a NUL character")

    settings: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line:
            raise ConfigurationError(f"configuration line {line_number} must use NAME=VALUE")
        name, raw_value = line.split("=", 1)
        if not _SETTING_NAME.fullmatch(name):
            raise ConfigurationError(f"configuration line {line_number} has an invalid setting name")
        if name == CONFIG_FILE_ENV:
            raise ConfigurationError(f"{CONFIG_FILE_ENV} must be supplied by the process environment")
        if _SENSITIVE_NAME.search(name):
            raise ConfigurationError(
                f"configuration line {line_number} names a credential setting; secrets are not file settings"
            )
        if name in settings:
            raise ConfigurationError(f"configuration line {line_number} duplicates a setting")

        value = raw_value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote or quote in value[1:-1] or "\\" in value:
                raise ConfigurationError(f"configuration line {line_number} has invalid quoting")
            value = value[1:-1]
        elif any(character.isspace() for character in value) or "#" in value or ";" in value:
            raise ConfigurationError(
                f"configuration line {line_number} must quote values containing whitespace or comment markers"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ConfigurationError(f"configuration line {line_number} contains a control character")
        settings[name] = value
    return settings


def load_configuration(
    environ: Mapping[str, str] | None = None,
    *,
    config_file: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Return settings with file values below the process environment.

    For the production process environment, file values are also added to
    ``os.environ`` only when the process did not already supply that setting.
    This lets the existing service components share the same explicit source
    precedence without logging or persisting configuration values.
    """
    process_environment = environ is None
    supplied = os.environ if process_environment else environ
    environment_values = dict(supplied)

    selected_file: Path | None
    if config_file is not None:
        selected_file = Path(config_file)
        optional = False
    elif CONFIG_FILE_ENV in environment_values:
        selected_file = Path(environment_values[CONFIG_FILE_ENV])
        optional = False
    elif process_environment:
        config_home = environment_values.get("XDG_CONFIG_HOME")
        if not config_home:
            config_home = str(Path(environment_values.get("HOME") or Path.home()) / ".config")
        selected_file = Path(config_home) / "mpv-webd" / "mpv-webd.env"
        optional = True
    else:
        selected_file = None
        optional = True

    file_values = _read_env_file(selected_file, optional=optional) if selected_file else {}
    merged = dict(file_values)
    merged.update(environment_values)

    if process_environment:
        for name, value in file_values.items():
            os.environ.setdefault(name, value)
        merged = dict(os.environ)
    return merged
