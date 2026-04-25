"""Assertion helpers for test scenarios.

Each returns (ok: bool, detail: str) so a scenario runner can aggregate
failures into a report instead of stopping on the first AssertionError.
"""
from __future__ import annotations

from typing import Any


_HANDSHAKE_TYPES = {"connected"}


def seq_unique(
    events: list[dict[str, Any]],
    exclude_types: set[str] | None = None,
) -> tuple[bool, str]:
    exclude = (exclude_types or set()) | _HANDSHAKE_TYPES
    filtered = [e for e in events if e.get("type") not in exclude]
    seqs = [int(e.get("seq", 0) or 0) for e in filtered if e.get("seq") is not None]
    seen: dict[int, str] = {}
    dupes: list[tuple[int, str, str]] = []
    for e, s in zip(filtered, seqs):
        t = e.get("type", "?")
        if s in seen:
            dupes.append((s, seen[s], t))
        else:
            seen[s] = t
    if dupes:
        return False, f"duplicate seqs: {dupes[:5]}"
    return True, f"{len(seqs)} seqs unique (range {min(seqs, default=0)}..{max(seqs, default=0)})"


def seq_is_monotonic(
    events: list[dict[str, Any]],
    exclude_types: set[str] | None = None,
) -> tuple[bool, str]:
    return seq_unique(events, exclude_types)


def sort_by_seq(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda e: int(e.get("seq", 0) or 0))


def event_order(
    events: list[dict[str, Any]],
    expected_types: list[str],
    strict_contiguous: bool = False,
) -> tuple[bool, str]:
    """Verify expected_types appear in this relative order. If strict_contiguous,
    no other events may sit between them. Duplicates in expected_types match
    distinct events."""
    types = [e.get("type") for e in events]
    remaining = list(expected_types)
    last_pos = -1
    for i, t in enumerate(types):
        if not remaining:
            return True, f"all expected types found by index {last_pos}"
        if t == remaining[0]:
            if strict_contiguous and last_pos >= 0 and i != last_pos + 1:
                return False, f"non-contiguous: expected {remaining[0]} right after index {last_pos}, found at {i}"
            remaining.pop(0)
            last_pos = i
    if remaining:
        return False, f"missing expected types (in order): {remaining}"
    return True, f"order OK across {len(types)} events"


def event_count(
    events: list[dict[str, Any]],
    event_type: str,
    minimum: int = 1,
    maximum: int | None = None,
) -> tuple[bool, str]:
    n = sum(1 for e in events if e.get("type") == event_type)
    if n < minimum:
        return False, f"{event_type}: {n} < min {minimum}"
    if maximum is not None and n > maximum:
        return False, f"{event_type}: {n} > max {maximum}"
    return True, f"{event_type}: {n} events"


def correlation_id_thread(
    events: list[dict[str, Any]],
    correlation_id: str,
    expected_types: list[str],
) -> tuple[bool, str]:
    thread = [e for e in events if (e.get("payload") or {}).get("correlation_id") == correlation_id]
    types = [e.get("type") for e in thread]
    missing = [t for t in expected_types if t not in types]
    if missing:
        return False, f"correlation_id={correlation_id} missing {missing}; got {types}"
    return True, f"correlation_id={correlation_id} has {len(thread)} events"


def no_event(events: list[dict[str, Any]], event_type: str) -> tuple[bool, str]:
    n = sum(1 for e in events if e.get("type") == event_type)
    if n:
        return False, f"{event_type}: expected 0 occurrences, got {n}"
    return True, f"{event_type}: 0 occurrences"


def ephemeral_types_absent_from_persistent(
    persistent_events: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Verify high-volume streaming events aren't stored in session_events."""
    ephemeral = {"token", "thinking_delta", "thinking_started", "thinking",
                 "preview:delta", "agent_progress", "streaming_frame"}
    bad = [e for e in persistent_events if e.get("type") in ephemeral]
    if bad:
        types = [e.get("type") for e in bad[:5]]
        return False, f"persistent log contains ephemeral events: {types}"
    return True, "no ephemeral events in persistent log"


def report(checks: list[tuple[str, tuple[bool, str]]]) -> tuple[bool, str]:
    lines = []
    all_ok = True
    for name, (ok, detail) in checks:
        tag = "PASS" if ok else "FAIL"
        lines.append(f"  [{tag}] {name}: {detail}")
        if not ok:
            all_ok = False
    return all_ok, "\n".join(lines)
