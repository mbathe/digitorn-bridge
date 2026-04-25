"""Deterministic time expression parser — no LLM, pure regex.

Parses natural-language time expressions (FR + EN) into scheduling
instructions for the SchedulerService.

Supported forms::

    "in 5m"              "in 5 minutes"        "dans 5 minutes"
    "in 2h"              "in 2 hours"           "dans 2 heures"
    "in 30s"             "in 30 seconds"        "dans 30 secondes"
    "in 1d"              "in 1 day"             "dans 1 jour"
    "in 1h30m"           "in 1 hour 30 minutes"

    "tomorrow at 9am"    "demain à 9h"          "demain à 9h30"
    "today at 14:30"     "aujourd'hui à 14h30"
    "2026-03-14T09:00:00Z"  (ISO 8601 passthrough)

    "every day at 9am"      "tous les jours à 9h"
    "every monday at 10am"  "tous les lundis à 10h"
    "every hour"            "toutes les heures"
    "every 5 minutes"       "toutes les 5 minutes"
    "0 9 * * *"             (raw cron passthrough)

Returns a ``ParsedTime`` with schedule_type, run_at, cron_expr, and
interval_seconds — ready to feed into ScheduledJob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_UNIT_MAP_EN = {
    "s": "seconds", "sec": "seconds", "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
}

_UNIT_MAP_FR = {
    "s": "seconds", "sec": "seconds", "seconde": "seconds", "secondes": "seconds",
    "m": "minutes", "min": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "heure": "hours", "heures": "hours",
    "j": "days", "jour": "days", "jours": "days",
}

_UNIT_MAP = {**_UNIT_MAP_EN, **_UNIT_MAP_FR}

_DAY_MAP_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_DAY_MAP_FR = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6,
    "lundis": 0, "mardis": 1, "mercredis": 2, "jeudis": 3,
    "vendredis": 4, "samedis": 5, "dimanches": 6,
}

_DAY_MAP = {**_DAY_MAP_EN, **_DAY_MAP_FR}

_DAY_TO_CRON = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}


@dataclass
class ParsedTime:
    """Result of parsing a time expression."""

    schedule_type: str
    run_at: str | None = None
    cron_expr: str | None = None
    interval_seconds: float | None = None
    error: str | None = None


def parse_time(expr: str, *, now: datetime | None = None) -> ParsedTime:
    """Parse a time expression into a ParsedTime.

    Args:
        expr: Natural-language or structured time expression.
        now: Override current time (for testing). Defaults to UTC now.

    Returns:
        ParsedTime with schedule_type and the relevant field filled.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    expr = expr.strip()
    if not expr:
        return ParsedTime(schedule_type="once", error="Empty time expression")

    result = _try_iso8601(expr)
    if result:
        return result

    result = _try_raw_cron(expr)
    if result:
        return result

    lower = expr.lower()

    result = _try_relative(lower, now)
    if result:
        return result

    result = _try_recurring(lower, now)
    if result:
        return result

    result = _try_absolute(lower, now)
    if result:
        return result

    return ParsedTime(
        schedule_type="once",
        error=f"Could not parse time expression: '{expr}'",
    )


def _try_iso8601(expr: str) -> ParsedTime | None:
    """Try to parse as ISO 8601 datetime."""
    try:
        dt = datetime.fromisoformat(expr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return ParsedTime(schedule_type="once", run_at=dt.isoformat())
    except ValueError:
        return None


_CRON_FIELD_RE = re.compile(r'^[\d*,/\-]+$')

def _try_raw_cron(expr: str) -> ParsedTime | None:
    """Try to parse as raw 5-field cron expression."""
    fields = expr.split()
    if len(fields) != 5:
        return None
    if all(_CRON_FIELD_RE.match(f) for f in fields):
        return ParsedTime(schedule_type="cron", cron_expr=expr)
    return None


_RELATIVE_COMPOUND_RE = re.compile(
    r'(\d+)\s*([a-zéè]+)',
    re.IGNORECASE,
)

def _try_relative(lower: str, now: datetime) -> ParsedTime | None:
    """Parse relative delay: 'in 5m', 'dans 30 minutes'."""
    for prefix in ("in ", "dans ", "after ", "après "):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    else:
        return None

    return _parse_duration_to_once(lower, now)


def _parse_duration_to_once(text: str, now: datetime) -> ParsedTime | None:
    """Parse a duration string into a 'once' ParsedTime."""
    total = timedelta()
    found = False
    for match in _RELATIVE_COMPOUND_RE.finditer(text):
        num = int(match.group(1))
        unit_raw = match.group(2).rstrip("s").rstrip("e")
        unit_key = match.group(2).lower()
        unit = _UNIT_MAP.get(unit_key) or _UNIT_MAP.get(unit_raw)
        if unit is None:
            continue
        if unit == "seconds":
            total += timedelta(seconds=num)
        elif unit == "minutes":
            total += timedelta(minutes=num)
        elif unit == "hours":
            total += timedelta(hours=num)
        elif unit == "days":
            total += timedelta(days=num)
        found = True

    if not found:
        return None

    target = now + total
    return ParsedTime(schedule_type="once", run_at=target.isoformat())


_TIME_RE = re.compile(
    r'(\d{1,2})(?::(\d{2})|h(\d{0,2}))?\s*(am|pm)?',
    re.IGNORECASE,
)

def _parse_time_of_day(text: str) -> tuple[int, int] | None:
    """Extract (hour, minute) from a time-of-day string."""
    m = _TIME_RE.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or m.group(3) or 0)
    ampm = m.group(4)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return (hour, minute)


