from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlsplit


ALLOWED_ORIGINS_ENV = "MPV_WEBD_ALLOWED_ORIGINS"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class OriginPolicyError(ValueError):
    """Raised when configured browser origins are ambiguous or unsafe."""


@dataclass(frozen=True, slots=True)
class BrowserOriginPolicy:
    origins: frozenset[str]

    def __init__(self, origins: Iterable[str] = ()) -> None:
        if isinstance(origins, str):
            raise OriginPolicyError("allowed origins must be a collection of origins")
        normalized: set[str] = set()
        for origin in origins:
            canonical = normalize_origin(origin)
            if canonical in normalized:
                raise OriginPolicyError("allowed origins contain duplicates")
            normalized.add(canonical)
        object.__setattr__(self, "origins", frozenset(normalized))

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "BrowserOriginPolicy":
        values = os.environ if environ is None else environ
        raw = values.get(ALLOWED_ORIGINS_ENV, "")
        if not isinstance(raw, str):
            raise OriginPolicyError(f"{ALLOWED_ORIGINS_ENV} must be a comma-separated string")
        if not raw:
            return cls()
        origins = raw.split(",")
        if any(not origin or origin.strip() != origin for origin in origins):
            raise OriginPolicyError(f"{ALLOWED_ORIGINS_ENV} contains an empty or padded origin")
        return cls(origins)

    def allows(self, origin: str | None) -> bool:
        if not isinstance(origin, str):
            return False
        try:
            canonical = normalize_origin(origin)
        except OriginPolicyError:
            return False
        return canonical in self.origins


def normalize_origin(origin: str) -> str:
    if (
        not isinstance(origin, str)
        or not origin
        or origin != origin.strip()
        or origin == "null"
        or "*" in origin
        or any(ord(character) < 33 or ord(character) == 127 for character in origin)
    ):
        raise OriginPolicyError("origin must be an explicit HTTP(S) origin")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as error:
        raise OriginPolicyError("origin is malformed") from error
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or parsed.path
        or parsed.query
        or parsed.fragment
        or "?" in origin
        or "#" in origin
    ):
        raise OriginPolicyError("origin must contain only an HTTP(S) scheme and host")
    host = _normalize_host(parsed.hostname)
    if port == (80 if scheme == "http" else 443):
        port = None
    display_host = f"[{host}]" if ":" in host else host
    netloc = display_host if port is None else f"{display_host}:{port}"
    return f"{scheme}://{netloc}"


def _normalize_host(value: str) -> str:
    if not value or "%" in value:
        raise OriginPolicyError("origin host is invalid")
    candidate = value.casefold()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise OriginPolicyError("origin host is invalid") from error
    if (
        not ascii_host
        or len(ascii_host) > 253
        or any(not _HOST_LABEL.fullmatch(label) for label in ascii_host.split("."))
    ):
        raise OriginPolicyError("origin host is invalid")
    return ascii_host
