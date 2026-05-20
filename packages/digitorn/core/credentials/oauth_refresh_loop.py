"""Background OAuth refresh loop."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.core.credentials.oauth_flow import (
    TokenExchange,
    TokenExchangeError,
)
from digitorn.core.credentials.oauth_providers import (
    OAuthProviderRegistry,
)
from digitorn.core.credentials.store import (
    CredentialStore,
    Status,
)

logger = logging.getLogger(__name__)


REFRESH_BUFFER_SECONDS = 600


class OAuthRefreshLoop:
    """Background task that keeps OAuth tokens fresh."""

    def __init__(
        self,
        *,
        store: CredentialStore,
        registry: OAuthProviderRegistry,
        interval_seconds: int = 300,
    ) -> None:
        self._store = store
        self._registry = registry
        self._interval = max(60, int(interval_seconds))
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        """Spawn the background task. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name="oauth-refresh-loop",
        )

    async def stop(self) -> None:
        """Signal stop and await the task."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        logger.info(
            "oauth_refresh_loop_started interval=%ds buffer=%ds",
            self._interval, REFRESH_BUFFER_SECONDS,
        )
        while not self._stop.is_set():
            try:
                await self._refresh_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "oauth_refresh_pass_failed: %s", exc, exc_info=True,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._interval,
                )
            except asyncio.TimeoutError:
                continue
        logger.info("oauth_refresh_loop_stopped")

    async def _refresh_pass(self) -> None:
        now = time.time()
        cutoff = now + REFRESH_BUFFER_SECONDS

        # Pull every OAuth-style credential. The store's filter
        # supports `provider_types`; we ask for all OAuth flavors.
        candidates: list[dict[str, Any]] = []
        for handler_type in ("oauth2", "oauth2_pkce", "device_code"):
            try:
                rows = await self._store.list_credentials(
                    user_id=None,
                    provider_types=[handler_type],
                    decrypt=True,  # we need refresh_token + provider_name
                )
            except TypeError:
                # list_credentials maybe doesn't accept these kwargs
                # in older deployments. Skip.
                continue
            except Exception as exc:
                logger.debug(
                    "oauth_refresh_list_failed handler=%s: %s",
                    handler_type, exc,
                )
                continue
            candidates.extend(rows or [])

        refreshed = 0
        skipped = 0
        failed = 0
        for cred in candidates:
            try:
                expires_at = cred.get("expires_at")
                if expires_at is None:
                    skipped += 1
                    continue
                if isinstance(expires_at, str):
                    # ISO timestamp - parse to epoch.
                    from datetime import datetime
                    try:
                        expires_at = datetime.fromisoformat(
                            expires_at.replace("Z", "+00:00"),
                        ).timestamp()
                    except Exception:
                        skipped += 1
                        continue
                if expires_at > cutoff:
                    skipped += 1
                    continue

                provider_name = cred.get("provider_name") or ""
                provider = self._registry.get(provider_name)
                if provider is None or not provider.is_configured():
                    failed += 1
                    continue

                fields = cred.get("fields") or {}
                refresh_token = fields.get("refresh_token", "")
                if not refresh_token:
                    # No refresh token - mark expired so the user is
                    # prompted to re-consent at next chat start.
                    if cred.get("status") != Status.EXPIRED:
                        try:
                            await self._store.update_status(
                                cred["id"], status=Status.EXPIRED,
                            )
                        except Exception as exc:
                            logger.debug("oauth_refresh_loop best-effort block failed: %s", exc)
                    failed += 1
                    continue

                try:
                    new_tokens = await TokenExchange.refresh_token(
                        provider, refresh_token,
                    )
                except TokenExchangeError as exc:
                    logger.warning(
                        "oauth_refresh_failed cred=%s provider=%s: %s",
                        cred.get("id"), provider_name, exc,
                    )
                    failed += 1
                    try:
                        await self._store.update_status(
                            cred["id"], status=Status.EXPIRED,
                            last_error=str(exc),
                        )
                    except Exception as exc:
                        logger.debug("oauth_refresh_loop best-effort block failed: %s", exc)
                    continue

                # Merge new tokens into the credential's fields.
                merged = dict(fields)
                if new_tokens.get("access_token"):
                    merged["access_token"] = new_tokens["access_token"]
                    merged["token"] = new_tokens["access_token"]
                if new_tokens.get("refresh_token"):
                    # Some providers rotate refresh tokens, others reuse.
                    merged["refresh_token"] = new_tokens["refresh_token"]

                new_expires_at: float | None = None
                if new_tokens.get("expires_in"):
                    try:
                        new_expires_at = now + float(
                            new_tokens["expires_in"],
                        )
                    except (ValueError, TypeError):
                        new_expires_at = None

                try:
                    await self._store.upsert_credential(
                        user_id=cred.get("user_id"),
                        app_id=cred.get("app_id"),
                        provider_name=provider_name,
                        provider_type=cred.get("provider_type") or "oauth2",
                        scope=cred.get("scope") or "per_user",
                        fields=merged,
                        status=Status.VALID,
                        expires_at=(
                            __import__("datetime").datetime.fromtimestamp(
                                new_expires_at,
                                tz=__import__("datetime").timezone.utc,
                            )
                            if new_expires_at else None
                        ),
                        name=cred.get("name") or None,
                    )
                    refreshed += 1
                except Exception as exc:
                    logger.warning(
                        "oauth_refresh_persist_failed cred=%s: %s",
                        cred.get("id"), exc,
                    )
                    failed += 1
            except Exception as exc:
                logger.warning(
                    "oauth_refresh_one_failed cred=%s: %s",
                    cred.get("id"), exc,
                )
                failed += 1

        if refreshed or failed:
            logger.info(
                "oauth_refresh_pass refreshed=%d skipped=%d failed=%d",
                refreshed, skipped, failed,
            )
