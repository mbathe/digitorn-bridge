"""ChatLog - scrollable message area using VerticalScroll + Static children.

Each message/tool call is a Static widget mounted into a VerticalScroll.
The spinner is the last widget, always right after the last message.
VerticalScroll handles all scrolling - no RichLog internal scroll conflicts.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from rich.markdown import Markdown
from rich.text import Text

from textual.containers import VerticalScroll
from textual.widgets import Static


# Icons
_DOT = "\u25cf"      # ● phase bullet
_SUB = "\u23bf"       # ⎿ legacy sub-item
_HOOK = "\u273b"      # ✻ hook event
_CHECK = "\u2713"     # ✓ success
_CROSS = "\u2717"     # ✗ error

# Timeline rails
_TREE_MID = "\u251c\u2500"  # ├─ sub-item (middle)
_TREE_END = "\u2514\u2500"  # └─ sub-item (last)
_RAIL_TEXT = "\u2502"        # │  model response rail
_RAIL_THINK = "\u250a"      # ┊  thinking rail
_THINK_MAX = 5               # visible thinking lines before collapse



def _extract_brief_from_data(label: str, data: dict) -> str:
    """Extract a compact brief from tool result data."""
    # Read → line count
    total = data.get("total_lines")
    if total and isinstance(total, int):
        return f"{total} lines"
    # Grep → match count
    count = data.get("count", data.get("numMatches"))
    if count and isinstance(count, int):
        nfiles = data.get("numFiles")
        if isinstance(nfiles, int) and nfiles > 1:
            return f"{count} matches in {nfiles} files"
        return f"{count} matches"
    # Glob/find → file count
    files = data.get("files")
    if isinstance(files, list):
        return f"{len(files)} files"
    # Search → result count
    results = data.get("results")
    if isinstance(results, list):
        return f"{len(results)} results"
    # Bash → exit code
    exit_code = data.get("exit_code")
    if exit_code is not None and exit_code != 0:
        return f"exit {exit_code}"
    stdout = data.get("stdout", "")
    if isinstance(stdout, str) and stdout.strip():
        first = stdout.strip().split("\n")[0][:40]
        return first
    # Fetch → content length
    length = data.get("length")
    if length and isinstance(length, int):
        return f"{length:,} chars"
    return ""


def _format_timing(elapsed: float) -> str:
    if elapsed < 0.01:
        return ""
    if elapsed < 1.0:
        return f"{elapsed * 1000:.0f}ms"
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    return f"{mins}m{secs:02d}s"


class ChatLog(VerticalScroll):
    """Main scrollable message area. Each entry is a Static child."""

    DEFAULT_CSS = """
    ChatLog {
        scrollbar-size: 1 1;
    }
    ChatLog .indented {
        padding-left: 2;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._streaming_buf: str = ""
        self._streaming_active: bool = False
        self._streaming_widget: Static | None = None
        self._tool_start_times: dict[str, float] = {}
        self._last_streamed_text: str = ""  # For tool narration detection
        self._last_streamed_widget: Static | None = None
        # Thread-safe text accumulator - agent thread writes, timer reads.
        # Bypasses call_from_thread batching so text appears progressively.
        self._stream_text_acc: list[str] = []  # Agent thread appends deltas here
        self._stream_lock = threading.Lock()  # Protects _stream_text_acc and _thinking_text_acc
        self._stream_flush_timer: Any = None
        # Thinking streaming (timer-driven for real-time display)
        self._thinking_buf: str = ""
        self._thinking_widget: Static | None = None
        self._thinking_active: bool = False
        self._thinking_rendered_len: int = 0  # How much of buf already rendered
        self._thinking_timer: Any = None
        # Thread-safe thinking accumulator (same pattern as streaming text)
        self._thinking_text_acc: list[str] = []  # Agent thread appends deltas here
        self._last_thinking_content: str | None = None
        self._last_thinking_widget: Static | None = None
        self._msg_count = 0
        # Tool widget tracking - maps "verb:detail" → widget_id for pulsing updates
        self._tool_widgets: dict[str, str] = {}
        self._pulse_timer: Any = None
        self._pulse_dim: bool = False
        # Tool grouping - track tools per turn for grouped display
        self._turn_tool_count: int = 0
        self._turn_group_widget: Static | None = None
        # Agent group tracking
        self._agent_group_widget: Static | None = None
        self._agents: dict[str, dict] = {}  # agent_id → {specialist, task, status, ...}
        self._bookmarks: list[str] = []  # widget IDs of bookmarked messages

    def _is_near_bottom(self) -> bool:
        """Check if the user is scrolled near the bottom (within 3 lines)."""
        try:
            max_scroll = self.max_scroll_y
            return (max_scroll - self.scroll_y) < 3
        except Exception:
            return True  # Default to auto-scroll if can't determine

    # Max widgets before pruning oldest. Keeps memory bounded for long sessions.
    _MAX_WIDGETS = 300
    _PRUNE_COUNT = 100  # Remove this many when threshold hit

    def _append(self, content: Text | Markdown | str, indent: bool = False,
                css_class: str = "") -> None:
        """Mount a new Static child at the end of the chat log.

        Only auto-scrolls if the user is already near the bottom.
        If the user scrolled up to read, their position is preserved.
        Prunes oldest widgets when count exceeds _MAX_WIDGETS.
        """
        was_near_bottom = self._is_near_bottom()
        self._msg_count += 1
        parts = []
        if indent:
            parts.append("indented")
        if css_class:
            parts.append(css_class)
        widget = Static(content, id=f"msg-{self._msg_count}", classes=" ".join(parts))
        try:
            spinner = self.query_one("#spinner-bar")
            self.mount(widget, before=spinner)
        except Exception:
            self.mount(widget)
        if was_near_bottom:
            self.scroll_end(animate=False)
        # Prune oldest widgets to keep memory bounded
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        """Remove oldest widgets when count exceeds threshold."""
        children = [c for c in self.children if c.id != "spinner-bar"]
        if len(children) <= self._MAX_WIDGETS:
            return
        # Check if a prune indicator already exists
        has_indicator = any(
            c.id and c.id.startswith("prune-") for c in children[:5]
        )
        # Remove oldest widgets
        to_remove = children[:self._PRUNE_COUNT]
        for child in to_remove:
            try:
                child.remove()
            except Exception:
                pass
        # Add indicator at top (if not already present)
        if not has_indicator:
            self._msg_count += 1
            indicator = Text()
            indicator.append("  \u2500\u2500\u2500 older messages trimmed \u2500\u2500\u2500", style="#475569")
            w = Static(indicator, id=f"prune-{self._msg_count}")
            try:
                first = list(self.children)[0]
                self.mount(w, before=first)
            except Exception:
                self.mount(w)

    def _spacer(self) -> None:
        """Insert a blank line between blocks."""
        self._append(Text(" "))

    def clear_all(self) -> None:
        """Remove all messages from the chat log."""
        self._end_streaming_if_active()
        for child in list(self.children):
            if child.id != "spinner-bar":
                try:
                    child.remove()
                except Exception:
                    pass
        self._msg_count = 0
        self._tool_widgets.clear()
        self._tool_start_times.clear()
        self._agents.clear()
        self._agent_group_widget = None

    def add_help_panel(self) -> None:
        """Show keyboard shortcuts and slash commands help panel."""
        self._spacer()
        t = Text()
        t.append("Keyboard Shortcuts\n", style="bold #94a3b8")
        shortcuts = [
            ("Escape", "Stop generation / focus input"),
            ("F1", "Show this help"),
            ("Ctrl+B", "Toggle sidebar"),
            ("Ctrl+Z", "Undo last file edit"),
            ("Ctrl+L", "Clear chat"),
            ("Ctrl+C", "Quit"),
        ]
        for key, desc in shortcuts:
            t.append(f"  {key:<16}", style="bold #64748b")
            t.append(f"{desc}\n", style="#94a3b8")
        t.append("\n")
        t.append("Slash Commands\n", style="bold #94a3b8")
        commands = [
            ("/help", "Show this help"),
            ("/status", "Session status (tokens, turns, mode)"),
            ("/tools [query]", "List available tools"),
            ("/sessions", "List sessions"),
            ("/resume <id>", "Resume a previous session"),
            ("/history [id]", "Show message history"),
            ("/fork", "Fork current session"),
            ("/mcp", "List MCP servers for this app"),
            ("/mcp health", "Health check MCP servers"),
            ("/tasks", "Show background tasks"),
            ("/watchers", "Show active watchers"),
            ("/clear", "Clear chat history"),
            ("/quit", "Exit"),
        ]
        for cmd, desc in commands:
            t.append(f"  {cmd:<16}", style="bold #64748b")
            t.append(f"{desc}\n", style="#94a3b8")
        self._append(t, indent=True)

    def add_info_panel(self, title: str, items: list[tuple[str, str]]) -> None:
        """Show a generic info panel with key-value pairs."""
        self._spacer()
        t = Text()
        t.append(f"{title}\n", style="bold #94a3b8")
        for key, value in items:
            t.append(f"  {key:<14}", style="bold #64748b")
            t.append(f"{value}\n", style="#e2e8f0")
        self._append(t, indent=True)

    # ── User messages ──────────────────────────────────────────────

    def add_user_message(self, text: str) -> None:
        # New turn - reset response widget so next stream creates a fresh one
        self._response_widget = None
        self._response_text = ""
        # Collapse thinking from the previous turn (not the current one)
        self._collapse_previous_thinking()
        t = Text()
        t.append("\u276f ", style="bold #22c55e")
        t.append(text, style="#f1f5f9")
        self._append(t)

    # ── Tool calls ─────────────────────────────────────────────────

    def _fit_detail(self, verb: str, detail: str, extra: str = "") -> str:
        """Shorten detail to fit on one line with the verb, dot, and extra text.

        Uses the actual widget width so it adapts to terminal size.
        """
        if not detail:
            return ""
        # Available width: widget width minus indent, dot, verb, parens, extra
        try:
            avail = self.size.width - 4  # 4 = indent padding
        except Exception:
            avail = 80
        # "● Verb(" = 2 + len(verb) + 1, ")" = 1, extra
        overhead = 2 + len(verb) + 1 + 1 + len(extra)
        max_detail = avail - overhead
        if max_detail < 15:
            max_detail = 15
        if len(detail) <= max_detail:
            return detail
        return detail[:max_detail - 1] + "\u2026"

    def add_tool_start(self, verb: str, detail: str) -> None:
        self._end_streaming_if_active()
        # Reset response widget - next text after the tool gets a fresh widget
        self._response_widget = None
        self._response_text = ""
        self._collapse_previous_thinking()
        key = f"{verb}:{detail}"
        self._tool_start_times[key] = time.monotonic()
        self._turn_tool_count += 1

        is_grouped = self._turn_tool_count > 1

        # Show the tool call - use tree connector if grouped
        if not is_grouped:
            self._spacer()
        t = Text()
        if is_grouped:
            t.append(f"{_SUB} ", style="#334155")
        else:
            t.append(f"{_DOT} ", style="bold #5769f7")
        t.append(verb, style="bold #5769f7")
        if detail:
            short = self._fit_detail(verb, detail)
            t.append("(", style="#5769f7")
            t.append(short, style="#e2e8f0")
            t.append(")", style="#5769f7")

        self._msg_count += 1
        widget_id = f"msg-{self._msg_count}"
        widget = Static(t, id=widget_id, classes="indented tool-running")
        try:
            spinner = self.query_one("#spinner-bar")
            self.mount(widget, before=spinner)
        except Exception:
            self.mount(widget)
        self.scroll_end(animate=False)
        self._maybe_prune()
        # Track widget for update when result arrives
        self._tool_widgets[key] = widget_id
        # Start pulse timer if not already running
        if self._pulse_timer is None:
            self._pulse_timer = self.set_interval(0.75, self._toggle_pulse)

    def add_tool_result(
        self, verb: str, detail: str,
        ok: bool, error: str, result: Any,
    ) -> None:
        # Reset response widget - next text after this tool gets a fresh widget
        self._response_widget = None
        self._response_text = ""
        key = f"{verb}:{detail}"
        start = self._tool_start_times.pop(key, None)
        elapsed = time.monotonic() - start if start else 0.0
        timing = _format_timing(elapsed)

        # Build final header line
        color = "#22c55e" if ok else "#ef4444"
        brief = self._brief_result(verb, result) if ok and not error else ""
        # Extra text that follows the detail on the same line
        extra_suffix = ""
        if timing:
            extra_suffix += f"  {timing}"
        if brief:
            extra_suffix += f"  {brief}"

        # Use tree connector for grouped tools (2nd+ in same turn)
        is_grouped = self._turn_tool_count > 1
        icon = _CHECK if ok else _CROSS
        prefix_style = f"bold {color}"

        t = Text()
        if is_grouped:
            t.append(f"{icon} ", style=prefix_style)
        else:
            t.append(f"{_DOT} ", style=prefix_style)
        t.append(verb, style=f"bold {color}")
        if detail:
            short = self._fit_detail(verb, detail, extra_suffix)
            t.append("(", style=color)
            t.append(short, style="#e2e8f0")
            t.append(")", style=color)
        if timing:
            t.append(f"  {timing}", style="#475569")
        if brief:
            t.append(f"  {brief}", style="#64748b")

        # Update existing widget (stop animation) or create new one
        widget_id = self._tool_widgets.pop(key, None)
        if widget_id:
            try:
                widget = self.query_one(f"#{widget_id}", Static)
                widget.update(t)
                widget.remove_class("tool-running")
                widget.remove_class("tool-dim")
            except Exception:
                self._spacer()
                self._append(t, indent=True)
        else:
            self._spacer()
            self._append(t, indent=True)
        # Stop pulse timer if no more running tools
        if not self._tool_widgets and self._pulse_timer is not None:
            try:
                self._pulse_timer.stop()
            except Exception:
                pass  # Timer may already be stopped
            self._pulse_timer = None
            self._pulse_dim = False

        # ⎿ children
        if error:
            self._append_sub(str(error)[:200], style="#ef4444")
        if result:
            self._show_edit_diff(result)
            # Only show bash output if it's multi-line (brief already shows 1-line output)
            if not brief:
                self._show_bash_output(verb, result, ok=ok, error=error)
            elif error:
                self._show_bash_output(verb, result, ok=ok, error=error)

    def _toggle_pulse(self) -> None:
        """Toggle dim/bright on all .tool-running widgets for a pulse effect."""
        self._pulse_dim = not self._pulse_dim
        for widget in self.query(".tool-running"):
            if self._pulse_dim:
                widget.add_class("tool-dim")
            else:
                widget.remove_class("tool-dim")

    def _show_bash_output(self, verb: str, result: Any, ok: bool = True,
                          error: str = "") -> None:
        """Show bash command output preview with syntax highlighting."""
        v = verb.lower()
        if v not in ("bash", "run", "running", "command"):
            return
        data = self._get_data(result)
        if not data:
            return
        output = data.get("stdout", data.get("output", ""))
        stderr = data.get("stderr", "")
        code = data.get("exit_code", data.get("returncode", 0))
        is_err = bool(code and code != 0)

        # For errors: show stderr first, then stdout
        text = ""
        if is_err:
            text = (stderr.strip() or output.strip() or "")
        else:
            text = output.strip() if output else ""
        if not text:
            return

        lines = text.split("\n")
        max_lines = 8 if is_err else 6
        show_lines = lines[:max_lines]
        overflow = len(lines) - len(show_lines)
        show_text = "\n".join(show_lines)

        # Try syntax highlighting via rich.Syntax
        if not is_err and len(show_text) > 10:
            try:
                from rich.syntax import Syntax
                # Detect language from command or output
                command = data.get("command", "")
                lexer = self._guess_output_lexer(command, show_text)
                if lexer and lexer in ("json", "diff", "python", "javascript", "yaml", "toml", "sql", "html", "css", "xml", "bash"):
                    # Render syntax-highlighted lines with tree connectors
                    highlighted_lines = show_text.split("\n")
                    self._append_tree(
                        [line[:120] for line in highlighted_lines],
                        style="#94a3b8",
                    )
                    if overflow > 0:
                        self._append_sub(
                            f"\u2026 +{overflow} lines", style="#475569", last=True,
                        )
                    return
            except Exception:
                pass  # Fallback to plain text

        # Plain text fallback - use tree connectors for proper alignment
        style = "#ef4444" if is_err else "#94a3b8"
        self._append_tree(
            [line[:120] for line in show_lines],
            style=style,
        )

        if overflow > 0:
            t = Text()
            t.append("  ", style="")
            t.append(f"\u2026 +{overflow} lines", style="#475569")
            self._append(t, indent=True)

    @staticmethod
    def _guess_output_lexer(command: str, output: str) -> str | None:
        """Guess the syntax lexer for command output."""
        cmd = command.strip().split()[0] if command.strip() else ""
        # JSON output
        stripped = output.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return "json"
        # Python
        if cmd in ("python", "python3", "pip"):
            return "python"
        # Test runners
        if "PASSED" in output or "FAILED" in output or "ERROR" in output:
            return None  # Test output is better as plain text with colors
        # Git
        if cmd == "git":
            if "diff" in command:
                return "diff"
            return None
        # General - if output looks like code, use generic
        if any(kw in output for kw in ("function ", "class ", "import ", "def ", "const ", "let ")):
            return "python"  # Rough guess
        return None

    # ── Brief result summaries ─────────────────────────────────────

    @staticmethod
    def _get_data(result: Any) -> dict:
        """Extract data dict from ActionResult or dict."""
        if isinstance(result, dict):
            d = result.get("data", result)
            return d if isinstance(d, dict) else {}
        if hasattr(result, "data") and isinstance(result.data, dict):
            return result.data
        return {}

    def _brief_result(self, verb: str, result: Any) -> str:
        """One-line summary for tool result. Returns empty string if nothing useful."""
        data = self._get_data(result)
        if not data:
            return ""

        v = verb.lower()

        # Read → line count
        if v in ("reading", "read"):
            total = data.get("total_lines", 0)
            if total:
                return f"{total} lines"

        # Listing/ls → entry count
        if v in ("listing", "ls", "list"):
            count = data.get("count", 0)
            if count:
                dirs = sum(1 for e in data.get("entries", []) if e.get("type") == "dir")
                files = count - dirs
                parts = []
                if files:
                    parts.append(f"{files} files")
                if dirs:
                    parts.append(f"{dirs} dirs")
                return ", ".join(parts) or f"{count} entries"

        # Find → file count
        if v in ("finding", "find", "search"):
            count = data.get("count", 0)
            if count:
                return f"{count} found"

        # Grep → match count
        if v in ("grep", "grepping", "searching"):
            count = data.get("count", 0)
            if count:
                files = len({m.get("file", "") for m in data.get("matches", [])})
                if files > 1:
                    return f"{count} matches in {files} files"
                return f"{count} matches"

        # Bash/run → exit code + first line
        if v in ("bash", "run", "running", "command"):
            code = data.get("exit_code", data.get("returncode"))
            output = data.get("stdout", data.get("output", ""))
            if isinstance(output, str) and output.strip():
                first = output.strip().split("\n")[0][:60]
                prefix = f"exit {code} · " if code is not None and code != 0 else ""
                return f"{prefix}{first}"
            if code is not None and code != 0:
                return f"exit {code}"

        # Edit/update → diff preview below handles this
        if v in ("editing", "edit", "replace lines", "replacing", "update"):
            return ""  # Summary shown in diff block

        # Write → diff preview handles this now
        if v in ("writing", "write", "creating"):
            return ""

        return ""

    _MAX_DIFF_LINES = 16  # Max lines to show in diff preview

    def _append_tree(self, lines: list[str], style: str = "#94a3b8") -> None:
        """Print result lines with ├─/└─ tree connectors."""
        if not lines:
            return
        show = lines[:self._MAX_DIFF_LINES]
        overflow = len(lines) - len(show)
        for i, line in enumerate(show):
            t = Text()
            is_last = (i == len(show) - 1) and overflow == 0
            connector = _TREE_END if is_last else _TREE_MID
            t.append(f"{connector} ", style="#334155")
            t.append(line[:120], style=style)
            self._append(t, indent=True)
        if overflow > 0:
            t = Text()
            t.append(f"{_TREE_END} ", style="#334155")
            t.append(f"\u2026 +{overflow} more lines", style="#475569")
            self._append(t, indent=True)

    def _append_sub(self, text: str, style: str = "#94a3b8", last: bool = False) -> None:
        connector = _TREE_END if last else _TREE_MID
        t = Text()
        t.append(f"{connector} ", style="#334155")
        t.append(text, style=style)
        self._append(t, indent=True)

    # ── Edit diff preview ────────────────────────────────────────

    # Diff code column width (background extends to this width)
    @property
    def _DIFF_CODE_WIDTH(self) -> int:
        """Dynamic diff width - fills the terminal width minus indent and line number prefix."""
        try:
            return max(self.size.width - 12, 40)
        except Exception:
            return 80

    # ── Pygments syntax highlighting for diff lines ──

    # Token type → color (Monokai-inspired, works on dark backgrounds)
    _TOKEN_COLORS: dict[str, str] = {}  # Lazily populated

    @classmethod
    def _get_token_colors(cls) -> dict[str, str]:
        """Build token type → color mapping (cached)."""
        if cls._TOKEN_COLORS:
            return cls._TOKEN_COLORS
        from pygments import token as T
        cls._TOKEN_COLORS = {
            # Keywords
            str(T.Keyword):            "bold #c792ea",
            str(T.Keyword.Constant):   "bold #f78c6c",
            str(T.Keyword.Namespace):  "bold #c792ea",
            str(T.Keyword.Type):       "italic #ffcb6b",
            # Names
            str(T.Name.Function):      "#82aaff",
            str(T.Name.Function.Magic):"#82aaff",
            str(T.Name.Class):         "bold #ffcb6b",
            str(T.Name.Decorator):     "italic #c792ea",
            str(T.Name.Builtin):       "#82aaff",
            str(T.Name.Builtin.Pseudo):"italic #f07178",
            str(T.Name.Exception):     "bold #f07178",
            str(T.Name.Tag):           "bold #f07178",
            str(T.Name.Attribute):     "#ffcb6b",
            # Literals
            str(T.Literal.String):     "#c3e88d",
            str(T.Literal.String.Doc): "italic #676e95",
            str(T.Literal.String.Escape): "bold #89ddff",
            str(T.Literal.String.Interpol): "#89ddff",
            str(T.Literal.String.Regex): "#89ddff",
            str(T.Literal.Number):     "#f78c6c",
            # Operators
            str(T.Operator):           "#89ddff",
            str(T.Operator.Word):      "bold #c792ea",
            str(T.Punctuation):        "#89ddff",
            # Comments
            str(T.Comment):            "italic #676e95",
            # Generic
            str(T.Name.Variable.Instance): "#f07178",
        }
        return cls._TOKEN_COLORS

    _lexer_cache: dict[str, object] = {}  # path_ext → lexer (class-level cache)

    @classmethod
    def _get_lexer(cls, path: str):
        """Get a Pygments lexer for a file path. Uses Pygments' 597 built-in lexers."""
        import os
        ext = os.path.splitext(path)[1].lower()
        if not ext:
            return None
        if ext in cls._lexer_cache:
            return cls._lexer_cache[ext]
        try:
            from pygments.lexers import get_lexer_for_filename
            lexer = get_lexer_for_filename(path, stripall=False)
            cls._lexer_cache[ext] = lexer
            return lexer
        except Exception:
            cls._lexer_cache[ext] = None
            return None

    @classmethod
    def _highlight_code(cls, code: str, t: Text, base_style: str, bg: str,
                        lexer: object = None, pad_to: int = 0) -> None:
        """Append code with Pygments syntax highlighting on a background color.

        If pad_to > 0, pads the line with spaces so the background extends
        to a fixed width (like an IDE gutter).
        """
        truncated = code[:100]
        char_count = 0

        if not lexer or not truncated.strip():
            t.append(truncated, style=f"{base_style} on {bg}")
            char_count = len(truncated)
        else:
            from pygments import lex
            colors = cls._get_token_colors()
            for tok_type, tok_value in lex(truncated, lexer):
                # Skip trailing newline that Pygments always adds
                if tok_value == "\n":
                    continue
                # Walk up the token type hierarchy to find a matching color
                color = None
                tt = tok_type
                while tt and not color:
                    color = colors.get(str(tt))
                    tt = tt.parent
                if color:
                    t.append(tok_value, style=f"{color} on {bg}")
                else:
                    t.append(tok_value, style=f"{base_style} on {bg}")
                char_count += len(tok_value)

        # Pad to fixed width so background extends across the full line
        if pad_to > 0 and char_count < pad_to:
            t.append(" " * (pad_to - char_count), style=f"on {bg}")

    def _show_edit_diff(self, result: Any) -> None:
        """Show diff in Claude Code style with background colors + syntax highlight.

        Format:
          ⎿  Added 3 lines, removed 1 line
              315    max_risk_level: medium
              316    grant:
              317 -    old_code()           ← red background
              317 +    new_code()           ← green background + keyword colors
              318 +    extra_line()
              319    context_after
        """
        data = self._get_data(result)
        if not data:
            return

        diff = data.get("diff") or ""
        if not diff:
            return

        # Parse diff lines - format from _mini_diff:
        # "+   42│content", "-   42│content", "    42│content", "@@ ... @@"
        lines = diff.strip().split("\n")

        # Count additions and deletions
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("@@"))
        removed = sum(1 for l in lines if l.startswith("-"))

        # ── Summary line (⎿  Added N lines, removed M lines) ──
        summary = Text()
        summary.append(f"{_SUB} ", style="#334155")
        parts: list[str] = []
        if added:
            parts.append(f"Added {added} line{'s' if added != 1 else ''}")
        if removed:
            parts.append(f"removed {removed} line{'s' if removed != 1 else ''}")
        summary.append(", ".join(parts) or "No changes", style="#94a3b8")
        self._append(summary, indent=True)

        # ── Diff lines ──
        show = lines[:self._MAX_DIFF_LINES]
        overflow = len(lines) - len(show)

        _BG_ADD = "#0d2818"      # Dark green background
        _BG_DEL = "#2d0f0f"      # Dark red background
        _FG_ADD = "#86efac"      # Light green text
        _FG_DEL = "#fca5a5"      # Light red text
        _FG_LN_ADD = "#3d6b4e"   # Dim green line numbers
        _FG_LN_DEL = "#6b3d3d"   # Dim red line numbers

        # Get Pygments lexer for syntax highlighting
        path = data.get("path") or ""
        lexer = self._get_lexer(path)

        for line in show:
            if line.startswith("@@"):
                continue

            t = Text()
            t.append("  ", style="")  # Child indent (2 extra within indented widget)

            if line.startswith("+"):
                # Added line - green background + Pygments syntax highlight
                rest = line[1:]
                if "\u2502" in rest:
                    ln_part, code = rest.split("\u2502", 1)
                    t.append(f"{ln_part.strip():>4} ", style=f"{_FG_LN_ADD} on {_BG_ADD}")
                    t.append("+", style=f"bold #22c55e on {_BG_ADD}")
                    self._highlight_code(code, t, _FG_ADD, _BG_ADD, lexer=lexer, pad_to=self._DIFF_CODE_WIDTH)
                else:
                    t.append("     ", style=f"on {_BG_ADD}")
                    t.append("+", style=f"bold #22c55e on {_BG_ADD}")
                    self._highlight_code(rest, t, _FG_ADD, _BG_ADD, lexer=lexer, pad_to=self._DIFF_CODE_WIDTH)

            elif line.startswith("-"):
                # Removed line - red background + Pygments syntax highlight
                rest = line[1:]
                if "\u2502" in rest:
                    ln_part, code = rest.split("\u2502", 1)
                    t.append(f"{ln_part.strip():>4} ", style=f"{_FG_LN_DEL} on {_BG_DEL}")
                    t.append("-", style=f"bold #ef4444 on {_BG_DEL}")
                    self._highlight_code(code, t, _FG_DEL, _BG_DEL, lexer=lexer, pad_to=self._DIFF_CODE_WIDTH)
                else:
                    t.append("     ", style=f"on {_BG_DEL}")
                    t.append("-", style=f"bold #ef4444 on {_BG_DEL}")
                    self._highlight_code(rest, t, _FG_DEL, _BG_DEL, lexer=lexer, pad_to=self._DIFF_CODE_WIDTH)

            else:
                # Context line - no background, dim syntax highlight
                content = line[1:] if line.startswith(" ") else line
                if "\u2502" in content:
                    ln_part, code = content.split("\u2502", 1)
                    t.append(f"{ln_part.strip():>4} ", style="#475569")
                    t.append(" ", style="")
                    t.append(code[:100], style="#64748b")
                else:
                    t.append("      ", style="")
                    t.append(content[:100], style="#64748b")

            self._append(t, indent=True)

        if overflow > 0:
            t = Text()
            t.append("  ", style="")
            t.append(f"     \u2026 +{overflow} more lines", style="#475569")
            self._append(t, indent=True)

    # ── Streaming (block-based rendering) ─────────────────────────
    #
    # Instead of showing tokens character-by-character, we accumulate
    # in a buffer while the spinner shows the live token count.
    # At paragraph boundaries (\n\n) the buffer is flushed as rendered
    # Markdown, then the spinner resumes for the next block.

    def start_streaming(self) -> None:
        # Don't collapse thinking here - it belongs to the current turn.
        # Collapse happens at the start of the NEXT turn (in add_user_message).
        self._streaming_buf = ""
        self._streaming_active = True
        self._streaming_widget = None
        self._streaming_flushed = ""  # All content already flushed as blocks
        # Do NOT clear _stream_text_acc - the agent thread may have already
        # written deltas via the fast-path in _make_poster before this runs.
        # The timer will drain them on the next tick.
        # Start timer to drain text accumulator at 10fps
        if self._stream_flush_timer is None:
            self._stream_flush_timer = self.set_interval(0.1, self._drain_stream_acc)

    def add_token(self, delta: str) -> None:
        """Called from agent thread (via call_from_thread) or directly.

        Appends to thread-safe accumulator; the timer drains it.
        """
        if not self._streaming_active:
            self.start_streaming()
        self._stream_text_acc.append(delta)

    # Regex for sentence-ending punctuation followed by space or newline.
    # Matches: ". ", "! ", "? ", ".\n", "!\n", "?\n" - safe split points
    # that won't cut words. Also matches ":" followed by newline (list intros).
    _SENTENCE_END_RE = re.compile(r'[.!?:]\s')

    def _drain_stream_acc(self) -> None:
        """Timer callback (10fps): drain thread-safe accumulator into buffer."""
        if not self._stream_text_acc:
            return
        # Atomically drain all pending deltas
        pending = self._stream_text_acc[:]
        self._stream_text_acc.clear()
        self._streaming_buf += "".join(pending)
        # Try to flush at natural boundaries
        self._flush_streaming_blocks()

    def _find_block_boundary(self, buf: str) -> int:
        """Find the best split point in buf - returns index AFTER the boundary.

        Priority:
        1. Paragraph break (\\n\\n) - strongest boundary
        2. Line break after a complete sentence (. / ! / ? at end of line)
        3. Any sentence end followed by space (mid-paragraph sentence boundary)

        Returns -1 if no good boundary found.
        """
        # 1. Paragraph break - always best
        idx = buf.rfind("\n\n")
        if idx != -1:
            return idx + 2  # Include the double newline

        # 2. Sentence end at line break: "text.\n" or "text!\n"
        for m in reversed(list(re.finditer(r'[.!?]\n', buf))):
            pos = m.end()
            if pos >= 20:  # Minimum block size to avoid micro-flushes
                return pos

        # 3. Sentence end mid-paragraph: "text. Next"
        for m in reversed(list(re.finditer(r'[.!?]\s', buf))):
            pos = m.end()
            if pos >= 40:  # Larger minimum for mid-paragraph splits
                return pos

        return -1

    # Widget that accumulates all streamed text for the current turn.
    # Updated in-place - never creates multiple widgets for the same response.
    _response_widget: Static | None = None
    _response_text: str = ""

    def _flush_streaming_blocks(self) -> None:
        """Update the single response widget with accumulated text."""
        if not self._streaming_buf.strip():
            return
        # Accumulate into the turn's response text
        self._response_text += self._streaming_buf
        self._streaming_buf = ""
        # Create or update the single response widget
        content = self._response_text.strip()
        if not content:
            return
        if self._response_widget is None:
            self._spacer()
            self._msg_count += 1
            self._response_widget = Static(
                Markdown(content, code_theme="monokai"),
                id=f"msg-{self._msg_count}",
                classes="rail-response",
            )
            self.mount(self._response_widget)
        else:
            self._response_widget.update(Markdown(content, code_theme="monokai"))
        self.scroll_end(animate=False)

    def end_streaming(self) -> None:
        if not self._streaming_active:
            if self._stream_text_acc:
                orphaned = "".join(self._stream_text_acc)
                self._stream_text_acc.clear()
                if orphaned.strip():
                    self._response_text += orphaned
                    self._flush_final_response()
            return
        self._streaming_active = False
        if self._stream_flush_timer is not None:
            try:
                self._stream_flush_timer.stop()
            except Exception:
                pass
            self._stream_flush_timer = None
        # Drain remaining accumulated text
        if self._stream_text_acc:
            self._streaming_buf += "".join(self._stream_text_acc)
            self._stream_text_acc.clear()
        # Final flush
        self._response_text += self._streaming_buf
        self._streaming_buf = ""
        self._streaming_flushed = ""
        self._flush_final_response()

    def _flush_final_response(self) -> None:
        """Final render of the response - single Markdown widget."""
        content = self._response_text.strip()
        if not content:
            self._last_streamed_text = ""
            self._response_widget = None
            self._response_text = ""
            return
        if self._response_widget is None:
            self._spacer()
            self._append(
                Markdown(content, code_theme="monokai"),
                css_class="rail-response",
            )
        else:
            self._response_widget.update(Markdown(content, code_theme="monokai"))
        self._last_streamed_text = content
        self._last_streamed_widget = self._response_widget
        # Don't reset _response_widget yet - next stream_done in same turn
        # will continue appending to the same widget

    @staticmethod
    def _is_pure_narration(text: str) -> bool:
        """Detect ONLY tool call narration that duplicates what the TUI already shows.

        A narration is strictly: a single short line like "Reading /path/to/file"
        or "Listing /dir → Reading /file" - just a verb + path, nothing else.

        Real agent responses (explanations, answers, summaries) must NEVER be
        filtered, even if short or starting with a verb-like word.
        """
        if not text or not text.strip():
            return False
        stripped = text.strip()

        # Multi-line or long text is NEVER narration - it's a real response
        lines = stripped.split("\n")
        non_empty = [l for l in lines if l.strip()]
        if len(non_empty) > 1 or len(stripped) > 120:
            return False

        line = non_empty[0].strip() if non_empty else ""
        if not line:
            return False

        # If it contains sentence-like markers, it's a real response
        # (commas, pronouns, conjunctions, question marks, etc.)
        _SENTENCE_MARKERS = (",", "?", "!", " je ", " j'", " i ", " i'", " the ", " le ", " la ",
                             " un ", " une ", " de ", " du ", " des ", " et ", " ou ", " and ",
                             " or ", " but ", " that ", " this ", " voici ", " voilà ")
        low = line.lower()
        if any(m in low for m in _SENTENCE_MARKERS):
            return False

        # Strict narration: verb + path-like argument ONLY
        _NARRATION_VERBS = (
            "listing ", "reading ", "searching ", "running ", "writing ",
            "finding ", "grepping ", "executing ", "fetching ", "loading ",
        )
        for verb in _NARRATION_VERBS:
            if low.startswith(verb):
                rest = low[len(verb):].strip()
                # Must be ONLY a path - no additional words
                if rest.startswith(("/", "./", "~")) and " " not in rest.rstrip(".:…"):
                    return True

        # "verb → verb" patterns (parallel tool narration)
        if "\u2192" in line or " → " in line:
            parts = line.split("\u2192") if "\u2192" in line else line.split("→")
            if all(
                any(p.strip().lower().startswith(v) for v in _NARRATION_VERBS)
                for p in parts if p.strip()
            ):
                return True

        return False

    def _end_streaming_if_active(self) -> None:
        # Always call end_streaming - it handles both active streaming
        # and orphaned text in the accumulator (race condition recovery).
        self.end_streaming()

    # ── Thinking (streamed progressively) ───────────────────────

    _THINK_STYLE = "italic #8a94a8"

    def start_thinking_stream(self) -> None:
        """Start streaming thinking content.

        Does NOT clear _thinking_text_acc - agent thread may have already
        written deltas before this runs (call_from_thread ordering).
        """
        self._end_streaming_if_active()
        try:
            self._collapse_previous_thinking()
        except Exception:
            pass  # Don't let collapsing block new thinking
        self._thinking_buf = ""
        self._thinking_rendered_len = 0
        self._thinking_active = True
        self._thinking_widget = None
        self._spacer()
        if self._thinking_timer is None:
            self._thinking_timer = self.set_interval(0.1, self._flush_thinking)

    def _flush_thinking(self) -> None:
        """Timer callback: drain accumulator and render any new thinking content."""
        # Drain thread-safe accumulator into buffer.
        # MUST use [:] + .clear() to keep the same list object - _make_poster
        # holds a direct reference to this list from the agent thread.
        if self._thinking_text_acc:
            pending = self._thinking_text_acc[:]
            self._thinking_text_acc.clear()
            self._thinking_buf += "".join(pending)

        if not self._thinking_active:
            if self._thinking_timer is not None:
                try:
                    self._thinking_timer.stop()
                except Exception:
                    pass
                self._thinking_timer = None
            return
        if not self._thinking_buf.strip():
            return
        # Only re-render if new content since last render
        if len(self._thinking_buf) <= self._thinking_rendered_len:
            return
        self._thinking_rendered_len = len(self._thinking_buf)
        display = self._render_thinking_text(self._thinking_buf)
        if self._thinking_widget is None:
            self._msg_count += 1
            self._thinking_widget = Static(
                display, id=f"msg-{self._msg_count}", classes="thinking",
            )
            try:
                spinner = self.query_one("#spinner-bar")
                self.mount(self._thinking_widget, before=spinner)
            except Exception:
                self.mount(self._thinking_widget)
        else:
            self._thinking_widget.update(display)
        self.scroll_end(animate=False)

    def end_thinking_stream(self) -> None:
        """Finalize thinking block - stop timer, render as Markdown, keep expanded."""
        if not self._thinking_active:
            return
        self._thinking_active = False
        # Stop the render timer
        if self._thinking_timer is not None:
            try:
                self._thinking_timer.stop()
            except Exception:
                pass
            self._thinking_timer = None
        # Drain any remaining accumulated text (keep same list reference)
        if self._thinking_text_acc:
            self._thinking_buf += "".join(self._thinking_text_acc)
            self._thinking_text_acc.clear()
        # Final content
        content = self._thinking_buf.strip()
        self._thinking_buf = ""
        self._thinking_rendered_len = 0

        if not content:
            if self._thinking_widget is not None:
                try:
                    self._thinking_widget.remove()
                except Exception:
                    pass  # Widget may already be removed
            self._thinking_widget = None
            return

        # Re-render as Markdown for proper formatting
        display = self._render_thinking_text(content, as_markdown=True)
        if self._thinking_widget is None:
            self._msg_count += 1
            self._thinking_widget = Static(
                display, id=f"msg-{self._msg_count}", classes="thinking",
            )
            try:
                spinner = self.query_one("#spinner-bar")
                self.mount(self._thinking_widget, before=spinner)
            except Exception:
                self.mount(self._thinking_widget)
        else:
            self._thinking_widget.update(display)
        self.scroll_end(animate=False)

        # Store content for later collapsing when next message arrives
        self._last_thinking_content = content
        self._last_thinking_widget = self._thinking_widget
        self._thinking_widget = None

    def _collapse_previous_thinking(self) -> None:
        """Collapse a previous thinking block into an expandable summary."""
        widget = self._last_thinking_widget
        content = self._last_thinking_content
        if widget is None or not content:
            return
        self._last_thinking_widget = None
        self._last_thinking_content = None

        lines = content.split("\n")
        if len(lines) <= 4:
            return  # Short thinking - keep as-is

        from .sidebar import ExpandableItem
        summary_line = ""
        for line in lines:
            line_s = line.strip()
            if line_s and len(line_s) > 10:
                summary_line = line_s
                break
        if not summary_line:
            summary_line = lines[0].strip()
        if len(summary_line) > 80:
            summary_line = summary_line[:79] + "\u2026"

        short = self._render_thinking_text(content, as_markdown=True, collapsed=True)

        full = self._render_thinking_text(content, as_markdown=True, collapsed=False)

        self._msg_count += 1
        expandable = ExpandableItem(short, full, id=f"msg-{self._msg_count}", classes="thinking")
        try:
            self.mount(expandable, after=widget)
            widget.remove()
        except Exception:
            pass  # Non-critical: widget mount/remove is best-effort during reflow

    def _render_thinking_text(self, text: str, label: bool = True,
                              as_markdown: bool = False,
                              collapsed: bool = False) -> Text | Markdown:
        """Render thinking text with ┊ rail. Collapses after _THINK_MAX lines."""
        clean = text.strip()
        lines = clean.split("\n")

        # For markdown path (end of thinking), still use rail
        if as_markdown:
            t = Text()
            if label:
                t.append(f"{_RAIL_THINK} ", style="#64748b")
                t.append("Thinking...\n", style="#64748b bold italic")
            visible = lines[:_THINK_MAX] if collapsed else lines
            for i, line in enumerate(visible):
                t.append(f"{_RAIL_THINK} ", style="#64748b")
                t.append(line, style="#94a3b8 italic")
                if i < len(visible) - 1:
                    t.append("\n")
            hidden = len(lines) - len(visible)
            if hidden > 0:
                t.append(f"\n{_RAIL_THINK} ", style="#64748b")
                t.append(f"\u25bc {hidden} more lines", style="#64748b dim")
            return t

        # Plain Text for fast streaming updates
        t = Text()
        if label:
            t.append(f"{_RAIL_THINK} ", style="#64748b")
            t.append("Thinking...\n", style="#64748b bold italic")
        for i, line in enumerate(lines):
            t.append(f"{_RAIL_THINK} ", style="#64748b")
            t.append(line, style=self._THINK_STYLE)
            if i < len(lines) - 1:
                t.append("\n")
        return t

    def add_thinking(self, text: str) -> None:
        """Display thinking as a complete block (non-streamed path)."""
        if self._thinking_active:
            return  # Already handled by streaming
        if not text or not text.strip():
            return
        self._collapse_previous_thinking()
        self._spacer()
        display = self._render_thinking_text(text, as_markdown=True)
        self._msg_count += 1
        widget = Static(display, id=f"msg-{self._msg_count}", classes="thinking")
        try:
            spinner = self.query_one("#spinner-bar")
            self.mount(widget, before=spinner)
        except Exception:
            self.mount(widget)
        self.scroll_end(animate=False)
        self._maybe_prune()
        # Keep expanded until next message arrives
        self._last_thinking_content = text.strip()
        self._last_thinking_widget = widget

    # ── Agent response ─────────────────────────────────────────────

    def add_response(self, content: str) -> None:
        self._end_streaming_if_active()
        # Don't collapse thinking here - it belongs to the current turn.
        # Collapse happens at the start of the NEXT turn (in add_user_message).
        if not content or not content.strip():
            return
        self._spacer()
        # Markdown with │ rail via CSS border-left
        self._append(
            Markdown(content.strip(), code_theme="monokai"),
            css_class="rail-response",
        )

    # ── Hook events ────────────────────────────────────────────────

    _compaction_widget: Static | None = None

    def add_hook(self, action_type: str, phase: str, details: dict | None = None) -> None:
        """Display hook events. Compaction uses an animated inline widget."""
        if action_type == "compact_context":
            if phase == "start":
                self._spacer()
                t = Text()
                t.append(f"{_HOOK} ", style="bold #a78bfa")
                t.append("Compacting context\u2026", style="#a78bfa")
                self._append(t, indent=True)
            elif phase == "end":
                strategy = details.get("strategy", "") if details else ""
                reduced = details.get("tokens_reduced", 0) if details else 0
                t = Text()
                t.append(f"{_CHECK} ", style="#22c55e")
                t.append("Context compacted", style="#22c55e")
                if reduced and reduced > 0:
                    rk = f"{reduced // 1000}k" if reduced >= 1000 else str(reduced)
                    if strategy == "truncate":
                        t.append(f" ({rk} tokens dropped)", style="#ef4444")
                    elif strategy == "summarize":
                        t.append(f" ({rk} tokens summarized)", style="#475569")
                    else:
                        t.append(f" (-{rk} tokens)", style="#475569")
                self._append(t, indent=True)
            elif phase == "error":
                t = Text()
                t.append(f"{_CROSS} ", style="#ef4444")
                t.append("Compaction failed", style="#ef4444")
                self._append(t, indent=True)

        elif action_type == "context_status":
            # Silently handled by the app (updates footer pressure)
            pass

        elif action_type == "inject_message":
            if phase == "start":
                t = Text()
                t.append(f"{_HOOK} ", style="#f59e0b dim")
                t.append("Injecting context", style="#f59e0b dim")
                self._append(t, indent=True)

    # ── Approval display ──────────────────────────────────────────

    _RISK_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#22c55e"}
    _RISK_ICONS = {"high": "\u26a0", "medium": "\u25c6", "low": _DOT}

    def add_approval_request(self, tool_name: str, params: dict, risk_level: str) -> None:
        """Display an approval request inline.

        Special handling for 'ask_user' tool: renders the question prominently
        and the content as markdown (for plans, code reviews, etc.).
        """
        self._end_streaming_if_active()
        self._spacer()
        color = self._RISK_COLORS.get(risk_level, "#f59e0b")
        icon = self._RISK_ICONS.get(risk_level, "\u25c6")

        # ── ask_user: enhanced rendering with markdown content ──
        if tool_name == "ask_user":
            question = params.get("question", "")
            content = params.get("content")

            # Header - question as the main focus
            t = Text()
            t.append("\u2753 ", style="bold #3b82f6")
            t.append("Agent is asking for your input", style="bold #3b82f6")
            self._append(t, indent=True)

            # Question
            t = Text()
            t.append(f"{_SUB} ", style="#334155")
            t.append(question, style="bold #e2e8f0")
            self._append(t, indent=True)

            # Content - render as markdown if present
            if content:
                self._spacer()
                try:
                    from rich.markdown import Markdown
                    md = Markdown(content)
                    self._append(md, indent=True)
                except ImportError:
                    # Fallback: render as plain text with line breaks
                    for line in content.splitlines():
                        t = Text()
                        t.append(f"  {line}", style="#cbd5e1")
                        self._append(t, indent=True)
                self._spacer()

            # Prompt hint
            t = Text()
            t.append("  ", style="")
            t.append("y", style="bold #22c55e")
            t.append(" approve  ", style="#94a3b8")
            t.append("n", style="bold #ef4444")
            t.append(" deny  ", style="#94a3b8")
            t.append("or type feedback", style="#64748b")
            self._append(t, indent=True)
            self.scroll_end(animate=False)
            return

        # ── Standard approval request (non ask_user) ──
        # Header
        t = Text()
        t.append(f"{icon} ", style=f"bold {color}")
        t.append("Approval required", style=f"bold {color}")
        t.append(f"  [{risk_level}]", style=color)
        self._append(t, indent=True)

        # Tool + detail
        action = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
        t = Text()
        t.append(f"{_SUB} ", style="#334155")
        t.append(action.replace("_", " ").capitalize(), style=f"bold {color}")
        detail = params.get("path") or params.get("command") or params.get("pattern") or ""
        if detail:
            t.append(f"  {str(detail)[:60]}", style="#e2e8f0")
        self._append(t, indent=True)

        # Key params (max 3, skip internal)
        _skip = {"_approved", "path", "command", "pattern", "name", "tool_name"}
        shown = 0
        for k, v in params.items():
            if k in _skip or shown >= 3:
                continue
            t = Text()
            t.append(f"  ", style="")
            t.append(f"{k}: ", style="#94a3b8")
            t.append(str(v)[:80], style="#e2e8f0")
            self._append(t, indent=True)
            shown += 1

        # Prompt hint
        t = Text()
        t.append(f"  ", style="")
        t.append("y", style="bold #22c55e")
        t.append(" approve  ", style="#94a3b8")
        t.append("n", style="bold #ef4444")
        t.append(" deny  ", style="#94a3b8")
        t.append("or type reason to deny", style="#64748b")
        self._append(t, indent=True)
        self.scroll_end(animate=False)

    def add_approval_result(self, approved: bool, message: str = "") -> None:
        t = Text()
        if approved:
            t.append(f"{_CHECK} ", style="bold #22c55e")
            t.append("Approved", style="#22c55e")
        else:
            t.append(f"{_CROSS} ", style="bold #ef4444")
            t.append("Denied", style="#ef4444")
            if message:
                t.append(f"  {message}", style="#94a3b8")
        self._append(t, indent=True)
        self.scroll_end(animate=False)

    # ── Undo display ────────────────────────────────────────────

    def add_undo_result(self, result: dict | None) -> None:
        t = Text()
        if result is None:
            t.append("  \u21a9 ", style="#475569")
            t.append("Nothing to undo", style="#475569")
        elif "error" in result:
            t.append("  \u21a9 ", style="#ef4444")
            t.append(f"Undo failed: {result['error']}", style="#ef4444")
        else:
            from pathlib import Path as _P
            name = _P(result.get("path", "")).name or "file"
            remaining = result.get("remaining", 0)
            t.append("  \u21a9 ", style="bold #a78bfa")
            t.append("Restored ", style="#a78bfa")
            t.append(name, style="bold #e2e8f0")
            if remaining:
                t.append(f"  ({remaining} more)", style="#475569")
        self._append(t)
        self.scroll_end(animate=False)

    # ── Parallel group display ───────────────────────────────────

    def add_parallel_group(self, results: list[dict], elapsed_ms: float) -> None:
        """Display run_parallel results as a grouped tree with briefs."""
        self._end_streaming_if_active()
        self._spacer()
        total = len(results)
        succeeded = sum(1 for r in results if r.get("success"))
        failed = total - succeeded

        # Header
        t = Text()
        if failed == 0:
            t.append(f"{_DOT} ", style="bold #22c55e")
            t.append(f"{total} parallel actions completed", style="bold #22c55e")
        elif succeeded == 0:
            t.append(f"{_CROSS} ", style="bold #ef4444")
            t.append(f"{total} parallel actions failed", style="bold #ef4444")
        else:
            t.append(f"{_DOT} ", style="bold #f59e0b")
            t.append(f"{total} parallel actions", style="bold #f59e0b")
            t.append(f" ({succeeded} done, {failed} failed)", style="#475569")
        timing = _format_timing(elapsed_ms / 1000)
        if timing:
            t.append(f"  {timing}", style="#475569")
        self._append(t, indent=True)

        # Sub-actions with tree connectors and briefs
        for i, r in enumerate(results):
            is_last = (i == total - 1)
            connector = "\u2514\u2500" if is_last else "\u251c\u2500"

            line = Text()
            line.append(f"  {connector} ", style="#334155")

            ok = r.get("success", False)
            icon = _CHECK if ok else _CROSS
            color = "#22c55e" if ok else "#ef4444"
            line.append(f"{icon} ", style=f"bold {color}")

            # Label - extract action name and make it readable
            label = r.get("label", "")
            if not label:
                name = r.get("name", "?")
                # "filesystem.grep" → "Grep", "web.search" → "Search"
                label = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1].replace("_", " ").capitalize()
            line.append(label, style=f"bold {color}")

            # Detail - file path, query, pattern, etc.
            detail = r.get("detail", "")
            if not detail:
                # Try to extract from params or result
                data = r.get("data", r.get("result", {}))
                if isinstance(data, dict):
                    detail = (data.get("path") or data.get("query") or
                              data.get("pattern") or data.get("url") or "")
            if detail:
                avail = max(self.size.width - 20 - len(label), 20)
                short = detail if len(detail) <= avail else detail[:avail - 3] + "\u2026"
                line.append(f"({short})", style="#94a3b8")

            # Brief result - match count, line count, etc.
            brief = r.get("brief", "")
            if not brief and ok:
                data = r.get("data", r.get("result", {}))
                if isinstance(data, dict):
                    brief = _extract_brief_from_data(label.lower(), data)
            if brief:
                line.append(f"  {brief}", style="#64748b")

            # Error
            err = r.get("error")
            if err and not ok:
                line.append(f"  {str(err)[:50]}", style="#ef4444")

            self._append(line, indent=True)

        self.scroll_end(animate=False)

    # ── Agent group display ─────────────────────────────────────

    _AGENT_ICONS = {
        "spawned": "\u25cc",     # ◌
        "running": "\u25cf",     # ●
        "completed": "\u2713",   # ✓
        "failed": "\u2717",      # ✗
        "cancelled": "\u25cb",   # ○
    }
    _AGENT_COLORS = {
        "spawned": "#f59e0b",
        "running": "#06b6d4",
        "completed": "#22c55e",
        "failed": "#ef4444",
        "cancelled": "#475569",
    }

    def add_agent_event(self, agent_id: str, status: str,
                        specialist: str = "", task: str = "",
                        duration: float = 0, preview: str = "",
                        turns_used: int = 0, token_count: int = 0) -> None:
        """Update the agent group display."""
        self._agents[agent_id] = {
            "specialist": specialist, "task": task, "status": status,
            "duration": duration, "preview": preview,
            "turns_used": turns_used, "token_count": token_count,
        }
        self._render_agent_group()

    def clear_agent_group(self) -> None:
        """Clear the agent group after all are done."""
        self._agents.clear()
        if self._agent_group_widget is not None:
            self._agent_group_widget = None

    def _render_agent_group(self) -> None:
        """Render/update the grouped agent tree with individual statuses."""
        if not self._agents:
            return

        agents = list(self._agents.values())
        running = [a for a in agents if a["status"] in ("spawned", "running")]
        completed = [a for a in agents if a["status"] == "completed"]
        failed = [a for a in agents if a["status"] == "failed"]
        total = len(agents)

        specialists = set(a.get("specialist") or "" for a in agents)
        _first_spec = next(iter(specialists), "")
        spec_label = _first_spec.capitalize() if len(specialists) == 1 and _first_spec else "Sub"

        # Header - shows status summary
        t = Text()
        if running:
            t.append(f"{_DOT} ", style="bold #a78bfa")
            t.append(f"Running {len(running)} {spec_label} agent{'s' if total > 1 else ''}\u2026", style="bold #a78bfa")
            if completed or failed:
                parts = []
                if completed:
                    parts.append(f"{len(completed)} done")
                if failed:
                    parts.append(f"{len(failed)} failed")
                t.append(f" ({', '.join(parts)})", style="#475569")
        else:
            # All done
            if failed and not completed:
                t.append(f"{_CROSS} ", style="bold #ef4444")
                t.append(f"{total} {spec_label} agent{'s' if total > 1 else ''} failed", style="bold #ef4444")
            elif failed:
                t.append(f"{_DOT} ", style="bold #f59e0b")
                t.append(f"{total} {spec_label} agent{'s' if total > 1 else ''}", style="bold #f59e0b")
                t.append(f" ({len(completed)} done, {len(failed)} failed)", style="#475569")
            else:
                t.append(f"{_DOT} ", style="bold #22c55e")
                t.append(f"{total} {spec_label} agent{'s' if total > 1 else ''} completed", style="bold #22c55e")
        t.append("\n")

        # Individual agent lines with tree connectors
        items = list(self._agents.items())
        for i, (aid, agent) in enumerate(items):
            is_last = (i == len(items) - 1)

            icon = self._AGENT_ICONS.get(agent["status"], "\u25cf")
            color = self._AGENT_COLORS.get(agent["status"], "#94a3b8")

            if i == 0:
                t.append(f"{_SUB} ", style="#334155")
            else:
                t.append(f"  ", style="")
            t.append(f"{icon} ", style=f"bold {color}")

            label = agent.get("specialist") or aid[:8]
            t.append(label.capitalize(), style=f"bold {color}")

            # Use available width minus prefix (connector + icon + label + padding)
            avail = max(self.size.width - 20 - len(label), 30)
            task_full = agent.get("task") or ""
            task_short = task_full[:avail] + "\u2026" if len(task_full) > avail else task_full
            if task_short:
                t.append(f": {task_short}", style="#94a3b8")

            # Stats for completed/failed
            stats = []
            if agent.get("turns_used"):
                stats.append(f"{agent['turns_used']} tools")
            if agent.get("duration") and agent["status"] in ("completed", "failed"):
                stats.append(_format_timing(agent["duration"]))
            if stats:
                sep = " \u00b7 "
                t.append(f" \u00b7 {sep.join(stats)}", style="#475569")
            t.append("\n")

        # Mount or update widget
        if self._agent_group_widget is None:
            self._msg_count += 1
            self._agent_group_widget = Static(t, id=f"msg-{self._msg_count}", classes="indented")
            try:
                spinner = self.query_one("#spinner-bar")
                self.mount(self._agent_group_widget, before=spinner)
            except Exception:
                self.mount(self._agent_group_widget)
        else:
            self._agent_group_widget.update(t)
        self.scroll_end(animate=False)

    # ── Separator & Error ──────────────────────────────────────────

    def add_separator(self) -> None:
        # Update tool group header with final count
        if self._turn_group_widget is not None and self._turn_tool_count > 1:
            t = Text()
            t.append(f"{_DOT} ", style="bold #5769f7")
            t.append(f"{self._turn_tool_count} tool calls", style="bold #5769f7")
            try:
                self._turn_group_widget.update(t)
            except Exception:
                pass
        self._turn_tool_count = 0
        self._turn_group_widget = None
        self._append(Text(""))

    def toggle_bookmark(self) -> None:
        """Bookmark/unbookmark the last message."""
        if self._msg_count == 0:
            return
        widget_id = f"msg-{self._msg_count}"
        try:
            widget = self.query_one(f"#{widget_id}", Static)
        except Exception:
            return
        if widget_id in self._bookmarks:
            self._bookmarks.remove(widget_id)
            widget.styles.border_left = None
        else:
            self._bookmarks.append(widget_id)
            widget.styles.border_left = ("solid", "#f59e0b")

    def jump_to_bookmark(self, direction: int = 1) -> None:
        """Jump to next (1) or previous (-1) bookmark."""
        if not self._bookmarks:
            return
        # Find bookmarked widgets and scroll to one
        for bid in (self._bookmarks if direction > 0 else reversed(self._bookmarks)):
            try:
                widget = self.query_one(f"#{bid}", Static)
                widget.scroll_visible()
                return
            except Exception:
                continue

    def add_error(self, error: str) -> None:
        self._spacer()
        t = Text()
        t.append(f"{_CROSS} Error: ", style="bold #ef4444")
        t.append(error, style="#ef4444")
        self._append(t, indent=True)
