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

    def _is_allowed(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self._allow_paths)

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

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

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
        try:
            claims = client.verify(token)
        except InvalidToken as exc:
            if "kid" in str(exc).lower():
                await client.maybe_refresh_jwks()
                try:
                    claims = client.verify(token)
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
        return await call_next(request)
