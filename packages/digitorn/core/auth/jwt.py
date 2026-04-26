"""JWT token generation and verification.

Uses PyJWT with HS256 (symmetric) by default.
The secret key is auto-generated on first daemon start and stored
in ~/.digitorn/jwt.key (same pattern as the Fernet encryption key).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import jwt as pyjwt

    _HAS_JWT = True
except ImportError:
    _HAS_JWT = False
    logger.warning("PyJWT not installed. Auth features disabled. Install with: pip install PyJWT")
_KEY_FILE = Path.home() / ".digitorn" / "jwt.key"

_DEFAULT_ACCESS_TTL = 900
_DEFAULT_REFRESH_TTL = 604800
_ALGORITHM = "HS256"
_ISSUER = "digitorn"


def _get_access_ttl() -> int:
    try:
        from digitorn.core.config import get_settings
        return get_settings().auth.access_token_ttl
    except Exception:
        return _DEFAULT_ACCESS_TTL


def _get_refresh_ttl() -> int:
    try:
        from digitorn.core.config import get_settings
        return get_settings().auth.refresh_token_ttl
    except Exception:
        return _DEFAULT_REFRESH_TTL

@dataclass

class TokenPayload:
    """Decoded JWT payload."""

    user_id: str
    email: str | None = None
    display_name: str | None = None
    roles: list[str] | None = None
    permissions: list[str] | None = None
    app_id: str | None = None
    token_type: str = "access"
    issued_at: float = 0
    expires_at: float = 0
    jti: str | None = None

def _ensure_secret(key_path: Path | None = None) -> str:
    """Load or generate the JWT secret key.

    Auto-generates a 64-byte hex key on first use and saves it to disk.
    The key file is chmod 600 (owner-only read/write).
    """
    path = key_path or _KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        return path.read_text().strip()

    key = secrets.token_hex(64)
    path.write_text(key)
    path.chmod(0o600)
    logger.info("jwt_key_generated path=%s", path)
    return key

class JWTService:
    """Stateless JWT token generation and verification."""

    def __init__(
        self,
        secret_key: str | None = None,
        access_ttl: int | None = None,
        refresh_ttl: int | None = None,
        algorithm: str = _ALGORITHM,
        key_path: Path | None = None,
    ):
        if not _HAS_JWT:
            raise RuntimeError("PyJWT is required for auth. Install with: pip install PyJWT")

        self._secret = secret_key or _ensure_secret(key_path)
        self._access_ttl = access_ttl if access_ttl is not None else _get_access_ttl()
        self._refresh_ttl = refresh_ttl if refresh_ttl is not None else _get_refresh_ttl()
        self._algorithm = algorithm

    def generate_access_token(
        self,
        user_id: str,
        email: str | None = None,
        display_name: str | None = None,
        roles: list[str] | None = None,
        permissions: list[str] | None = None,
        app_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """Generate an access token. When ``access_ttl == 0`` the JWT is
        issued without an ``exp`` claim → never expires. PyJWT skips the
        expiry check entirely when the claim is absent."""
        now = time.time()
        payload: dict[str, Any] = {
            "sub": user_id,
            "type": "access",
            "iss": _ISSUER,
            "iat": int(now),
            "jti": secrets.token_hex(16),
        }
        if self._access_ttl > 0:
            payload["exp"] = int(now + self._access_ttl)
        if email:
            payload["email"] = email
        if display_name:
            payload["name"] = display_name
        if roles:
            payload["roles"] = roles
        if permissions:
            payload["perms"] = permissions
        if app_id:
            payload["app"] = app_id
        if extra:
            payload.update(extra)

        return pyjwt.encode(payload, self._secret, algorithm=self._algorithm)

    def generate_refresh_token(self, user_id: str) -> tuple[str, str]:
        """Generate a refresh token. When ``refresh_ttl == 0`` the JWT
        is issued without an ``exp`` claim → never expires.

        Returns (token_string, token_hash) — the hash is stored in DB,
        the raw token is returned to the client.
        """
        now = time.time()
        payload: dict[str, Any] = {
            "sub": user_id,
            "type": "refresh",
            "iss": _ISSUER,
            "iat": int(now),
            "jti": secrets.token_hex(16),
        }
        if self._refresh_ttl > 0:
            payload["exp"] = int(now + self._refresh_ttl)
        token = pyjwt.encode(payload, self._secret, algorithm=self._algorithm)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash

    def verify(self, token: str) -> TokenPayload:
        """Verify and decode a JWT token.

        Raises:
            jwt.ExpiredSignatureError: Token has expired.
            jwt.InvalidTokenError: Token is malformed or invalid.
        """
        decoded = pyjwt.decode(
            token,
            self._secret,
            algorithms=[self._algorithm],
            issuer=_ISSUER,
        )

        return TokenPayload(
            user_id=decoded["sub"],
            email=decoded.get("email"),
            display_name=decoded.get("name"),
            roles=decoded.get("roles"),
            permissions=decoded.get("perms"),
            app_id=decoded.get("app"),
            token_type=decoded.get("type", "access"),
            issued_at=decoded.get("iat", 0),
            expires_at=decoded.get("exp", 0),
            jti=decoded.get("jti"),
        )

    def hash_token(self, token: str) -> str:
        """Hash a token for DB storage (SHA-256)."""
        return hashlib.sha256(token.encode()).hexdigest()