def _try_absolute(lower: str, now: datetime) -> ParsedTime | None:
    """Parse absolute time: 'tomorrow at 9am', 'demain à 9h'."""
    base_day = None
    if "tomorrow" in lower or "demain" in lower:
        base_day = now + timedelta(days=1)
    elif "today" in lower or "aujourd" in lower:
        base_day = now
    else:
        for day_name, day_num in _DAY_MAP.items():
            if day_name in lower:
                days_ahead = (day_num - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                base_day = now + timedelta(days=days_ahead)
                break

    if base_day is None:
        return None

    time_part = lower
    for sep in (" at ", " à ", " a "):
        if sep in lower:
            time_part = lower.split(sep, 1)[1]
            break

    tod = _parse_time_of_day(time_part)
    if tod is None:
        return None

    hour, minute = tod
    target = base_day.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    return ParsedTime(schedule_type="once", run_at=target.isoformat())


def _try_recurring(lower: str, now: datetime) -> ParsedTime | None:
    """Parse recurring: 'every day at 9am', 'tous les jours à 9h', 'every 5 minutes'."""
    recurring_text = None
    for prefix in ("every ", "tous les ", "toutes les ", "chaque "):
        if lower.startswith(prefix):
            recurring_text = lower[len(prefix):]
            break

    if recurring_text is None:
        return None

    interval_result = _try_recurring_interval(recurring_text)
    if interval_result:
        return interval_result

    if recurring_text.strip() in ("hour", "heure", "heures"):
        return ParsedTime(schedule_type="cron", cron_expr="0 * * * *")

    if recurring_text.startswith(("day ", "jour ", "jours ")):
        rest = recurring_text.split(None, 1)[1] if " " in recurring_text else ""
        for sep in ("at ", "à ", "a "):
            if rest.startswith(sep):
                rest = rest[len(sep):]
                break
        tod = _parse_time_of_day(rest)
        if tod:
            return ParsedTime(
                schedule_type="cron",
                cron_expr=f"{tod[1]} {tod[0]} * * *",
            )
        return ParsedTime(schedule_type="cron", cron_expr="0 0 * * *")

    for day_name, day_num in _DAY_MAP.items():
        if recurring_text.startswith(day_name):
            rest = recurring_text[len(day_name):].strip()
            for sep in ("at ", "à ", "a "):
                if rest.startswith(sep):
                    rest = rest[len(sep):]
                    break
            tod = _parse_time_of_day(rest)
            cron_day = _DAY_TO_CRON[day_num]
            if tod:
                return ParsedTime(
                    schedule_type="cron",
                    cron_expr=f"{tod[1]} {tod[0]} * * {cron_day}",
                )
            return ParsedTime(
                schedule_type="cron",
                cron_expr=f"0 0 * * {cron_day}",
            )

    return None


def _try_recurring_interval(text: str) -> ParsedTime | None:
    """Parse 'N minutes/hours' as an interval schedule."""
    m = re.match(r'(\d+)\s+([a-zéè]+)', text)
    if not m:
        return None
    num = int(m.group(1))
    unit_raw = m.group(2).lower()
    unit = _UNIT_MAP.get(unit_raw)
    if unit is None:
        return None
    if unit == "seconds":
        return ParsedTime(schedule_type="interval", interval_seconds=float(num))
    if unit == "minutes":
        return ParsedTime(schedule_type="interval", interval_seconds=float(num * 60))
    if unit == "hours":
        return ParsedTime(schedule_type="interval", interval_seconds=float(num * 3600))
    if unit == "days":
        return ParsedTime(schedule_type="interval", interval_seconds=float(num * 86400))
    return None
