"""Per-session behavioral state - generic tracking for any app type.

The state tracks two kinds of data:

1. **Universal** (every app): turn, tool counts, violations, consecutive tool.
   These are used by the engine regardless of app type.

2. **Generic collections** (any app can use):
   - ``sets``: named sets of strings - track targets per category
     e.g. {"read_files": {"a.py", "b.py"}, "searched_urls": {"https://..."}}
   - ``counters``: named integer counters - track "since" patterns
     e.g. {"changes_since_test": 3, "queries_since_verify": 1}
   - ``flags``: named booleans
     e.g. {"has_web_searched": True, "plan_approved": False}
   - ``tool_history``: ordered list of (tool_name, target) for recent actions

Built-in rules in ``rules.py`` use convenience properties that map to the
generic collections (``read_files`` → ``sets["read_files"]``) so existing
YAML configs work unchanged. New apps define their own set/counter/flag
names without touching this code.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BhvSessionState:
    """Mutable state tracked across an entire session.

    Generic by design: ``sets``, ``counters``, and ``flags`` hold
    arbitrary named values that rules and the classifier reference.
    """

    # ── Universal (every app) ──

    turn: int = 0
    violations_count: int = 0
    last_violation: str = ""
    tool_calls_this_turn: int = 0
    total_tool_calls: int = 0
    last_tool_name: str = ""
    consecutive_same_tool: int = 0
    plan_stated: bool = False

    # ── Generic collections ──

    # Named sets of strings (e.g. "read_files", "edited_files", "fetched_urls")
    sets: dict[str, set[str]] = field(default_factory=dict)

    # Named integer counters (e.g. "changes_since_test", "reads_since_search")
    counters: dict[str, int] = field(default_factory=dict)

    # Named boolean flags (e.g. "has_web_searched", "plan_approved")
    flags: dict[str, bool] = field(default_factory=dict)

    # Recent tool calls: list of (tool_name, target_or_summary)
    tool_history: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=50),
    )

    # ── Universal actions ──

    def on_new_turn(self) -> None:
        """Reset per-turn state."""
        self.plan_stated = False
        self.tool_calls_this_turn = 0
        self.turn += 1

    def on_tool_call(self, tool_name: str, target: str = "") -> None:
        self.tool_calls_this_turn += 1
        self.total_tool_calls += 1
        self.tool_history.append((tool_name, target))
        if tool_name == self.last_tool_name:
            self.consecutive_same_tool += 1
        else:
            self.last_tool_name = tool_name
            self.consecutive_same_tool = 1

    # ── Generic collection helpers ──

    def add_to_set(self, name: str, value: str) -> None:
        """Add a value to a named set."""
        if name not in self.sets:
            self.sets[name] = set()
        self.sets[name].add(value)

    def get_set(self, name: str) -> set[str]:
        """Get a named set (empty if missing)."""
        return self.sets.get(name, set())

    def increment_counter(self, name: str, amount: int = 1) -> None:
        """Increment a named counter."""
        self.counters[name] = self.counters.get(name, 0) + amount

    def reset_counter(self, name: str) -> None:
        """Reset a named counter to 0."""
        self.counters[name] = 0

    def get_counter(self, name: str) -> int:
        """Get a named counter (0 if missing)."""
        return self.counters.get(name, 0)

    def set_flag(self, name: str, value: bool = True) -> None:
        """Set a named boolean flag."""
        self.flags[name] = value

    def get_flag(self, name: str) -> bool:
        """Get a named flag (False if missing)."""
        return self.flags.get(name, False)

    # ── Convenience properties for backward compat with built-in rules ──
    # These map to the generic collections so existing rules.py code
    # using state.read_files, state.edited_files, etc. still works.

    @property
    def read_files(self) -> set[str]:
        return self.get_set("read_files")

    @property
    def edited_files(self) -> set[str]:
        return self.get_set("edited_files")

    @property
    def written_files(self) -> set[str]:
        return self.get_set("written_files")

    @property
    def searched_patterns(self) -> list[str]:
        # Return as list for backward compat (was a list before)
        return list(self.get_set("searched_patterns"))

    @property
    def reads_since_search(self) -> int:
        return self.get_counter("reads_since_search")

    @reads_since_search.setter
    def reads_since_search(self, val: int) -> None:
        self.counters["reads_since_search"] = val

    @property
    def changes_since_test(self) -> int:
        return self.get_counter("changes_since_test")

    @changes_since_test.setter
    def changes_since_test(self, val: int) -> None:
        self.counters["changes_since_test"] = val

    @property
    def has_web_searched(self) -> bool:
        return self.get_flag("has_web_searched")

    @has_web_searched.setter
    def has_web_searched(self, val: bool) -> None:
        self.flags["has_web_searched"] = val

    # ── Convenience mutation methods (backward compat) ──

    def on_read(self, file_path: str) -> None:
        self.add_to_set("read_files", file_path)
        self.increment_counter("reads_since_search")

    def on_edit(self, file_path: str) -> None:
        self.add_to_set("edited_files", file_path)
        self.increment_counter("changes_since_test")

    def on_write(self, file_path: str) -> None:
        self.add_to_set("written_files", file_path)
        self.increment_counter("changes_since_test")

    def on_search(self, pattern: str) -> None:
        self.add_to_set("searched_patterns", pattern)
        self.reset_counter("reads_since_search")

    def on_test_run(self) -> None:
        self.reset_counter("changes_since_test")

    def on_web_search(self) -> None:
        self.set_flag("has_web_searched", True)

    # ── Snapshot for the classifier ──

    def snapshot(self) -> dict[str, Any]:
        """Return a complete state snapshot for the classifier.

        Generic: includes all sets, counters, flags, and universal
        fields. The classifier formats whatever it finds - nothing
        is hardcoded to a specific app type.
        """
        snap: dict[str, Any] = {
            "turn": self.turn,
            "total_tool_calls": self.total_tool_calls,
            "tool_calls_this_turn": self.tool_calls_this_turn,
            "consecutive_same_tool": self.consecutive_same_tool,
            "last_tool_name": self.last_tool_name,
            "violations_count": self.violations_count,
            "last_violation": self.last_violation,
        }

        # Dump all named sets (only non-empty)
        for name, values in self.sets.items():
            if values:
                snap[name] = list(values)

        # Dump all named counters (only non-zero)
        for name, count in self.counters.items():
            if count:
                snap[f"counter:{name}"] = count

        # Dump all named flags (only True)
        for name, flag in self.flags.items():
            if flag:
                snap[f"flag:{name}"] = True

        # Recent tool history (last 10)
        if self.tool_history:
            recent = list(self.tool_history)[-10:]
            snap["recent_tools"] = [
                f"{name}({target})" if target else name
                for name, target in recent
            ]

        return snap


# Backward-compat aliases
SessionBehaviorState = BhvSessionState  # noqa: F841
BehaviorSessionState = BhvSessionState  # noqa: F841
