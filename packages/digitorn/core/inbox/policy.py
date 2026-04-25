"""NotificationPolicy — decide which channels fire for an inbox item.

Reads a user's prefs from the ``inbox_notification_prefs`` table
and applies:

1. **Per-kind channel routing**: ``prefs.events[kind] → [channels]``.
   If the kind isn't listed, a default is applied (desktop only).

2. **Quiet hours**: during the user's quiet window, non-critical
   events are silenced across all channels except the SSE stream
   (which is always live for in-app usage). Critical kinds —
   approval requests, failures — ignore quiet hours.

3. **Master enable**: if ``prefs.enabled`` is false, the policy
   returns an empty channel list → nothing fires.

The policy is **pure**: no DB calls, no IO. It accepts a prefs
dict and a kind, returns a channel list. The dispatcher is the
one that fetches prefs from the store and routes to backends.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from digitorn.core.inbox.kinds import InboxKind


# Channels the daemon can dispatch to. "desktop" is a no-op on the
# server — the client's SSE stream delivers it. It's listed so the
# policy can explicitly include/exclude it in the routing decision.
CHANNELS = ("desktop", "push", "email")


# Kinds that bypass quiet hours (the user wants to know NOW).
CRITICAL_KINDS: frozenset[str] = frozenset({
    InboxKind.SESSION_FAILED,
    InboxKind.SESSION_AWAITING_APPROVAL,
    InboxKind.CREDENTIAL_EXPIRED,
    InboxKind.CREDENTIAL_MISSING,
    InboxKind.QUOTA_WARNING,
})


# Default routing when a kind isn't in prefs.events. Matches the
# Flutter client's out-of-the-box behavior (desktop toast only for
# routine completion events).
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
        """Return the list of channels that should receive this event.

        Two input shapes are supported:

        **Granular** (daemon-native)::

            { "enabled": True,
              "events": { "session.failed": ["desktop", "push"] },
              "quiet_hours": { "start": 22, "end": 7 } }

        **Flat** (Flutter client SharedPreferences shape)::

            { "desktop": True, "sound": True,
              "events": ["session.failed", "session.awaiting_approval"],
              "quiet_hours": { "start_hour": 22, "end_hour": 7, "tz": "..." } }

        In the flat shape, ``events`` is a whitelist of kinds that
        should trigger notifications at all. The top-level
        ``desktop``/``push``/``email`` booleans control the active
        channels. Matching an event that is NOT in the whitelist
        returns an empty channel list.

        Args:
            kind:  Inbox kind string (see ``InboxKind``).
            prefs: The user's stored notification prefs dict, or
                   None when no prefs are set (apply defaults).
            now:   Current time for quiet-hours testing.
        """
        prefs = prefs or {}

        # Master switch (granular shape)
        if prefs.get("enabled") is False:
            return []

        events_field = prefs.get("events")
        routing: list[str]

        if isinstance(events_field, list):
            # ── Flat shape (Flutter client) ──
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
            # ── Granular shape (daemon-native) ──
            override = events_field.get(kind)
            if override is None:
                routing = list(_DEFAULT_ROUTING.get(kind, ["desktop"]))
            else:
                routing = [c for c in override if c in CHANNELS]
        else:
            # No events config at all → apply defaults
            routing = list(_DEFAULT_ROUTING.get(kind, ["desktop"]))

        # Quiet hours — apply to non-critical kinds only
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
        """Return True if a specific channel would fire for a kind.

        Convenience wrapper — equivalent to ``channel in
        channels_for(...)`` but keeps the intent explicit at call
        sites.
        """
        return channel in NotificationPolicy.channels_for(
            kind=kind, prefs=prefs, now=now,
        )


def _in_quiet_hours(
    quiet: dict[str, Any], now: datetime | None = None,
) -> bool:
    """Check if ``now`` falls inside the user's quiet-hours window.

    Accepts an integer ``start``/``end`` (hour in 0-23) OR a string
    ``HH:MM``. Wrap-around midnight is handled (e.g. 22 → 7 means
    10pm to 7am). If ``tz`` is present, ``now`` is converted; else
    UTC is assumed. Missing/invalid values → returns False (off).
    """
    # Accept both shapes: {start, end} (granular) and
    # {start_hour, end_hour} (Flutter client).
    start = quiet.get("start", quiet.get("start_hour"))
    end = quiet.get("end", quiet.get("end_hour"))
    if start is None or end is None:
        return False

    now = now or datetime.now(timezone.utc)

    # Optional timezone — if pytz/zoneinfo isn't available, fall
    # back to UTC silently rather than raising.
    tz_name = quiet.get("tz")
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            now = now.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass

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
