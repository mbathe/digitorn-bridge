"""SpinnerBar - animated spinner using Textual's reactive auto-refresh."""

from __future__ import annotations

import time

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


_BASE_FRAMES = ["·", "✢", "✳", "✶", "✻", "✽"]
_FRAMES = _BASE_FRAMES + list(reversed(_BASE_FRAMES[1:-1]))

_VERB_INTERVAL = 3.5
_C_BLUE = "#6366f1"       # Indigo - tool execution
_C_BLUE_HI = "#818cf8"
_C_DIM = "#64748b"        # Slate - idle/dim states
_C_THINK = "#a78bfa"      # Violet - thinking
_C_THINK_HI = "#c4b5fd"
_C_GENERATE = "#34d399"   # Emerald - generating text
_C_GENERATE_HI = "#6ee7b7"


def _fmt_count(n: int) -> str:
    """Format a token count: 42, 1.5k, 12k."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_elapsed(s: float) -> str:
    if s < 1:
        return ""
    if s < 60:
        return f"{s:.0f}s"
    return f"{int(s // 60)}m{int(s % 60):02d}s"


_C_WARN = "#f59e0b"       # Amber for rate-limited/waiting
_C_WARN_HI = "#fbbf24"

# Mode → (icon, label, color, highlight_color)
_MODE_STYLES = {
    "idle":         ("●",  "Ready",         _C_DIM,      _C_DIM),
    "thinking":     ("🧠", "Thinking",      _C_THINK,    _C_THINK_HI),
    "generating":   ("✦",  "Generating",    _C_GENERATE, _C_GENERATE_HI),
    "streaming":    ("✦",  "Generating",    _C_GENERATE, _C_GENERATE_HI),
    "tool_use":     ("⚙",  "Running",       _C_BLUE,     _C_BLUE_HI),
    "requesting":   ("◇",  "Requesting",    _C_DIM,      _C_BLUE),
    "responding":   ("◇",  "Processing",    _C_DIM,      _C_BLUE),
    "rate_limited": ("⏳", "Rate limited",  _C_WARN,     _C_WARN_HI),
    "waiting":      ("◇",  "Reconnecting",  _C_DIM,      _C_WARN),
}


class SpinnerBar(Static):
    """Animated spinner line above the input."""

    # Reactive frame counter - changes trigger render()
    _frame: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._active = False
        self._mode = "responding"
        self._label = ""
        self._start_time = 0.0
        self._last_activity = 0.0  # last time mode/tokens changed
        self._timer = None
        # Direct link to app's thread-safe token accumulator [out, in].
        # Real counts from provider only - no estimates.
        self._token_source: list[int] | None = None
        self._last_token_snapshot = (0, 0)  # for stall detection

    def set_mode(self, mode: str, label: str = "") -> None:
        """Change spinner mode without restarting timers. Updates stall detection."""
        self._mode = mode
        if label:
            self._label = label
        self._last_activity = time.monotonic()

    def start(self, mode: str = "responding", label: str = "",
              reset_tokens: bool = False) -> None:
        self._active = True
        self._mode = mode
        self._label = label
        self._last_activity = time.monotonic()
        if not self._start_time or reset_tokens:
            self._start_time = time.monotonic()
        if self._timer is None:
            self._timer = self.set_interval(1 / 8, self._tick)
        self.display = True

    def stop(self) -> None:
        """Switch to idle mode - spinner stays visible but shows 'Ready'."""
        self._mode = "idle"
        self._label = ""
        self._start_time = 0.0
        # Keep timer running for idle animation (subtle pulse)
        # Keep display = True - spinner never hides

    def _tick(self) -> None:
        """Increment frame counter - triggers reactive render."""
        if self._active:
            self._frame += 1

    def render(self) -> Text:
        """Called by Textual whenever _frame changes."""
        now = time.monotonic()
        elapsed = now - self._start_time if self._start_time else 0

        # Idle mode - simple static label, no animation
        if self._mode == "idle":
            t = Text()
            t.append("● ", style=_C_DIM)
            t.append("Ready", style=_C_DIM)
            return t

        if not self._active:
            t = Text()
            t.append("● ", style=_C_DIM)
            t.append("Ready", style=_C_DIM)
            return t

        # Spinner frame
        frame = _FRAMES[self._frame % len(_FRAMES)]

        # Stall detection: if requesting/responding > 15s with no token activity, show "waiting"
        _out = 0
        _in = 0
        if self._token_source is not None:
            _out = self._token_source[0]
            _in = self._token_source[1]
        current_snapshot = (_out, _in)
        if current_snapshot != self._last_token_snapshot:
            self._last_activity = now
            self._last_token_snapshot = current_snapshot

        display_mode = self._mode
        stall_time = now - self._last_activity
        if self._mode in ("requesting", "responding") and stall_time > 8:
            display_mode = "waiting"
        elif self._mode == "tool_use" and stall_time > 30:
            display_mode = "waiting"

        # Mode-specific styling - all labels come from daemon events
        style = _MODE_STYLES.get(display_mode)
        if style:
            icon, default_label, color, hi_color = style
            label = self._label or default_label
        else:
            icon = frame
            label = self._label or "Working"
            color = _C_BLUE
            hi_color = _C_BLUE_HI

        # Build line
        t = Text()
        t.append(f"{icon} ", style=f"bold {color}")

        # Shimmer effect on label
        shimmer_pos = int(elapsed * 8) % (len(label) + 8)
        for i, ch in enumerate(f"{label}\u2026"):
            dist = abs(i - shimmer_pos)
            if dist <= 1:
                t.append(ch, style=f"bold {hi_color}")
            else:
                t.append(ch, style=color)

        # Progress bar for long operations (> 2s)
        if elapsed > 2.0 and display_mode == "tool_use":
            bar_w = 12
            # Animated fill - bounces back and forth
            cycle = int(elapsed * 2) % (bar_w * 2)
            pos = cycle if cycle < bar_w else (bar_w * 2 - cycle)
            t.append(" [", style=_C_DIM)
            for i in range(bar_w):
                if abs(i - pos) <= 1:
                    t.append("\u2588", style=color)
                else:
                    t.append("\u2591", style="#1e293b")
            t.append("]", style=_C_DIM)

        # Suffix: elapsed + real token counts from provider
        el = _fmt_elapsed(elapsed)
        has_suffix = el or _out > 0 or _in > 0
        if has_suffix:
            t.append(" (", style=_C_DIM)
            if el:
                t.append(el, style=_C_DIM)
            if _out > 0:
                if el:
                    t.append(" \u00b7 ", style=_C_DIM)
                t.append(f"\u2193 {_fmt_count(_out)} tokens", style="#e2e8f0")
            if _in > 0:
                if el or _out > 0:
                    t.append(" \u00b7 ", style=_C_DIM)
                t.append(f"\u2191 {_fmt_count(_in)} tokens", style="#94a3b8")
            t.append(")", style=_C_DIM)

        return t
