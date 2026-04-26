"""POST /auth/daemon-bridge — mint a Hub session for a user vouched for
by a registered central daemon.

Threat model
------------
- Only daemons whose ed25519 public key is in `trusted_daemons` and not
  revoked can call this. A leaked daemon key is contained by revoking
  the row (sets `revoked_at`) — every subsequent call gets 401.
- Replay is blocked by an idempotent insert into `daemon_bridge_nonces`;
  the same nonce twice = 409 (the daemon should retry with a fresh one).
- Clock skew is bounded by `daemon_bridge_max_clock_skew_seconds`
  (default 60s).

The endpoint is a no-op (404) when `enable_daemon_bridge` is False, so
rolling out is a single env-var flip.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.daemon_bridge import (
    SIGNED_FIELDS,
    BridgeVerifyError,
    fresh,
    verify_signature,
)
from ..auth.jwt import encode
from ..db import get_session
from ..models import DaemonBridgeNonce, TrustedDaemon, User
from ..schemas import DaemonBridgeRequest, DaemonBridgeResponse, UserOut
from ..settings import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/daemon-bridge",
    response_model=DaemonBridgeResponse,
)
async def daemon_bridge(
    payload: DaemonBridgeRequest,
    session: AsyncSession = Depends(get_session),
) -> DaemonBridgeResponse:
    s = get_settings()
    if not s.enable_daemon_bridge:
        # Hidden until explicitly enabled. Same status code FastAPI
        # uses for missing routes so probes don't leak the feature.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")

    # ── 1. Freshness ─────────────────────────────────────────────────
    if not fresh(payload.ts, s.daemon_bridge_max_clock_skew_seconds):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"timestamp outside ±{s.daemon_bridge_max_clock_skew_seconds}s window",
        )

    # ── 2. Look up the trusted daemon ────────────────────────────────
    stmt = select(TrustedDaemon).where(
        TrustedDaemon.name == payload.daemon_name,
        TrustedDaemon.revoked_at.is_(None),
    )
    daemon = (await session.execute(stmt)).scalar_one_or_none()
    if daemon is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "unknown or revoked daemon"
        )

    # ── 3. Verify ed25519 signature ──────────────────────────────────
    fields = {
        k: getattr(payload, k) for k in SIGNED_FIELDS
    }
    try:
        verify_signature(daemon.public_key, fields, payload.signature)
    except BridgeVerifyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # ── 4. Anti-replay (insert nonce, fail on duplicate) ─────────────
    nonce_row = DaemonBridgeNonce(
        nonce=payload.nonce,
        daemon_name=payload.daemon_name,
    )
    session.add(nonce_row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "nonce already used"
        ) from exc

    # ── 5. Find or auto-provision the Hub user ───────────────────────
    email = str(payload.email).lower()
    user_stmt = select(User).where(User.email == email)
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    created = False
    if user is None:
        # Auto-provision. The user can never log in via password
        # because we plant a random unguessable hash they don't know.
        # If they want password access later they go through
        # /auth/forgot-password (TODO when that exists) or the bridge
        # keeps re-issuing tokens transparently — both fine.
        from ..auth.passwords import hash_password

        user = User(
            email=email,
            password_hash=hash_password(secrets.token_urlsafe(32)),
            display_name=payload.display_name or email.split("@")[0],
        )
        session.add(user)
        try:
            await session.flush()
        except IntegrityError:
            # Race: a sibling bridge call inserted the same email.
            # Re-fetch and proceed.
            await session.rollback()
            session.add(nonce_row)  # re-stage the nonce too
            user = (await session.execute(user_stmt)).scalar_one()
        else:
            created = True

    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user disabled")

    # ── 6. Mark daemon usage + commit ────────────────────────────────
    await session.execute(
        update(TrustedDaemon)
        .where(TrustedDaemon.id == daemon.id)
        .values(last_used_at=datetime.now(timezone.utc))
    )
    await session.commit()
    await session.refresh(user)

    # ── 7. Issue session token ───────────────────────────────────────
    access = encode(user.id, "access")
    return DaemonBridgeResponse(
        access_token=access,
        expires_in=s.daemon_bridge_session_ttl_minutes * 60,
        user=UserOut.model_validate(user),
        created=created,
    )
