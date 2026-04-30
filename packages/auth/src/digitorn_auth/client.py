"""Client SDK for any service that wants to consume digitorn-auth.

Usage in a daemon::

    from digitorn_auth.client import RemoteAuthClient

    auth = RemoteAuthClient(issuer="https://auth.digitorn.ai")
    await auth.start()  # fetches JWKS once

    @app.get("/api/whatever")
    async def handler(token: str = Depends(get_bearer)):
        claims = auth.verify(token)
        # claims.user_id, claims.email, claims.roles, ...

The client caches the JWKS for 24h by default and refreshes it lazily
on a kid-miss (token signed by a key not currently in the cache → one
re-fetch attempt before failing). Verification is fully offline once
the cache is warm — no network roundtrip on the request path.

For a drop-in FastAPI middleware that handles all of this, see
``digitorn_auth.fastapi.RemoteAuthMiddleware``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_JWKS_TTL = 24 * 3600           # 24h: keys rotate slowly
_NEGATIVE_CACHE_BACKOFF = 30            # don't hammer central on transient failures


@dataclass
class RemoteAuthClaims:
    """Decoded claims from a verified access token."""

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


class RemoteAuthError(Exception):
    """Base for client errors."""


class JWKSUnavailable(RemoteAuthError):
    """Could not fetch JWKS at start time AND no cached copy is available."""


class InvalidToken(RemoteAuthError):
    """Token verification failed (signature, expiry, issuer, …)."""


class RemoteAuthClient:
    """Verifies JWTs issued by a digitorn-auth service.

    Designed for long-lived processes (daemons, services). Instantiate
    once at startup, call ``await start()`` to warm the JWKS cache,
    then use ``verify(token)`` synchronously on the request path.
    """

    def __init__(
        self,
        issuer: str,
        *,
        jwks_ttl: int = _DEFAULT_JWKS_TTL,
        http_timeout: float = 5.0,
        # When the auth service is on the local loopback in dev, the
        # issuer claim in tokens may differ from the URL we use to
        # fetch JWKS. Pass `accept_issuers=` to accept extra ones.
        accept_issuers: list[str] | None = None,
    ):
        self._issuer = issuer.rstrip("/")
        self._accept_issuers = list(accept_issuers or [])
        self._accept_issuers.append("digitorn")  # legacy / embedded daemon JWTs
        self._jwks_url: str | None = None
        self._jwks: dict[str, Any] = {"keys": []}
        self._jwks_loaded_at: float = 0
        self._jwks_ttl = jwks_ttl
        self._http_timeout = http_timeout
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_failure: float = 0

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Discover endpoints + fetch JWKS. Tolerates a network failure
        (the cache stays empty; ``verify()`` re-tries once before failing).
        """
        await self._discover()
        try:
            await self._refresh_jwks(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_auth_initial_jwks_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )

    async def _discover(self) -> None:
        """Resolve the JWKS URL via OIDC discovery."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as http:
                r = await http.get(f"{self._issuer}/.well-known/openid-configuration")
                r.raise_for_status()
                self._jwks_url = r.json().get("jwks_uri")
        except Exception as exc:  # noqa: BLE001
            # Fallback to the conventional path
            self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
            logger.debug(
                "remote_auth_discovery_fallback issuer=%s exc=%s url=%s",
                self._issuer, exc, self._jwks_url,
            )

    async def _refresh_jwks(self, force: bool = False) -> None:
        """Refresh the JWKS cache. Coalesces concurrent calls via a lock."""
        if not force:
            now = time.time()
            if now - self._jwks_loaded_at < self._jwks_ttl:
                return
            if now - self._last_refresh_failure < _NEGATIVE_CACHE_BACKOFF:
                return  # back off after a recent failure

        async with self._refresh_lock:
            # Double-check under the lock
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
                        "remote_auth_jwks_refreshed issuer=%s keys=%d",
                        self._issuer, len(self._jwks.get("keys", [])),
                    )
            except Exception as exc:  # noqa: BLE001
                self._last_refresh_failure = time.time()
                logger.warning(
                    "remote_auth_jwks_refresh_failed issuer=%s exc=%s",
                    self._issuer, exc,
                )
                if not self._jwks.get("keys"):
                    raise JWKSUnavailable(
                        f"Cannot fetch JWKS from {url} and no cached copy"
                    ) from exc

    # ── Verification ───────────────────────────────────────────────

    def verify(self, token: str) -> RemoteAuthClaims:
        """Verify a JWT against the cached JWKS. Synchronous: O(1) +
        signature check, no network access on the hot path.

        Raises ``InvalidToken`` for any verification failure.
        """
        import jwt as pyjwt

        try:
            header = pyjwt.get_unverified_header(token)
        except Exception as exc:
            raise InvalidToken(f"Cannot read token header: {exc}") from exc

        alg = header.get("alg")
        if alg not in ("RS256", "HS256"):
            raise InvalidToken(f"Unsupported alg: {alg!r}")

        if alg == "HS256":
            # HS256 path is intentionally NOT supported here — a remote
            # client has no way to know the central's HS256 secret.
            # For HS256 the daemon must use the embedded JWTService
            # against the shared on-disk secret instead. RS256 is the
            # only safe path for cross-machine validation.
            raise InvalidToken(
                "HS256 not supported by RemoteAuthClient — use RS256 "
                "(set DIGITORN_AUTH_JWT_ALGORITHM=RS256 on the auth service)."
            )

        kid = header.get("kid")
        key = self._find_key(kid)
        if key is None:
            # Soft-miss: maybe a key rotation happened. We don't await
            # here (keep verify() sync), but the next request will
            # trigger an async refresh via maybe_refresh_jwks().
            raise InvalidToken(
                f"No JWKS key matches kid={kid!r}. Call await client.refresh_jwks()."
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
            raise InvalidToken("Token expired") from exc
        except Exception as exc:  # noqa: BLE001
            raise InvalidToken(f"Token validation failed: {exc}") from exc

        # Issuer check: accept the configured issuer plus any extras.
        iss = decoded.get("iss", "")
        if iss not in self._accept_issuers and iss != self._issuer:
            raise InvalidToken(
                f"Issuer mismatch: token iss={iss!r}, expected one of "
                f"{[*self._accept_issuers, self._issuer]}"
            )

        return RemoteAuthClaims(
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
        """Best-effort background refresh — call from a periodic task or
        after a verify() raised InvalidToken with a kid-miss."""
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
    """Build an RSA public key object from a JWK dict."""
    import base64
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    def _b64dec_int(s: str) -> int:
        padded = s + "=" * (-len(s) % 4)
        return int.from_bytes(base64.urlsafe_b64decode(padded), "big")

    n = _b64dec_int(jwk["n"])
    e = _b64dec_int(jwk["e"])
    return _rsa.RSAPublicNumbers(e=e, n=n).public_key()
