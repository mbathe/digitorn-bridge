"""Data models for test results - structured, inspectable, assertable."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class LiveEvent:
    """A real-time event seen during a turn."""
    type: str  # "tool_call", "tool_result", "text", "behavior", "agent_event", "approval"
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_name(self) -> str:
        return self.data.get("name", "")

    @property
    def text(self) -> str:
        return self.data.get("content", "") or self.data.get("text", "")

    def __repr__(self) -> str:
        if self.type == "tool_call":
            return f"[tool] {self.tool_name}({str(self.data.get('arguments', ''))[:50]})"
        if self.type == "tool_result":
            return f"[result] {str(self.data.get('content', ''))[:80]}"
        if self.type == "text":
            return f"[text] {self.text[:80]}"
        if self.type == "behavior":
            return f"[BEHAVIOR] {self.text[:80]}"
        return f"[{self.type}] {str(self.data)[:60]}"


# Callback type for live progress
OnLiveEvent = Callable[[LiveEvent], None]


@dataclass
class ToolCall:
    """A single tool call made by the agent."""
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    success: bool = True

    @property
    def file_path(self) -> str:
        return self.arguments.get("file_path") or self.arguments.get("path") or ""

    @property
    def command(self) -> str:
        return self.arguments.get("command") or ""

    @property
    def pattern(self) -> str:
        return self.arguments.get("pattern") or self.arguments.get("query") or ""


@dataclass
class TurnResult:
    """Result of a single turn (message + response)."""
    success: bool = True
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    turn_number: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    live_events: list[LiveEvent] = field(default_factory=list)

    # ── Convenience properties ──

    @property
    def tools_used(self) -> list[str]:
        """List of tool names used in this turn."""
        return [tc.name for tc in self.tool_calls]

    @property
    def files_read(self) -> list[str]:
        """Files that were Read in this turn."""
        return [tc.file_path for tc in self.tool_calls
                if tc.name in ("Read", "read", "filesystem.read") and tc.file_path]

    @property
    def files_edited(self) -> list[str]:
        """Files that were Edited in this turn."""
        return [tc.file_path for tc in self.tool_calls
                if tc.name in ("Edit", "edit", "filesystem.edit") and tc.file_path]

    @property
    def files_written(self) -> list[str]:
        """Files that were Written in this turn."""
        return [tc.file_path for tc in self.tool_calls
                if tc.name in ("Write", "write", "filesystem.write") and tc.file_path]

    @property
    def bash_commands(self) -> list[str]:
        """Bash commands executed in this turn."""
        return [tc.command for tc in self.tool_calls
                if tc.name in ("Bash", "bash", "shell.bash") and tc.command]

    @property
    def used_glob(self) -> bool:
        return any(n in ("Glob", "glob", "filesystem.glob") for n in self.tools_used)

    @property
    def used_grep(self) -> bool:
        return any(n in ("Grep", "grep", "filesystem.grep") for n in self.tools_used)

    @property
    def used_bash_for_files(self) -> bool:
        """True if Bash was used for file operations (cat/head/find/ls)."""
        import re
        bad = re.compile(r"\b(cat|head|tail|less|sed\s|awk\s|find\s+\.|ls\s+-[lRa]|tree)\b", re.I)
        return any(bad.search(cmd) for cmd in self.bash_commands)

    @property
    def has_behavior_violations(self) -> bool:
        return any("BEHAVIOR" in str(m.get("content", ""))
                   for m in self.raw_messages if m.get("role") == "system")

    @property
    def behavior_violations(self) -> list[str]:
        return [str(m.get("content", "")) for m in self.raw_messages
                if m.get("role") == "system" and "BEHAVIOR" in str(m.get("content", ""))]


@dataclass
class SessionHandle:
    """A live conversation session - send messages and inspect results."""
    session_id: str
    app_id: str
    daemon_url: str
    workspace: str = ""
    turns: list[TurnResult] = field(default_factory=list)
    _closed: bool = False

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def all_tools_used(self) -> list[str]:
        """All tools used across all turns."""
        tools = []
        for t in self.turns:
            tools.extend(t.tools_used)
        return tools

    @property
    def all_text(self) -> str:
        """All assistant text concatenated."""
        return "\n\n".join(t.text for t in self.turns if t.text)

    @property
    def last(self) -> TurnResult | None:
        return self.turns[-1] if self.turns else None


@dataclass
class AppHandle:
    """A deployed app - create sessions and chat."""
    app_id: str
    daemon_url: str
    status: str = ""
    mode: str = ""
    agents: list[str] = field(default_factory=list)
    total_tools: int = 0

    @property
    def is_deployed(self) -> bool:
        return bool(self.status) or bool(self.mode)
