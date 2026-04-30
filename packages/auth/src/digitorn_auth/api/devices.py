"""Device pairing endpoints.

A "paired device" is a daemon instance bound to a user account. The
daemon stores a long-lived ``device_token`` issued by this service; on
every API call it can authenticate the user offline (the daemon caches
the central JWKS and verifies the token cryptographically).

Endpoints:
  - POST /auth/devices/pair         — first pair, requires fresh access token
  - POST /auth/devices/{id}/revalidate — periodic check from the daemon
  - DELETE /auth/devices/{id}       — user revokes a device from the dashboard
  - GET /auth/devices               — list current user's devices

The device_token JWT carries:
  iss  = AuthSettings.issuer
  sub  = user.id
  aud  = "daemon-{device.id}"          (binds token to THIS daemon only)
  scope = "daemon-pair"                 (distinguish from access/refresh)
  exp  = now + AuthSettings.device_token_ttl  (default 90 days)
  jti  = unique per token (rolling refresh writes a fresh one)
  device_id, device_label, email      (convenience claims for the daemon)
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn_auth.api.deps import get_auth_service, get_db, require_user
from digitorn_auth.config import get_settings
from digitorn_auth.models import PairedDevice, User
from digitorn_auth.service import AuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/devices", tags=["devices"])


# ── Request / response models ──────────────────────────────────────


class PairRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=128)
    daemon_public_key: str | None = Field(
        default=None,
        description=(
            "Optional: PEM-encoded daemon public key. Stored for future "
            "proof-of-possession during revalidate. v1 doesn't use it."
        ),
    )


class PairResponse(BaseModel):
    device_id: str
    device_token: str
    expires_at: int  # epoch seconds
    central_iss: str
    # NOTE: until the auth service moves to RS256+JWKS, daemons that
    # need to verify device_tokens themselves will need to share the
    # HS256 secret (out-of-band) or call the revalidate endpoint. The
    # ``central_iss`` field is included so v2 daemons can immediately
    # use the standard OIDC discovery flow (`/.well-known/...`) without
    # a re-pair.


class RevalidateResponse(BaseModel):
    valid: bool
    revoked_reason: str | None = None
    renewed_token: str | None = None
    renewed_expires_at: int | None = None


class DeviceSummary(BaseModel):
    id: str
    label: str
    created_at: datetime
    last_seen_at: datetime | None = None
    last_seen_ip: str | None = None
    last_seen_user_agent: str | None = None
    revoked_at: datetime | None = None
    is_active: bool


# ── Helpers ────────────────────────────────────────────────────────


def _mint_device_token(
    auth: AuthService,
    user: User,
    device: PairedDevice,
) -> tuple[str, int, str]:
    """Sign a device_token JWT. Returns (token, expires_at, jti).

    Notes on claims:
      - We DON'T override ``iss`` here. The JWTService's verify path
        enforces ``iss == 'digitorn'`` and pyjwt rejects mismatches as
        InvalidIssuerError — leave the default until we move to a
        config-driven RS256 setup.
      - ``aud`` binds the token to a single daemon: ``daemon-{device.id}``.
      - ``type`` is ``device-pair`` so revalidate can distinguish device
        tokens from access tokens (which a logged-in user could otherwise
        accidentally pass to /revalidate).
    """
    settings = get_settings()
    now = int(time.time())
    exp = now + settings.device_token_ttl
    jti = secrets.token_hex(16)
    token = auth._jwt.generate_access_token(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        roles=None,
        permissions=None,
        extra={
            "type": "device-pair",
            "scope": "daemon-pair",
            "aud": f"daemon-{device.id}",
            "exp": exp,
            "jti": jti,
            "device_id": device.id,
            "device_label": device.label,
        },
    )
    return token, exp, jti


def _capture_telemetry(request: Request, device: PairedDevice) -> None:
    """Update last-seen fields on the device row."""
    device.last_seen_at = datetime.now(timezone.utc)
    if request.client is not None:
        device.last_seen_ip = request.client.host
    ua = request.headers.get("user-agent", "")[:255]
    if ua:
        device.last_seen_user_agent = ua


# ── Endpoints ──────────────────────────────────────────────────────


@router.post("/pair", response_model=PairResponse)
async def pair_device(
    body: PairRequest,
    request: Request,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Pair a daemon to the calling user's account.

    The user must be logged in (fresh access token in the
    Authorization header). The response carries a long-lived
    device_token the daemon stores and uses for offline auth.
    """
    device = PairedDevice(
        user_id=user.id,
        label=body.label,
        daemon_public_key=body.daemon_public_key,
    )
    db.add(device)
    await db.flush()  # populate device.id without committing yet

    token, exp, jti = _mint_device_token(auth, user, device)
    device.last_token_jti = jti
    _capture_telemetry(request, device)
    await db.commit()

    settings = get_settings()
    logger.info(
        "device_paired user_id=%s device_id=%s label=%s",
        user.id, device.id, device.label,
    )

    return PairResponse(
        device_id=device.id,
        device_token=token,
        expires_at=exp,
        central_iss=settings.issuer,
    )


