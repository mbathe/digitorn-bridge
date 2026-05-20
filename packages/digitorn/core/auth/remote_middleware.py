"""Drop-in FastAPI integration for the central auth service."""

from __future__ import annotations

import fnmatch
import logging
from typing import Annotated, Iterable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from digitorn.core.auth.remote_client import (
    InvalidToken,
    JWKSUnavailable,
    RemoteAuthClaims,
    RemoteAuthClient,
)

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)


async def install_remote_auth(
    app: FastAPI,
    issuer: str,
    *,
    accept_issuers: list[str] | None = None,
) -> RemoteAuthClient:
    """Initialize a RemoteAuthClient and stash it on ``app.state``."""
    client = RemoteAuthClient(issuer=issuer, accept_issuers=accept_issuers)
    await client.start()
    app.state.remote_auth_client = client
    return client


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
    """Return claims if a valid Bearer token is present, else None."""
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
    """Require a valid Bearer token."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    client = _client(request)
    try:
        return client.verify(credentials.credentials)
    except InvalidToken as exc:
        # kid-miss proves JWKS cache is stale (keys rotated); force a refresh.
        if "kid" in str(exc).lower():
            try:
                await client.refresh_jwks()
            except Exception as exc:
                logger.debug("fastapi best-effort block failed: %s", exc)
            try:
                return client.verify(credentials.credentials)
            except InvalidToken as exc2:
                raise HTTPException(status_code=401, detail=str(exc2)) from exc2
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except JWKSUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"Cannot reach auth service: {exc}",
        ) from exc


class RemoteAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every request against the central auth service."""

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
        # JIT cache keyed by user_id; refreshed every _provision_ttl seconds.
        self._provisioned_user_ids: dict[str, float] = {}
        self._provision_ttl: float = 3600.0

    def _is_allowed(self, path: str) -> bool:
        # enforce segment parity so '*' doesn't greedily cross '/' boundaries.
        path_segs = path.count("/")
        for pattern in self._allow_paths:
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
        """Mirror the auth-validated user into the daemon's local users table after /auth/me confirms."""
        client = getattr(request.app.state, "remote_auth_client", None)
        if client is None:
            return True

        remote_user = await client.fetch_me(token)
        if remote_user is None:
            logger.warning(
                "user_jit_provision_auth_denied user_id=%s "
                "(auth service did not confirm user via /me)",
                claims.user_id,
            )
            return False

        # token sub MUST match /me response or it's a replay / routing bug.
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
                self._provisioned_user_ids[claims.user_id] = __import__("time").time()
                return True
            async with factory() as db:
                # dialect-specific JSON cast + now() so the same INSERT runs on Postgres and SQLite.
                dialect = db.bind.dialect.name if db.bind else "postgresql"
                schema = "public." if dialect == "postgresql" else ""
                attr_expr = (
                    "CAST(:attributes AS jsonb)"
                    if dialect == "postgresql" else ":attributes"
                )
                now_fn = "NOW()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"

                await db.execute(
                    text(
                        f"INSERT INTO {schema}users "
                        "(id, external_id, provider, email, display_name, "
                        " phone, avatar_url, attributes, is_active, "
                        " created_at, updated_at, last_seen_at) "
                        "VALUES (:id, :external_id, :provider, :email, "
                        " :display_name, :phone, :avatar_url, "
                        f" {attr_expr}, :is_active, "
                        f" {now_fn}, {now_fn}, {now_fn}) "
                        "ON CONFLICT (id) DO NOTHING"
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
            logger.warning(
                "user_jit_provision_db_mirror_failed user_id=%s err=%s",
                claims.user_id, exc,
            )
            return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # CORS preflight cannot carry Authorization; let CORSMiddleware handle it.
        if request.method == "OPTIONS":
            return await call_next(request)

        if self._is_allowed(path):
            return await call_next(request)

        client = await self._ensure_client(request)

        # Token sources: Authorization header preferred, ?token= query param fallback.
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
        # JWT verify is CPU-bound; offload to a thread so it doesn't stall the event loop.
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

        # shield protects the JIT-provision coroutine from starlette client-disconnect cancellation.
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
                    raise
                except Exception as exc:
                    logger.warning(
                        "user_jit_provision_unhandled_error user_id=%s err=%s",
                        claims.user_id, exc,
                    )
                    ok = True
                if not ok:
                    return JSONResponse(
                        {"detail": "User not recognized by auth service"},
                        status_code=401,
                    )
        request.state.access_token = token
        # ContextVar lets spawned async tasks forward the bearer without touching Request.
        try:
            from digitorn.core.runtime.request_context import set_inbound_user_jwt
            set_inbound_user_jwt(token)
        except Exception as exc:
            logger.debug("fastapi best-effort block failed: %s", exc)
        return await call_next(request)
