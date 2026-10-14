from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")
_TOKEN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class TokenNotActive(ValueError):
    """Raised when rotation targets a missing or already-revoked token."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    id: str
    label: str
    token: str = field(repr=False)
    created_at: str


class TokenStore:
    """Issue opaque tokens and persist only their SHA-256 digests."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect

    def issue(self, label: str) -> IssuedToken:
        normalized_label = _validated_label(label)
        token = secrets.token_urlsafe(32)
        token_id = uuid.uuid4().hex
        created_at = _utc_now()
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    "INSERT INTO auth_tokens(id,label,token_hash,created_at) VALUES(?,?,?,?)",
                    (token_id, normalized_label, hash_token(token), created_at),
                )
        finally:
            connection.close()
        return IssuedToken(token_id, normalized_label, token, created_at)

    def rotate(self, token_id: str, *, label: str | None = None) -> IssuedToken:
        if not isinstance(token_id, str) or not token_id:
            raise TokenNotActive("token is not active")
        new_token = secrets.token_urlsafe(32)
        new_id = uuid.uuid4().hex
        created_at = _utc_now()
        connection = self._connect()
        try:
            with connection:
                row = connection.execute(
                    "SELECT label FROM auth_tokens WHERE id=? AND revoked_at IS NULL",
                    (token_id,),
                ).fetchone()
                if row is None:
                    raise TokenNotActive("token is not active")
                next_label = _validated_label(label if label is not None else str(row[0]))
                revoked_at = _utc_now()
                updated = connection.execute(
                    "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (revoked_at, token_id),
                )
                if updated.rowcount != 1:
                    raise TokenNotActive("token is not active")
                connection.execute(
                    "INSERT INTO auth_tokens(id,label,token_hash,created_at) VALUES(?,?,?,?)",
                    (new_id, next_label, hash_token(new_token), created_at),
                )
        finally:
            connection.close()
        return IssuedToken(new_id, next_label, new_token, created_at)

    def revoke(self, token_id: str) -> bool:
        if not isinstance(token_id, str) or not token_id:
            return False
        connection = self._connect()
        try:
            with connection:
                result = connection.execute(
                    "UPDATE auth_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (_utc_now(), token_id),
                )
                return result.rowcount == 1
        finally:
            connection.close()

    def authenticate(self, token: str | None) -> bool:
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return False
        candidate_hash = hash_token(token)
        connection = self._connect()
        try:
            with connection:
                rows = connection.execute(
                    "SELECT id,token_hash FROM auth_tokens WHERE revoked_at IS NULL"
                ).fetchall()
                matched_id: str | None = None
                for token_id, stored_hash in rows:
                    encoded_hash = str(stored_hash)
                    if not _TOKEN_HASH_PATTERN.fullmatch(encoded_hash):
                        # Keep one fixed-size comparison even if persisted data is corrupt.
                        hmac.compare_digest(candidate_hash, "0" * 64)
                        continue
                    matched = hmac.compare_digest(candidate_hash, encoded_hash)
                    if matched:
                        matched_id = str(token_id)
                if matched_id is None:
                    return False
                connection.execute(
                    "UPDATE auth_tokens SET last_used_at=? WHERE id=? AND revoked_at IS NULL",
                    (_utc_now(), matched_id),
                )
                return True
        finally:
            connection.close()


def bearer_token(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    scheme, separator, value = authorization.partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not _TOKEN_PATTERN.fullmatch(value)
    ):
        return None
    return value


def hash_token(token: str) -> str:
    if not isinstance(token, str):
        raise TypeError("token must be a string")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validated_label(label: str) -> str:
    if (
        not isinstance(label, str)
        or not label
        or len(label) > 120
        or label.strip() != label
        or any(ord(character) < 32 or ord(character) == 127 for character in label)
    ):
        raise ValueError("token label must be 1-120 printable characters without edge whitespace")
    return label


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
