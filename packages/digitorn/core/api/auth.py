"""Auth API endpoints — login, register, token refresh, user management.

All endpoints return JSON. Token-based authentication via Bearer header.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    username: str | None = Field(default=None, description="Username (for local/LDAP)")
    email: str | None = Field(default=None, description="Email (alternative to username)")
    password: str = Field(..., description="Password")
    provider: str | None = Field(default=None, description="Auth provider (default: local)")

class RegisterRequest(BaseModel):
    model_config = {"populate_by_name": True, "extra": "ignore"}

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=12, max_length=128)
    email: str | None = Field(default=None)
    # BUG-060: older SDKs send `name` instead of `display_name`. Accept
    # either via the ``name`` alias so the field stops silently vanishing
    # from the stored profile.
    display_name: str | None = Field(default=None, alias="name")

class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token to exchange")


class LogoutRequest(BaseModel):
    """Logout body — everything is optional.

    Callers typically POST an empty body and rely on the Authorization
    header to identify the access token to revoke. Older SDKs that pass
    a refresh_token keep working; both get revoked atomically.
    """

    refresh_token: str | None = Field(default=None)

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    expires_in: int = 0
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = []
    permissions: list[str] = []
    is_admin: bool = False

class UserResponse(BaseModel):
    user_id: str
    email: str | None = None
    display_name: str | None = None
    roles: list[str] = []
    permissions: list[str] = []
    is_admin: bool = False
    # Extra UI metadata — avatar_url / created_at / last_seen_at
    # come from the User DB row (populated by the profile store).
    avatar_url: str | None = None
    created_at: str | None = None
    last_seen_at: str | None = None
    phone: str | None = None


def _is_admin(roles: list[str] | None, permissions: list[str] | None) -> bool:
    return "admin" in (roles or []) or "*" in (permissions or [])

class ErrorResponse(BaseModel):
    error: str

class SessionResponse(BaseModel):
    session_id: str
    app_id: str
    created_at: str
    last_active_at: str

def _get_auth(request: Request):
    """Get AuthService from app state. Raises 503 if auth is disabled."""
    auth = getattr(request.app.state, "auth_service", None)
    if auth is None:
        from starlette.exceptions import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Authentication is disabled on this daemon",
        )
    return auth

@router.post("/login", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def login(body: LoginRequest, request: Request):
    """Authenticate and receive JWT tokens."""
    auth = _get_auth(request)

    credentials = {"password": body.password}
    if body.username:
        credentials["username"] = body.username
    if body.email:
        credentials["email"] = body.email
    # Pass the caller's real IP so the lockout is keyed by (identifier,
    # ip) instead of identifier alone — stops email-scoped DoS where
    # an attacker locked any account by spamming bad passwords.
    client = getattr(request, "client", None)
    credentials["ip"] = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or getattr(client, "host", "") or ""
    )

    result = await auth.login(credentials, provider=body.provider)

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/register", response_model=TokenResponse, responses={400: {"model": ErrorResponse}})
async def register(body: RegisterRequest, request: Request):
    """Register a new local user and receive JWT tokens."""
    auth = _get_auth(request)

    result = await auth.register(
        username=body.username,
        password=body.password,
        email=body.email,
        display_name=body.display_name,
    )

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=400, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        email=result.email,
        display_name=result.display_name,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/refresh", response_model=TokenResponse, responses={401: {"model": ErrorResponse}})
async def refresh(body: RefreshRequest, request: Request):
    """Exchange a refresh token for a new access token."""
    auth = _get_auth(request)

    result = await auth.refresh(body.refresh_token)

    if not result.success:
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": result.error})

    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        roles=result.roles or [],
        permissions=result.permissions or [],
        is_admin=_is_admin(result.roles, result.permissions),
    )

@router.post("/logout")
async def logout(request: Request, body: LogoutRequest | None = None):
    """Revoke the caller's session.

    Accepts an empty body. The access token is taken from the
    ``Authorization: Bearer`` header (the same place the middleware
    found it to let the request through), so logout is a single call
    from the client with no payload required.
    """
    auth = _get_auth(request)
    refresh_token = body.refresh_token if body else None
    access_token: str | None = None
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        access_token = header[7:].strip() or None
    success = await auth.logout(
        refresh_token=refresh_token,
        access_token=access_token,
    )
    return {"success": success}

@router.get("/me", response_model=UserResponse, responses={401: {"model": ErrorResponse}})
async def me(request: Request):
    """Get current authenticated user info.

    Joins the ``users`` DB row on top of the in-memory auth user
    so the response carries avatar_url, created_at, last_seen_at,
    and phone — the fields the Flutter Settings > General screen
    hydrates from in one round-trip.
    """
    user = request.state.user
    if user is None:
        # Dev mode (auth disabled) — middleware sets user_id="local",
        # roles=["admin"], permissions=["*"]. Return a synthetic admin
        # stub so Flutter sees is_admin=true locally.
        state_perms = list(getattr(request.state, "permissions", []) or [])
        state_roles = list(getattr(request.state, "roles", []) or [])
        state_uid = getattr(request.state, "user_id", "anonymous")
        if state_uid and state_uid != "anonymous":
            return UserResponse(
                user_id=state_uid,
                email=None,
                display_name=state_uid.title(),
                roles=state_roles,
                permissions=state_perms,
                is_admin=_is_admin(state_roles, state_perms),
            )
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    avatar_url: str | None = None
    created_at: str | None = None
    last_seen_at: str | None = None
    phone: str | None = None

    try:
        from digitorn.core.database import get_session_factory
        from digitorn.core.models import User
        from sqlalchemy import select as _sel
        async with get_session_factory()() as db:
            row = (
                await db.execute(
                    _sel(User).where(User.id == user.user_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                avatar_url = row.avatar_url
                created_at = row.created_at.isoformat() if row.created_at else None
                last_seen_at = (
                    row.last_seen_at.isoformat() if row.last_seen_at else None
                )
                phone = row.phone
    except Exception:
        # Best-effort join — if the users table isn't populated
        # (anonymous / dev mode), we still return the basic fields.
        pass

    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        display_name=user.display_name,
        roles=user.roles or [],
        permissions=user.permissions or [],
        is_admin=_is_admin(user.roles, user.permissions),
        avatar_url=avatar_url,
        created_at=created_at,
        last_seen_at=last_seen_at,
        phone=phone,
    )

@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(request: Request):
    """List all sessions for the current user."""
    user_id = request.state.user_id
    if user_id == "anonymous":
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    from digitorn.core.models import UserSession
    from digitorn.core.database import get_session_factory

    factory = get_session_factory()
    from sqlalchemy import select

    async with factory() as session:
        stmt = select(UserSession).where(UserSession.user_id == user_id).order_by(UserSession.last_active_at.desc())
        result = await session.execute(stmt)
        sessions = result.scalars().all()

        return [
            SessionResponse(
                session_id=s.session_id,
                app_id=s.app_id,
                created_at=s.created_at.isoformat(),
                last_active_at=s.last_active_at.isoformat(),
            )
            for s in sessions
        ]

@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    """Delete a session (current user only)."""
    user_id = request.state.user_id
    if user_id == "anonymous":
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    from digitorn.core.models import UserSession
    from digitorn.core.database import get_session_factory
    from sqlalchemy import select, delete

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(UserSession).where(
            UserSession.session_id == session_id,
            UserSession.user_id == user_id,
        )
        us = (await session.execute(stmt)).scalar_one_or_none()
        if not us:
            from starlette.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "Session not found"})

        await session.delete(us)
        await session.commit()

    return {"deleted": session_id}

@router.get("/sessions/{session_id}/history")
async def session_history(session_id: str, request: Request):
    """Get the message history of a session for resuming."""
    user_id = request.state.user_id
    if user_id == "anonymous":
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    from digitorn.core.models import UserSession
    from digitorn.core.database import get_session_factory
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(UserSession).where(
            UserSession.session_id == session_id,
            UserSession.user_id == user_id,
        )
        us = (await session.execute(stmt)).scalar_one_or_none()
        if not us:
            from starlette.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "Session not found"})

    from digitorn.core.app.sessions import SessionStore
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        return {"session_id": session_id, "messages": [], "note": "Session store not available"}

    messages = store.load_messages(us.app_id, session_id)
    return {
        "session_id": session_id,
        "app_id": us.app_id,
        "messages": messages or [],
        "message_count": len(messages) if messages else 0,
    }

class ForkRequest(BaseModel):
    new_session_id: str | None = Field(default=None, description="ID for the forked session (auto-generated if omitted)")

@router.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, body: ForkRequest, request: Request):
    """Fork a session — create a copy with its own message history."""
    user_id = request.state.user_id
    if user_id == "anonymous":
        from starlette.responses import JSONResponse
        return JSONResponse(status_code=401, content={"error": "Not authenticated"})

    from digitorn.core.models import UserSession
    from digitorn.core.database import get_session_factory
    from sqlalchemy import select
    import uuid

    factory = get_session_factory()
    async with factory() as session:
        stmt = select(UserSession).where(
            UserSession.session_id == session_id,
            UserSession.user_id == user_id,
        )
        us = (await session.execute(stmt)).scalar_one_or_none()
        if not us:
            from starlette.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "Session not found"})

        new_id = body.new_session_id or str(uuid.uuid4())

        new_us = UserSession(
            session_id=new_id,
            app_id=us.app_id,
            user_id=user_id,
        )
        session.add(new_us)
        await session.commit()

    store = getattr(request.app.state, "session_store", None)
    if store:
        store.fork(us.app_id, session_id, new_id)

    return {
        "forked_from": session_id,
        "new_session_id": new_id,
        "app_id": us.app_id,
    }
