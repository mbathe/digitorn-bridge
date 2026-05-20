"""Background task that pings the central auth service to confirm."""

from __future__ import annotations

import asyncio
import logging

from digitorn.core.auth.local_device import LocalDeviceAuth

logger = logging.getLogger(__name__)

async def revalidate_loop(
    local_auth: LocalDeviceAuth,
    interval_s: int = 3600,
) -> None:
    """Run forever. Cancelled at daemon shutdown."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await _revalidate_once(local_auth)
        except Exception as exc:  # noqa: BLE001
            logger.debug("device revalidate failed (offline?): %s", exc)

async def _revalidate_once(local_auth: LocalDeviceAuth) -> None:
    if not local_auth.device_token:
        return  # Wiped by an earlier revoke - nothing to do.

    import httpx
    auth_url = local_auth.auth_url.rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{auth_url}/auth/devices/{local_auth.device_id}/revalidate",
                headers={"Authorization": f"Bearer {local_auth.device_token}"},
            )
    except httpx.RequestError as exc:
        logger.debug("device revalidate network error: %s", exc)
        return  # offline / DNS / TLS failure - try again next tick

    if response.status_code in (401, 403):
        logger.warning(
            "device unauthorized at central (HTTP %s) - wiping local secrets",
            response.status_code,
        )
        local_auth.wipe()
        return

    if response.status_code != 200:
        logger.debug(
            "device revalidate transient error: HTTP %s body=%s",
            response.status_code, response.text[:200],
        )
        return

    body = response.json()
    if not body.get("valid", False):
        reason = body.get("revoked_reason", "unknown")
        logger.warning("device revoked by central: reason=%s", reason)
        local_auth.wipe()
        return

    renewed = body.get("renewed_token")
    renewed_exp = body.get("renewed_expires_at")
    if renewed and renewed_exp:
        local_auth.update_token(renewed, int(renewed_exp))
        logger.info(
            "device token rolling-refreshed, +%d days",
            local_auth.days_until_expiry,
        )
