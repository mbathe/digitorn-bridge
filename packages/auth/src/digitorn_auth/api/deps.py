"""FastAPI dependencies shared across the API routers.

Centralises:
  - DB session (``get_db``)
  - Current user resolution (``require_user``)
  - JWT-bearing AuthService access (``get_auth_service``)
"""

from __future__ import annotations

from typing import Annotated, AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_auth.database import get_session_factory
from digitorn_auth.models import User
from digitorn_auth.service import AuthService

bearer_scheme = HTTPBearer(auto_error=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a fresh DB session per request, closed on exit."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_auth_service(request: Request) -> AuthService:
    svc = getattr(request.app.state, "auth_service", None)
    if svc is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service not initialised",
        )
    return svc


async def require_user(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """Require a valid access token in the Authorization header.

    Returns the User row for downstream handlers. Raises 401 on missing
    or invalid token.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    try:
        payload = auth._jwt.verify(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")
    if payload.token_type != "access":
        raise HTTPException(status_code=401, detail="Wrong token type")

    # Reject revoked jtis. With ACCESS_TOKEN_TTL=0 (never-expire) this
    # is the only way logout actually invalidates a token here -
    # otherwise the JWT signature stays valid forever.
    if payload.jti and payload.jti in auth._revoked_jtis:
        raise HTTPException(status_code=401, detail="Token revoked")

    user = await db.get(User, payload.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")
    return user
