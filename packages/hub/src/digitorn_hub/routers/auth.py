from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthPrincipal, get_current_principal, get_current_user
from ..auth.jwt import JWTError, decode, encode
from ..auth.passwords import hash_password, verify_password
from ..auth.tokens import issue, normalize_scopes
from ..db import get_session
from ..models import ApiToken, User
from ..schemas import (
    ApiTokenCreate,
    ApiTokenIssued,
    ApiTokenOut,
    RefreshIn,
    TokenPair,
    UserLogin,
    UserOut,
    UserRegister,
)
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_pair(user_id) -> TokenPair:
    s = get_settings()
    return TokenPair(
        access_token=encode(user_id, "access"),
        refresh_token=encode(user_id, "refresh"),
        expires_in=s.jwt_access_ttl_minutes * 60,
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserRegister, session: AsyncSession = Depends(get_session)
) -> User:
    user = User(
        email=str(payload.email).lower(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email already registered")
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(
    payload: UserLogin, session: AsyncSession = Depends(get_session)
) -> TokenPair:
    stmt = select(User).where(User.email == str(payload.email).lower())
    user = (await session.execute(stmt)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user disabled")
    return _token_pair(user.id)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshIn) -> TokenPair:
    try:
        decoded = decode(payload.refresh_token, expected_type="refresh")
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc))
    import uuid as _uuid

    return _token_pair(_uuid.UUID(decoded["sub"]))


@router.get("/me", response_model=UserOut)
async def me(current: User = Depends(get_current_user)) -> User:
    return current


# ---------- API tokens (managed by JWT-authed user) ----------


@router.post(
    "/tokens",
    response_model=ApiTokenIssued,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_token(
    payload: ApiTokenCreate,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> ApiTokenIssued:
    if principal.via_token:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API tokens cannot mint other API tokens - log in with a JWT",
        )
    issued = issue()
    record = ApiToken(
        user_id=principal.user_id,
        name=payload.name,
        token_hash=issued.token_hash,
        prefix=issued.prefix,
        scopes=normalize_scopes(payload.scopes),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return ApiTokenIssued(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        scopes=record.scopes,
        last_used_at=record.last_used_at,
        created_at=record.created_at,
        revoked_at=record.revoked_at,
        plaintext=issued.plaintext,
    )


@router.get("/tokens", response_model=list[ApiTokenOut])
async def list_api_tokens(
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> list[ApiToken]:
    stmt = (
        select(ApiToken)
        .where(ApiToken.user_id == principal.user_id)
        .order_by(ApiToken.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars())


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_api_token(
    token_id,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> None:
    import uuid as _uuid
    from datetime import datetime, timezone

    try:
        tid = _uuid.UUID(str(token_id))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid token id")
    token = await session.get(ApiToken, tid)
    if not token or token.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    if token.revoked_at is None:
        token.revoked_at = datetime.now(timezone.utc)
        await session.commit()
