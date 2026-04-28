"""Daemon-side ed25519 signer for the Hub's `/auth/daemon-bridge`.

When `hub.daemon_bridge.enabled` is True the daemon transparently
exchanges its current daemon user for a Hub session token, removing
the need for the user to ever log in to the Hub explicitly.

The wire format is the mirror of `digitorn_hub.auth.daemon_bridge`:
deterministic JSON of `SIGNED_FIELDS` signed with the daemon's
ed25519 private key.

Keypair lifecycle
-----------------
- On first call, if `private_key_path` is empty AND the default
  `~/.digitorn/keys/<daemon_name>.sk` doesn't exist, we generate
  a fresh keypair and write both `.sk` (mode 0600) and `.pub`.
- The `.pub` must be handed off to the Hub admin who runs
  `python -m digitorn_hub.admin daemon register --name <name>
  --pubkey-file <name>.pub` once.
- Rotating the key = delete the `.sk`, restart the daemon, re-register
  on the Hub side. Old sessions stay valid until their TTL expires.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from digitorn.core.config import get_settings
from digitorn.core.models import HubSession

logger = logging.getLogger(__name__)


# Mirror of digitorn_hub.auth.daemon_bridge.SIGNED_FIELDS - keep in sync.
SIGNED_FIELDS = ("daemon_name", "user_id", "email", "display_name", "ts", "nonce")


# ─── Keypair handling ───────────────────────────────────────────────


def _default_key_dir() -> Path:
    return Path.home() / ".digitorn" / "keys"


def _resolve_key_path() -> Path:
    cfg = get_settings().hub.daemon_bridge
    if cfg.private_key_path:
        return Path(cfg.private_key_path).expanduser()
    return _default_key_dir() / f"{cfg.daemon_name}.sk"


def _public_key_path(sk_path: Path) -> Path:
    return sk_path.with_suffix(".pub")


def _load_or_generate_private_key() -> Ed25519PrivateKey:
    sk_path = _resolve_key_path()
    if sk_path.exists():
        raw = base64.b64decode(sk_path.read_text().strip())
        return Ed25519PrivateKey.from_private_bytes(raw)

    # First boot - generate, persist, log a clear message so the
    # operator knows to register the public key with the Hub.
    sk_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sk = Ed25519PrivateKey.generate()
    sk_raw = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    sk_path.write_text(base64.b64encode(sk_raw).decode("ascii") + "\n")
    sk_path.chmod(0o600)
    pk_path = _public_key_path(sk_path)
    pk_b64 = base64.b64encode(pk_raw).decode("ascii")
    pk_path.write_text(pk_b64 + "\n")
    logger.warning(
        "[hub-bridge] generated new ed25519 keypair at %s - register the "
        "public key with the Hub: python -m digitorn_hub.admin daemon "
        "register --name %s --pubkey-file %s",
        sk_path,
        get_settings().hub.daemon_bridge.daemon_name,
        pk_path,
    )
    return sk


def public_key_b64() -> str:
    """Return the daemon's current public key (for diagnostics / CLI)."""
    sk_path = _resolve_key_path()
    pk_path = _public_key_path(sk_path)
    if pk_path.exists():
        return pk_path.read_text().strip()
    sk = _load_or_generate_private_key()
    pk_raw = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pk_raw).decode("ascii")


# ─── Signing & bridging ─────────────────────────────────────────────


def _canonical(fields: dict[str, Any]) -> bytes:
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sign_request(
    *,
    user_id: str,
    email: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    cfg = get_settings().hub.daemon_bridge
    sk = _load_or_generate_private_key()
    fields: dict[str, Any] = {
        "daemon_name": cfg.daemon_name,
        "user_id": user_id,
        "email": email,
        "display_name": display_name,
        "ts": int(time.time()),
        "nonce": secrets.token_hex(16),
    }
    sig = sk.sign(_canonical(fields))
    return {**fields, "signature": base64.b64encode(sig).decode("ascii")}


async def bridge_session(
    *,
    daemon_user_id: str,
    email: str,
    display_name: str | None,
    hub_url: str,
    http_client: httpx.AsyncClient,
) -> tuple[str, datetime, dict[str, Any]] | None:
    """Call the Hub bridge endpoint. Returns (token, expires_at, hub_user)
    on success, None on any failure (caller falls back to email/password)."""
    payload = _sign_request(
        user_id=daemon_user_id,
        email=email,
        display_name=display_name,
    )
    try:
        r = await http_client.post(
            "/api/v1/auth/daemon-bridge",
            json=payload,
        )
    except httpx.HTTPError as exc:
        logger.warning("[hub-bridge] transport error: %s", exc)
        return None

    if r.status_code == 404:
        # Feature flag off on the Hub - silent fallback.
        logger.info("[hub-bridge] Hub returned 404 (bridge disabled)")
        return None
    if r.status_code >= 400:
        logger.warning(
            "[hub-bridge] Hub rejected bridge: %d %s",
            r.status_code,
            r.text[:300],
        )
        return None

    body = r.json()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(body.get("expires_in", 3600))
    )
    return body["access_token"], expires_at, body.get("user", {})


async def ensure_hub_session(
    *,
    session: AsyncSession,
    daemon_user_id: str,
    daemon_user_email: str,
    daemon_user_display_name: str | None,
    hub_url: str,
    http_client_factory,
) -> HubSession | None:
    """Top-level helper: check the cache, refresh via the bridge if
    needed, return the persisted [HubSession] row (or None when the
    bridge is disabled / unreachable).

    `http_client_factory()` must return an `httpx.AsyncClient` already
    pointed at the Hub base URL - the helper only POSTs to
    `/api/v1/auth/daemon-bridge`."""
    cfg = get_settings().hub.daemon_bridge
    if not cfg.enabled:
        return None

    # Check the cache first.
    existing = (
        await session.execute(
            select(HubSession).where(
                HubSession.daemon_user_id == daemon_user_id,
                HubSession.hub_url == hub_url,
            )
        )
    ).scalar_one_or_none()
    if existing and existing.access_token and existing.expires_at:
        if existing.expires_at > datetime.now(timezone.utc) + timedelta(seconds=30):
            return existing

    async with http_client_factory() as c:
        result = await bridge_session(
            daemon_user_id=daemon_user_id,
            email=daemon_user_email,
            display_name=daemon_user_display_name,
            hub_url=hub_url,
            http_client=c,
        )
    if result is None:
        return None

    token, expires_at, hub_user = result
    if existing is None:
        existing = HubSession(
            daemon_user_id=daemon_user_id,
            hub_url=hub_url,
            hub_user_email=hub_user.get("email", daemon_user_email),
            hub_user_id=str(hub_user.get("id", "")),
            access_token=token,
            refresh_token="",  # bridge doesn't issue refresh tokens
            expires_at=expires_at,
        )
        session.add(existing)
    else:
        existing.access_token = token
        existing.expires_at = expires_at
        existing.hub_user_email = hub_user.get("email", existing.hub_user_email)
        existing.hub_user_id = str(hub_user.get("id", existing.hub_user_id))
    await session.commit()
    await session.refresh(existing)
    return existing
