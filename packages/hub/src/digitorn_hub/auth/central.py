"""Central-auth (digitorn-auth) JWT verifier.

Accepts RS256 JWTs signed by ``auth.digitorn.ai`` (or any equivalent
issuer configured via ``HUB_AUTH_SERVICE_URL``). The verifier fetches
the issuer's JWKS once at startup, caches it for 24 h, and refreshes
lazily on a kid-miss (key rotation).

Vendored rather than imported from ``digitorn_auth`` because the Hub
ships in its own Docker image with an isolated build context — pulling
in the full auth package would mean restructuring the monorepo build
plumbing for a single 80-line dependency.

Usage::

    client = CentralAuthClient(issuer="https://auth.digitorn.ai")
    await client.start()                    # warms JWKS cache
    claims = client.verify(bearer_token)    # raises InvalidCentralToken on bad signature/issuer
    # claims.user_id, claims.email, claims.display_name, claims.roles
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_NEGATIVE_CACHE_BACKOFF = 30  # seconds


class CentralAuthError(Exception):
    """Base for verifier errors."""


class InvalidCentralToken(CentralAuthError):
    """Token verification failed (signature, expiry, issuer, kid-miss, ...)."""


class JWKSUnavailable(CentralAuthError):
    """Could not fetch JWKS at start time AND no cached copy is available."""


@dataclass
class CentralClaims:
    user_id: str
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    token_type: str = "access"
    issued_at: int = 0
    expires_at: int = 0
    jti: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class CentralAuthClient:
    """Verifies JWTs issued by a digitorn-auth service.

    Long-lived: instantiate once at app startup, call ``await start()``
    to warm the JWKS cache, then ``verify(token)`` synchronously on the
    request path. JWKS refresh on a kid-miss is opportunistic — the
    next request triggers it via ``maybe_refresh_jwks``.
    """

    def __init__(
        self,
        issuer: str,
        *,
        jwks_ttl: int = 24 * 3600,
        http_timeout: float = 5.0,
        accept_issuers: list[str] | None = None,
    ):
        self._issuer = issuer.rstrip("/")
        self._accept_issuers = list(accept_issuers or [])
        self._jwks_url: str | None = None
        self._jwks: dict[str, Any] = {"keys": []}
        self._jwks_loaded_at: float = 0
        self._jwks_ttl = jwks_ttl
        self._http_timeout = http_timeout
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_failure: float = 0

    async def start(self) -> None:
        await self._discover()
        try:
            await self._refresh_jwks(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "central_auth_initial_jwks_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )

    async def _discover(self) -> None:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as http:
                r = await http.get(
                    f"{self._issuer}/.well-known/openid-configuration",
                )
                r.raise_for_status()
                self._jwks_url = r.json().get("jwks_uri")
        except Exception:
            self._jwks_url = f"{self._issuer}/.well-known/jwks.json"

    async def _refresh_jwks(self, force: bool = False) -> None:
        if not force:
            now = time.time()
            if now - self._jwks_loaded_at < self._jwks_ttl:
                return
            if now - self._last_refresh_failure < _NEGATIVE_CACHE_BACKOFF:
                return
        async with self._refresh_lock:
            if not force and (time.time() - self._jwks_loaded_at) < self._jwks_ttl:
                return
            url = self._jwks_url or f"{self._issuer}/.well-known/jwks.json"
            import httpx
            try:
                async with httpx.AsyncClient(timeout=self._http_timeout) as http:
                    r = await http.get(url)
                    r.raise_for_status()
                    self._jwks = r.json()
                    self._jwks_loaded_at = time.time()
                    self._last_refresh_failure = 0
                    logger.info(
                        "central_auth_jwks_refreshed issuer=%s keys=%d",
                        self._issuer, len(self._jwks.get("keys", [])),
                    )
            except Exception as exc:  # noqa: BLE001
                self._last_refresh_failure = time.time()
                logger.warning(
                    "central_auth_jwks_refresh_failed issuer=%s exc=%s",
                    self._issuer, exc,
                )
                if not self._jwks.get("keys"):
                    raise JWKSUnavailable(
                        f"Cannot fetch JWKS from {url} and no cached copy",
                    ) from exc

    def verify(self, token: str) -> CentralClaims:
        """Verify a JWT against the cached JWKS.

        Raises ``InvalidCentralToken`` on any verification failure.
        Synchronous: O(1) + RSA signature check, no network access on
        the request path.
        """
        import jwt as pyjwt

        try:
            header = pyjwt.get_unverified_header(token)
        except Exception as exc:
            raise InvalidCentralToken(f"Cannot read token header: {exc}") from exc

        alg = header.get("alg")
        if alg != "RS256":
            # We deliberately reject HS256 and other algs - the Hub's
            # own HS256 path lives elsewhere (see auth.deps._resolve_jwt).
            raise InvalidCentralToken(f"Unsupported alg for central path: {alg!r}")

        kid = header.get("kid")
        key = self._find_key(kid)
        if key is None:
            raise InvalidCentralToken(
                f"No JWKS key matches kid={kid!r}. JWKS may be stale - "
                f"call await client.maybe_refresh_jwks().",
            )

        public_key = _jwk_to_public_key(key)

        try:
            decoded = pyjwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise InvalidCentralToken("Token expired") from exc
        except Exception as exc:  # noqa: BLE001
            raise InvalidCentralToken(f"Token validation failed: {exc}") from exc

        iss = decoded.get("iss", "")
        accepted = [self._issuer, *self._accept_issuers]
        if iss not in accepted:
            raise InvalidCentralToken(
                f"Issuer mismatch: token iss={iss!r}, expected one of {accepted}",
            )

        return CentralClaims(
            user_id=decoded["sub"],
            email=decoded.get("email"),
            display_name=decoded.get("name"),
            roles=decoded.get("roles") or [],
            permissions=decoded.get("perms") or [],
            token_type=decoded.get("type", "access"),
            issued_at=int(decoded.get("iat", 0)),
            expires_at=int(decoded.get("exp", 0)),
            jti=decoded.get("jti"),
            raw=decoded,
        )

    async def maybe_refresh_jwks(self) -> None:
        try:
            await self._refresh_jwks(force=False)
        except Exception:
            pass

    def _find_key(self, kid: str | None) -> dict | None:
        keys = self._jwks.get("keys", [])
        if not keys:
            return None
        if kid:
            for k in keys:
                if k.get("kid") == kid:
                    return k
            return None
        return keys[0]


def _jwk_to_public_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    def _b64dec_int(s: str) -> int:
        padded = s + "=" * (-len(s) % 4)
        return int.from_bytes(base64.urlsafe_b64decode(padded), "big")

    n = _b64dec_int(jwk["n"])
    e = _b64dec_int(jwk["e"])
    return _rsa.RSAPublicNumbers(e=e, n=n).public_key()


def looks_like_rs256(token: str) -> bool:
    """Quick, non-cryptographic peek at the token header.

    Used by the resolver to decide whether to route to the central
    verifier or to the Hub's local HS256 path. False on malformed
    input - the local path will surface a clean 401 in that case.
    """
    try:
        import jwt as pyjwt
        header = pyjwt.get_unverified_header(token)
        return header.get("alg") == "RS256"
    except Exception:
        return False
