"""StatusFooter — bottom status bar with tokens, context pressure, session info."""

from __future__ import annotations

import subprocess
from pathlib import Path

from rich.text import Text
from textual.widgets import Static


def _fmt_tokens(n: int) -> str:
    n = int(n) if n else 0
    if n >= 100_000:
        return f"{n // 1000}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _compact_path(p: str) -> str:
    """Shorten path: replace $HOME with ~."""
    home = str(Path.home())
    if p.startswith(home):
        return "~" + p[len(home):]
    return p


def _fit_label(label: str, max_width: int) -> str:
    """Truncate workspace label to fit: ~/…/folder:branch."""
    if len(label) <= max_width:
        return label
    # Split path:branch
    if ":" in label:
        path, branch = label.rsplit(":", 1)
        suffix = ":" + branch
    else:
        path, suffix = label, ""
    # Keep first segment (~/), ellipsis, last folder, and branch
    parts = path.split("/")
    # Always keep first part (~ or root) and last part (folder name)
    head = parts[0] + "/" if parts else ""
    tail = parts[-1] if len(parts) > 1 else ""
    short = f"{head}\u2026/{tail}{suffix}"
    if len(short) <= max_width:
        return short
    # Still too long — just truncate with ellipsis
    return label[:max_width - 1] + "\u2026"


def _git_branch(workspace: str) -> str:
    """Get current git branch, or '' if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass  # Non-critical: git branch detection is best-effort
    return ""


class StatusFooter(Static):
    """Bottom status bar: hints | tokens | context pressure | session."""

    DEFAULT_CSS = """
    StatusFooter {
        height: 1;
        padding: 0 4;
        background: $surface;
        color: #475569;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.mode = "standalone"
        self.turns = 0
        self.session_id = ""
        self.model = ""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tool_calls = 0
        self.tool_success = 0
        self.tool_failed = 0
        self.cost_usd = 0.0
        self.context_pressure = 0.0
        self.context_threshold = 0.75
        self.context_max_tokens = 0
        self.context_effective_max = 0
        self.context_output_reserved = 0
        self.context_system_pct = 0.0
        self.context_tools_pct = 0.0
        self.context_messages_pct = 0.0
        self._busy = False
        self._workspace = ""
        self._workspace_label = ""

    def set_workspace(self, workspace: str) -> None:
        """Set workspace path — detects git branch once."""
        self._workspace = workspace
        short = _compact_path(workspace)
        branch = _git_branch(workspace)
        if branch:
            self._workspace_label = f"{short}:{branch}"
        else:
            self._workspace_label = short

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.refresh_bar()

    def refresh_bar(self) -> None:
        """Rebuild the status bar content."""
        t = Text()

        # Left side
        if self._busy:
            left = "esc to interrupt"
        else:
            left = "F1 help \u00b7 ctrl+z undo \u00b7 ctrl+b sidebar"
        t.append(left, style="#475569")

        # Right side parts
        right_parts: list[str] = []

        # Model
        if self.model:
            right_parts.append(self.model)

        # Token counts
        if self.prompt_tokens > 0 or self.completion_tokens > 0:
            right_parts.append(f"\u2191{_fmt_tokens(self.prompt_tokens)} \u2193{_fmt_tokens(self.completion_tokens)}")

        # Tool calls
        if self.tool_calls > 0:
            tc = f"\u2699{self.tool_calls}"
            if self.tool_failed > 0:
                tc += f" ({self.tool_failed}\u2717)"
            right_parts.append(tc)

        # Context pressure — compact bar
        if self.context_pressure > 0:
            pct = int(min(self.context_pressure, 1.0) * 100)
            right_parts.append(f"ctx:{pct}%")

        # Cost
        if self.cost_usd > 0:
            right_parts.append(f"${self.cost_usd:.4f}")

        # Turns
        if self.turns:
            right_parts.append(f"T{self.turns}")

        right = " \u00b7 ".join(right_parts)

        # Workspace label — centered between left and right
        # Guard against narrow terminals
        width = max(self.size.width, 40)
        ws = self._workspace_label
        used = len(left) + len(right) + 8
        available = width - used
        if ws and available > 12:
            ws_display = _fit_label(ws, max(available - 4, 8))
            gap_total = max(available, 1)
            ws_start = max((gap_total - len(ws_display)) // 2, 1)
            t.append(" " * ws_start)
            t.append(ws_display, style="#64748b")
            remaining = max(gap_total - ws_start - len(ws_display), 0)
            t.append(" " * remaining)
        else:
            gap = max(width - used, 1)
            t.append(" " * gap)

        t.append(right, style="#475569")
        self.update(t)
