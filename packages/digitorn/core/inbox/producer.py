"""InboxProducer - background task that materializes bus events into rows."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.store import InboxStore

logger = logging.getLogger(__name__)


_DEDUP_WINDOW_SECONDS = 60.0
# Hard cap on dedup cache entries to bound memory. LRU eviction kicks
# in past this. Even a busy daemon emits well under this in 60 s.
_DEDUP_CACHE_MAX = 4096


class InboxProducer:
    """One producer instance per daemon."""

    def __init__(
        self,
        *,
        store: InboxStore,
        event_bus: Any,
        dispatcher: Any = None,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._dispatcher = dispatcher  # NotificationDispatcher | None
        self._started = False
        self._dedup_seen: dict[str, float] = {}

    async def start(self) -> None:
        if self._started:
            return
        if hasattr(self._bus, "add_handler"):
            self._bus.add_handler(self._on_envelope)
            self._started = True
            logger.info("inbox_producer_started")
        else:
            logger.warning(
                "inbox_producer: event_bus has no add_handler - disabled",
            )

    async def stop(self) -> None:
        if not self._started:
            return
        if hasattr(self._bus, "remove_handler"):
            self._bus.remove_handler(self._on_envelope)
        self._started = False
        logger.info("inbox_producer_stopped")

    def _evict_dedup(self, now: float) -> None:
        """Drop dedup entries past their window, then enforce cap."""
        cutoff = now - _DEDUP_WINDOW_SECONDS
        if self._dedup_seen:
            stale = [
                k for k, ts in self._dedup_seen.items() if ts < cutoff
            ]
            for k in stale:
                self._dedup_seen.pop(k, None)
        # Hard cap fallback: if pure window eviction left the dict
        # too large (very high event rate), drop the oldest entries.
        if len(self._dedup_seen) > _DEDUP_CACHE_MAX:
            sorted_keys = sorted(
                self._dedup_seen.items(), key=lambda kv: kv[1],
            )
            for k, _ in sorted_keys[:len(self._dedup_seen) - _DEDUP_CACHE_MAX]:
                self._dedup_seen.pop(k, None)

    async def _on_envelope(
        self, user_id: str, envelope: dict[str, Any],
    ) -> None:
        """Handler registered on the session bus."""
        try:
            await self._handle_envelope(user_id, envelope)
        except Exception as exc:
            logger.warning(
                "inbox_producer handle error user=%s type=%s: %s",
                user_id, envelope.get("type"), exc,
            )

    async def _persist(
        self,
        user_id: str,
        *,
        force: bool = False,
        **item_fields: Any,
    ) -> None:
        """Persist an inbox row AND fire the dispatcher."""
        kind_for_filter = item_fields.get("kind") or ""
        from digitorn.core.inbox.policy import CRITICAL_KINDS
        if kind_for_filter not in CRITICAL_KINDS:
            # Skip the durable write but STILL run the dispatcher so
            # the live SocketIO/push/email channels fire normally.
            if self._dispatcher is not None:
                synthetic_item = {
                    "id": "live-only-" + (item_fields.get("session_id") or ""),
                    "user_id": user_id,
                    **item_fields,
                }
                try:
                    await self._dispatcher.dispatch(user_id, synthetic_item)
                except Exception as exc:
                    logger.debug(
                        "inbox_live_dispatch_failed user=%s kind=%s: %s",
                        user_id, kind_for_filter, exc,
                    )
            return

        session_id = item_fields.get("session_id")
        if not force and session_id:
            try:
                from digitorn.core.events import presence as _presence
                if _presence.is_user_in_session(user_id, session_id):
                    logger.debug(
                        "inbox_skip_live user=%s session=%s kind=%s",
                        user_id, session_id, item_fields.get("kind"),
                    )
                    return
            except Exception as exc:
                logger.debug(
                    "inbox_presence_check_failed user=%s session=%s: %s",
                    user_id, session_id, exc,
                )

        kind_for_dedup = item_fields.get("kind") or ""
        meta = item_fields.get("metadata") or {}
        correlation_id = (
            meta.get("correlation_id")
            if isinstance(meta, dict)
            else None
        ) or ""
        if correlation_id:
            dedup_key = (
                f"{user_id}|{kind_for_dedup}|{session_id or ''}|{correlation_id}"
            )
            now = time.monotonic()
            self._evict_dedup(now)
            seen_at = self._dedup_seen.get(dedup_key)
            if seen_at is not None and (now - seen_at) <= _DEDUP_WINDOW_SECONDS:
                logger.debug(
                    "inbox_dedup_drop user=%s kind=%s session=%s "
                    "corr=%s age=%.1fs",
                    user_id, kind_for_dedup, session_id,
                    correlation_id, now - seen_at,
                )
                return
            self._dedup_seen[dedup_key] = now

        try:
            item = await self._store.create_item(
                user_id=user_id, **item_fields,
            )
        except Exception as exc:
            logger.warning(
                "inbox_producer create_item failed user=%s: %s",
                user_id, exc,
            )
            return

        if self._dispatcher is None:
            return
        try:
            result = await self._dispatcher.dispatch(user_id, item)
            logger.debug(
                "inbox_dispatch user=%s kind=%s channels=%s delivered=%s",
                user_id, item.get("kind"),
                result.get("channels"), result.get("delivered"),
            )
        except Exception as exc:
            logger.warning(
                "inbox_producer dispatch failed user=%s kind=%s: %s",
                user_id, item.get("kind"), exc,
            )

    async def _handle_envelope(
        self, user_id: str, env: dict[str, Any],
    ) -> None:
        """Decide whether an envelope becomes an inbox row."""
        raw_type = env.get("type")
        kind = env.get("kind")
        app_id = env.get("app_id")
        session_id = env.get("session_id")
        payload = env.get("payload") or {}

        if raw_type == "ping":
            return

        if not session_id:
            return

        if raw_type in ("result", "turn_complete"):
            if payload.get("error") and payload.get("error") != "aborted":
                return
            preview = _extract_preview(payload)
            await self._persist(
                user_id,
                force=True,  # terminal: bypass live-session race
                kind=InboxKind.SESSION_COMPLETED,
                title=_title_for_app(app_id, "Response ready"),
                subtitle=preview,
                app_id=app_id,
                session_id=session_id,
                metadata={
                    "tokens": payload.get("tokens")
                              or payload.get("prompt_tokens"),
                    "duration": payload.get("duration")
                                or payload.get("duration_ms"),
                    "cost": payload.get("cost"),
                    "preview": preview,
                    "truncated": payload.get("truncated"),
                    "correlation_id": env.get("correlation_id")
                                       or payload.get("correlation_id"),
                },
            )
            return

        if raw_type == "error" or kind == "error":
            # Skip CredentialAuthRequired - it's handled as its
            # own kind below.
            err_code = (payload or {}).get("code", "")
            if err_code == "credential_auth_required":
                await self._persist(
                    user_id,
                    force=True,  # terminal: bypass live-session race
                    kind=InboxKind.CREDENTIAL_MISSING,
                    title="Authorize a credential",
                    subtitle=(
                        f"{payload.get('provider', '?')} needed by "
                        f"{app_id or 'an app'}"
                    ),
                    app_id=app_id,
                    session_id=session_id,
                    credential_provider=payload.get("provider"),
                    metadata={
                        **(payload or {}),
                        "correlation_id": env.get("correlation_id")
                                           or payload.get("correlation_id"),
                    },
                )
                return
            await self._persist(
                user_id,
                force=True,  # terminal: bypass live-session race
                kind=InboxKind.SESSION_FAILED,
                title=_title_for_app(app_id, "Something went wrong"),
                subtitle=(payload.get("error") or "Unknown error")[:200],
                app_id=app_id,
                session_id=session_id,
                metadata={
                    "code": payload.get("code"),
                    "category": payload.get("category"),
                    "detail": payload.get("detail"),
                    "correlation_id": env.get("correlation_id")
                                       or payload.get("correlation_id"),
                },
            )
            return

        if raw_type == "approval_request":
            tool = payload.get("tool") or payload.get("tool_name") or "a tool"
            await self._persist(
                user_id,
                kind=InboxKind.SESSION_AWAITING_APPROVAL,
                title=_title_for_app(app_id, "Approval needed"),
                subtitle=f"{tool} is waiting for your approval",
                app_id=app_id,
                session_id=session_id,
                metadata={
                    **(payload or {}),
                    "correlation_id": env.get("correlation_id")
                                       or payload.get("correlation_id"),
                },
            )
            return


def _title_for_app(app_id: str | None, fallback: str) -> str:
    if not app_id:
        return fallback
    # Title-case: "job-hunter" → "Job Hunter"
    pretty = app_id.replace("-", " ").replace("_", " ").title()
    return f"{pretty}: {fallback}"


def _extract_preview(payload: dict[str, Any]) -> str:
    """Pull a short human-readable line from an event payload."""
    for key in ("preview", "summary", "content", "text", "message"):
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val[:200]
    # Result messages carry {role, content} under payload
    if isinstance(payload.get("content"), str):
        return str(payload["content"])[:200]
    return ""
