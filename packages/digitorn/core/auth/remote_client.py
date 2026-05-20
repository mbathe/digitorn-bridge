"""Client SDK for any service that wants to consume digitorn-auth."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_JWKS_TTL = 24 * 3600
_NEGATIVE_CACHE_BACKOFF = 30


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
    """Verifies JWTs issued by a digitorn-auth service."""

    def __init__(
        self,
        issuer: str,
        *,
        jwks_ttl: int = _DEFAULT_JWKS_TTL,
        http_timeout: float = 5.0,
        accept_issuers: list[str] | None = None,
        revocation_poll_interval: int = 300,
    ):
        self._issuer = issuer.rstrip("/")
        self._accept_issuers = list(accept_issuers or [])
        self._accept_issuers.append("digitorn")
        self._jwks_url: str | None = None
        self._jwks: dict[str, Any] = {"keys": []}
        self._jwks_loaded_at: float = 0
        self._jwks_ttl = jwks_ttl
        self._http_timeout = http_timeout
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_failure: float = 0
        self._revoked_jtis: dict[str, int] = {}
        self._last_revocation_sync: float = 0
        self._revocation_poll_interval = revocation_poll_interval
        self._revocation_task: asyncio.Task[None] | None = None
        self._http: Any | None = None

    async def _get_http(self) -> Any:
        """Return the shared httpx client, creating it on first use."""
        if self._http is None:
            import asyncio as _asyncio
            import httpx

            def _build_client() -> Any:
                return httpx.AsyncClient(timeout=self._http_timeout)

            async with self._refresh_lock:
                if self._http is None:
                    self._http = await _asyncio.to_thread(_build_client)
        return self._http


    async def start(self) -> None:
        """Discover endpoints + fetch JWKS + start revocation sync."""
        await self._discover()
        try:
            await self._refresh_jwks(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_auth_initial_jwks_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )
        try:
            await self._refresh_revocations()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_auth_initial_revocations_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )
        if self._revocation_task is None or self._revocation_task.done():
            self._revocation_task = asyncio.create_task(
                self._revocation_sync_loop(),
                name="remote_auth_revocation_sync",
            )

    async def close(self) -> None:
        """Stop the revocation sync task."""
        if self._revocation_task is not None and not self._revocation_task.done():
            self._revocation_task.cancel()
            try:
                await self._revocation_task
            except (asyncio.CancelledError, Exception):
                pass
        self._revocation_task = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception as exc:
                logger.debug("client best-effort block failed: %s", exc)
            self._http = None

    async def _discover(self) -> None:
        """Resolve the JWKS URL via OIDC discovery."""
        try:
            http = await self._get_http()
            r = await http.get(
                f"{self._issuer}/.well-known/openid-configuration",
            )
            r.raise_for_status()
            self._jwks_url = r.json().get("jwks_uri")
        except Exception as exc:  # noqa: BLE001
            self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
            logger.debug(
                "remote_auth_discovery_fallback issuer=%s exc=%s url=%s",
                self._issuer, exc, self._jwks_url,
            )

    async def _refresh_jwks(self, force: bool = False) -> None:
        """Refresh the JWKS cache."""
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
            try:
                http = await self._get_http()
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


    def verify(self, token: str) -> RemoteAuthClaims:
        """Verify a JWT against the cached JWKS."""
        import jwt as pyjwt

        try:
            header = pyjwt.get_unverified_header(token)
        except Exception as exc:
            raise InvalidToken(f"Cannot read token header: {exc}") from exc

        alg = header.get("alg")
        if alg not in ("RS256", "HS256"):
            raise InvalidToken(f"Unsupported alg: {alg!r}")

        if alg == "HS256":
            raise InvalidToken(
                "HS256 not supported by RemoteAuthClient — use RS256 "
                "(set DIGITORN_AUTH_JWT_ALGORITHM=RS256 on the auth service)."
            )

        kid = header.get("kid")
        key = self._find_key(kid)
        if key is None:
            raise InvalidToken(
                f"No JWKS key matches kid={kid!r}. Call await client.refresh_jwks()."
            )

        public_key = _jwk_to_public_key(key)

        try:
            decoded = pyjwt.decode(
                token,
                public_key,
                algorithms=["RS256"],
                leeway=60,
                options={
                    "verify_aud": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise InvalidToken("Token expired") from exc
        except Exception as exc:  # noqa: BLE001
            raise InvalidToken(f"Token validation failed: {exc}") from exc

        if decoded.get("iat"):
            import time as _time
            skew = int(_time.time()) - int(decoded["iat"])
            if abs(skew) > 60 and getattr(self, "_logged_skew", None) != "logged":
                logger.warning(
                    "auth_clock_skew_detected seconds=%d "
                    "(verifier clock differs from issuer by this much; "
                    "tokens still accepted, but NTP sync recommended)",
                    skew,
                )
                self._logged_skew = "logged"

        iss = decoded.get("iss", "")
        if iss not in self._accept_issuers and iss != self._issuer:
            raise InvalidToken(
                f"Issuer mismatch: token iss={iss!r}, expected one of "
                f"{[*self._accept_issuers, self._issuer]}"
            )

        jti = decoded.get("jti")
        if jti and jti in self._revoked_jtis:
            raise InvalidToken(f"Token revoked (jti={jti})")

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
        """Best-effort background refresh; skips when the TTL cache is still fresh."""
        try:
            await self._refresh_jwks(force=False)
        except Exception as exc:
            logger.debug("client best-effort block failed: %s", exc)

    async def refresh_jwks(self) -> None:
        """Force-refresh the JWKS cache, bypassing the TTL guard."""
        await self._refresh_jwks(force=True)

    async def fetch_me(self, token: str) -> dict | None:
        """Call ``GET /auth/me`` with the bearer token."""
        url = f"{self._issuer}/auth/me"
        try:
            http = await self._get_http()
            r = await http.get(
                url, headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, dict) and data.get("id"):
                return data
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "remote_auth_fetch_me_failed url=%s exc=%s", url, exc,
            )
            return None


    async def _refresh_revocations(self) -> None:
        """Pull the active revocation list from /auth/revocations using ``since=`` for delta."""
        url = f"{self._issuer}/auth/revocations"
        params = (
            {"since": self._last_revocation_sync}
            if self._last_revocation_sync else {}
        )
        http = await self._get_http()
        r = await http.get(url, params=params)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            return
        now_ts = time.time()
        latest = self._last_revocation_sync
        for item in items:
            jti = item.get("jti")
            if not jti:
                continue
            self._revoked_jtis[jti] = int(item.get("expires_at") or 0)
            revoked_at = item.get("revoked_at") or 0
            if revoked_at and revoked_at > latest:
                latest = float(revoked_at)
        self._last_revocation_sync = latest or now_ts
        self._gc_revocations()
        logger.debug(
            "remote_auth_revocations_synced count=%d total_cached=%d",
            len(items), len(self._revoked_jtis),
        )

    def _gc_revocations(self) -> None:
        """Drop revocations whose original token has already expired."""
        now = int(time.time())
        stale = [
            jti for jti, exp in self._revoked_jtis.items()
            if exp and exp < now
        ]
        for jti in stale:
            self._revoked_jtis.pop(jti, None)

    async def _revocation_sync_loop(self) -> None:
        """Background loop: refresh the revocation cache every N seconds."""
        while True:
            try:
                await asyncio.sleep(self._revocation_poll_interval)
                await self._refresh_revocations()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "remote_auth_revocation_sync_failed exc=%s", exc,
                )

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