@router.post("/{device_id}/revalidate", response_model=RevalidateResponse)
async def revalidate_device(
    device_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
):
    """Periodic check from the daemon: 'am I still authorized?'.

    Uses the device_token directly (no fresh access token needed —
    the daemon may have been offline for weeks). On success, MAY
    return a rolling-refreshed token if the original is close to
    expiry. On revoke, returns valid=false with a reason — the daemon
    wipes its local secrets.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing device token")
    token_str = auth_header.split(" ", 1)[1].strip()

    try:
        payload = auth._jwt.verify(token_str)
    except Exception:
        return RevalidateResponse(valid=False, revoked_reason="invalid_signature")

    # Defensive: only "device-pair" tokens are valid here. An access
    # token (15-min TTL) wouldn't survive the daemon's offline window
    # anyway, but explicit-deny prevents misuse.
    if payload.token_type != "device-pair":
        return RevalidateResponse(valid=False, revoked_reason="wrong_token_type")

    device = await db.get(PairedDevice, device_id)
    if device is None:
        return RevalidateResponse(valid=False, revoked_reason="device_not_found")
    if device.revoked_at is not None:
        return RevalidateResponse(valid=False, revoked_reason="device_revoked")
    if device.user_id != payload.user_id:
        return RevalidateResponse(valid=False, revoked_reason="user_mismatch")

    _capture_telemetry(request, device)

    # Rolling refresh: if the token expires inside the threshold,
    # mint a fresh one. The daemon picks it up and replaces its
    # stored copy. Net effect: an active device stays valid forever
    # as long as it phones home at least once every (ttl - threshold).
    settings = get_settings()
    seconds_until_expiry = max(0, int(payload.expires_at) - int(time.time()))
    renewed_token: str | None = None
    renewed_exp: int | None = None
    if (
        payload.expires_at > 0
        and seconds_until_expiry < settings.device_token_rolling_refresh_threshold
    ):
        user = await db.get(User, device.user_id)
        if user is None or not user.is_active:
            await db.commit()
            return RevalidateResponse(valid=False, revoked_reason="user_disabled")
        renewed_token, renewed_exp, jti = _mint_device_token(auth, user, device)
        device.last_token_jti = jti

    await db.commit()
    return RevalidateResponse(
        valid=True,
        renewed_token=renewed_token,
        renewed_expires_at=renewed_exp,
    )


@router.get("", response_model=list[DeviceSummary])
async def list_devices(
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """List all devices paired to the calling user."""
    rows = (
        await db.execute(
            select(PairedDevice).where(PairedDevice.user_id == user.id)
            .order_by(PairedDevice.created_at.desc())
        )
    ).scalars().all()
    return [
        DeviceSummary(
            id=d.id,
            label=d.label,
            created_at=d.created_at,
            last_seen_at=d.last_seen_at,
            last_seen_ip=d.last_seen_ip,
            last_seen_user_agent=d.last_seen_user_agent,
            revoked_at=d.revoked_at,
            is_active=d.is_active,
        )
        for d in rows
    ]


@router.delete("/{device_id}", status_code=204)
async def revoke_device(
    device_id: str,
    user: Annotated[User, Depends(require_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """User-initiated revoke from the dashboard.

    Marks the device revoked. The daemon's next periodic revalidate
    sees ``valid=false`` and wipes its local secrets, forcing
    re-pairing on the next connection.
    """
    device = await db.get(PairedDevice, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(status_code=404, detail="Device not found")
    if device.revoked_at is None:
        device.revoked_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info(
            "device_revoked user_id=%s device_id=%s",
            user.id, device.id,
        )
