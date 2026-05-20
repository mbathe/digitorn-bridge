"""NotificationPolicy - decide which channels fire for an inbox item."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
from datetime import datetime, time, timezone
from typing import Any

from digitorn.core.inbox.kinds import InboxKind


CHANNELS = ("desktop", "push", "email")


# Kinds that bypass quiet hours (the user wants to know NOW).
CRITICAL_KINDS: frozenset[str] = frozenset({
    InboxKind.SESSION_FAILED,
    InboxKind.SESSION_AWAITING_APPROVAL,
    InboxKind.CREDENTIAL_EXPIRED,
    InboxKind.CREDENTIAL_MISSING,
    InboxKind.QUOTA_WARNING,
})


_DEFAULT_ROUTING: dict[str, list[str]] = {
    InboxKind.SESSION_COMPLETED:         ["desktop"],
    InboxKind.SESSION_FAILED:            ["desktop", "push"],
    InboxKind.SESSION_AWAITING_APPROVAL: ["desktop", "push"],
    InboxKind.BG_ACTIVATION_COMPLETED:   ["desktop"],
    InboxKind.BG_ACTIVATION_FAILED:      ["desktop", "push"],
    InboxKind.CREDENTIAL_EXPIRED:        ["desktop", "email"],
    InboxKind.CREDENTIAL_MISSING:        ["desktop"],
    InboxKind.QUOTA_WARNING:             ["desktop", "email"],
}


class NotificationPolicy:
    """Pure decision layer for notification routing."""

    @staticmethod
    def channels_for(
        *,
        kind: str,
        prefs: dict[str, Any] | None,
        now: datetime | None = None,
    ) -> list[str]:
        """Return the list of channels that should receive this event."""
        prefs = prefs or {}

        # Master switch (granular shape)
        if prefs.get("enabled") is False:
            return []

        events_field = prefs.get("events")
        routing: list[str]

        if isinstance(events_field, list):
            # The whitelist: if the kind isn't listed, silence it.
            if kind not in events_field:
                routing = []
            else:
                # Build channels from the top-level toggles
                routing = []
                if prefs.get("desktop", True):
                    routing.append("desktop")
                if prefs.get("push", True):
                    routing.append("push")
                if prefs.get("email", False):
                    routing.append("email")
        elif isinstance(events_field, dict) and events_field:
            override = events_field.get(kind)
            if override is None:
                routing = list(_DEFAULT_ROUTING.get(kind, ["desktop"]))
            else:
                routing = [c for c in override if c in CHANNELS]
        else:
            # No events config at all → apply defaults
            routing = list(_DEFAULT_ROUTING.get(kind, ["desktop"]))

        # Quiet hours - apply to non-critical kinds only
        if routing and kind not in CRITICAL_KINDS:
            quiet = prefs.get("quiet_hours") or {}
            if _in_quiet_hours(quiet, now):
                return []

        return routing

    @staticmethod
    def would_deliver(
        *,
        kind: str,
        channel: str,
        prefs: dict[str, Any] | None,
        now: datetime | None = None,
    ) -> bool:
        """Return True if a specific channel would fire for a kind."""
        return channel in NotificationPolicy.channels_for(
            kind=kind, prefs=prefs, now=now,
        )


def _in_quiet_hours(
    quiet: dict[str, Any], now: datetime | None = None,
) -> bool:
    """Check if `now` falls inside the user's quiet-hours window."""
    # Accept both shapes: {start, end} (granular) and
    # {start_hour, end_hour} (client).
    start = quiet.get("start", quiet.get("start_hour"))
    end = quiet.get("end", quiet.get("end_hour"))
    if start is None or end is None:
        return False

    now = now or datetime.now(timezone.utc)

    # Optional timezone - if pytz/zoneinfo isn't available, fall
    # back to UTC silently rather than raising.
    tz_name = quiet.get("tz")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(tz_name))
        except Exception as exc:
            logger.debug("policy best-effort block failed: %s", exc)

    try:
        start_t = _parse_hour(start)
        end_t = _parse_hour(end)
    except (TypeError, ValueError):
        return False

    current = now.time()
    if start_t == end_t:
        return False  # zero-length window → off
    if start_t < end_t:
        return start_t <= current < end_t
    # Wrap-around midnight: e.g. 22:00 → 07:00
    return current >= start_t or current < end_t


def _parse_hour(value: Any) -> time:
    if isinstance(value, int):
        return time(hour=value % 24)
    if isinstance(value, str):
        if ":" in value:
            h, m = value.split(":", 1)
            return time(hour=int(h) % 24, minute=int(m) % 60)
        return time(hour=int(value) % 24)
    raise TypeError(f"unsupported quiet-hours value: {value!r}")
