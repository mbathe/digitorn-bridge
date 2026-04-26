from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt

from ..settings import get_settings

TokenType = Literal["access", "refresh"]


class JWTError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ttl(token_type: TokenType) -> timedelta:
    s = get_settings()
    if token_type == "access":
        return timedelta(minutes=s.jwt_access_ttl_minutes)
    return timedelta(days=s.jwt_refresh_ttl_days)


def encode(user_id: uuid.UUID, token_type: TokenType = "access") -> str:
    s = get_settings()
    now = _now()
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + _ttl(token_type)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode(token: str, expected_type: TokenType | None = None) -> dict:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise JWTError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise JWTError(f"invalid token: {exc}") from exc

    if expected_type and payload.get("type") != expected_type:
        raise JWTError(f"expected token type {expected_type}, got {payload.get('type')}")
    return payload
