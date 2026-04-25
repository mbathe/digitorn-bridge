"""Sidebar — workspace panel showing goal, todos, progress, facts.

Workspace panel rendered with Textual/Rich.
Updated via MemoryUpdate messages from the backend.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static


class ExpandableItem(Static):
    """A Static that toggles between truncated and full text on click."""

    DEFAULT_CSS = """
    ExpandableItem {
        height: auto;
    }
    """

    def __init__(self, short: Text, full: Text, **kwargs) -> None:
        super().__init__(short, **kwargs)
        self._short = short
        self._full = full
        self._expanded = False

    def on_click(self) -> None:
        self._expanded = not self._expanded
        self.update(self._full if self._expanded else self._short)


class Sidebar(VerticalScroll):
    """Right sidebar showing workspace state."""

    DEFAULT_CSS = """
    Sidebar {
        width: 34;
        background: #0f1320;
        border-left: tall #1e293b;
        padding: 1 1;
        scrollbar-size: 1 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._goal = ""
        self._plan: list[str] = []
        self._plan_step = 0
        self._todos: list[dict] = []
        self._facts: list[dict] = []
        self._notes: list[dict] = []
        self._agents: list[dict] = []
        self._git_branch = ""
        self._git_changes: list[dict] = []
        self._git_ahead = 0
        self._git_behind = 0
        self._workspace_files: list[tuple[str, str]] = []  # (path, action) — recent file activity
        self._MAX_WORKSPACE_FILES = 10
        # Temporary command panel (shown at top, cleared on next _rebuild from agent)
        self._command_panel: tuple[str, list[tuple[str, str]]] | None = None

    def show_command_panel(self, title: str, items: list[tuple[str, str]]) -> None:
        """Show a temporary info panel at the top of the sidebar.

        Used by slash commands (/status, /tools, /mcp, /sessions, /tasks, /watchers).
        Cleared automatically when the agent updates memory (next _rebuild from agent).
        """
        self._command_panel = (title, items)
        self._rebuild()

    def clear_command_panel(self) -> None:
        """Explicitly clear the command panel."""
        if self._command_panel is not None:
            self._command_panel = None
            self._rebuild()

    def update_git(self, branch: str, changes: list[dict],
                   ahead: int = 0, behind: int = 0) -> None:
        self._git_branch = branch
        self._git_changes = changes
        self._git_ahead = ahead
        self._git_behind = behind
        self._rebuild()

    def on_mount(self) -> None:
        self._rebuild()

    def update_memory(self, action: str, result: dict) -> None:
        """Process a memory_update event and refresh display.

        Accepts both FQN actions (memory.set_goal) and short names (SetGoal, TodoAdd).
        """
        # Normalize action: strip module prefix, map short names to snake_case
        _SHORT_TO_ACTION = {
            "SetGoal": "set_goal", "Remember": "remember",
            "Recall": "recall", "Forget": "forget",
            "TodoAdd": "add_todo", "TodoUpdate": "update_todo",
        }
        act = action.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        act = _SHORT_TO_ACTION.get(act, act)

        if act == "set_goal":
            new_goal = result.get("goal", "")
            if new_goal and new_goal != self._goal:
                # New goal = new mission — reset everything from previous goal
                self._goal = new_goal
                self._todos.clear()
                self._facts.clear()
                self._notes.clear()
            elif new_goal:
                self._goal = new_goal

        elif act in ("add_todo", "update_todo"):
            # Always replace the full todo list from daemon (source of truth)
            todos = result.get("todos")
            if todos is not None and isinstance(todos, list):
                self._todos = todos
            # Goal may be updated alongside todos
            goal = result.get("goal")
            if goal:
                self._goal = goal

        elif act == "remember":
            fact = result.get("content") or result.get("id", "")
            if fact and isinstance(fact, str):
                # Deduplicate
                existing = {f.get("content", "") for f in self._facts}
                if fact not in existing:
                    self._facts.append({"content": fact})
                self._facts = self._facts[-10:]

        elif act == "forget":
            forgotten_id = result.get("forgotten", "")
            self._facts = [f for f in self._facts if f.get("id") != forgotten_id]

        self._rebuild()

    def update_agent(self, agent_id: str, status: str, specialist: str = "",
                     task: str = "", duration: float = 0) -> None:
        """Update sub-agent state. Auto-clean old completed/failed agents."""
        import time as _time

        # Normalize status variants
        if status in ("done", "success"):
            status = "completed"
        elif status in ("timeout", "error"):
            status = "failed"

        # When a new agent spawns, only clear agents that finished > 4s ago
        # (don't instantly remove freshly-completed agents the user hasn't seen)
        if status in ("spawned", "running"):
            now = _time.monotonic()
            self._agents = [
                a for a in self._agents
                if a["status"] in ("spawned", "running")
                or now - a.get("_done_at", now) < 4.0
            ]

        for a in self._agents:
            if a["id"] == agent_id:
                a["status"] = status
                a["duration"] = duration
                if specialist:
                    a["specialist"] = specialist
                if task:
                    a["task"] = task
                if status not in ("spawned", "running"):
                    a["_done_at"] = _time.monotonic()
                self._rebuild()
                return
        entry = {
            "id": agent_id, "status": status,
            "specialist": specialist, "task": task, "duration": duration,
        }
        if status not in ("spawned", "running"):
            entry["_done_at"] = _time.monotonic()
        self._agents.append(entry)
        self._agents = self._agents[-8:]
        self._rebuild()

        # Schedule cleanup of completed agents after 8 seconds
        if status not in ("spawned", "running"):
            self.set_timer(8.0, self._cleanup_done_agents)

    def _cleanup_done_agents(self) -> None:
        """Remove agents that have been done/failed for > 6 seconds."""
        import time as _time
        now = _time.monotonic()
        before = len(self._agents)
        self._agents = [
            a for a in self._agents
            if a["status"] in ("spawned", "running")
            or now - a.get("_done_at", now) < 6.0
        ]
        if len(self._agents) != before:
            self._rebuild()

    def add_workspace_file(self, path: str, action: str = "read") -> None:
        """Track a file read/modified in the workspace panel."""
        # Deduplicate: update existing entry
        self._workspace_files = [(p, a) for p, a in self._workspace_files if p != path]
        self._workspace_files.insert(0, (path, action))
        self._workspace_files = self._workspace_files[:self._MAX_WORKSPACE_FILES]
        self._rebuild()

    def _max_text(self) -> int:
        """Max text width based on sidebar size."""
        return max(self.size.width - 6, 16)

    def _trunc(self, text: str, extra: int = 0) -> str:
        """Truncate text with … if too long for sidebar."""
        mx = self._max_text() - extra
        if len(text) <= mx:
            return text
        return text[:mx - 1] + "\u2026"

    def _mount_item(self, short_text: Text, full_content: str, style: str = "") -> None:
        """Mount an expandable item if text was truncated, otherwise plain Static."""
        mx = self._max_text()
        if len(full_content) > mx:
            full = Text()
            full.append(full_content, style=style or "#94a3b8")
            self.mount(ExpandableItem(short_text, full, classes="sidebar-item"))
        else:
            self.mount(Static(short_text, classes="sidebar-item"))

    def _rebuild(self) -> None:
        """Rebuild the entire sidebar content."""
        for child in list(self.children):
            try:
                child.remove()
            except Exception:
                pass  # Widget already removed or app shutting down

        # ── Title ──
        title = Text()
        title.append("Workspace", style="bold #94a3b8")
        self.mount(Static(title, classes="sidebar-section"))

        # ── Command Panel (temporary, shown at top) ──
        if self._command_panel is not None:
            panel_title, panel_items = self._command_panel
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            hdr = Text()
            hdr.append(panel_title, style="bold #3b82f6")
            self.mount(Static(hdr, classes="sidebar-item"))
            for key, value in panel_items:
                item_t = Text()
                key_display = self._trunc(key, extra=0)
                item_t.append(f" {key_display}", style="bold #e2e8f0")
                if value:
                    val_display = self._trunc(value, extra=len(key_display) + 2)
                    item_t.append(f"  {val_display}", style="#94a3b8")
                self.mount(Static(item_t, classes="sidebar-item"))
            # Close button hint
            close_t = Text()
            close_t.append("  esc to close", style="#475569")
            self.mount(Static(close_t, classes="sidebar-item"))

        # ── Git ──
        if self._git_branch:
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            gt = Text()
            gt.append("\u2387 ", style="bold #a78bfa")
            gt.append(self._git_branch, style="bold #a78bfa")
            if self._git_ahead:
                gt.append(f" \u2191{self._git_ahead}", style="#22c55e")
            if self._git_behind:
                gt.append(f" \u2193{self._git_behind}", style="#ef4444")
            self.mount(Static(gt, classes="sidebar-item"))

            _SC = {"M": "#f59e0b", "A": "#22c55e", "D": "#ef4444",
                   "?": "#64748b", "R": "#06b6d4", "U": "#ef4444"}
            for ch in self._git_changes[:10]:
                st = ch.get("status", "?")
                c = _SC.get(st, "#94a3b8")
                short_name = ch.get("path", "")
                full_path = ch.get("full_path", short_name)
                # Short version (filename only)
                short_t = Text()
                short_t.append(f" {st} ", style=f"bold {c}")
                short_t.append(self._trunc(short_name, extra=4), style=c)
                # Full version (full relative path)
                full_t = Text()
                full_t.append(f" {st} ", style=f"bold {c}")
                full_t.append(full_path, style=c)
                if full_path != short_name:
                    self.mount(ExpandableItem(short_t, full_t, classes="sidebar-item"))
                else:
                    self.mount(Static(short_t, classes="sidebar-item"))
            rest = self._git_changes[10:]
            if rest:
                short_t = Text()
                short_t.append(f"  +{len(rest)} more", style="bold #475569")
                # Full: list all remaining files
                full_t = Text()
                for i, ch in enumerate(rest):
                    st = ch.get("status", "?")
                    c = _SC.get(st, "#94a3b8")
                    name = ch.get("path", ch.get("full_path", ""))
                    full_t.append(f" {st} ", style=f"bold {c}")
                    full_t.append(name, style=c)
                    if i < len(rest) - 1:
                        full_t.append("\n")
                self.mount(ExpandableItem(short_t, full_t, classes="sidebar-item"))

        # ── Workspace Files ──
        if self._workspace_files:
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            hdr = Text()
            hdr.append("Files", style="bold #94a3b8")
            hdr.append(f"  ({len(self._workspace_files)})", style="#475569")
            self.mount(Static(hdr, classes="sidebar-item"))

            _WF_ICONS = {"read": "\u2193", "modified": "\u270e"}  # ↓ read, ✎ modified
            _WF_COLORS = {"read": "#60a5fa", "modified": "#f59e0b"}
            for path, action in self._workspace_files:
                icon = _WF_ICONS.get(action, "\u2022")
                color = _WF_COLORS.get(action, "#94a3b8")
                short_name = path.rsplit("/", 1)[-1] if "/" in path else path
                short_name = short_name.rsplit("\\", 1)[-1] if "\\" in short_name else short_name
                ft = Text()
                ft.append(f" {icon} ", style=f"bold {color}")
                ft.append(self._trunc(short_name, extra=4), style=color)
                self._mount_item(ft, path, style=color)

        # ── Goal ──
        if self._goal:
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            goal_t = Text()
            goal_t.append("\u25cf ", style="bold #f59e0b")
            goal_t.append(self._trunc(self._goal, extra=2), style="#f59e0b")
            self._mount_item(goal_t, self._goal, style="#f59e0b")

        # ── Tasks (unified: plan + todos) ──
        if self._todos:
            done = sum(1 for t in self._todos if t.get("status") == "done")
            total = len(self._todos)
            pct = (done * 100 // total) if total > 0 else 0
            self.mount(Static(Text(""), classes="sidebar-spacer"))

            hdr = Text()
            hdr.append("Tasks", style="bold #94a3b8")
            hdr.append(f"  {done}/{total} ({pct}%)", style="#475569")
            self.mount(Static(hdr, classes="sidebar-item"))

            # Progress bar
            bar_w = 20
            filled = int(bar_w * done / total) if total > 0 else 0
            bar = Text()
            bar.append("\u2588" * filled, style="#22c55e")
            bar.append("\u2591" * (bar_w - filled), style="#1e293b")
            self.mount(Static(bar, classes="sidebar-item"))

            # Todo items (show non-done first, then done)
            active = [t for t in self._todos if t.get("status") != "done"]
            completed = [t for t in self._todos if t.get("status") == "done"]
            blocked = [t for t in self._todos if t.get("status") == "blocked"]
            pending = [t for t in self._todos if t.get("status") not in ("done", "blocked", "in_progress")]
            in_progress = [t for t in self._todos if t.get("status") == "in_progress"]
            for todo in (in_progress + blocked + pending + completed)[:10]:
                t = Text()
                status = todo.get("status", "pending")
                full_content = todo.get("content", "")
                content = self._trunc(full_content, extra=2)
                if status == "done":
                    t.append("\u2713 ", style="#475569")
                    t.append(content, style="strike #475569")
                    self._mount_item(t, full_content, style="strike #475569")
                elif status == "in_progress":
                    t.append("\u25b6 ", style="bold #f59e0b")
                    t.append(content, style="bold #f59e0b")
                    self._mount_item(t, full_content, style="bold #f59e0b")
                elif status == "blocked":
                    t.append("\u25a0 ", style="bold #ef4444")
                    t.append(content, style="#ef4444")
                    self._mount_item(t, full_content, style="#ef4444")
                else:
                    t.append("\u25fb ", style="#94a3b8")
                    t.append(content, style="#94a3b8")
                    self._mount_item(t, full_content, style="#94a3b8")
            if len(self._todos) > 10:
                extra_t = Text()
                extra_t.append(f"  +{len(self._todos) - 10} more", style="#475569")
                self.mount(Static(extra_t, classes="sidebar-item"))

        # ── Facts ──
        if self._facts:
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            hdr = Text()
            hdr.append("Facts", style="bold #94a3b8")
            hdr.append(f"  ({len(self._facts)})", style="#475569")
            self.mount(Static(hdr, classes="sidebar-item"))

            for fact in self._facts[-6:]:
                full_content = fact.get("content", str(fact))
                content = self._trunc(full_content, extra=2)
                f = Text()
                f.append(f"\u2022 {content}", style="#94a3b8")
                self._mount_item(f, full_content, style="#94a3b8")

        # ── Notes ──
        if self._notes:
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            hdr = Text()
            hdr.append("Notes", style="bold #f59e0b")
            self.mount(Static(hdr, classes="sidebar-item"))

            for note in self._notes:
                n = Text()
                content = self._trunc(note.get("content", str(note)), extra=2)
                n.append(f"\u2022 {content}", style="#f59e0b")
                self.mount(Static(n, classes="sidebar-item"))

        # ── Agents ──
        if self._agents:
            active = [a for a in self._agents if a["status"] in ("spawned", "running")]
            done = [a for a in self._agents if a["status"] not in ("spawned", "running")]
            self.mount(Static(Text(""), classes="sidebar-spacer"))
            hdr = Text()
            hdr.append("Agents", style="bold #06b6d4")
            if active:
                hdr.append(f"  {len(active)} running", style="#06b6d4")
            self.mount(Static(hdr, classes="sidebar-item"))

            _icons = {
                "spawned": "\u25cc", "running": "\u25cf",
                "completed": "\u2713", "failed": "\u2717",
                "cancelled": "\u25cb",
            }
            _colors = {
                "spawned": "#f59e0b", "running": "#06b6d4",
                "completed": "#22c55e", "failed": "#ef4444",
                "cancelled": "#475569",
            }
            # Active first, then done
            for agent in active + done:
                a = Text()
                icon = _icons.get(agent["status"], "\u25cf")
                color = _colors.get(agent["status"], "#94a3b8")
                a.append(f"{icon} ", style=f"bold {color}")
                label = agent.get("specialist") or agent["id"]
                a.append(self._trunc(label, extra=2), style=color)
                self.mount(Static(a, classes="sidebar-item"))
