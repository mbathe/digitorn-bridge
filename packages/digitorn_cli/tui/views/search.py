"""Search overlay - Ctrl+F to search in chat messages."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Static, Input


class SearchBar(Static):
    """Floating search bar at the top of the chat."""

    DEFAULT_CSS = """
    SearchBar {
        dock: top;
        height: 3;
        background: #1e293b;
        border-bottom: solid #334155;
        padding: 0 2;
        display: none;
    }

    SearchBar.visible {
        display: block;
    }

    SearchBar Input {
        width: 1fr;
        background: #0f172a;
        color: #f1f5f9;
        border: solid #334155;
        height: 1;
    }

    SearchBar Input:focus {
        border: solid #3b82f6;
    }

    SearchBar .search-count {
        width: auto;
        padding: 0 2;
        color: #94a3b8;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._match_count = 0
        self._current_match = 0
        self._query = ""
        self._on_search = None  # callback(query: str) → int (match count)
        self._on_navigate = None  # callback(direction: int) → None
        self._on_close = None  # callback() → None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(" 🔍 ", classes="search-icon")
            yield Input(placeholder="Search...", id="search-input")
            yield Static("", id="search-count", classes="search-count")

    def open(self, on_search=None, on_navigate=None, on_close=None) -> None:
        self._on_search = on_search
        self._on_navigate = on_navigate
        self._on_close = on_close
        self.add_class("visible")
        try:
            inp = self.query_one("#search-input", Input)
            inp.value = ""
            inp.focus()
        except Exception as exc:
            logger.debug("search best-effort block failed: %s", exc)

    def close(self) -> None:
        self.remove_class("visible")
        self._query = ""
        self._match_count = 0
        self._current_match = 0
        if self._on_close:
            self._on_close()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        if self._on_search:
            self._match_count = self._on_search(event.value)
            self._current_match = 1 if self._match_count > 0 else 0
        self._update_count()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter → next match
        if self._on_navigate and self._match_count > 0:
            self._current_match = min(self._current_match + 1, self._match_count)
            self._on_navigate(1)
            self._update_count()

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.close()
            event.prevent_default()
            event.stop()
        elif event.key == "up" and self._match_count > 0:
            self._current_match = max(self._current_match - 1, 1)
            if self._on_navigate:
                self._on_navigate(-1)
            self._update_count()
            event.prevent_default()
            event.stop()

    def _update_count(self) -> None:
        try:
            label = self.query_one("#search-count", Static)
            if self._match_count > 0:
                label.update(Text(f"{self._current_match}/{self._match_count}", style="#94a3b8"))
            elif self._query:
                label.update(Text("No matches", style="#ef4444"))
            else:
                label.update("")
        except Exception as exc:
            logger.debug("search best-effort block failed: %s", exc)
