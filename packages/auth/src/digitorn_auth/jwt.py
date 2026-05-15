"""JWT token generation and verification.

Supports two signing modes:

  * **RS256** (default): asymmetric. The auth service holds a 2048-bit
    RSA private key, signs with it, and exposes the public key via
    ``GET /.well-known/jwks.json``. Daemons (and any other service)
    fetch the public key once, cache it, and verify offline. The
    private key NEVER leaves this service. This is the prod default.

  * **HS256**: legacy symmetric. The same secret signs and verifies.
    Useful for single-machine dev where there's no JWKS plumbing yet,
    or for the transition window where the embedded daemon auth still
    talks to the same shared secret. Set ``DIGITORN_AUTH_JWT_ALGORITHM=HS256``
    to opt in.

Keys are auto-generated on first start when the configured paths are
absent. Existing keys are reused — restart-safe.
"""

from __future__ import annotations

import base64
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
    logger.warning(
        "PyJWT not installed. Auth features disabled. "
        "Install with: pip install 'PyJWT[crypto]'"
    )

_KEY_FILE = Path.home() / ".digitorn" / "jwt.key"

_DEFAULT_ACCESS_TTL = 900
_DEFAULT_REFRESH_TTL = 604800
_DEFAULT_ALGORITHM = "RS256"
_ISSUER = "digitorn"

# Pre-date ``iat`` by this many seconds when minting. Pure constant
# subtraction at sign time -- zero background work, zero polling, zero
# extra cost. Absorbs clock skew between this service and downstream
# verifiers (daemon, mobile client) so a verifier whose wall clock is
# a few seconds BEHIND ours doesn't reject our freshly-minted token as
# "issued in the future". Pairs with ``leeway=60`` on the verifier
# side (see ``client.py:verify``) — combined budget is ~65 s, which
# covers realistic NTP drift between Fly.io edge VMs and consumer
# laptops without weakening security in any meaningful way (an
# attacker still can't backdate a token because the issuer is signed).
_IAT_SKEW_BUFFER_S = 5


def _get_access_ttl() -> int:
    try:
        from digitorn_auth.config import get_settings
        return get_settings().access_token_ttl
    except Exception:
        return _DEFAULT_ACCESS_TTL


def _get_refresh_ttl() -> int:
    try:
        from digitorn_auth.config import get_settings
        return get_settings().refresh_token_ttl
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
    """Load or generate the HS256 secret key."""
    path = key_path or _KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        return path.read_text().strip()

    key = secrets.token_hex(64)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except Exception:
        pass
    logger.info("jwt_hs256_secret_generated path=%s", path)
    return key


def _ensure_rsa_keypair(
    private_path: Path,
    public_path: Path,
) -> tuple[bytes, bytes]:
    """Load or generate an RSA-2048 keypair (PEM-encoded).

    Returns (private_pem, public_pem). Generates and persists both
    files on first call; subsequent calls just read the existing pair.
    """
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)

    if private_path.exists() and public_path.exists():
        return private_path.read_bytes(), public_path.read_bytes()

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    try:
        private_path.chmod(0o600)
    except Exception:
        pass
    logger.info("jwt_rsa_keypair_generated private=%s public=%s", private_path, public_path)
    return private_pem, public_pem


