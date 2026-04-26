from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

_PREFIX = "dghub"
_PREFIX_LEN = 8
_SECRET_LEN = 40


@dataclass(frozen=True)
class IssuedToken:
    plaintext: str
    prefix: str
    token_hash: str


def issue() -> IssuedToken:
    prefix = secrets.token_hex(_PREFIX_LEN // 2)
    secret = secrets.token_urlsafe(_SECRET_LEN)
    plaintext = f"{_PREFIX}_{prefix}_{secret}"
    token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    return IssuedToken(plaintext=plaintext, prefix=prefix, token_hash=token_hash)


def hash_for_lookup(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def parse_authorization_header(header_value: str | None) -> str | None:
    if not header_value:
        return None
    parts = header_value.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def looks_like_api_token(token: str) -> bool:
    return token.startswith(f"{_PREFIX}_")


VALID_SCOPES = frozenset(
    {
        "packages:read",
        "packages:write",
        "publishers:write",
        "*",
    }
)


def normalize_scopes(scopes: list[str]) -> list[str]:
    cleaned = sorted({s.strip() for s in scopes if s and s.strip() in VALID_SCOPES})
    return cleaned or ["packages:read"]


def has_scope(granted: list[str], required: str) -> bool:
    return "*" in granted or required in granted
