"""Sessions view - browse, preview, resume, delete sessions."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import time
from typing import Any, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Static, ListView, ListItem, Input


def _time_ago(ts: float) -> str:
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


class SessionItem(ListItem):
    """A single session row in the list."""

    def __init__(self, session: dict[str, Any], index: int) -> None:
        super().__init__()
        self.session_data = session
        self.index = index

    def compose(self) -> ComposeResult:
        s = self.session_data
        sid = s.get("session_id", "?")[:8]
        title = s.get("title", "untitled")[:40] or "untitled"
        msgs = s.get("message_count", 0)
        ts = s.get("last_active", 0)
        ago = _time_ago(ts) if ts else "?"
        active = s.get("is_active", False)

        t = Text()
        t.append(f" {self.index + 1:>2} ", style="bold cyan")
        t.append(f" {sid} ", style="dim")
        if active:
            t.append(" ● ", style="bold green")
        t.append(f" {title} ", style="white")
        t.append(f" {msgs} msgs ", style="green")
        t.append(f" {ago} ", style="dim")
        yield Static(t)


class SessionPreview(Static):
    """Right panel showing session details and last messages."""

    def update_session(self, session: dict[str, Any], messages: list[dict] | None = None) -> None:
        t = Text()

        # Header
        sid = session.get("session_id", "?")
        title = session.get("title", "untitled") or "untitled"
        t.append(f"  {title}\n", style="bold white")
        t.append(f"  {sid}\n\n", style="dim")

        # Metadata
        msgs = session.get("message_count", 0)
        turns = session.get("turn_count", session.get("turns", 0))
        ts = session.get("last_active", 0)
        created = session.get("created_at", 0)

        t.append("  Messages  ", style="bold #94a3b8")
        t.append(f"{msgs}\n", style="white")
        t.append("  Turns     ", style="bold #94a3b8")
        t.append(f"{turns}\n", style="white")
        t.append("  Last      ", style="bold #94a3b8")
        t.append(f"{_time_ago(ts) if ts else '?'}\n", style="white")
        t.append("  Created   ", style="bold #94a3b8")
        t.append(f"{_time_ago(created) if created else '?'}\n", style="white")

        # Last messages preview
        if messages:
            t.append("\n  Last messages:\n", style="bold #94a3b8")
            for msg in messages[-6:]:
                role = msg.get("role", "?")
                content = msg.get("content", "")
                if not content:
                    continue
                preview = content[:80].replace("\n", " ")
                if role == "user":
                    t.append("  > ", style="bold green")
                    t.append(f"{preview}\n", style="green")
                elif role == "assistant":
                    t.append("  │ ", style="#475569")
                    t.append(f"{preview}\n", style="#e2e8f0")

        # Keybindings
        t.append("\n")
        t.append("  Enter ", style="bold cyan")
        t.append("Resume  ", style="dim")
        t.append("d ", style="bold red")
        t.append("Delete  ", style="dim")
        t.append("Esc ", style="bold")
        t.append("Close", style="dim")

        self.update(t)


class SessionsScreen(Screen):
    """Modal screen for browsing sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
        Binding("d", "delete", "Delete session"),
    ]

    DEFAULT_CSS = """
    SessionsScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #sessions-modal {
        width: 90%;
        height: 80%;
        background: #0f172a;
        border: tall #334155;
    }

    #sessions-header {
        height: 3;
        background: #111827;
        padding: 1 2;
    }

    #sessions-body {
        height: 1fr;
        layout: horizontal;
    }

    #sessions-list {
        width: 1fr;
        background: #0a0e1a;
        border-right: solid #1e293b;
    }

    #sessions-list > ListItem {
        height: 1;
        padding: 0;
    }

    #sessions-list > ListItem.--highlight {
        background: #1e293b;
    }

    #sessions-preview {
        width: 40;
        background: #0f1320;
        padding: 1;
    }

    #sessions-footer {
        height: 1;
        background: #111827;
        padding: 0 2;
    }
    """

    def __init__(
        self,
        app_id: str,
        fetch_sessions: Callable,
        fetch_history: Callable | None = None,
        delete_session: Callable | None = None,
    ) -> None:
        super().__init__()
        self._app_id = app_id
        self._fetch_sessions = fetch_sessions
        self._fetch_history = fetch_history
        self._delete_session = delete_session
        self._sessions: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="sessions-modal"):
            yield Static(
                Text.assemble(
                    ("  Sessions ", "bold white"),
                    (f"- {self._app_id}", "dim"),
                ),
                id="sessions-header",
            )
            with Horizontal(id="sessions-body"):
                yield ListView(id="sessions-list")
                yield SessionPreview("  Loading...", id="sessions-preview")
            yield Static(
                Text.assemble(
                    ("  ↑↓ ", "bold cyan"), ("Navigate  ", "dim"),
                    ("Enter ", "bold cyan"), ("Resume  ", "dim"),
                    ("d ", "bold red"), ("Delete  ", "dim"),
                    ("Esc ", "bold"), ("Close", "dim"),
                ),
                id="sessions-footer",
            )

    async def on_mount(self) -> None:
        await self._load_sessions()

    async def _load_sessions(self) -> None:
        """Fetch sessions from daemon and populate the list."""
        try:
            self._sessions = await self._fetch_sessions(self._app_id)
        except Exception as e:
            self.query_one("#sessions-preview", SessionPreview).update(
                Text(f"  Error: {e}", style="red")
            )
            return

        lv = self.query_one("#sessions-list", ListView)
        await lv.clear()
        for i, s in enumerate(self._sessions):
            await lv.append(SessionItem(s, i))

        if self._sessions:
            self._update_preview(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item and isinstance(event.item, SessionItem):
            self._update_preview(event.item.index)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter pressed - resume this session."""
        if event.item and isinstance(event.item, SessionItem):
            sid = event.item.session_data.get("session_id")
            self._selected_session = sid
            self.app.pop_screen()

    def _update_preview(self, index: int) -> None:
        if index >= len(self._sessions):
            return
        session = self._sessions[index]
        preview = self.query_one("#sessions-preview", SessionPreview)
        preview.update_session(session)

    def action_cancel(self) -> None:
        self.app.pop_screen()

    async def action_delete(self) -> None:
        lv = self.query_one("#sessions-list", ListView)
        if lv.highlighted_child and isinstance(lv.highlighted_child, SessionItem):
            sid = lv.highlighted_child.session_data.get("session_id", "")
            if self._delete_session:
                try:
                    await self._delete_session(self._app_id, sid)
                    await self._load_sessions()  # Refresh list
                except Exception as exc:
                    logger.debug("sessions best-effort block failed: %s", exc)