def public_jwk_from_pem(public_pem: bytes, kid: str) -> dict[str, str]:
    """Convert an RSA public key (PEM) to a JWK dict for /.well-known/jwks.json."""
    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(public_pem)
    numbers = public_key.public_numbers()

    def _b64(n: int) -> str:
        # JWK requires unsigned big-endian, base64url, no padding.
        b = n.to_bytes((n.bit_length() + 7) // 8, byteorder="big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }


class JWTService:
    """Stateless JWT signing and verification.

    Instantiate ONCE per process. Key material is loaded eagerly so a
    misconfiguration fails fast at startup rather than at first request.
    """

    def __init__(
        self,
        secret_key: str | None = None,
        access_ttl: int | None = None,
        refresh_ttl: int | None = None,
        algorithm: str = _DEFAULT_ALGORITHM,
        key_path: Path | None = None,
        private_key_path: Path | None = None,
        public_key_path: Path | None = None,
        kid: str = "auth-default",
    ):
        if not _HAS_JWT:
            raise RuntimeError("PyJWT is required. Install: pip install 'PyJWT[crypto]'")

        self._algorithm = algorithm
        self._kid = kid
        self._access_ttl = access_ttl if access_ttl is not None else _get_access_ttl()
        self._refresh_ttl = refresh_ttl if refresh_ttl is not None else _get_refresh_ttl()

        if algorithm == "HS256":
            self._secret = secret_key or _ensure_secret(key_path)
            self._private_pem: bytes | None = None
            self._public_pem: bytes | None = None
        elif algorithm == "RS256":
            if private_key_path is None or public_key_path is None:
                raise ValueError(
                    "RS256 requires private_key_path and public_key_path"
                )
            priv, pub = _ensure_rsa_keypair(private_key_path, public_key_path)
            self._private_pem = priv
            self._public_pem = pub
            self._secret = priv  # used as the signing key by pyjwt for RS256
        else:
            raise ValueError(f"Unsupported algorithm: {algorithm}")

    # ── Public material exposed to /.well-known/jwks.json ───────────

    def public_jwks(self) -> dict[str, list[dict[str, str]]]:
        """Return the JWKS dict served at /.well-known/jwks.json.

        Empty for HS256 (no public key to expose — secret is shared).
        For RS256, contains a single key entry with the active kid.
        """
        if self._algorithm != "RS256" or self._public_pem is None:
            return {"keys": []}
        return {"keys": [public_jwk_from_pem(self._public_pem, self._kid)]}

    # ── Token issuance ──────────────────────────────────────────────

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
        """Generate an access token. ``access_ttl == 0`` ⇒ no ``exp``."""
        now = time.time()
        payload: dict[str, Any] = {
            "sub": user_id,
            "type": "access",
            "iss": _ISSUER,
            # Pre-dated by ``_IAT_SKEW_BUFFER_S`` so verifiers whose
            # clock lags ours by a few seconds don't reject this token
            # as "not yet valid". ``exp`` keeps its full TTL from real
            # ``now`` (token lifetime isn't shortened).
            "iat": int(now) - _IAT_SKEW_BUFFER_S,
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

        headers = {"kid": self._kid} if self._algorithm == "RS256" else None
        return pyjwt.encode(
            payload, self._secret, algorithm=self._algorithm, headers=headers,
        )

    def generate_refresh_token(self, user_id: str) -> tuple[str, str]:
        """Generate a refresh token. Returns (raw_token, sha256_hash)."""
        now = time.time()
        payload: dict[str, Any] = {
            "sub": user_id,
            "type": "refresh",
            "iss": _ISSUER,
            # Same clock-skew buffer as access tokens — see comment on
            # ``_IAT_SKEW_BUFFER_S`` for rationale.
            "iat": int(now) - _IAT_SKEW_BUFFER_S,
            "jti": secrets.token_hex(16),
        }
        if self._refresh_ttl > 0:
            payload["exp"] = int(now + self._refresh_ttl)
        headers = {"kid": self._kid} if self._algorithm == "RS256" else None
        token = pyjwt.encode(
            payload, self._secret, algorithm=self._algorithm, headers=headers,
        )
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash

    # ── Verification ────────────────────────────────────────────────

    def verify(self, token: str) -> TokenPayload:
        """Verify and decode a JWT signed by this service.

        ``verify_aud=False`` because device tokens carry an ``aud`` claim
        (``daemon-{device_id}``) that we validate at the business layer,
        not via pyjwt's automatic check.
        """
        verification_key = self._public_pem if self._algorithm == "RS256" else self._secret
        decoded = pyjwt.decode(
            token,
            verification_key,
            algorithms=[self._algorithm],
            issuer=_ISSUER,
            options={"verify_aud": False},
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
        return hashlib.sha256(token.encode()).hexdigest()
