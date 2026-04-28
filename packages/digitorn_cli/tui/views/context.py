"""Context view - visual context window breakdown with progress bars.

Shows token usage, pressure, breakdown (system prompt, tools, messages),
and context configuration.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static


def _bar(value: float, max_val: float, width: int = 30, color: str = "#3b82f6") -> Text:
    """Render a progress bar."""
    if max_val <= 0:
        pct = 0.0
    else:
        pct = min(value / max_val, 1.0)
    filled = int(width * pct)
    t = Text()
    t.append("\u2588" * filled, style=color)
    t.append("\u2591" * (width - filled), style="#1e293b")
    t.append(f" {pct * 100:.1f}%", style="bold white")
    return t


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class ContextScreen(Screen):
    """Modal screen showing context window breakdown."""

    BINDINGS = [
        Binding("escape", "cancel", "Close", priority=True),
        Binding("r", "refresh", "Refresh"),
    ]

    DEFAULT_CSS = """
    ContextScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    #context-modal {
        width: 70%;
        height: 70%;
        background: #0f172a;
        border: tall #334155;
        padding: 2;
    }

    #context-header {
        height: 3;
        padding: 1 0;
    }

    #context-body {
        height: 1fr;
    }

    #context-footer {
        height: 1;
    }
    """

    def __init__(self, context_data: dict[str, Any], usage_data: dict[str, Any]) -> None:
        super().__init__()
        self._context = context_data
        self._usage = usage_data

    def compose(self) -> ComposeResult:
        with Vertical(id="context-modal"):
            yield Static(id="context-header")
            yield Static(id="context-body")
            yield Static(
                Text.assemble(("  r ", "bold cyan"), ("Refresh  ", "dim"), ("Esc ", "bold"), ("Close", "dim")),
                id="context-footer",
            )

    def on_mount(self) -> None:
        self._render()

    def _render(self) -> None:
        ctx = self._context
        usage = self._usage

        # Header
        max_tokens = ctx.get("max_tokens", 0)
        effective = ctx.get("effective_max", max_tokens)
        pressure = ctx.get("pressure", 0.0)

        header = Text()
        header.append("  Context Window\n", style="bold white")
        header.append(f"  {_fmt_tokens(effective)} / {_fmt_tokens(max_tokens)} tokens", style="dim")
        self.query_one("#context-header", Static).update(header)

        # Body
        t = Text()

        # Overall pressure bar
        pressure_color = "#22c55e" if pressure < 0.5 else "#f59e0b" if pressure < 0.8 else "#ef4444"
        t.append("  Pressure\n", style="bold #94a3b8")
        t.append("  ")
        t.append_text(_bar(pressure, 1.0, width=40, color=pressure_color))
        t.append("\n\n")

        # Token breakdown
        in_tokens = usage.get("input_tokens", usage.get("total_input_tokens", 0))
        out_tokens = usage.get("output_tokens", usage.get("total_output_tokens", 0))
        total = in_tokens + out_tokens

        t.append("  Token Usage\n", style="bold #94a3b8")
        t.append(f"  Input   {_fmt_tokens(in_tokens):>8}  ", style="white")
        t.append_text(_bar(in_tokens, max_tokens, width=30, color="#3b82f6"))
        t.append(f"\n  Output  {_fmt_tokens(out_tokens):>8}  ", style="white")
        t.append_text(_bar(out_tokens, max_tokens, width=30, color="#22c55e"))
        t.append(f"\n  Total   {_fmt_tokens(total):>8}  ", style="white")
        t.append_text(_bar(total, max_tokens, width=30, color="#a78bfa"))
        t.append("\n\n")

        # Context breakdown (if available)
        sys_pct = ctx.get("system_prompt_pct", 0)
        tools_pct = ctx.get("tools_schema_pct", 0)
        msg_pct = ctx.get("message_history_pct", 0)

        if sys_pct or tools_pct or msg_pct:
            t.append("  Breakdown\n", style="bold #94a3b8")
            t.append(f"  System    {sys_pct:>5.1f}%  ", style="white")
            t.append_text(_bar(sys_pct, 100, width=30, color="#f59e0b"))
            t.append(f"\n  Tools     {tools_pct:>5.1f}%  ", style="white")
            t.append_text(_bar(tools_pct, 100, width=30, color="#3b82f6"))
            t.append(f"\n  Messages  {msg_pct:>5.1f}%  ", style="white")
            t.append_text(_bar(msg_pct, 100, width=30, color="#a78bfa"))
            t.append("\n\n")

        # Config
        strategy = ctx.get("strategy", "?")
        threshold = ctx.get("threshold", ctx.get("compaction_threshold", 0.75))
        reserved = ctx.get("output_reserved", 0)
        compactions = ctx.get("compactions", 0)

        t.append("  Configuration\n", style="bold #94a3b8")
        t.append(f"  Strategy       ", style="dim")
        t.append(f"{strategy}\n", style="white")
        t.append(f"  Threshold      ", style="dim")
        t.append(f"{threshold:.0%}\n", style="white")
        t.append(f"  Output reserve ", style="dim")
        t.append(f"{_fmt_tokens(reserved)}\n", style="white")
        t.append(f"  Compactions    ", style="dim")
        t.append(f"{compactions}\n", style="white")

        # Cost
        cost = usage.get("cost_usd", 0)
        if cost:
            t.append(f"\n  Cost           ", style="dim")
            t.append(f"${cost:.4f}\n", style="bold #22c55e")

        self.query_one("#context-body", Static).update(t)

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_refresh(self) -> None:
        self._render()
