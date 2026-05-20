"""TabBar - session tabs at the top of the chat area."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.message import Message
from textual.widgets import Static


class TabBar(Static):
    """Horizontal tab bar showing open sessions."""

    DEFAULT_CSS = """
    TabBar {
        height: 1;
        background: #111827;
        padding: 0 1;
    }

    TabBar.hidden {
        display: none;
    }
    """

    class TabSelected(Message):
        """User clicked/switched to a tab."""
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class TabClosed(Message):
        """User closed a tab."""
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class NewTabRequested(Message):
        """User wants a new session tab."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tabs: list[dict[str, Any]] = []  # [{id, title, active}]
        self._active_id: str = ""

    def add_tab(self, session_id: str, title: str = "New") -> None:
        """Add a new tab."""
        # Deactivate all
        for t in self._tabs:
            t["active"] = False
        self._tabs.append({"id": session_id, "title": title[:20], "active": True})
        self._active_id = session_id
        self._render()
        # Show tab bar when we have 2+ tabs
        if len(self._tabs) >= 2:
            self.remove_class("hidden")

    def set_active(self, session_id: str) -> None:
        """Switch to an existing tab."""
        for t in self._tabs:
            t["active"] = (t["id"] == session_id)
        self._active_id = session_id
        self._render()

    def close_tab(self, session_id: str) -> str | None:
        """Close a tab."""
        self._tabs = [t for t in self._tabs if t["id"] != session_id]
        if not self._tabs:
            self.add_class("hidden")
            return None
        # Activate the last tab
        self._tabs[-1]["active"] = True
        self._active_id = self._tabs[-1]["id"]
        self._render()
        if len(self._tabs) < 2:
            self.add_class("hidden")
        return self._active_id

    def update_title(self, session_id: str, title: str) -> None:
        """Update the title of a tab."""
        for t in self._tabs:
            if t["id"] == session_id:
                t["title"] = title[:20]
                break
        self._render()

    def next_tab(self) -> str | None:
        """Switch to next tab."""
        if len(self._tabs) < 2:
            return None
        idx = next((i for i, t in enumerate(self._tabs) if t["active"]), 0)
        new_idx = (idx + 1) % len(self._tabs)
        return self._switch_to(new_idx)

    def prev_tab(self) -> str | None:
        """Switch to previous tab."""
        if len(self._tabs) < 2:
            return None
        idx = next((i for i, t in enumerate(self._tabs) if t["active"]), 0)
        new_idx = (idx - 1) % len(self._tabs)
        return self._switch_to(new_idx)

    def tab_at(self, index: int) -> str | None:
        """Switch to tab at index (0-based)."""
        if index >= len(self._tabs):
            return None
        return self._switch_to(index)

    def _switch_to(self, idx: int) -> str:
        for i, t in enumerate(self._tabs):
            t["active"] = (i == idx)
        self._active_id = self._tabs[idx]["id"]
        self._render()
        return self._active_id

    @property
    def tab_count(self) -> int:
        return len(self._tabs)

    @property
    def active_session_id(self) -> str:
        return self._active_id

    def _render(self) -> None:
        t = Text()
        for i, tab in enumerate(self._tabs):
            is_active = tab["active"]
            sid_short = tab["id"][:6]
            title = tab["title"] or sid_short

            if is_active:
                t.append(f" {i+1}:{title} ", style="bold white on #334155")
            else:
                t.append(f" {i+1}:{title} ", style="#64748b")
            t.append("\u2502", style="#1e293b")  # │ separator

        if self._tabs:
            t.append("  +", style="bold #3b82f6")  # New tab hint

        self.update(t)
