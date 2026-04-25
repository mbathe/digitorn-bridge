"""InfoPanel — floating overlay panel in the chat area.

Shows data like a dropdown menu. Escape or any key closes it.
Used for /context, /tools, /sessions, /status, /cost, /model.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class InfoPanel(Static):
    """A floating info panel that appears in the chat area.

    Mount it, show data, press Escape to close.
    """

    DEFAULT_CSS = """
    InfoPanel {
        background: #111827;
        border: round #334155;
        padding: 1 2;
        margin: 0 4;
        width: 50;
        height: auto;
        max-height: 20;
        display: none;
    }

    InfoPanel.visible {
        display: block;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)

    def show(self, title: str, rows: list[tuple[str, str]],
             bars: dict[str, float] | None = None) -> None:
        """Display a panel with title and key-value rows.

        Args:
            title: Panel header
            rows: List of (label, value) pairs
            bars: Optional dict of label → percentage (0-1) for progress bars
        """
        t = Text()

        # Title
        t.append(f"  {title}\n", style="bold white")
        t.append(f"  {'─' * 30}\n", style="#334155")

        # Progress bars first (if any)
        if bars:
            for label, pct in bars.items():
                pct = max(0.0, min(1.0, pct))
                bar_w = 20
                filled = int(bar_w * pct)
                color = "#22c55e" if pct < 0.5 else "#f59e0b" if pct < 0.8 else "#ef4444"
                t.append(f"  {label:<12} ", style="bold #94a3b8")
                t.append("\u2588" * filled, style=color)
                t.append("\u2591" * (bar_w - filled), style="#1e293b")
                t.append(f" {pct*100:.0f}%\n", style="white")
            t.append("\n")

        # Key-value rows
        for label, value in rows:
            t.append(f"  {label:<14} ", style="#94a3b8")
            t.append(f"{value}\n", style="white")

        # Footer
        t.append(f"\n  {'─' * 30}\n", style="#334155")
        t.append("  Esc to close", style="#475569")

        self.update(t)
        self.add_class("visible")
        self.scroll_visible()

    def close(self) -> None:
        self.remove_class("visible")

    @property
    def is_visible(self) -> bool:
        return self.has_class("visible")
