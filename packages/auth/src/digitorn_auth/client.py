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
        revocation_poll_interval: int = 300,
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
        # Shared httpx client. Per-request ``async with httpx.AsyncClient(...)``
        # combined with starlette's anyio task-groups triggers the
        # "cancel scope in a different task" RuntimeError under
        # concurrent middleware requests (request cancellation cleans
        # up the scope from a different task than the one that opened
        # it). A long-lived shared client owned by ``RemoteAuthClient``
        # avoids that whole class of bug, plus reduces TCP/TLS
        # handshake cost on every request-path call.
        self._http: Any | None = None  # httpx.AsyncClient, lazy-init

    async def _get_http(self) -> Any:
        """Return the shared httpx client, creating it on first use.

        ``httpx.AsyncClient(...)`` constructor calls
        ``ssl.create_default_context()`` synchronously, which on
        Windows walks the OS certificate store and loads every CA
        cert. That walk routinely takes 6-30 seconds the first time
        and blocks the event loop -- the watchdog observed this
        exact pattern (``httpx/_client.py:189 __init__`` in stacks).
        We off-load the construction to a worker thread so the loop
        stays free.
        """
        if self._http is None:
            import asyncio as _asyncio
            import httpx

            def _build_client() -> Any:
                return httpx.AsyncClient(timeout=self._http_timeout)

            # Coalesce concurrent first-use callers behind the same
            # build so we don't pay the SSL ctx cost N times.
            async with self._refresh_lock:
                if self._http is None:
                    self._http = await _asyncio.to_thread(_build_client)
        return self._http

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
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None

    async def _discover(self) -> None:
        """Resolve the JWKS URL via OIDC discovery.

        Uses the shared httpx client to avoid the Windows CA-store
        load (``ssl.create_default_context``) that fires every time a
        new client is built -- 6-30 second event-loop stalls observed.
        """
        try:
            http = await self._get_http()
            r = await http.get(
                f"{self._issuer}/.well-known/openid-configuration",
            )
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
            try:
                # Shared httpx client -- per-call construction triggers
                # the Windows CA-store load which blocks the main loop.
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
                # We DELIBERATELY skip ``iat`` and ``nbf`` validation.
                # Per RFC 7519 §4.1.6 ``iat`` is informational ("can be
                # used to determine the age of the JWT") — NOT a
                # security boundary. The signature already proves the
                # token was issued by the auth service; ``iat`` being
                # "in the future" only happens when the verifier's
                # clock is BEHIND the issuer's. Blocking on that locks
                # out legitimate users whose laptop / VM clock drifted
                # (NTP failure, suspend/resume, wrong TZ config) — a
                # routine consumer-machine scenario.
                #
                # ``exp`` STAYS enforced (with 60 s leeway): a stolen
                # token must not outlive its declared lifetime even on
                # a slow clock. ``leeway`` here absorbs realistic
                # ~1-second drift between auth service and daemon
                # without weakening the exp boundary in any
                # meaningful way.
                #
                # ``nbf`` is also disabled because it has the exact
                # same skew failure mode as ``iat`` and we don't use
                # it. Auth service doesn't issue tokens with ``nbf``.
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

        # Diagnostic only — never blocks. If the verifier's wall clock
        # is way off from the issuer's, log it once so the operator
        # can fix NTP. We're already past the verify so this is
        # purely informational and doesn't add a request-path cost
        # beyond a single integer subtraction + bounded set check.
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
        """Best-effort background refresh — call from a periodic task.
        Skips when the TTL cache is still fresh."""
        try:
            await self._refresh_jwks(force=False)
        except Exception:
            pass

    async def refresh_jwks(self) -> None:
        """Force-refresh the JWKS cache, bypassing the TTL guard.

        Use after a ``verify()`` raised ``InvalidToken`` with a
        kid-miss: the miss itself proves the cache is stale, so the
        TTL-based ``maybe_refresh_jwks`` skip is exactly wrong. We
        still respect the lock so concurrent kid-misses don't fan-out
        N HTTP fetches.
        """
        await self._refresh_jwks(force=True)

    async def fetch_me(self, token: str) -> dict | None:
        """Call the canonical ``GET /auth/me`` endpoint with the bearer
        token. Returns the user dict on success, ``None`` on any
        failure (network, 404, 401, etc).

        Used by consuming services to mirror the auth-owned identity
        into their local row before applying FK-bound writes. The auth
        service is authoritative -- if /me fails for an active user,
        the row must NOT be mirrored.

        Uses the long-lived shared ``self._http`` client; per-call
        ``async with httpx.AsyncClient(...)`` would create a cancel
        scope owned by the request task, which under starlette's
        request cancellation can get torn down in a different task
        and trip anyio's "cancel scope exited in a different task"
        RuntimeError (which then bubbles up and shuts the daemon
        down). Shared client = no cancel scope = no race.
        """
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

    # ── Revocation sync ────────────────────────────────────────────

    async def _refresh_revocations(self) -> None:
        """Pull the active revocation list from /auth/revocations.

        Uses ``since=`` so each tick only fetches new revocations — the
        in-memory dict is the union of every batch we've ever pulled.
        Old jtis whose original `expires_at` has passed get pruned by
        ``_gc_revocations`` after each merge.

        Uses the shared long-lived ``httpx.AsyncClient`` (same client
        as ``fetch_me`` + ``_refresh_jwks``). Constructing a fresh
        ``httpx.AsyncClient`` per poll loaded the Windows OS CA store
        each time -- a 6-30 second synchronous syscall observed by the
        event-loop watchdog. Sharing the client keeps the SSL context
        warm across ticks.
        """
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
