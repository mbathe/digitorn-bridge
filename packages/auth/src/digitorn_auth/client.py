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
        revocation_poll_interval: int = 30,
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
        # Revocation list: jti -> expires_at (epoch seconds; 0 = never).
        # Hot-path verify() refuses any token whose jti is in this set.
        # Refreshed by the background sync task that polls
        # /auth/revocations on the central auth service.
        self._revoked_jtis: dict[str, int] = {}
        self._last_revocation_sync: float = 0
        self._revocation_poll_interval = revocation_poll_interval
        self._revocation_task: asyncio.Task[None] | None = None

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        """Discover endpoints + fetch JWKS + start revocation sync.

        Tolerates a network failure: the JWKS cache stays empty
        (``verify()`` re-tries once before failing) and the revocation
        list starts empty (gets populated on the next sync tick).
        """
        await self._discover()
        try:
            await self._refresh_jwks(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_auth_initial_jwks_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )
        # Initial revocation pull so a token revoked before this client
        # started gets caught on the very first verify().
        try:
            await self._refresh_revocations()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "remote_auth_initial_revocations_fetch_failed issuer=%s exc=%s",
                self._issuer, exc,
            )
        # Background sync. Runs forever; cancelled via close().
        if self._revocation_task is None or self._revocation_task.done():
            self._revocation_task = asyncio.create_task(
                self._revocation_sync_loop(),
                name="remote_auth_revocation_sync",
            )

    async def close(self) -> None:
        """Stop the revocation sync task. Safe to call multiple times."""
        if self._revocation_task is not None and not self._revocation_task.done():
            self._revocation_task.cancel()
            try:
                await self._revocation_task
            except (asyncio.CancelledError, Exception):
                pass
        self._revocation_task = None

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

        # Revocation check. With ACCESS_TOKEN_TTL=0 (never-expire), this
        # is the only path that lets a logout actually invalidate a
        # token at the daemon: the central writes the jti to
        # ``revoked_tokens``, ``_refresh_revocations`` pulls it into
        # this dict on the next tick, and verify() rejects it here.
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
        """Best-effort background refresh — call from a periodic task or
        after a verify() raised InvalidToken with a kid-miss."""
        try:
            await self._refresh_jwks(force=False)
        except Exception:
            pass

    # ── Revocation sync ────────────────────────────────────────────

    async def _refresh_revocations(self) -> None:
        """Pull the active revocation list from /auth/revocations.

        Uses ``since=`` so each tick only fetches new revocations — the
        in-memory dict is the union of every batch we've ever pulled.
        Old jtis whose original `expires_at` has passed get pruned by
        ``_gc_revocations`` after each merge.
        """
        import httpx
        url = f"{self._issuer}/auth/revocations"
        params = {"since": self._last_revocation_sync} if self._last_revocation_sync else {}
        async with httpx.AsyncClient(timeout=self._http_timeout) as http:
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
        """Drop revocations whose original token has already expired —
        the signature check rejects them on its own, no need to keep
        tracking. ``expires_at == 0`` means the original token never
        expires; those rows stay forever."""
        now = int(time.time())
        stale = [
            jti for jti, exp in self._revoked_jtis.items()
            if exp and exp < now
        ]
        for jti in stale:
            self._revoked_jtis.pop(jti, None)

    async def _revocation_sync_loop(self) -> None:
        """Background loop: refresh the revocation cache every N
        seconds. Survives transient errors (logs and retries on the
        next tick). Cancelled via ``close()``."""
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
