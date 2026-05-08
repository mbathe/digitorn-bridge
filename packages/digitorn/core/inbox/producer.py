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
import time
from typing import Any

from digitorn.core.inbox.kinds import InboxKind
from digitorn.core.inbox.store import InboxStore

logger = logging.getLogger(__name__)


# Sliding window for the in-memory dedup cache. Two events with the
# same (user_id, kind, session_id, correlation_id) tuple within this
# many seconds collapse into a single inbox row. 60 s comfortably
# covers daemon retries / replay-after-crash for one turn without
# letting unrelated events with the same correlation (rare) collide
# across hours of activity.
_DEDUP_WINDOW_SECONDS = 60.0
# Hard cap on dedup cache entries to bound memory. LRU eviction kicks
# in past this. Even a busy daemon emits well under this in 60 s.
_DEDUP_CACHE_MAX = 4096


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
        # In-memory dedup window: maps an idempotency key derived from
        # (user_id, kind, session_id, correlation_id) to the monotonic
        # timestamp of the first time we saw the event in the current
        # process. ``_persist`` looks the key up before INSERT'ing -
        # if a fresh hit lands within ``_DEDUP_WINDOW_SECONDS`` we drop
        # the duplicate silently. The cache is best-effort: process
        # restart wipes it (acceptable, the row is already in DB).
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
        # Window-based eviction. O(n) but n is tiny in practice.
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
        self,
        user_id: str,
        *,
        force: bool = False,
        **item_fields: Any,
    ) -> None:
        """Persist an inbox row AND fire the dispatcher.

        ``force=True`` bypasses the live-session skip. Used for
        terminal events (``result``, ``turn_complete``, ``error``)
        because there is a microsecond race where:

          1. agent emits ``result``
          2. socket broadcasts to session room
          3. user's tab is closing - socket may not yet have
             disconnected, so presence still says "live"
          4. producer runs, sees presence True, skips
          5. tab close completes, presence cleared - too late

        Result: terminal event silently lost. By forcing the persist
        for terminal kinds we accept the cost of an occasional
        duplicate row (user saw the event live AND has an inbox
        entry) in exchange for never losing a turn outcome.

        Mid-turn events (``approval_request``) keep the strict
        presence skip because the modal UI is the canonical surface
        when the user is live - duplicating it as an inbox row would
        be redundant noise.

        The dispatcher itself still respects presence: see the
        ``presence.is_user_in_session`` check at the dispatch layer
        for push / email - we don't want a mobile push to fire when
        the user is staring at the desktop session.
        """
        # Persist policy: only CRITICAL_KINDS land in the inbox table.
        # Non-critical events (e.g. session.completed, bg_activation.*)
        # still fire on the live SocketIO stream via the dispatcher
        # below, so the user's bell badge updates in real time -- but
        # they do NOT create a durable row. The actionable inbox stays
        # tight: if it appears in the bell list it's because the user
        # has to ACT (failed run, awaiting approval, broken cred, quota).
        # Rationale: at 1M users the volume of "session completed" rows
        # would dominate the inbox table for zero functional value
        # (the chat list already shows completed sessions).
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
                # Presence registry import / read failure must NOT
                # block notifications - default to "user is not live"
                # so we lean on the side of delivering rather than
                # silently swallowing an event.
                logger.debug(
                    "inbox_presence_check_failed user=%s session=%s: %s",
                    user_id, session_id, exc,
                )

        # In-memory dedup: collapse repeated events for the same
        # (user, kind, session, correlation) within the sliding
        # window. Catches daemon retries, bus replay races, and the
        # occasional double-emit of ``result`` after reconnect.
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
        """Decide whether an envelope becomes an inbox row.

        Strict whitelist (per product contract): only THREE families
        of events become notifications, and only when the user isn't
        already watching the session live.

          1. ``error`` events that occur inside a session
          2. ``approval_request``                  - awaits user input
          3. ``result`` / ``turn_complete``         - turn finished

        Everything else (tokens, thinking deltas, tool calls, BG
        activation results, ping, …) is dropped here. The
        ``_persist`` helper layers the live-session filter on top so
        even one of the three whitelisted events is silently dropped
        when the user has the session open in a tab right now.
        """
        raw_type = env.get("type")
        kind = env.get("kind")
        app_id = env.get("app_id")
        session_id = env.get("session_id")
        payload = env.get("payload") or {}

        if raw_type == "ping":
            return

        # Notifications are session-scoped by contract: an event
        # without a session_id can't belong to any session the user
        # might be live on, and the three notification families above
        # are all session-bound. Drop anything that lacks one before
        # doing more work.
        if not session_id:
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

        # ── Session failed ────────────────────────────────────
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

        # ── Awaiting approval ─────────────────────────────────
        if raw_type == "approval_request":
            tool = payload.get("tool") or payload.get("tool_name") or "a tool"
            await self._persist(
                user_id,
                # Mid-turn: keep the live-session skip. If the user is
                # in the session the modal already shows; an inbox row
                # would just be noise. force=False (default).
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

        # Anything else: not in the three-family whitelist. Drop
        # silently - the bus carries dozens of token / thinking /
        # tool / hook event types per turn and none of them deserves
        # a "ding". Background activation results
        # (``notification_result``) used to land here too but the
        # product scope was tightened to in-session events only.


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
