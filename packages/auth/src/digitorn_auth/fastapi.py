"""Drop-in FastAPI integration for the central auth service.

Two surfaces, depending on what the consuming service prefers:

  * Middleware — every request goes through, Authorization header is
    parsed, claims land on ``request.state``::

        from digitorn_auth.fastapi import RemoteAuthMiddleware

        app.add_middleware(
            RemoteAuthMiddleware,
            issuer="https://auth.digitorn.ai",
            allow_paths=["/health", "/.well-known/*"],
        )

  * Dependency — for endpoints that opt-in explicitly::

        from digitorn_auth.fastapi import (
            install_remote_auth, require_user, current_claims,
        )

        # In your lifespan:
        await install_remote_auth(app, issuer="https://auth.digitorn.ai")

        @app.get("/me")
        def me(claims = Depends(require_user)):
            return {"user_id": claims.user_id}

Both share the same ``RemoteAuthClient`` instance (stored on
``app.state.remote_auth_client``).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Annotated, Iterable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from digitorn_auth.client import (
    InvalidToken,
    JWKSUnavailable,
    RemoteAuthClaims,
    RemoteAuthClient,
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


# ── Setup helper ──────────────────────────────────────────────────


async def install_remote_auth(
    app: FastAPI,
    issuer: str,
    *,
    accept_issuers: list[str] | None = None,
) -> RemoteAuthClient:
    """Initialize a RemoteAuthClient and stash it on ``app.state``.

    Call this from your lifespan/startup event so JWKS is warm before
    the first request lands.
    """
    client = RemoteAuthClient(issuer=issuer, accept_issuers=accept_issuers)
    await client.start()
    app.state.remote_auth_client = client
    return client


# ── Dependency ────────────────────────────────────────────────────


def _client(request: Request) -> RemoteAuthClient:
    c: RemoteAuthClient | None = getattr(request.app.state, "remote_auth_client", None)
    if c is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RemoteAuthClient not installed (call install_remote_auth in lifespan)",
        )
    return c


async def current_claims(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> RemoteAuthClaims | None:
    """Return claims if a valid Bearer token is present, else None.

    Use this when an endpoint accepts both authenticated and anonymous
    callers (rare). For endpoints that REQUIRE auth, prefer
    ``require_user`` which raises 401.
    """
    if credentials is None or not credentials.credentials:
        return None
    client = _client(request)
    try:
        return client.verify(credentials.credentials)
    except InvalidToken:
        return None


async def require_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> RemoteAuthClaims:
    """Require a valid Bearer token. Raises 401 otherwise."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    client = _client(request)
    try:
        return client.verify(credentials.credentials)
    except InvalidToken as exc:
        # On a kid-miss, attempt one async refresh and retry. Keeps
        # the path responsive across key rotations without forcing a
        # restart of the consuming service.
        if "kid" in str(exc).lower():
            await client.maybe_refresh_jwks()
            try:
                return client.verify(credentials.credentials)
            except InvalidToken as exc2:
                raise HTTPException(status_code=401, detail=str(exc2)) from exc2
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except JWKSUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Cannot reach auth service: {exc}",
        ) from exc


# ── Middleware ────────────────────────────────────────────────────


class RemoteAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every request against the central auth service.

    Reads ``Authorization: Bearer <jwt>``, verifies the token using
    a shared ``RemoteAuthClient``, and stashes the result on
    ``request.state.user_id`` / ``user_email`` / ``roles`` /
    ``permissions`` / ``claims``. Returns 401 on missing or invalid
    tokens, except for ``allow_paths``.

    On startup the middleware lazily installs a ``RemoteAuthClient``
    on ``app.state`` if one isn't there already — meaning a service
    that just wants ``app.add_middleware(RemoteAuthMiddleware,
    issuer=...)`` and nothing else gets a working setup.
    """

    def __init__(
        self,
        app,
        *,
        issuer: str,
        allow_paths: Iterable[str] = (
            "/health", "/healthz", "/.well-known/*", "/docs", "/redoc",
            "/openapi.json", "/auth/login", "/auth/register",
            "/auth/refresh", "/auth/oauth/*", "/auth/revocations",
            "/auth/avatars/*",
        ),
        accept_issuers: list[str] | None = None,
    ):
        super().__init__(app)
        self._issuer = issuer
        self._allow_paths = tuple(allow_paths)
        self._accept_issuers = accept_issuers or []
        self._lazy_client_ready = False
        # JIT user provisioning cache. Auth service is the identity
        # authority -- the daemon's ``users`` table is just a local
        # mirror so FK constraints resolve. We call ``/auth/me`` on
        # first sight and then again every ``_provision_ttl`` seconds
        # to pick up profile changes (email rename, display name,
        # account deactivation). Warm path is a single dict lookup +
        # one ``time.time()`` comparison.
        self._provisioned_user_ids: dict[str, float] = {}
        self._provision_ttl: float = 3600.0  # 1 hour

    def _is_allowed(self, path: str) -> bool:
        # fnmatch's ``*`` greedily matches across ``/`` separators, so
        # ``/api/apps/*/preview`` would also let ``/api/apps/X/sessions/
        # Y/preview`` through and bypass auth. Enforce segment-parity:
        # the pattern's slash count must match the path's slash count
        # (a literal ``*`` always represents exactly one URL segment).
        path_segs = path.count("/")
        for pattern in self._allow_paths:
            # Patterns ending in ``/*`` are explicitly multi-segment
            # (``/.well-known/*``, ``/auth/*``, etc.) — keep loose
            # match so they cover sub-paths as before.
            if pattern.endswith("/*"):
                if fnmatch.fnmatch(path, pattern):
                    return True
                continue
            if pattern.count("/") != path_segs:
                continue
            if fnmatch.fnmatch(path, pattern):
                return True
        return False

    async def _ensure_client(self, request: Request) -> RemoteAuthClient:
        existing = getattr(request.app.state, "remote_auth_client", None)
        if existing is not None:
            return existing
        client = RemoteAuthClient(
            issuer=self._issuer, accept_issuers=self._accept_issuers,
        )
        await client.start()
        request.app.state.remote_auth_client = client
        self._lazy_client_ready = True
        logger.info("remote_auth_client_installed issuer=%s", self._issuer)
        return client

    async def _provision_user(self, request: Request, claims, token: str) -> bool:
        """Mirror the auth-validated user into the daemon's local
        ``users`` table -- but ONLY after confirming the user
        currently exists in the auth service via ``GET /auth/me``.
        The JWT signature being valid is NOT enough: the user could
        have been deleted, disabled, or revoked since issuance. The
        local table must never carry a row the remote auth service
        does not.

        Returns ``True`` if the user is confirmed live (row upserted
        or no DB to mirror into), ``False`` if the auth service says
        the user is gone (caller should reject the request).

        Cached per-process in ``_provisioned_user_ids`` so the warm
        path costs one set-lookup.
        """
        client = getattr(request.app.state, "remote_auth_client", None)
        if client is None:
            return True  # No client available; can't verify, accept best-effort.

        remote_user = await client.fetch_me(token)
        if remote_user is None:
            # Auth service did not confirm this user. Could be a
            # revoked/deleted account, a network blip, or an issuer we
            # can't reach. Don't mirror, and signal upstream so the
            # request can be refused.
            logger.warning(
                "user_jit_provision_auth_denied user_id=%s "
                "(auth service did not confirm user via /me)",
                claims.user_id,
            )
            return False

        # Cross-check: the token's sub MUST match the /me response.
        # A mismatch would mean the JWT and the auth service disagree
        # on identity (token replay, mis-routed auth service, ...).
        if str(remote_user.get("id") or "") != claims.user_id:
            logger.warning(
                "user_jit_provision_identity_mismatch jwt_sub=%s me_id=%s",
                claims.user_id, remote_user.get("id"),
            )
            return False

        try:
            from sqlalchemy import text
            from digitorn.core.database import get_session_factory
            try:
                factory = get_session_factory()
            except Exception:
                # No daemon DB to mirror into (sandbox / pre-bootstrap).
                # User is confirmed live; let the request through.
                self._provisioned_user_ids[claims.user_id] = __import__("time").time()
                return True
            async with factory() as db:
                await db.execute(
                    text(
                        "INSERT INTO public.users "
                        "(id, external_id, provider, email, display_name, "
                        " phone, avatar_url, attributes, is_active, "
                        " created_at, updated_at, last_seen_at) "
                        "VALUES (:id, :external_id, :provider, :email, "
                        " :display_name, :phone, :avatar_url, "
                        " CAST(:attributes AS jsonb), :is_active, "
                        " NOW(), NOW(), NOW()) "
                        "ON CONFLICT (id) DO UPDATE SET "
                        "  last_seen_at = NOW(), "
                        "  email = COALESCE(EXCLUDED.email, public.users.email), "
                        "  display_name = COALESCE(EXCLUDED.display_name, "
                        "                         public.users.display_name), "
                        "  is_active = EXCLUDED.is_active"
                    ),
                    {
                        "id": remote_user.get("id"),
                        "external_id": remote_user.get("external_id")
                            or remote_user.get("id"),
                        "provider": remote_user.get("provider") or "remote_auth",
                        "email": remote_user.get("email"),
                        "display_name": remote_user.get("display_name"),
                        "phone": remote_user.get("phone"),
                        "avatar_url": remote_user.get("avatar_url"),
                        "attributes": __import__("json").dumps(
                            remote_user.get("attributes") or {}
                        ),
                        "is_active": bool(remote_user.get("is_active", True)),
                    },
                )
                await db.commit()
            self._provisioned_user_ids[claims.user_id] = __import__("time").time()
            return True
        except Exception as exc:
            # DB-mirror failed but the user IS confirmed live by /me.
            # Log loudly and let the request through. Don't blacklist
            # (would mask transient DB issues forever).
            logger.warning(
                "user_jit_provision_db_mirror_failed user_id=%s err=%s",
                claims.user_id, exc,
            )
            return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # CORS preflight (OPTIONS) MUST bypass auth. The fetch spec
        # forbids browsers from attaching ``Authorization`` to a
        # preflight; the server is expected to reply with the CORS
        # headers and 2xx, after which the browser fires the real
        # request WITH the credentials. Authenticating the preflight
        # itself returns 401, the browser cancels the cycle, and the
        # actual request never reaches us. Cross-origin clients
        # (web app on a different host, Flutter web build, future
        # public hub.digitorn.ai callbacks) all need this bypass.
        # The downstream ``CORSMiddleware`` already handles the
        # response shape — we just let it through.
        if request.method == "OPTIONS":
            return await call_next(request)

        if self._is_allowed(path):
            return await call_next(request)

        client = await self._ensure_client(request)

        # Token resolution order:
        #   1. ``Authorization: Bearer <jwt>`` header (default for
        #      ``fetch`` / ``axios`` / mobile clients).
        #   2. ``?token=<jwt>`` query param (fallback for HTML
        #      surfaces that can't set custom headers - iframes
        #      embedded by the preview SDK, ``<img>``/``<script>``
        #      tags loaded by browser engines, etc). Mirror of the
        #      websocket-upgrade convention used by ``/preview-server/ws``.
        token: str | None = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
        else:
            qp_token = request.query_params.get("token")
            if qp_token:
                token = qp_token.strip()
        if not token:
            return JSONResponse(
                {"detail": "Missing bearer token"}, status_code=401,
            )
        # JWT signature verification is CPU-bound crypto (RSA / ECDSA
        # signature check via ``cryptography`` C bindings). The C call
        # itself releases the GIL but Python-side claim parsing /
        # validation does not. Per-request cost runs 5-50ms, but under
        # concurrent load these stack up and serialize on the GIL,
        # which on Windows + winloop manifests as 3-20s main-loop
        # stalls. ``asyncio.to_thread`` moves it off the main thread.
        try:
            import asyncio as _asyncio
            claims = await _asyncio.to_thread(client.verify, token)
        except InvalidToken as exc:
            if "kid" in str(exc).lower():
                await client.maybe_refresh_jwks()
                try:
                    claims = await _asyncio.to_thread(client.verify, token)
                except InvalidToken as exc2:
                    return JSONResponse({"detail": str(exc2)}, status_code=401)
            else:
                return JSONResponse({"detail": str(exc)}, status_code=401)
        except JWKSUnavailable as exc:
            return JSONResponse(
                {"detail": f"Auth service unavailable: {exc}"}, status_code=503,
            )

        request.state.user_id = claims.user_id
        request.state.user_email = claims.email
        request.state.roles = claims.roles
        request.state.permissions = claims.permissions
        request.state.claims = claims

        # JIT-mirror the user into the local ``users`` table so daemon
        # FK constraints (``user_sessions.user_id``, ``user_devices``,
        # ``agent_runs`` ...) resolve. Auth service is the source of
        # truth: we call ``GET /auth/me`` to confirm the user EXISTS
        # there before inserting locally, otherwise a revoked/deleted
        # account whose JWT is still within its expiry window would
        # leak into the local table. Re-verified every
        # ``_provision_ttl`` seconds to catch profile changes /
        # deactivations.
        #
        # ``asyncio.shield`` is critical: starlette's
        # ``BaseHTTPMiddleware`` internally uses ``anyio.create_task_group``
        # to coordinate request lifecycle. A client disconnect mid-await
        # cancels the dispatch coroutine; without shielding, the
        # in-flight ``_provision_user`` task can be torn down from a
        # different task than the one that entered it, tripping
        # anyio's "cancel scope exited in a different task"
        # ``RuntimeError`` -- which propagates up to the lifespan and
        # shuts the daemon down. Shielding lets the provisioning
        # finish on its own task graph.
        if claims.user_id:
            import asyncio as _aio
            import time as _time
            _last_ts = self._provisioned_user_ids.get(claims.user_id)
            _now = _time.time()
            if _last_ts is None or (_now - _last_ts) > self._provision_ttl:
                try:
                    ok = await _aio.shield(
                        self._provision_user(request, claims, token),
                    )
                except _aio.CancelledError:
                    # The CALLER was cancelled (client disconnect).
                    # Shield kept the provisioning coroutine running
                    # to completion -- which is what we want. Just
                    # propagate the cancellation cleanly so starlette
                    # tears down without confusion.
                    raise
                except Exception as exc:
                    logger.warning(
                        "user_jit_provision_unhandled_error user_id=%s err=%s",
                        claims.user_id, exc,
                    )
                    ok = True  # don't block on internal errors
                if not ok:
                    return JSONResponse(
                        {"detail": "User not recognized by auth service"},
                        status_code=401,
                    )
        # Stash the raw token so downstream code (e.g. the LLM
        # gateway router) can forward it. The token has already been
        # verified above; we keep the raw form because re-issuing or
        # re-signing is not allowed (the daemon is a verifier, not
        # an issuer here).
        request.state.access_token = token
        # Also publish on a ContextVar so async code spawned from the
        # request handler (background turn dispatcher, run tracker)
        # can read it without touching the FastAPI Request object.
        # ContextVars propagate across ``asyncio.create_task``.
        try:
            from digitorn.core.runtime.request_context import set_inbound_user_jwt
            set_inbound_user_jwt(token)
        except Exception:
            pass
        return await call_next(request)
