"""InboxProducer - background task that materializes bus events into rows.

Runs as a single long-lived coroutine per daemon. Registers one
handler on the session event bus via ``add_handler()`` and reacts
to every envelope published, creating inbox rows for the ones that
matter to the user. Detection logic ("should I create an inbox row
for this event?") is the only real content of this module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.store import InboxStore

logger = logging.getLogger(__name__)


class InboxProducer:
    """One producer instance per daemon. Managed by the lifespan."""

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

    async def _on_envelope(
        self, user_id: str, envelope: dict[str, Any],
    ) -> None:
        """Handler registered on the session bus. Dispatches to
        the per-kind inbox logic."""
        try:
            await self._handle_envelope(user_id, envelope)
        except Exception as exc:
            logger.warning(
                "inbox_producer handle error user=%s type=%s: %s",
                user_id, envelope.get("type"), exc,
            )

    async def _persist(
        self, user_id: str, **item_fields: Any,
    ) -> None:
        """Persist an inbox row AND fire the dispatcher.

        All producer branches go through this helper so the
        "write + dispatch" path is guaranteed to stay in sync.
        Delivery failures are swallowed; the inbox write is the
        source of truth.
        """
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
        """Decide whether an envelope becomes an inbox row.

        The decision table is deliberately explicit - adding new
        kinds should be a one-line change here, not a refactor.
        """
        raw_type = env.get("type")
        kind = env.get("kind")
        app_id = env.get("app_id")
        session_id = env.get("session_id")
        payload = env.get("payload") or {}

        if raw_type == "ping":
            return

        # ── Session completed ─────────────────────────────────
        # The canonical end-of-turn event published by
        # ``AppManager`` is ``result`` (see manager.py line ~1205).
        # We also accept ``turn_complete`` as an alias in case the
        # runtime layer ever starts publishing it directly.
        #
        # Guard against duplicates: ``result`` sometimes carries an
        # error (when the turn failed mid-way). If ``payload.error``
        # is set, skip here - the dedicated ``error`` event emitted
        # right after will create the session.failed row.
        if raw_type in ("result", "turn_complete"):
            if payload.get("error") and payload.get("error") != "aborted":
                return
            preview = _extract_preview(payload)
            await self._persist(
                user_id=user_id,
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
                },
            )
            return

        # ── Session failed ────────────────────────────────────
        if raw_type == "error" or kind == "error":
            # Skip CredentialAuthRequired - it's handled as its
            # own kind below.
            err_code = (payload or {}).get("code", "")
            if err_code == "credential_auth_required":
                await self._persist(
                    user_id=user_id,
                    kind=InboxKind.CREDENTIAL_MISSING,
                    title="Authorize a credential",
                    subtitle=(
                        f"{payload.get('provider', '?')} needed by "
                        f"{app_id or 'an app'}"
                    ),
                    app_id=app_id,
                    session_id=session_id,
                    credential_provider=payload.get("provider"),
                    metadata=payload,
                )
                return
            await self._persist(
                user_id=user_id,
                kind=InboxKind.SESSION_FAILED,
                title=_title_for_app(app_id, "Something went wrong"),
                subtitle=(payload.get("error") or "Unknown error")[:200],
                app_id=app_id,
                session_id=session_id,
                metadata={
                    "code": payload.get("code"),
                    "category": payload.get("category"),
                    "detail": payload.get("detail"),
                },
            )
            return

        # ── Awaiting approval ─────────────────────────────────
        if raw_type == "approval_request":
            tool = payload.get("tool") or payload.get("tool_name") or "a tool"
            await self._persist(
                user_id=user_id,
                kind=InboxKind.SESSION_AWAITING_APPROVAL,
                title=_title_for_app(app_id, "Approval needed"),
                subtitle=f"{tool} is waiting for your approval",
                app_id=app_id,
                session_id=session_id,
                metadata=payload,
            )
            return

        # ── Background activation finished ────────────────────
        if raw_type == "notification_result":
            await self._persist(
                user_id=user_id,
                kind=InboxKind.BG_ACTIVATION_COMPLETED,
                title=_title_for_app(app_id, "Background activity completed"),
                subtitle=_extract_preview(payload),
                app_id=app_id,
                session_id=session_id,
                activation_id=payload.get("activation_id") or payload.get("trigger_id"),
                metadata=payload,
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
