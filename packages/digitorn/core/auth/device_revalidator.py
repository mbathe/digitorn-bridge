"""Background task that pings the central auth service to confirm
the paired device is still authorized AND grabs a rolling-refreshed
device token when expiry approaches.

Non-blocking: any network failure is logged and retried on the next
tick. The user keeps using the daemon with the cached token in the
meantime — that's the whole point of offline auth.

State machine the daemon walks through::

    paired (fresh token)          → keeps revalidating, rolling-refreshes
    paired (token close to exp)   → revalidate triggers refresh, store updates
    paired (central revoked)      → revalidate returns valid=false → wipe()
    paired (central unreachable)  → keep cached token, retry next tick
    not paired                    → revalidator exits (LocalDeviceAuth.load fails)
"""

from __future__ import annotations

import asyncio
import logging

from digitorn.core.auth.local_device import LocalDeviceAuth

logger = logging.getLogger(__name__)


async def revalidate_loop(
    local_auth: LocalDeviceAuth,
    interval_s: int = 3600,
) -> None:
    """Run forever. Cancelled at daemon shutdown.

    The first tick fires after ``interval_s`` so we don't hammer the
    central on cold start (the device just paired or just woke up;
    let it settle).
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            await _revalidate_once(local_auth)
        except Exception as exc:  # noqa: BLE001
            logger.debug("device revalidate failed (offline?): %s", exc)


async def _revalidate_once(local_auth: LocalDeviceAuth) -> None:
    """Single ping to the central. Updates ``local_auth`` in-place
    when central rolling-refreshes the token, or wipes it if revoked.
    """
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
        return  # offline / DNS / TLS failure — try again next tick

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
