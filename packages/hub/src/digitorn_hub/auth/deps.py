from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import ApiToken, User
from .central import (
    CentralAuthClient,
    CentralClaims,
    InvalidCentralToken,
    looks_like_rs256,
)
from .jwt import JWTError, decode
from .tokens import (
    has_scope,
    hash_for_lookup,
    looks_like_api_token,
    parse_authorization_header,
)

logger = logging.getLogger(__name__)


class AuthPrincipal:
    def __init__(self, user: User, scopes: list[str], via_token: bool):
        self.user = user
        self.scopes = scopes
        self.via_token = via_token

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    def has(self, scope: str) -> bool:
        return has_scope(self.scopes, scope)


async def _resolve_jwt(
    raw: str, session: AsyncSession
) -> AuthPrincipal:
    try:
        payload = decode(raw, expected_type="access")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    user_id = uuid.UUID(payload["sub"])
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or disabled")
    return AuthPrincipal(user=user, scopes=["*"], via_token=False)


async def _resolve_api_token(
    raw: str, session: AsyncSession
) -> AuthPrincipal:
    token_hash = hash_for_lookup(raw)
    stmt = (
        select(ApiToken, User)
        .join(User, User.id == ApiToken.user_id)
        .where(ApiToken.token_hash == token_hash)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    api_token, user = row
    if api_token.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token revoked")
    if not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user disabled")

    await session.execute(
        update(ApiToken)
        .where(ApiToken.id == api_token.id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    await session.commit()

    return AuthPrincipal(user=user, scopes=list(api_token.scopes), via_token=True)


async def _resolve_central_jwt(
    raw: str,
    session: AsyncSession,
    client: CentralAuthClient,
) -> AuthPrincipal:
    """Verify a JWT issued by the central auth service and bind it to
    a Hub user, auto-provisioning by email on first contact.

    The Hub keeps its own ``users`` table because Hub-owned data
    (publishers, reviews, downloads, api_tokens) FKs to it. We mirror
    the central identity by email - the central is the single source
    of truth for ``email`` / ``display_name`` / ``role``, the Hub row
    is just a local handle to attach Hub-side data to.
    """
    try:
        claims = client.verify(raw)
    except InvalidCentralToken as exc:
        # Soft-retry once on a kid-miss (key rotation) before failing.
        if "kid" in str(exc).lower():
            await client.maybe_refresh_jwks()
            try:
                claims = client.verify(raw)
            except InvalidCentralToken as exc2:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, str(exc2),
                ) from exc2
        else:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, str(exc),
            ) from exc

    if not claims.email:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "central JWT has no email claim - cannot link to Hub user",
        )

    # Find existing Hub user by email; auto-provision if missing.
    stmt = select(User).where(User.email == claims.email)
    user = (await session.execute(stmt)).scalar_one_or_none()

    if user is None:
        user = User(
            email=claims.email,
            display_name=claims.display_name or claims.email,
            # No local password - this account is managed by the
            # central. Store an unusable hash so the local /auth/login
            # path cannot be used to bypass central revocation.
            password_hash="!central-managed!",
            role="admin" if "admin" in claims.roles else "user",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(
            "hub_user_provisioned_from_central email=%s user_id=%s",
            claims.email, user.id,
        )
    elif not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "user disabled",
        )

    # Central JWTs grant the wildcard scope - the central handles
    # fine-grained authz upstream. Hub-local API tokens still keep
    # their own scope set.
    return AuthPrincipal(user=user, scopes=["*"], via_token=False)


async def get_current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    session: AsyncSession = Depends(get_session),
) -> AuthPrincipal:
    raw = parse_authorization_header(authorization)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    # Hub-local API tokens win first (clear prefix - cheap to detect).
    if looks_like_api_token(raw):
        return await _resolve_api_token(raw, session)

    # Central RS256 JWTs - only when the central client was wired up
    # at startup (HUB_AUTH_SERVICE_URL set + JWKS reachable).
    central: CentralAuthClient | None = getattr(
        request.app.state, "central_auth", None,
    )
    if central is not None and looks_like_rs256(raw):
        return await _resolve_central_jwt(raw, session, central)

    # Hub-local HS256 JWTs (legacy / single-machine dev).
    return await _resolve_jwt(raw, session)


async def get_current_user(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> User:
    return principal.user


def require_scope(scope: str):
    async def _checker(
        principal: AuthPrincipal = Depends(get_current_principal),
    ) -> AuthPrincipal:
        if not principal.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"missing required scope: {scope}"
            )
        return principal

    return _checker


def require_admin(
    principal: AuthPrincipal = Depends(get_current_principal),
) -> AuthPrincipal:
    if principal.user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return principal
