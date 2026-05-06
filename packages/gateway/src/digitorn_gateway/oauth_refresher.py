"""Background OAuth/token refresh loop.

Some auth types carry short-lived access tokens that need to be
refreshed before they expire (``oauth2``, ``github_copilot``,
``claude_code``). This task scans the cache every minute, picks the
ones whose ``expires_at`` is within the safety window, and renews
them out-of-band so the dispatch path always sees a fresh token.

The refresh logic is per-provider:

  * ``claude_code``: re-derives ``access_token`` from ``refresh_token``
    via the Anthropic OAuth endpoint (POST refresh).
  * ``github_copilot``: exchanges the long-lived ``oauth_token`` for a
    short-lived ``api_key`` via GitHub's Copilot internal token endpoint.
  * ``oauth2``: standard RFC 6749 refresh_token grant against the
    catalogued ``token_endpoint`` + ``client_id``.

Every refresh is wrapped in try/except - a refresh failure logs and
moves on; the cache keeps the old token so dispatch can still try.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from digitorn_gateway.cipher import encrypt_dict
from digitorn_gateway.config_cache import get_cache, CachedCredential
from digitorn_gateway.models_db import GatewayCredential

logger = logging.getLogger(__name__)


REFRESH_LOOP_INTERVAL_S = 60.0
# Refresh when the token has < 10 minutes of life left.
REFRESH_SAFETY_WINDOW_S = 10 * 60


async def _refresh_claude_code(secret: dict[str, str]) -> dict[str, str] | None:
    """Trade ``refresh_token`` for a fresh ``access_token`` against
    Anthropic's OAuth refresh endpoint. Falls back to None on failure
    so the loop logs + keeps the old token."""
    refresh = secret.get("refresh_token")
    if not refresh:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                "https://console.anthropic.com/v1/oauth/token",
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("claude_code refresh failed: %s", exc)
        return None
    return {
        "access_token": str(body.get("access_token", "")),
        "refresh_token": str(body.get("refresh_token", refresh)),
        "expires_at": str(int(time.time() * 1000) + int(body.get("expires_in", 0)) * 1000),
    }


async def _refresh_github_copilot(secret: dict[str, str]) -> dict[str, str] | None:
    """Use the GitHub OAuth token to mint a fresh Copilot API key."""
    oauth = secret.get("oauth_token")
    if not oauth:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(
                "https://api.github.com/copilot_internal/v2/token",
                headers={
                    "Authorization": f"Bearer {oauth}",
                    "Editor-Plugin-Version": "digitorn-gateway/1.0",
                    "Editor-Version": "digitorn-gateway/1.0",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("github_copilot refresh failed: %s", exc)
        return None
    return {
        "oauth_token": oauth,
        "api_key": str(body.get("token", "")),
        "api_key_expires_at": str(body.get("expires_at", 0)),
    }


async def _refresh_oauth2(secret: dict[str, str]) -> dict[str, str] | None:
    refresh = secret.get("refresh_token")
    endpoint = secret.get("token_endpoint")
    client_id = secret.get("client_id")
    if not (refresh and endpoint and client_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        logger.warning("oauth2 refresh (%s) failed: %s", endpoint, exc)
        return None
    out = {**secret}
    out["access_token"] = str(body.get("access_token", ""))
    if body.get("refresh_token"):
        out["refresh_token"] = str(body["refresh_token"])
    if body.get("expires_in"):
        out["expires_at"] = str(int(time.time()) + int(body["expires_in"]))
    return out


REFRESHERS = {
    "claude_code": _refresh_claude_code,
    "github_copilot": _refresh_github_copilot,
    "oauth2": _refresh_oauth2,
}


def _expires_at_seconds(secret: dict[str, str], auth_type: str) -> int:
    """Normalise the various ``expires_at`` field shapes into unix
    seconds. Handles claude_code (millis), github_copilot (seconds)
    and standard oauth2 (seconds). Returns 0 when missing."""
    raw = (
        secret.get("api_key_expires_at")
        if auth_type == "github_copilot"
        else secret.get("expires_at")
    )
    if not raw:
        return 0
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 0
    # Heuristic: timestamps > year-3000 (~3.2e10) are millis.
    if v > 32_000_000_000:
        v //= 1000
    return v


async def _refresh_one(
    factory: async_sessionmaker[AsyncSession],
    cred: CachedCredential,
    auth_type: str,
) -> None:
    refresher = REFRESHERS.get(auth_type)
    if refresher is None:
        return
    new_secret = await refresher(cred.secret_data)
    if not new_secret:
        return

    # Persist + cache.
    try:
        encrypted = encrypt_dict(new_secret)
    except Exception as exc:
        logger.warning("refresh: encrypt_dict failed for cred=%s: %s", cred.id, exc)
        return
    async with factory() as db:
        row = await db.get(GatewayCredential, cred.id)
        if row is None:
            return
        row.encrypted_value = encrypted
        await db.commit()

    get_cache().upsert_credential(
        cred.id,
        provider_slug=cred.provider_slug,
        label=cred.label,
        secret_data=new_secret,
        status=cred.status,
    )
    logger.info(
        "oauth refreshed cred=%s provider=%s",
        cred.id, cred.provider_slug,
    )


async def _scan_and_refresh(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    cache = get_cache()
    now = int(time.time())
    # Snapshot of credentials so the loop sees a stable view.
    creds = list(cache._credentials.values())  # type: ignore[attr-defined]
    for cred in creds:
        provider = cache.provider(cred.provider_slug)
        if provider is None:
            continue
        if provider.auth_type not in REFRESHERS:
            continue
        exp = _expires_at_seconds(cred.secret_data, provider.auth_type)
        if exp == 0:
            # No expiry recorded - skip (nothing to refresh against).
            continue
        if exp - now > REFRESH_SAFETY_WINDOW_S:
            continue  # plenty of life left
        await _refresh_one(factory, cred, provider.auth_type)


_task: asyncio.Task | None = None


async def start_refresh_loop(
    factory: async_sessionmaker[AsyncSession],
    interval_s: float = REFRESH_LOOP_INTERVAL_S,
) -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                await _scan_and_refresh(factory)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("oauth_refresher loop error (continuing): %s", exc)

    _task = asyncio.create_task(_loop(), name="gateway-oauth-refresh")
    return _task


async def stop_refresh_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
