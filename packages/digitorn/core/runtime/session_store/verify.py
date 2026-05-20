"""Byte-identical verification helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event

logger = logging.getLogger(__name__)


@dataclass
class DiffReport:
    """Result of a byte-identical comparison."""

    equal: bool = True
    event_count_left: int = 0
    event_count_right: int = 0
    field_diffs: list[str] = field(default_factory=list)
    seq_misses: list[int] = field(default_factory=list)

    def add_diff(self, msg: str) -> None:
        self.equal = False
        self.field_diffs.append(msg)

    def summary(self) -> str:
        if self.equal:
            return (
                f"EQUAL ({self.event_count_left} events, "
                "byte-identical row-for-row)"
            )
        head = (
            f"DIFFER (left={self.event_count_left}, "
            f"right={self.event_count_right} events; "
            f"{len(self.field_diffs)} field diff(s))"
        )
        details = "\n  ".join(self.field_diffs[:10])
        if len(self.field_diffs) > 10:
            details += f"\n  ... and {len(self.field_diffs) - 10} more"
        return f"{head}\n  {details}"


def diff_event_lists(
    left: list[Event], right: list[Event],
) -> DiffReport:
    """Compare two event sequences. Equal when same length AND every"""
    report = DiffReport(
        event_count_left=len(left),
        event_count_right=len(right),
    )
    if len(left) != len(right):
        report.equal = False
        report.add_diff(
            f"event_count: left={len(left)} vs right={len(right)}"
        )
    seen_left = {ev.seq: ev for ev in left}
    seen_right = {ev.seq: ev for ev in right}
    for seq in sorted(set(seen_left) | set(seen_right)):
        l = seen_left.get(seq)
        r = seen_right.get(seq)
        if l is None:
            report.seq_misses.append(seq)
            report.add_diff(f"seq={seq}: missing on left")
            continue
        if r is None:
            report.seq_misses.append(seq)
            report.add_diff(f"seq={seq}: missing on right")
            continue
        l_dict = l.to_dict()
        r_dict = r.to_dict()
        if l_dict != r_dict:
            for k in sorted(set(l_dict) | set(r_dict)):
                if l_dict.get(k) != r_dict.get(k):
                    report.add_diff(
                        f"seq={seq} field={k}: "
                        f"left={l_dict.get(k)!r} vs right={r_dict.get(k)!r}"
                    )
    return report


async def diff_stores_for_session(
    *,
    left: InMemorySessionStore,
    right: InMemorySessionStore,
    sid: str,
    include_compacted: bool = False,
) -> DiffReport:
    """Compare two stores' view of the same session."""
    if include_compacted:
        left_events = await _read_full_history(left, sid)
        right_events = await _read_full_history(right, sid)
    else:
        left_state = left.state(sid)
        right_state = right.state(sid)
        left_events = list(left_state.events) if left_state else []
        right_events = list(right_state.events) if right_state else []
    return diff_event_lists(left_events, right_events)


async def _read_full_history(
    store: InMemorySessionStore, sid: str,
) -> list[Event]:
    out: list[Event] = []
    async for ev in store.stream_full_history(sid):
        out.append(ev)
    return out


def diff_jsonl_files(
    left_path: Path, right_path: Path,
) -> DiffReport:
    """Compare two events.jsonl files line-by-line. Used to verify"""
    left_lines = _read_jsonl(left_path)
    right_lines = _read_jsonl(right_path)
    return diff_event_lists(left_lines, right_lines)


def _read_jsonl(path: Path) -> list[Event]:
    if not path.exists():
        return []
    out: list[Event] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Event.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning(
                    "diff_jsonl_skip_bad_line path=%s err=%s", path, exc,
                )
    return out
