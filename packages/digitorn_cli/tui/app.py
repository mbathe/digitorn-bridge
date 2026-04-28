"""Digitorn TUI - Terminal User Interface powered by Textual."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static, Input, TextArea
from textual import on, work
from textual.message import Message
from rich.text import Text


class PromptInput(TextArea):
    """Multiline input: Enter=submit, Shift+Enter=newline.

    Overrides TextArea's ctrl+c (copy) to let it bubble to app (quit).
    Set `menu_open = True` to let Enter pass through to the app.
    """

    # Override TextArea bindings - remove ctrl+c=copy so it bubbles to app=quit
    BINDINGS = [
        b for b in TextArea.BINDINGS
        if "ctrl+c" not in b.key
    ]

    menu_open: bool = False

    class Submitted(Message):
        """Fired when user presses Enter (without Shift)."""
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            if self.menu_open:
                event.prevent_default()
                return
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            self.clear()
            self.post_message(self.Submitted(text))
            return
        # ctrl+c → stop SSE thread + quit via action
        if event.key == "ctrl+c":
            stop = getattr(self.app, "_backend", None)
            if stop:
                ev_stop = getattr(stop, "_event_stop", None)
                if ev_stop:
                    ev_stop.set()
            self.app.action_quit()
            return
        await super()._on_key(event)

from digitorn_cli.tui.messages import (
    TokenReceived, StreamDone, OutTokenCount, InTokenCount,
    ToolStarted, ToolCompleted,
    ThinkingStarted, ThinkingDelta, ThinkingReceived,
    HookFired, TurnComplete, BackendReady, BackendError,
    AgentEvent, MemoryUpdate, ApprovalRequested,
    StatusUpdate, TerminalOutput,
    Notification, NotificationResult,
    HistoryLoaded,
)
from digitorn_cli.tui.widgets.chat_log import ChatLog
from digitorn_cli.tui.widgets.spinner_bar import SpinnerBar
from digitorn_cli.tui.widgets.status_footer import StatusFooter
from digitorn_cli.tui.widgets.sidebar import Sidebar


# ── Slash command autocomplete menu ──────────────────────────

SLASH_COMMANDS = [
    ("/help", "Show keyboard shortcuts and slash commands"),
    ("/status", "Session status (tokens, turns, mode)"),
    ("/tools", "List available tools"),
    ("/compact", "Compact context to free tokens"),
    ("/cost", "Show token usage and estimated cost"),
    ("/diff", "Show git diff of workspace"),
    ("/commit", "Commit changes with AI-generated message"),
    ("/model", "Show or change the current model"),
    ("/context", "Show context window breakdown"),
    ("/sessions", "List sessions"),
    ("/resume", "Resume a previous session"),
    ("/history", "Show message history"),
    ("/fork", "Fork current session"),
    ("/mcp", "List MCP servers for this app"),
    ("/tasks", "Show background tasks"),
    ("/watchers", "Show active watchers"),
    ("/doctor", "System diagnostics check"),
    ("/clear", "Clear chat history"),
    ("/quit", "Exit"),
]


class SlashMenuItem(Static):
    """A single item in the slash menu."""

    DEFAULT_CSS = """
    SlashMenuItem {
        height: 1;
        padding: 0 1;
        background: #1e293b;
    }
    SlashMenuItem.highlighted {
        background: #334155;
    }
    """

    def __init__(self, cmd: str, desc: str, index: int, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.cmd = cmd
        self.desc = desc
        self.index = index
        self._render_item(highlighted=False)

    def _render_item(self, highlighted: bool = False) -> None:
        t = Text()
        style_cmd = "bold #f1f5f9" if highlighted else "bold #e2e8f0"
        style_desc = "#cbd5e1" if highlighted else "#94a3b8"
        indicator = "\u25b6 " if highlighted else "  "
        t.append(indicator, style="#3b82f6" if highlighted else "#1e293b")
        t.append(f"{self.cmd:<14}", style=style_cmd)
        t.append(f" {self.desc}", style=style_desc)
        self.update(t)

    def set_highlighted(self, value: bool) -> None:
        if value:
            self.add_class("highlighted")
        else:
            self.remove_class("highlighted")
        self._render_item(highlighted=value)


class SlashMenu(Static):
    """Autocomplete menu for slash commands. Arrow keys to navigate, Enter to select."""

    DEFAULT_CSS = """
    SlashMenu {
        display: none;
        dock: bottom;
        width: 54;
        height: auto;
        max-height: 14;
        background: #1e293b;
        border: tall #334155;
        padding: 0;
        margin-bottom: 3;
        margin-left: 3;
    }
    SlashMenu.visible {
        display: block;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(" ", **kwargs)
        self._filtered: list[tuple[str, str]] = []
        self._selected: int = 0
        self._items: list[SlashMenuItem] = []
        self._visible = False

    @property
    def is_open(self) -> bool:
        return self._visible

    def filter(self, text: str) -> None:
        """Filter commands and rebuild menu items."""
        query = text.lower()
        if query == "/":
            self._filtered = SLASH_COMMANDS[:]
        else:
            self._filtered = [
                (cmd, desc) for cmd, desc in SLASH_COMMANDS
                if cmd.startswith(query) or any(
                    w.startswith(query.lstrip("/"))
                    for w in desc.lower().split()
                )
            ]

        if not self._filtered:
            self.hide()
            return

        self._selected = 0
        self._rebuild_items()
        self._visible = True
        self.add_class("visible")

    def _rebuild_items(self) -> None:
        """Rebuild menu item widgets."""
        # Remove old items
        for item in self._items:
            try:
                item.remove()
            except Exception:
                pass
        self._items.clear()

        for i, (cmd, desc) in enumerate(self._filtered[:10]):
            item = SlashMenuItem(cmd, desc, i)
            self.mount(item)
            self._items.append(item)

        # Highlight first
        if self._items:
            self._items[0].set_highlighted(True)

        # Hide the parent Static text since we use children now
        self.update("")

    def move_up(self) -> None:
        """Move selection up."""
        if not self._items:
            return
        self._items[self._selected].set_highlighted(False)
        self._selected = (self._selected - 1) % len(self._items)
        self._items[self._selected].set_highlighted(True)

    def move_down(self) -> None:
        """Move selection down."""
        if not self._items:
            return
        self._items[self._selected].set_highlighted(False)
        self._selected = (self._selected + 1) % len(self._items)
        self._items[self._selected].set_highlighted(True)

    def get_selected(self) -> str | None:
        """Return the currently selected command."""
        if 0 <= self._selected < len(self._filtered):
            return self._filtered[self._selected][0]
        return None

    def hide(self) -> None:
        for item in self._items:
            try:
                item.remove()
            except Exception:
                pass
        self._items.clear()
        self._filtered = []
        self._selected = 0
        self._visible = False
        self.update(" ")
        self.remove_class("visible")


CSS_PATH = Path(__file__).parent / "theme.tcss"


class DigitornTUI(App):
    """Main TUI application."""

    CSS_PATH = str(CSS_PATH)
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "abort_or_focus", "Stop/Focus"),
        ("ctrl+b", "toggle_sidebar", "Toggle sidebar"),
        ("ctrl+z", "undo", "Undo last edit"),
        ("ctrl+l", "clear_chat", "Clear chat"),
        ("ctrl+f", "search", "Search chat"),
        ("ctrl+d", "bookmark", "Bookmark"),
        ("ctrl+p", "toggle_preview", "Code preview"),
        ("ctrl+t", "new_tab", "New session"),
        ("ctrl+w", "close_tab", "Close tab"),
        ("f2", "next_bookmark", "Next bookmark"),
        ("f3", "next_tab", "Next tab"),
        ("f4", "prev_tab", "Prev tab"),
        ("f1", "show_help", "Help"),
    ]

    def _handle_exception(self, error: Exception) -> None:
        """Catch non-fatal Textual errors (widget removal, selection, timer)
        instead of crashing the entire TUI."""
        import logging as _log
        _non_fatal = (
            "NoWidget", "DOMError", "QueryError", "NoMatches",
            "widget has been removed", "no longer in the DOM",
            "timer already stopped", "not mounted",
        )
        err_str = f"{type(error).__name__}: {error}"
        if any(kw in err_str for kw in _non_fatal):
            _log.getLogger(__name__).debug("non-fatal TUI error: %s", err_str)
            return
        # Fatal errors - let Textual handle normally
        super()._handle_exception(error)

    def __init__(
        self,
        backend: Any,
        *,
        initial_message: str | None = None,
        exit_on_complete: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._backend = backend
        self._initial_message = initial_message
        self._exit_on_complete = exit_on_complete
        self._busy = False
        self._generation = 0  # Monotonic counter - prevents stale cleanup
        self._turn_count = 0
        self._streamed_this_turn = False
        self._pending_approval: ApprovalRequested | None = None
        # Thread-safe token accumulator - real counts from provider only.
        # [out_tokens, in_tokens] - written by agent thread, read by spinner at 8fps.
        self._token_acc = [0, 0]
        # Session totals - updated from daemon's authoritative data on each turn
        self._total_in_tokens = 0
        self._total_out_tokens = 0
        self._session_cost_usd = 0.0
        # Input history - Up/Down to navigate previous messages
        self._input_history: list[str] = []
        self._history_index = -1  # -1 = not browsing, 0..N = index from end
        self._history_draft = ""  # Saved draft when browsing history

    def compose(self) -> ComposeResult:
        from digitorn_cli.tui.views.search import SearchBar
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        from digitorn_cli.tui.widgets.code_preview import CodePreview
        from digitorn_cli.tui.widgets.info_panel import InfoPanel
        yield Static("\u25cf digitorn", id="header")
        yield TabBar(id="tab-bar", classes="hidden")
        yield SearchBar(id="search-bar")
        with Horizontal(id="main-content"):
            yield ChatLog(id="chat-log")
            yield CodePreview(id="code-preview")
            yield Sidebar(id="sidebar")
        yield InfoPanel(id="info-panel")
        yield SpinnerBar(id="spinner-bar")
        yield SlashMenu(id="slash-menu")
        with Horizontal(id="input-area"):
            yield Static(" \u276f ", id="prompt-icon")
            yield PromptInput("", id="prompt-input", language=None)
        yield StatusFooter(id="status-footer")

    async def on_mount(self) -> None:
        self.query_one("#prompt-input", PromptInput).focus()
        self.query_one("#status-footer", StatusFooter).refresh_bar()
        self._spinner._token_source = self._token_acc
        self._spinner.start(mode="requesting", label="Connecting")
        self._init_backend()
        # Spinner safety net - ensure spinner visible when agent is busy
        self.set_interval(0.5, self._ensure_spinner)

    @property
    def _spinner(self) -> SpinnerBar:
        return self.query_one("#spinner-bar", SpinnerBar)

    @work(thread=True)
    def _init_backend(self) -> None:
        post = self._make_poster()
        try:
            self._backend.initialize(post)
        except Exception as exc:
            self._spinner.stop()
            self.post_message(BackendError(str(exc)))

    def _make_poster(self):
        app = self
        acc = self._token_acc
        # Direct references to chat_log's thread-safe accumulators.
        # Bypasses call_from_thread batching so text/thinking appear
        # progressively via the chat_log's 10fps timers.
        chat_text_acc: list[str] | None = None
        think_text_acc: list[str] | None = None
        def _get_chat_accs() -> None:
            nonlocal chat_text_acc, think_text_acc
            if chat_text_acc is None:
                try:
                    chat = app.query_one("#chat-log", ChatLog)
                    chat_text_acc = chat._stream_text_acc
                    think_text_acc = chat._thinking_text_acc
                except Exception:
                    pass  # Widget not mounted yet - will retry on next call
        def _post(msg: Any) -> None:
            # Real token counts from provider → write to accumulator (spinner reads at 8fps)
            # out_tokens: cumulative per turn (each LLM call generates new tokens)
            # in_tokens: latest value (= current context size, replaces previous)
            if isinstance(msg, OutTokenCount):
                acc[0] += msg.count
                return
            elif isinstance(msg, InTokenCount):
                acc[1] = msg.count
                return
            # Fast path: write text/thinking directly to thread-safe accumulators.
            # BOTH still fall through to call_from_thread so handlers fire.
            if isinstance(msg, TokenReceived):
                _get_chat_accs()
                if chat_text_acc is not None:
                    chat_text_acc.append(msg.delta)
            elif isinstance(msg, ThinkingDelta):
                _get_chat_accs()
                if think_text_acc is not None:
                    think_text_acc.append(msg.delta)
            try:
                app.call_from_thread(app.post_message, msg)
            except RuntimeError:
                app.post_message(msg)
        return _post

    # ── Helpers ─────────────────────────────────────────────────────

    def _ensure_spinner(self) -> None:
        """Safety net: if agent is busy but spinner is off, restart it.

        Only when nothing visible is happening - not during text streaming
        or thinking streaming. Runs every 0.5s.
        """
        if self._busy and not self._spinner._active:
            chat = self.query_one("#chat-log", ChatLog)
            if not chat._streaming_active and not chat._thinking_active:
                self._spinner.start(mode="responding")

    @staticmethod
    def _extract_result_data(result: Any) -> dict | None:
        if isinstance(result, dict):
            return result.get("data", result) if "data" in result else result
        if hasattr(result, "data") and isinstance(result.data, dict):
            return result.data
        return None

    # ── Tool name resolution ──────────────────────────────────────

    @staticmethod
    def _resolve_tool(name: str, params: dict) -> tuple[str, dict]:
        """Unwrap meta-tools (discovery mode) and normalize names."""
        if name in ("execute_tool", "execute"):
            real_name = params.get("tool_name") or params.get("name") or name
            real_params = params.get("params") or params.get("arguments") or {}
            if isinstance(real_params, str):
                import json
                try:
                    real_params = json.loads(real_params)
                except Exception:
                    real_params = {}
            return DigitornTUI._normalize_name(real_name), real_params
        return DigitornTUI._normalize_name(name), params

    @staticmethod
    def _normalize_name(name: str) -> str:
        if "  " in name:
            parts = name.split("  ", 1)
            return f"{parts[0].lower()}.{parts[1]}"
        if "__" in name and "." not in name:
            parts = name.split("__", 1)
            return f"{parts[0]}.{parts[1]}"
        if "_" in name and "." not in name and name not in (
            "set_goal", "add_todo", "update_todo", "remember", "recall", "forget",
        ):
            parts = name.split("_", 1)
            return f"{parts[0]}.{parts[1]}"
        return name

    # ── Message Handlers ──────────────────────────────────────────

    def on_backend_ready(self, msg: BackendReady) -> None:
        self._spinner.stop()

        header = Text()
        header.append("\u25cf ", style="bold #60a5fa")
        header.append("digitorn", style="bold #60a5fa")
        header.append(f"  {msg.app_name}", style="bold white")
        header.append(f"  ({msg.agent_id} \u00b7 {msg.mode} \u00b7 {msg.total_tools} tools)", style="#475569")
        self.query_one("#header", Static).update(header)

        footer = self.query_one("#status-footer", StatusFooter)
        footer.mode = msg.mode
        footer.model = msg.model
        if msg.workspace:
            footer.set_workspace(msg.workspace)
        footer.refresh_bar()

        # Register first tab
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        tab_bar = self.query_one("#tab-bar", TabBar)
        sid = getattr(self._backend, '_session_id', 'main')
        tab_bar.add_tab(sid, msg.app_name)

        if msg.greeting:
            chat = self.query_one("#chat-log", ChatLog)
            for line in msg.greeting.strip().split("\n"):
                chat._append(Text(line, style="#94a3b8"))

        # Check for updates (non-blocking, best-effort)
        self._check_for_updates()

        # Restore session costs if resuming
        pass  # Session costs tracked by daemon

        # Auto-send initial message if provided (one-shot or conversation with message)
        if self._initial_message:
            text = self._initial_message
            self._initial_message = None  # consume it
            self._busy = True
            self._generation += 1
            self._streamed_this_turn = False
            self._token_acc[0] = 0
            self._token_acc[1] = 0
            self.query_one("#chat-log", ChatLog).add_user_message(text)
            self._spinner.start(mode="requesting", reset_tokens=True)
            self.query_one("#status-footer", StatusFooter).set_busy(True)
            self._run_agent_turn(text, self._generation)

    def on_history_loaded(self, msg: HistoryLoaded) -> None:
        """Restore EVERYTHING from session history.

        Rebuilds the chat UI by replaying events in chronological order.
        This is the full restore - messages, tools, thinking, memory,
        token counts, everything.
        """
        chat = self.query_one("#chat-log", ChatLog)
        sidebar = self.query_one("#sidebar", Sidebar)
        footer = self.query_one("#status-footer", StatusFooter)
        info = msg.session_info

        # Header
        chat._spacer()
        t = Text()
        t.append("  \u21bb ", style="bold #3b82f6")
        t.append("Restoring session", style="#3b82f6")
        if info.get("title"):
            t.append(f" - {info['title'][:40]}", style="dim")
        chat._append(t)

        # ── 1. Replay events in chronological order ─────────────────
        # Events are the ground truth - they contain everything that happened.
        # Messages are already in the events (turn_start has user msg, turn_end has content).
        # But we also use the messages array for user/assistant display.

        # First: display messages (user questions + assistant responses)
        for message in msg.messages:
            role = message.get("role", "")
            content = message.get("content", "")
            if not content:
                continue
            if role == "user":
                chat.add_user_message(content)
            elif role == "assistant":
                chat.add_response(content)

        # Second: replay events for tool calls, thinking, token counts
        for event in msg.events:
            etype = event.get("type", "")
            edata = event.get("data", {})

            if etype == "tool_call":
                name = edata.get("name", "")
                label = edata.get("label", name)
                detail = edata.get("detail", "")
                ok = edata.get("success", True)
                error = edata.get("error", "")
                if label and not self._is_silent_tool(name, name):
                    chat.add_tool_result(label, detail, ok, error, edata)
                footer.tool_calls += 1
                if ok:
                    footer.tool_success += 1
                else:
                    footer.tool_failed += 1

            elif etype == "thinking":
                text = edata.get("text", "")
                if text:
                    chat.add_thinking(text)

            elif etype == "token_count":
                footer.completion_tokens += edata.get("out_tokens", 0)
                footer.prompt_tokens = edata.get("in_tokens", footer.prompt_tokens)
                self._total_out_tokens += edata.get("out_tokens", 0)
                self._total_in_tokens = edata.get("in_tokens", self._total_in_tokens)

            elif etype == "turn_end":
                # Each turn_end has the final content + tool counts
                pass  # Already displayed via messages

        # ── 2. Restore memory (goal, todos, facts) in sidebar ───────
        if msg.memory:
            # Working memory has nested structure
            working = msg.memory.get("working", msg.memory)
            goal = working.get("goal", "")
            if goal:
                sidebar.update_memory("set_goal", {"goal": goal})

            todos = working.get("todos", [])
            if todos:
                sidebar.update_memory("add_todo", {"todos": todos})

            facts = working.get("key_facts", working.get("facts", []))
            if facts:
                sidebar.update_memory("remember", {"facts": facts})

            if goal or todos or facts:
                if sidebar.has_class("hidden"):
                    sidebar.remove_class("hidden")

        # ── 3. Update footer with session totals ────────────────────
        footer.turns = info.get("turn_count", 0)
        footer.cost_usd = self._session_cost_usd
        footer.refresh_bar()

        # ── 4. Update tab title ─────────────────────────────────────
        if info.get("title"):
            try:
                from digitorn_cli.tui.widgets.tab_bar import TabBar
                tab_bar = self.query_one("#tab-bar", TabBar)
                tab_bar.update_title(
                    getattr(self._backend, '_session_id', ''),
                    info["title"][:20],
                )
            except Exception:
                pass

        # ── 5. Final status ─────────────────────────────────────────
        chat.add_separator()
        t2 = Text()
        msg_count = info.get("message_count", len(msg.messages))
        evt_count = len(msg.events)
        t2.append(f"  \u2713 ", style="#22c55e")
        t2.append(f"{msg_count} messages, {evt_count} events, ", style="#22c55e dim")
        t2.append(f"{footer.tool_calls} tool calls restored", style="#22c55e dim")
        if info.get("interrupted"):
            t2.append("  \u26a0 interrupted - resuming...", style="bold #f59e0b")
        chat._append(t2)
        chat.scroll_end(animate=False)

    def on_backend_error(self, msg: BackendError) -> None:
        # Reconnection messages are warnings, not fatal errors
        if "reconnecting" in msg.error.lower():
            self._spinner.start(mode="waiting", label="Reconnecting")
            return
        self._spinner.stop()
        self.query_one("#chat-log", ChatLog).add_error(msg.error)
        self._busy = False

    def on_token_received(self, msg: TokenReceived) -> None:
        # Show "Generating" while text streams - spinner always visible
        if self._spinner._mode != "generating":
            self._spinner.start(mode="generating")
        # Text is already in chat_log's _stream_text_acc (written by _make_poster).
        # Just ensure streaming mode is active so the timer drains it.
        chat = self.query_one("#chat-log", ChatLog)
        if not chat._streaming_active:
            chat.start_streaming()
        self._streamed_this_turn = True

    def on_stream_done(self, msg: StreamDone) -> None:
        self.query_one("#chat-log", ChatLog).end_streaming()
        # Model may still be generating tool call tokens - restart spinner
        self._spinner.start(mode="responding")

    def on_status_update(self, msg: StatusUpdate) -> None:
        """Daemon status phase → drive spinner directly, no guessing."""
        phase = msg.phase
        details = msg.details
        if phase == "turn_start":
            self._spinner.start(mode="requesting", label="Processing")
        elif phase == "requesting":
            model = details.get("model", "")
            if model:
                self.query_one("#status-footer", StatusFooter).model = model
            self._spinner.start(mode="requesting")
        elif phase == "tool_use":
            self._spinner.start(mode="tool_use")
        elif phase == "turn_end":
            # Turn ended - spinner will be stopped by TurnComplete
            pass

    def on_terminal_output(self, msg: TerminalOutput) -> None:
        """Display shell command output in chat."""
        chat = self.query_one("#chat-log", ChatLog)
        if msg.stdout:
            chat._append_sub(f"stdout: {msg.stdout[:200]}", style="#94a3b8")
        if msg.stderr:
            chat._append_sub(f"stderr: {msg.stderr[:200]}", style="#f59e0b")

    def on_notification(self, msg: Notification) -> None:
        """Background task or watcher notification."""
        chat = self.query_one("#chat-log", ChatLog)
        chat._spacer()
        t = Text()
        icon = "\u23f0" if msg.source == "watcher" else "\u2709"  # ⏰ or ✉
        t.append(f"  {icon} ", style="bold #f59e0b")
        t.append(f"[{msg.source}] ", style="#f59e0b")
        t.append(msg.message[:200], style="#e2e8f0")
        chat._append(t)

    def on_notification_result(self, msg: NotificationResult) -> None:
        """Auto-triggered agent response from a notification."""
        chat = self.query_one("#chat-log", ChatLog)
        if msg.error:
            chat.add_error(msg.error)
        elif msg.content:
            chat.add_response(msg.content)

    def on_tool_started(self, msg: ToolStarted) -> None:
        from digitorn_cli.labels import tool_label
        name, params = self._resolve_tool(msg.name, msg.params)

        # Memory/agent tools → keep spinner, don't update label
        if self._is_silent_tool(msg.name, name):
            return

        # Parallel → just spinner, no dot in chat
        action = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        verb_check, _ = tool_label(name, params)
        if action in ("run_parallel", "parallele", "batch", "concurrent") or verb_check.lower() == "parallel":
            n = len(params.get("actions", []))
            self._spinner.start(mode="tool_use", label=f"Running {n} actions in parallel")
            return

        verb, detail = tool_label(name, params)
        if verb.lower() in ("tool", "executing", "unknown tool"):
            verb = action.replace("_", " ").capitalize()
            if not detail:
                detail = params.get("path") or params.get("command") or params.get("pattern") or ""
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_tool_start(verb, detail)
        label = f"{verb}({detail[:30]})" if detail else verb
        self._spinner.start(mode="tool_use", label=label)

    # Tools to hide from chat (visible in sidebar workspace instead)
    _SILENT_TOOLS = {
        # Memory/workspace (FQN action names - unified + internal)
        "set_goal", "remember", "recall", "forget",
        "add_todo", "update_todo", "task_create", "task_update",
        # Memory/workspace (short API names)
        "Remember", "TaskCreate", "TaskUpdate",
        # Agents (FQN action names - unified + internal)
        "agent", "spawn_agent", "agent_wait", "agent_wait_all", "agent_result",
        "agent_status", "agent_cancel", "agent_list", "reassign_agent",
        # Agents (short API names)
        "Agent", "AgentWaitAll",
        # Discovery meta-tools (internal plumbing)
        "search_tools", "get_tool", "list_categories", "browse_category",
        "SearchTools", "GetTool", "ListCategories", "BrowseCategory",
    }

    def _is_silent_tool(self, raw_name: str, resolved_name: str) -> bool:
        """Check if a tool should be hidden from chat."""
        # Raw meta-tools that are always internal plumbing
        if raw_name in self._SILENT_TOOLS:
            return True
        # Check resolved action part
        action = resolved_name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        if action in self._SILENT_TOOLS:
            return True
        # Memory or agent tool by keyword
        for kw in ("memory", "agent_spawn", "spawn_agent", "agent_wait", "agent_result", "agent_cancel"):
            if kw in raw_name or kw in resolved_name:
                return True
        # execute_tool that wraps a silent tool (resolved name is the inner tool)
        if raw_name in ("execute_tool", "execute"):
            inner_action = resolved_name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
            if inner_action in self._SILENT_TOOLS:
                return True
        return False

    def on_tool_completed(self, msg: ToolCompleted) -> None:
        from digitorn_cli.labels import tool_label, result_status
        name, params = self._resolve_tool(msg.name, msg.params)

        # Update tool call counters in footer
        footer = self.query_one("#status-footer", StatusFooter)
        footer.tool_calls += 1
        ok, _ = result_status(msg.result)
        if ok:
            footer.tool_success += 1
        else:
            footer.tool_failed += 1
        footer.refresh_bar()

        # Memory/agent tools → silent (visible in sidebar)
        if self._is_silent_tool(msg.name, name):
            return  # Spinner keeps running

        # Parallel group → tree display
        action = name.rsplit(".", 1)[-1].rsplit("__", 1)[-1]
        verb_check, _ = tool_label(name, params)
        if action in ("run_parallel", "parallele", "batch", "concurrent") or verb_check.lower() == "parallel":
            data = self._extract_result_data(msg.result)
            chat = self.query_one("#chat-log", ChatLog)
            if data and isinstance(data, dict) and "results" in data:
                chat.add_parallel_group(data["results"], data.get("elapsed_ms", 0))
            elif data and isinstance(data, dict):
                # Fallback: show as simple summary
                total = data.get("total", data.get("count", "?"))
                ok_count = data.get("succeeded", data.get("completed", "?"))
                t = Text()
                t.append("\u25cf ", style="bold #22c55e")
                t.append(f"Parallel: {ok_count}/{total} done", style="#22c55e")
                elapsed = data.get("elapsed_ms", 0)
                if elapsed:
                    t.append(f"  {elapsed:.0f}ms", style="#475569")
                chat._append(t, indent=True)
            self._spinner.start(mode="requesting")
            return

        verb, detail = tool_label(name, params)

        # Fix generic "Tool" fallback - use the action name instead
        if verb.lower() in ("tool", "executing", "unknown tool"):
            verb = action.replace("_", " ").capitalize()
            if not detail:
                detail = params.get("path") or params.get("command") or params.get("pattern") or ""

        ok, error = result_status(msg.result)
        self.query_one("#chat-log", ChatLog).add_tool_result(verb, detail, ok, error, msg.result)
        self.query_one("#chat-log", ChatLog).scroll_end(animate=False)
        # Restart spinner - next step is LLM call, so show "requesting"
        self._spinner.start(mode="requesting")

    def _start_thinking_if_needed(self) -> None:
        """Ensure thinking stream + spinner are active."""
        chat = self.query_one("#chat-log", ChatLog)
        if not chat._thinking_active:
            self._spinner.start(mode="thinking")
            chat.start_thinking_stream()

    def on_thinking_started(self, msg: ThinkingStarted) -> None:
        self._start_thinking_if_needed()

    def on_thinking_delta(self, msg: ThinkingDelta) -> None:
        # Text already in accumulator (written by _make_poster).
        # Just ensure stream is active so the timer drains it.
        self._start_thinking_if_needed()

    def on_thinking_received(self, msg: ThinkingReceived) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        if chat._thinking_active:
            # Streaming was in progress - finalize it
            chat.end_thinking_stream()
        elif msg.text and msg.text.strip():
            # Batch mode (daemon sends full thinking in one event) - display directly
            chat.add_thinking(msg.text)
        self._spinner.start(mode="responding")

    def on_hook_fired(self, msg: HookFired) -> None:
        event = msg.event
        action_type = getattr(event, "action_type", "")
        phase = getattr(event, "phase", "")
        details = getattr(event, "details", {}) or {}

        # Update context pressure + breakdown in footer from context_status events
        if action_type == "context_status":
            footer = self.query_one("#status-footer", StatusFooter)
            pressure = details.get("pressure", 0)
            threshold = details.get("threshold", 0.75)
            if isinstance(pressure, (int, float)):
                footer.context_pressure = float(pressure)
            if isinstance(threshold, (int, float)):
                footer.context_threshold = float(threshold)
            # Context breakdown percentages
            sys_pct = details.get("system_prompt_pct", 0)
            tools_pct = details.get("tools_schema_pct", 0)
            msg_pct = details.get("message_history_pct", 0)
            if isinstance(sys_pct, (int, float)):
                footer.context_system_pct = float(sys_pct)
            if isinstance(tools_pct, (int, float)):
                footer.context_tools_pct = float(tools_pct)
            if isinstance(msg_pct, (int, float)):
                footer.context_messages_pct = float(msg_pct)
            footer.refresh_bar()
            return

        # Start spinner during compaction (no token count - just time)
        if phase == "start" and action_type == "compact_context":
            # Reset token accumulators - post-compaction LLM call starts fresh
            self._token_acc[0] = 0
            self._token_acc[1] = 0
            self._spinner.start(mode="tool_use", label="Compacting context", reset_tokens=True)

        # Connection retry - show rate_limited spinner with attempt info
        if action_type == "connection_retry":
            if phase == "start":
                attempt = details.get("attempt", "?")
                delay = details.get("delay", "?")
                self._spinner.start(
                    mode="rate_limited",
                    label=f"Retrying ({attempt}/3, {delay}s wait)",
                )
            elif phase == "end":
                self._spinner.start(mode="requesting", label="Reconnected")
            return  # Don't show in chat log - too noisy

        # Rate limit from provider - show spinner with wait info
        if action_type == "rate_limit":
            attempt = details.get("attempt", "?")
            max_att = details.get("max_attempts", "?")
            wait = details.get("wait", 0)
            self._spinner.start(
                mode="rate_limited",
                label=f"Rate limited ({attempt}/{max_att}, {wait:.0f}s)",
            )
            return

        # Model fallback - show in chat
        if action_type == "model_fallback" and phase == "start":
            chat = self.query_one("#chat-log", ChatLog)
            primary = details.get("primary", "?")
            fallback = details.get("fallback", "?")
            t = Text()
            t.append("\u21bb ", style="bold #f59e0b")
            t.append(f"Falling back: {primary} → {fallback}", style="#f59e0b")
            chat._append(t)
            self._spinner.start(mode="requesting", label=f"Trying {fallback}")
            return

        self.query_one("#chat-log", ChatLog).add_hook(action_type, phase, details)

        # After compaction/injection ends, restart spinner (LLM call coming next)
        if phase in ("end", "error") and action_type in ("compact_context", "inject_message"):
            self._spinner.start(mode="requesting", label="Resuming")

    def on_agent_event(self, msg: AgentEvent) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_agent_event(
            agent_id=msg.agent_id,
            status=msg.status,
            specialist=msg.specialist,
            task=msg.task,
            duration=msg.duration,
            preview=msg.preview,
        )
        # Also update sidebar
        sidebar = self.query_one("#sidebar", Sidebar)
        sidebar.update_agent(msg.agent_id, msg.status, msg.specialist, msg.task, msg.duration)

    def on_memory_update(self, msg: MemoryUpdate) -> None:
        """Memory state changed - update sidebar."""
        sidebar = self.query_one("#sidebar", Sidebar)
        sidebar.update_memory(msg.action, msg.result)
        if sidebar.has_class("hidden"):
            sidebar.remove_class("hidden")

    def on_turn_complete(self, msg: TurnComplete) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        # Finalize any pending thinking/streaming before stopping spinner
        chat.end_thinking_stream()
        chat._end_streaming_if_active()
        self._spinner.stop()

        if msg.error and not msg.content:
            chat.add_error(msg.error)
        elif msg.content and not self._streamed_this_turn:
            chat.add_response(msg.content)

        chat.add_separator()
        self._busy = False
        self._streamed_this_turn = False

        # Bell notification - terminal beep when turn completes
        self.bell()

        footer = self.query_one("#status-footer", StatusFooter)

        # Use authoritative data from daemon (if available)
        usage = msg.usage
        if usage:
            footer.prompt_tokens = usage.get("total_input_tokens", usage.get("input_tokens", 0))
            footer.completion_tokens = usage.get("total_output_tokens", usage.get("output_tokens", 0))
            self._total_in_tokens = footer.prompt_tokens
            self._total_out_tokens = footer.completion_tokens
            self._session_cost_usd = usage.get("cost_usd", 0.0)
            footer.cost_usd = self._session_cost_usd
            if usage.get("model"):
                footer.model = usage["model"]
        else:
            footer.completion_tokens = self._token_acc[0]
            footer.prompt_tokens = self._token_acc[1]
            self._total_in_tokens += self._token_acc[1]
            self._total_out_tokens += self._token_acc[0]

        if msg.turn_number > 0:
            self._turn_count = msg.turn_number
        else:
            self._turn_count += 1
        footer.turns = self._turn_count

        # Update context from daemon
        if msg.context:
            footer.context_pressure = msg.context.get("pressure", footer.context_pressure)
            footer.context_max_tokens = msg.context.get("max_tokens", 0)
            footer.context_effective_max = msg.context.get("effective_max", 0)
            footer.context_output_reserved = msg.context.get("output_reserved", 0)
            footer.context_system_pct = msg.context.get("system_prompt_pct", 0)
            footer.context_tools_pct = msg.context.get("tools_schema_pct", 0)
            footer.context_messages_pct = msg.context.get("message_history_pct", 0)

        # Update git status from daemon (no more local subprocess polling)
        ws = msg.workspace_status
        if ws and ws.get("branch"):
            sidebar = self.query_one("#sidebar", Sidebar)
            sidebar.update_git(
                ws.get("branch", ""),
                ws.get("changes", []),
                ws.get("ahead", 0),
                ws.get("behind", 0),
            )

        footer.set_busy(False)
        footer.refresh_bar()
        self.query_one("#prompt-input", PromptInput).focus()

        # One-shot mode: exit after first turn
        if self._exit_on_complete:
            self.set_timer(0.3, lambda: self.exit())

    # ── Input ─────────────────────────────────────────────────────

    @on(TextArea.Changed, "#prompt-input")
    def on_input_changed(self, event: TextArea.Changed) -> None:
        """Show/hide slash command menu as user types."""
        text = event.text_area.text
        # Close info panel only when user types new text (not on clear)
        if text.strip() and not text.startswith("/"):
            self._close_info_panel()
        inp = self.query_one("#prompt-input", PromptInput)
        menu = self.query_one("#slash-menu", SlashMenu)
        if text.startswith("/") and not self._busy:
            menu.filter(text.split("\n")[0])
            inp.menu_open = True
        else:
            menu.hide()
            inp.menu_open = False

    def _close_info_panel(self) -> None:
        """Close the info panel if open."""
        try:
            from digitorn_cli.tui.widgets.info_panel import InfoPanel
            panel = self.query_one("#info-panel", InfoPanel)
            if panel.is_visible:
                panel.close()
        except Exception:
            pass

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        """Handle Enter key from PromptInput."""
        # Don't close panel here - slash commands open panels
        # Panel closes on Escape or when user types a non-slash message
        self._submit_input(event.text)

    def on_key(self, event) -> None:
        """Key handler for slash menu and history navigation."""
        inp = self.query_one("#prompt-input", PromptInput)
        menu = self.query_one("#slash-menu", SlashMenu)

        # Slash menu takes priority when open
        if menu.is_open:
            if event.key == "up":
                menu.move_up()
                event.prevent_default()
                event.stop()
            elif event.key == "down":
                menu.move_down()
                event.prevent_default()
                event.stop()
            elif event.key == "tab":
                # Tab → autocomplete into input (don't submit)
                selected = menu.get_selected()
                if selected:
                    inp.clear()
                    inp.insert(selected + " ")
                    menu.hide()
                    inp.menu_open = False
                    event.prevent_default()
                    event.stop()
            elif event.key == "enter":
                # Enter → submit the selected command immediately
                selected = menu.get_selected()
                menu.hide()
                inp.menu_open = False
                inp.clear()
                event.prevent_default()
                event.stop()
                if selected:
                    self._submit_input(selected)
            return

        # Input history (Up/Down when input is empty or single-line cursor at start)
        is_empty_or_start = not inp.text.strip() or (
            inp.cursor_location[0] == 0 and "\n" not in inp.text
        )
        if event.key == "up" and not self._busy and self._input_history and is_empty_or_start:
            if self._history_index == -1:
                self._history_draft = inp.text
                self._history_index = len(self._input_history) - 1
            elif self._history_index > 0:
                self._history_index -= 1
            else:
                return
            inp.clear()
            inp.insert(self._input_history[self._history_index])
            event.prevent_default()
            event.stop()
        elif event.key == "down" and not self._busy and self._history_index >= 0:
            if self._history_index < len(self._input_history) - 1:
                self._history_index += 1
                inp.clear()
                inp.insert(self._input_history[self._history_index])
            else:
                self._history_index = -1
                inp.clear()
                inp.insert(self._history_draft)
            event.prevent_default()
            event.stop()

    def _submit_input(self, text: str = "") -> None:
        """Submit text. Called by PromptInput.Submitted handler."""
        menu = self.query_one("#slash-menu", SlashMenu)

        # If slash menu is open, use the selected command
        if menu.is_open:
            selected = menu.get_selected()
            menu.hide()
            if selected:
                text = selected
        else:
            menu.hide()

        text = text.strip()

        if not text:
            return

        # Record in history (skip duplicates of last entry)
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
            if len(self._input_history) > 100:
                self._input_history = self._input_history[-100:]
        self._history_index = -1
        self._history_draft = ""

        # Approval mode - intercept input
        if self._pending_approval:
            self._handle_approval_response(text)
            return

        if self._busy:
            chat = self.query_one("#chat-log", ChatLog)
            t = Text()
            t.append("Agent is busy - ", style="#f59e0b")
            t.append("press esc to interrupt", style="#64748b")
            chat._append(t)
            return

        if text.lower() in ("/quit", "/exit", "quit", "exit"):
            self.exit()
            return
        if text.startswith("/"):
            self._handle_slash(text)
            return

        # Real message - close info panel, start agent turn
        self._close_info_panel()
        self._busy = True
        self._generation += 1
        self._streamed_this_turn = False
        self._token_acc[0] = 0
        self._token_acc[1] = 0
        self.query_one("#chat-log", ChatLog).add_user_message(text)
        self._spinner.start(mode="requesting", reset_tokens=True)
        self.query_one("#chat-log", ChatLog).scroll_end(animate=False)
        self.query_one("#status-footer", StatusFooter).set_busy(True)
        self._run_agent_turn(text, self._generation)

    @work(thread=False, exclusive=True)
    async def _run_agent_turn(self, text: str, generation: int = 0) -> None:
        post = self._make_poster()
        try:
            await self._backend.send_message(text, post)
        except Exception as exc:
            self.post_message(BackendError(str(exc)))
        finally:
            # Safety net: if this generation is still current and busy wasn't cleared,
            # force-clear it. Prevents the app from being permanently stuck.
            if self._generation == generation and self._busy:
                self._busy = False
                self._spinner.stop()
                self.query_one("#status-footer", StatusFooter).set_busy(False)

    def _handle_slash(self, text: str) -> None:
        """Process slash commands."""
        chat = self.query_one("#chat-log", ChatLog)
        cmd = text.strip().lower().split()[0]
        args = text.strip()[len(cmd):].strip()

        pass  # Commands handled below

        if cmd == "/help":
            chat.add_help_panel()

        elif cmd == "/clear":
            self.action_clear_chat()

        elif cmd == "/status":
            self._show_status()

        elif cmd == "/tools":
            self._show_tools(args)

        elif cmd == "/compact":
            self._do_compact()

        elif cmd == "/cost":
            self._show_cost()

        elif cmd == "/diff":
            self._show_diff()

        elif cmd == "/commit":
            self._do_commit(args)

        elif cmd == "/model":
            self._show_model(args)

        elif cmd == "/context":
            self._show_context()

        elif cmd == "/sessions":
            self._show_sessions()

        elif cmd == "/resume":
            self._resume_session(args)

        elif cmd == "/history":
            self._show_history(args)

        elif cmd == "/fork":
            self._fork_session()

        elif cmd == "/export":
            self._export_conversation(args)

        elif cmd == "/mcp":
            self._show_mcp(args)

        elif cmd == "/tasks":
            self._show_tasks()

        elif cmd == "/watchers":
            self._show_watchers()

        elif cmd == "/doctor":
            self._show_doctor()

        elif cmd == "/theme":
            self._toggle_theme()

        elif cmd == "/vim":
            self._toggle_vim()

        elif cmd == "/image":
            self._send_image(args)

        elif cmd in ("/quit", "/exit"):
            self.exit()

        else:
            # Check if it's a skill (starts with /)
            skill_name = cmd.lstrip("/")
            # Try to send as a skill invocation to the agent
            if self._busy:
                chat._append(Text("Agent is busy. Wait for it to finish.", style="#f59e0b"))
            else:
                # Send the slash command as user input - the agent can invoke skills
                self._busy = True
                self._generation += 1
                self._streamed_this_turn = False
                self._token_acc[0] = 0
                self._token_acc[1] = 0
                chat.add_user_message(text)
                self._spinner.start(mode="requesting", reset_tokens=True)
                self.query_one("#status-footer", StatusFooter).set_busy(True)
                self._run_agent_turn(text, self._generation)

    def _show_status(self) -> None:
        """Show session status in sidebar."""
        footer = self.query_one("#status-footer", StatusFooter)
        sidebar = self.query_one("#sidebar", Sidebar)
        lines = []
        lines.append(("Session", f"turn {self._turn_count}, {'busy' if self._busy else 'idle'}"))
        if footer.prompt_tokens > 0:
            lines.append(("Context", f"{footer.prompt_tokens:,} tokens in"))
        if footer.completion_tokens > 0:
            lines.append(("Output", f"{footer.completion_tokens:,} tokens out"))
        if footer.mode:
            lines.append(("Mode", footer.mode))
        sid = getattr(self._backend, 'session_id', '?')
        if sid and sid != '?':
            lines.append(("Session ID", sid[:16]))
        sidebar.show_command_panel("Status", lines)
        if sidebar.has_class("hidden"):
            sidebar.remove_class("hidden")

    @work(thread=True)
    def _show_tools(self, query: str = "") -> None:
        """Show tool catalog in info panel."""
        from digitorn_cli.tui.widgets.info_panel import InfoPanel
        try:
            import httpx
            headers = self._backend._fresh_headers()
            url = f"{self._backend._daemon_url}/api/apps/{self._backend._app_id}/tools/search"
            params = {"q": query} if query else {}
            resp = self._backend._http.get(url, headers=headers, params=params, timeout=10)
            data = resp.json()
            tools = data.get("data", {}).get("results", data.get("data", []))
            if isinstance(tools, dict):
                tools = tools.get("tools", [])

            _RISK = {"low": "\u25cf", "medium": "\u25cb", "high": "\u25cf"}
            _RCOL = {"low": "green", "medium": "yellow", "high": "red"}
            rows = []
            for t in tools[:30]:
                name = t.get("name", t.get("short_name", "?"))
                risk = t.get("risk_level", "low")
                rows.append((name, f"{_RISK.get(risk, '?')} {risk}"))

            title = f"Tools ({len(tools)})"

            def _render():
                self.query_one("#info-panel", InfoPanel).show(title, rows)
            self.call_from_thread(_render)
        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#chat-log", ChatLog).add_error(str(exc))
            )

    @work(thread=True)
    def _show_sessions(self) -> None:
        """Show sessions list in info panel."""
        from digitorn_cli.tui.widgets.info_panel import InfoPanel
        try:
            import httpx
            headers = self._backend._fresh_headers()
            url = f"{self._backend._daemon_url}/api/apps/{self._backend._app_id}/sessions"
            resp = self._backend._http.get(url, headers=headers, timeout=10)
            data = resp.json()
            sessions = data.get("data", {}).get("sessions", [])

            import time as _t
            rows = []
            for s in sessions[:20]:
                sid = s.get("session_id", "?")[:8]
                title = s.get("title", "")[:20] or "untitled"
                msgs = s.get("message_count", 0)
                ts = s.get("last_active", 0)
                ago = ""
                if ts:
                    diff = _t.time() - ts
                    if diff < 60: ago = "now"
                    elif diff < 3600: ago = f"{int(diff//60)}m"
                    elif diff < 86400: ago = f"{int(diff//3600)}h"
                    else: ago = f"{int(diff//86400)}d"
                rows.append((f"{sid} {title}", f"{msgs}msg {ago}"))

            if not rows:
                rows = [("(none)", "No sessions")]

            title = f"Sessions ({len(sessions)})"

            def _render():
                self.query_one("#info-panel", InfoPanel).show(title, rows)
            self.call_from_thread(_render)
        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#chat-log", ChatLog).add_error(str(exc))
            )

    @work(thread=True)
    def _show_tasks(self) -> None:
        """Show background tasks in sidebar (via daemon API)."""
        sidebar = self.query_one("#sidebar", Sidebar)
        try:
            # Use the daemon's background-tasks endpoint via _request
            if hasattr(self._backend, "_request"):
                resp = self._backend._request(
                    "GET",
                    f"{self._backend._daemon_url}/api/apps/{self._backend._app_id}/background-tasks",
                )
                data = resp.json()
                tasks = data.get("data", []) if data.get("success") else []
            else:
                tasks = []
            if tasks:
                lines = []
                for t in tasks[:20]:
                    tid = t.get("task_id", "?")[:10]
                    status = t.get("status", "?")
                    tool = t.get("tool", "?")[:25]
                    lines.append((tid, f"{tool} \u00b7 {status}"))
                def _render():
                    sidebar.show_command_panel(f"Tasks ({len(tasks)})", lines)
                    if sidebar.has_class("hidden"):
                        sidebar.remove_class("hidden")
                self.call_from_thread(_render)
            else:
                self.call_from_thread(
                    lambda: sidebar.show_command_panel("Tasks", [("(none)", "No background tasks")])
                )
        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#chat-log", ChatLog)._append(
                    Text(f"Error: {exc}", style="#ef4444")
                )
            )

    @work(thread=True)
    def _show_watchers(self) -> None:
        """Show active watchers in sidebar (via daemon API)."""
        sidebar = self.query_one("#sidebar", Sidebar)
        try:
            if hasattr(self._backend, "_request"):
                resp = self._backend._request(
                    "GET",
                    f"{self._backend._daemon_url}/api/apps/{self._backend._app_id}/watchers",
                )
                data = resp.json()
                watchers = data.get("data", []) if data.get("success") else []
            else:
                watchers = []
            if watchers:
                lines = []
                for w in watchers[:20]:
                    wid = w.get("watcher_id", "?")[:10]
                    status = w.get("status", "?")
                    desc = w.get("description", wid)[:30]
                    lines.append((wid, f"{desc} \u00b7 {status}"))
                def _render():
                    sidebar.show_command_panel(f"Watchers ({len(watchers)})", lines)
                    if sidebar.has_class("hidden"):
                        sidebar.remove_class("hidden")
                self.call_from_thread(_render)
            else:
                self.call_from_thread(
                    lambda: sidebar.show_command_panel("Watchers", [("(none)", "No active watchers")])
                )
        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#chat-log", ChatLog)._append(
                    Text(f"Error: {exc}", style="#ef4444")
                )
            )

    # ── /compact - trigger context compaction ──────────────

    @work(thread=True)
    def _do_compact(self) -> None:
        """Trigger context compaction via daemon API."""
        chat = self.query_one("#chat-log", ChatLog)
        try:
            result = self._backend.compact()
            if result is None:
                self.call_from_thread(
                    lambda: chat._append(Text("Compaction not available.", style="#f59e0b"))
                )
                return
            before = result.get("before", 0)
            after = result.get("after", 0)
            freed = result.get("freed", before - after)
            note = result.get("note", "")
            def _show():
                t = Text()
                if note:
                    t.append(f"\u25cf {note}", style="#64748b")
                else:
                    t.append("\u2713 ", style="#22c55e")
                    t.append(f"Compacted: {before} → {after} messages ", style="#22c55e")
                    t.append(f"({freed} removed)", style="#64748b")
                chat._append(t)
            self.call_from_thread(_show)
        except Exception as exc:
            self.call_from_thread(
                lambda: chat._append(Text(f"Compact error: {exc}", style="#ef4444"))
            )

    # ── /theme - toggle dark/light ──────────────────────

    _current_theme = "dark"

    def _toggle_theme(self) -> None:
        """Switch between dark and light theme."""
        from pathlib import Path as _P
        theme_dir = _P(__file__).parent
        if self._current_theme == "dark":
            css_path = theme_dir / "theme_light.tcss"
            self._current_theme = "light"
        else:
            css_path = theme_dir / "theme.tcss"
            self._current_theme = "dark"
        if css_path.exists():
            self.stylesheet.read(css_path)
            self.stylesheet.reparse()
            self.refresh(layout=True)
        chat = self.query_one("#chat-log", ChatLog)
        chat._append(Text(f"  Theme: {self._current_theme}", style="dim"))

    # ── /image - send an image file ─────────────────────

    def _send_image(self, path_str: str) -> None:
        """Send an image to the agent. Usage: /image <path>"""
        from pathlib import Path as _P
        chat = self.query_one("#chat-log", ChatLog)
        if not path_str.strip():
            chat._append(Text("  Usage: /image <path>", style="dim"))
            return
        path = _P(path_str.strip())
        if not path.exists():
            chat._append(Text(f"  File not found: {path}", style="#ef4444"))
            return
        ext = path.suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            chat._append(Text(f"  Unsupported format: {ext}", style="#ef4444"))
            return
        # Send as a message with image reference
        t = Text()
        t.append("  \U0001f4ce ", style="bold #60a5fa")
        t.append(path.name, style="#e2e8f0")
        chat._append(t)
        # The message text tells the agent about the image
        self._submit_input(f"[Attached image: {path}]")

    # ── /vim - toggle vim mode ──────────────────────────

    _vim_mode = False

    def _toggle_vim(self) -> None:
        """Toggle vim keybindings on the input."""
        self._vim_mode = not self._vim_mode
        chat = self.query_one("#chat-log", ChatLog)
        if self._vim_mode:
            chat._append(Text("  Vim mode ON (i=insert, Esc=normal, dd=clear, :w=submit)", style="dim"))
        else:
            chat._append(Text("  Vim mode OFF", style="dim"))

    # ── /cost - show token usage ─────────────────────────

    def _show_cost(self) -> None:
        """Show token usage and estimated cost - all data from daemon."""
        footer = self.query_one("#status-footer", StatusFooter)
        sidebar = self.query_one("#sidebar", Sidebar)
        lines = []
        # This turn (from streaming accumulator - display only)
        in_turn = self._token_acc[1]
        out_turn = self._token_acc[0]
        if in_turn or out_turn:
            lines.append(("Last turn in", f"{in_turn:,}"))
            lines.append(("Last turn out", f"{out_turn:,}"))
        # Session totals (from daemon)
        lines.append(("Total in", f"{self._total_in_tokens:,}"))
        lines.append(("Total out", f"{self._total_out_tokens:,}"))
        total = self._total_in_tokens + self._total_out_tokens
        lines.append(("Total tokens", f"{total:,}"))
        # Cost from daemon (single source of truth)
        if self._session_cost_usd > 0:
            lines.append(("Cost", f"${self._session_cost_usd:.4f}"))
        lines.append(("Turns", str(self._turn_count)))
        if footer.context_pressure > 0:
            lines.append(("Context", f"{int(footer.context_pressure * 100)}% used"))
        sidebar.show_command_panel("Session Cost", lines)
        if sidebar.has_class("hidden"):
            sidebar.remove_class("hidden")

    # ── /diff - show git diff ────────────────────────────

    @work(thread=True)
    def _show_diff(self) -> None:
        """Show git diff in the chat log."""
        import subprocess
        chat = self.query_one("#chat-log", ChatLog)
        try:
            cwd = self._backend.workspace_path if hasattr(self._backend, "workspace_path") else "."
            result = subprocess.run(
                ["git", "diff", "--stat", "--no-color"],
                capture_output=True, text=True, timeout=5, cwd=cwd,
            )
            diff_stat = result.stdout.strip()
            if not diff_stat:
                self.call_from_thread(
                    lambda: chat._append(Text("No changes (working tree clean).", style="#64748b"))
                )
                return
            # Also get short diff (limited to 50 lines)
            result2 = subprocess.run(
                ["git", "diff", "--no-color"],
                capture_output=True, text=True, timeout=5, cwd=cwd,
            )
            full_diff = result2.stdout.strip()
            lines = full_diff.split("\n")
            if len(lines) > 50:
                full_diff = "\n".join(lines[:50]) + f"\n... +{len(lines) - 50} more lines"

            def _show():
                t = Text()
                t.append("\u2387 git diff\n", style="bold #a78bfa")
                t.append(diff_stat, style="#94a3b8")
                chat._append(t)
                if full_diff:
                    from textual.widgets import Static
                    from rich.syntax import Syntax
                    syntax = Syntax(full_diff, "diff", theme="monokai", line_numbers=False)
                    chat._msg_count += 1
                    w = Static(syntax, id=f"msg-{chat._msg_count}", classes="indented")
                    try:
                        spinner = chat.query_one("#spinner-bar")
                        chat.mount(w, before=spinner)
                    except Exception:
                        chat.mount(w)
                    chat.scroll_end(animate=False)
            self.call_from_thread(_show)
        except Exception as exc:
            self.call_from_thread(
                lambda: chat._append(Text(f"Diff error: {exc}", style="#ef4444"))
            )

    # ── Update check ────────────────────────────────────────

    @work(thread=True)
    def _check_for_updates(self) -> None:
        """Check PyPI for newer digitorn version (non-blocking, best-effort)."""
        try:
            import digitorn
            current = getattr(digitorn, "__version__", None)
            if not current:
                return
            import urllib.request, json
            req = urllib.request.Request(
                "https://pypi.org/pypi/digitorn/json",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            latest = data.get("info", {}).get("version", "")
            if latest and latest != current:
                def _show():
                    chat = self.query_one("#chat-log", ChatLog)
                    t = Text()
                    t.append("\u2191 ", style="bold #3b82f6")
                    t.append(f"Update available: {current} → {latest}", style="#3b82f6")
                    t.append("  pip install -U digitorn", style="#64748b")
                    chat._append(t)
                self.call_from_thread(_show)
        except Exception:
            pass  # Network error, no PyPI, etc. - silently ignore

    # ── Cost persistence ────────────────────────────────────

    # Session costs are now tracked by the daemon - no local persistence needed.

    # ── /commit - send commit instruction to agent ─────────

    def _do_commit(self, args: str = "") -> None:
        """Send a commit instruction to the agent."""
        if self._busy:
            self.query_one("#chat-log", ChatLog)._append(
                Text("Agent is busy - wait for it to finish.", style="#f59e0b")
            )
            return
        msg = args.strip() if args.strip() else ""
        prompt = (
            f"Review the current git changes (git diff --staged and git diff), "
            f"then create a commit with a clear, concise message. "
        )
        if msg:
            prompt += f"Commit message hint: {msg}"
        else:
            prompt += "Generate the commit message from the diff content."
        self._busy = True
        self._generation += 1
        self._streamed_this_turn = False
        self._token_acc[0] = 0
        self._token_acc[1] = 0
        chat = self.query_one("#chat-log", ChatLog)
        t = Text()
        t.append("/commit", style="bold #3b82f6")
        if msg:
            t.append(f" {msg}", style="#94a3b8")
        chat._append(t)
        self._spinner.start(mode="requesting", reset_tokens=True)
        self.query_one("#status-footer", StatusFooter).set_busy(True)
        self._run_agent_turn(prompt, self._generation)

    # ── /model - show or change model ────────────────────

    @work(thread=True)
    def _show_model(self, args: str = "") -> None:
        """Show current model or change it. Works in both standalone and daemon mode."""
        sidebar = self.query_one("#sidebar", Sidebar)
        chat = self.query_one("#chat-log", ChatLog)
        if args.strip():
            self.call_from_thread(lambda: chat._append(
                Text("Model switching is configured in app.yaml", style="#f59e0b")
            ))
            return
        footer = self.query_one("#status-footer", StatusFooter)
        lines = []
        lines.append(("Model", footer.model or "unknown"))
        lines.append(("Mode", footer.mode or "?"))

        info = self._backend.get_app_info()
        lines.append(("Model", info.get("model", footer.model or "?")))
        lines.append(("Tools", str(info.get("total_tools", "?"))))
        agents = info.get("agents", [])
        if agents:
            lines.append(("Agents", ", ".join(agents[:5])))
        idx = self._backend.get_index_info()
        if idx:
            ctx_max = idx.get("context_window", idx.get("max_tokens", 0))
            if ctx_max:
                lines.append(("Context", f"{ctx_max:,} tokens"))
            inj = idx.get("tool_injection_mode", "")
            if inj:
                lines.append(("Tool mode", inj))

        def _render():
            sidebar.show_command_panel("Model", lines)
            if sidebar.has_class("hidden"):
                sidebar.remove_class("hidden")
        self.call_from_thread(_render)

    # ── /context - show context window breakdown ─────────

    def _show_context(self) -> None:
        """Show context window breakdown in info panel."""
        from digitorn_cli.tui.widgets.info_panel import InfoPanel
        footer = self.query_one("#status-footer", StatusFooter)
        panel = self.query_one("#info-panel", InfoPanel)

        max_tok = footer.context_max_tokens or 128000
        pressure = footer.context_pressure or 0.0
        in_tok = footer.prompt_tokens or self._token_acc[1]
        out_tok = footer.completion_tokens or self._token_acc[0]

        bars = {"Pressure": pressure}
        sp = footer.context_system_pct / 100 if footer.context_system_pct else 0
        tp = footer.context_tools_pct / 100 if footer.context_tools_pct else 0
        mp = footer.context_messages_pct / 100 if footer.context_messages_pct else 0
        if sp or tp or mp:
            bars["System"] = sp
            bars["Tools"] = tp
            bars["Messages"] = mp

        rows = [
            ("Max tokens", f"{max_tok:,}"),
            ("Input", f"{in_tok:,}"),
            ("Output", f"{out_tok:,}"),
            ("Total", f"{in_tok + out_tok:,}"),
        ]
        if self._session_cost_usd > 0:
            rows.append(("Cost", f"${self._session_cost_usd:.4f}"))
        rows.append(("Turns", str(self._turn_count)))
        rows.append(("Tool calls", str(footer.tool_calls)))

        panel.show("Context Window", rows, bars=bars)

    # ── /doctor - system diagnostics ───────────────────────

    @work(thread=True)
    def _show_doctor(self) -> None:
        """Run system diagnostics and show results in chat."""
        import subprocess, sys, platform
        chat = self.query_one("#chat-log", ChatLog)
        checks: list[tuple[str, bool, str]] = []  # (name, ok, detail)

        # Python version
        ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ok = sys.version_info >= (3, 12)
        checks.append(("Python", ok, f"{ver}" + ("" if ok else " (3.12+ required)")))

        # Platform
        checks.append(("Platform", True, f"{platform.system()} {platform.release()}"))

        # Git
        try:
            r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
            checks.append(("Git", r.returncode == 0, r.stdout.strip()))
        except Exception as e:
            checks.append(("Git", False, str(e)[:50]))

        # Backend mode
        checks.append(("Backend", True, "daemon"))

        # Daemon diagnostics (app, model, modules, tools, MCP - all server-side)
        daemon_checks = self._backend.diagnostics()
        checks.extend(daemon_checks)

        # Provider health (from footer - set by BackendReady)
        footer = self.query_one("#status-footer", StatusFooter)
        checks.append(("Model", bool(footer.model), footer.model or "none"))

        def _render():
            t = Text()
            t.append("\u2695 System Diagnostics\n", style="bold #3b82f6")
            passed = sum(1 for _, ok, _ in checks if ok)
            total = len(checks)
            t.append(f"  {passed}/{total} checks passed\n\n", style="#64748b")
            for name, ok, detail in checks:
                icon = "\u2713" if ok else "\u2717"
                color = "#22c55e" if ok else "#ef4444"
                t.append(f"  {icon} ", style=f"bold {color}")
                t.append(f"{name:<16}", style="bold #e2e8f0")
                t.append(f"{detail}\n", style="#94a3b8")
            chat._append(t)
            chat.scroll_end(animate=False)
        self.call_from_thread(_render)

    # ── Session management commands ─────────────────────────

    @work(thread=True)
    def _resume_session(self, session_id: str) -> None:
        """Resume a previous session - full UI reconstruction from persisted events."""
        chat = self.query_one("#chat-log", ChatLog)
        sidebar = self.query_one("#sidebar", Sidebar)
        if not session_id:
            self.call_from_thread(
                lambda: chat._append(Text("Usage: /resume <session_id>", style="#f59e0b"))
            )
            return
        session_id = session_id.strip()
        try:
            ok = self._backend.resume_session(session_id)
            if not ok:
                self.call_from_thread(
                    lambda: chat._append(Text(f"Session '{session_id}' not found.", style="#ef4444"))
                )
                return

            history = self._backend.get_session_history(session_id)
            if not history:
                self.call_from_thread(
                    lambda: chat._append(Text(f"Resumed {session_id[:12]} (empty)", style="#3b82f6"))
                )
                return

            msgs = history.get("messages", [])
            events = history.get("events", [])
            mem_snap = history.get("memory_snapshot", {})
            turn_count = history.get("turn_count", 0)

            def _render():
                chat.clear_all()

                # ── Header ──
                t = Text()
                t.append("\u21ba ", style="bold #3b82f6")
                t.append(f"Resumed session {session_id[:12]}", style="#3b82f6")
                t.append(f"  ({len(msgs)} messages, {turn_count} turns)", style="#64748b")
                chat._append(t)

                # ── Restore sidebar from memory snapshot ──
                if mem_snap:
                    goal = mem_snap.get("goal", "")
                    if goal:
                        sidebar.update_memory("set_goal", {"goal": goal})
                    todos = mem_snap.get("todos", [])
                    if todos:
                        sidebar.update_memory("add_todo", {"todos": todos, "goal": goal})
                    facts = mem_snap.get("key_facts", [])
                    for fact in facts[:10]:
                        content = fact.get("content", str(fact)) if isinstance(fact, dict) else str(fact)
                        sidebar.update_memory("remember", {"content": content})

                # ── Replay events to reconstruct chat + agent status ──
                _replayed_tools = 0
                _replayed_agents = set()
                for ev in events:
                    ev_type = ev.get("type", "")
                    ev_data = ev.get("data", {})

                    if ev_type == "tool_call":
                        name = ev_data.get("name", "")
                        label = ev_data.get("label", name)
                        detail = ev_data.get("detail", "")
                        ok = ev_data.get("success", True)
                        err = ev_data.get("error", "")
                        # Show tool result in chat (compact)
                        tt = Text()
                        icon = "\u2713" if ok else "\u2717"
                        color = "#22c55e" if ok else "#ef4444"
                        tt.append(f" {icon} ", style=f"bold {color}")
                        tt.append(f"{label}", style=f"bold {color}")
                        if detail:
                            tt.append(f" ({detail[:40]})", style="#94a3b8")
                        chat._append(tt, indent=True)
                        _replayed_tools += 1

                    elif ev_type == "turn_start":
                        msg_text = ev_data.get("message", "")
                        if msg_text:
                            chat.add_user_message(msg_text[:200])

                    elif ev_type == "turn_end":
                        content = ev_data.get("content", "")
                        if content:
                            preview = content[:400]
                            if len(content) > 400:
                                preview += "\u2026"
                            chat.add_response(preview)
                        chat.add_separator()

                # ── If no events, fall back to message-based display ──
                if not events:
                    for m in msgs[-10:]:
                        role = m.get("role", "")
                        content = m.get("content", "")
                        if not content or role == "system":
                            continue
                        if role == "user":
                            chat.add_user_message(content[:200])
                        elif role == "assistant":
                            preview = content[:400]
                            if len(content) > 400:
                                preview += "\u2026"
                            chat.add_response(preview)
                    chat.add_separator()

                # ── Summary ──
                st = Text()
                st.append(f"  {_replayed_tools} tool calls replayed", style="#64748b")
                if mem_snap.get("goal"):
                    st.append(f" \u00b7 goal restored", style="#64748b")
                if mem_snap.get("todos"):
                    st.append(f" \u00b7 {len(mem_snap['todos'])} tasks restored", style="#64748b")
                chat._append(st)

                # Make sidebar visible
                if sidebar.has_class("hidden"):
                    sidebar.remove_class("hidden")

                chat.scroll_end(animate=False)

            self.call_from_thread(_render)
            # Restore cost tracking
            pass  # Session costs tracked by daemon
            # Update turn count
            self._turn_count = turn_count

        except Exception as exc:
            self.call_from_thread(
                lambda: chat._append(Text(f"Resume failed: {exc}", style="#ef4444"))
            )

    @work(thread=True)
    def _show_history(self, session_id: str = "") -> None:
        """Show message history for a session."""
        chat = self.query_one("#chat-log", ChatLog)
        sid = session_id.strip() if session_id else self._backend.session_id
        try:
            history = self._backend.get_session_history(sid)
            if not history or not history.get("messages"):
                self.call_from_thread(
                    lambda: chat._append(Text(f"No history for session {sid[:12]}.", style="#64748b"))
                )
                return
            msgs = history["messages"]
            lines = []
            for m in msgs:
                role = m.get("role", "?")
                content = str(m.get("content", ""))[:80]
                if role in ("user", "assistant"):
                    lines.append((role, content))
            self.call_from_thread(
                lambda: chat.add_info_panel(f"History ({len(msgs)} messages)", lines[-20:])
            )
        except Exception as exc:
            self.call_from_thread(
                lambda: chat._append(Text(f"Error: {exc}", style="#ef4444"))
            )

    @work(thread=True)
    def _export_conversation(self, format: str = "") -> None:
        """Export conversation history to a file. Usage: /export [md|json] [path]"""
        import json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _dt

        chat = self.query_one("#chat-log", ChatLog)
        parts = format.strip().split(None, 1)
        fmt = parts[0].lower() if parts else "md"
        if fmt not in ("md", "markdown", "json"):
            fmt = "md"

        # Generate filename
        timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        sid = getattr(self._backend, '_session_id', 'session')[:8]
        ext = "json" if fmt == "json" else "md"
        if len(parts) > 1:
            filepath = _Path(parts[1])
        else:
            filepath = _Path(f"conversation_{sid}_{timestamp}.{ext}")

        try:
            history = self._backend.get_history()
            if not history:
                self.call_from_thread(lambda: chat._append(
                    Text("  No messages to export.", style="dim")
                ))
                return

            if fmt == "json":
                content = _json.dumps(history, indent=2, ensure_ascii=False)
            else:
                lines = []
                for msg in history:
                    role = msg.get("role", "?")
                    text = msg.get("content", "")
                    if role == "user":
                        lines.append(f"## User\n\n{text}\n")
                    elif role == "assistant":
                        lines.append(f"## Assistant\n\n{text}\n")
                    elif role == "system":
                        lines.append(f"*System: {text[:100]}...*\n")
                content = "\n---\n\n".join(lines)

            filepath.write_text(content, encoding="utf-8")
            path_str = str(filepath)
            self.call_from_thread(lambda: chat._append(
                Text(f"  \u2713 Exported to {path_str}", style="bold #22c55e")
            ))
        except Exception as exc:
            err = str(exc)
            self.call_from_thread(lambda: chat._append(
                Text(f"  \u2717 Export failed: {err}", style="#ef4444")
            ))

    @work(thread=True)
    def _fork_session(self) -> None:
        """Fork the current session."""
        chat = self.query_one("#chat-log", ChatLog)
        try:
            result = self._backend.fork_session()
            if result:
                new_id = result.get("new_session_id", "?")
                self.call_from_thread(lambda: chat._append(
                    Text(f"\u2442 Forked → {new_id[:12]}", style="bold #3b82f6")
                ))
            else:
                self.call_from_thread(
                    lambda: chat._append(Text("Fork failed.", style="#ef4444"))
                )
        except Exception as exc:
            self.call_from_thread(
                lambda: chat._append(Text(f"Fork error: {exc}", style="#ef4444"))
            )

    # ── MCP management commands ──────────────────────────────

    @work(thread=True)
    def _show_mcp(self, subcommand: str = "") -> None:
        """MCP server management in sidebar - scoped to current app + user."""
        sidebar = self.query_one("#sidebar", Sidebar)
        sub = subcommand.strip().lower()

        try:
            if sub == "health":
                servers = self._backend.mcp_health()
                if not servers:
                    self.call_from_thread(
                        lambda: sidebar.show_command_panel("MCP Health", [("(none)", "No servers")])
                    )
                    return
                lines = []
                for s in servers:
                    sid = s.get("server_id", s.get("name", "?"))
                    status = s.get("status", s.get("health", "?"))
                    icon = "\u2713" if status in ("connected", "healthy", "ok") else "\u2717"
                    lines.append((sid, f"{icon} {status}"))
                def _render():
                    sidebar.show_command_panel("MCP Health", lines)
                    if sidebar.has_class("hidden"):
                        sidebar.remove_class("hidden")
                self.call_from_thread(_render)

            else:
                servers = self._backend.list_mcp_servers()
                if not servers:
                    def _empty():
                        sidebar.show_command_panel("MCP Servers", [("(none)", "No servers for this app")])
                        if sidebar.has_class("hidden"):
                            sidebar.remove_class("hidden")
                    self.call_from_thread(_empty)
                    return
                lines = []
                for s in servers:
                    sid = s.get("server_id", s.get("name", "?"))
                    transport = s.get("transport", "?")
                    status = s.get("status", "?")
                    tools_count = s.get("tools_count", "")
                    detail = f"{transport} \u00b7 {status}"
                    if tools_count:
                        detail += f" \u00b7 {tools_count}t"
                    lines.append((sid, detail))
                def _render():
                    sidebar.show_command_panel(f"MCP ({len(servers)})", lines)
                    if sidebar.has_class("hidden"):
                        sidebar.remove_class("hidden")
                self.call_from_thread(_render)

        except Exception as exc:
            self.call_from_thread(
                lambda: self.query_one("#chat-log", ChatLog)._append(
                    Text(f"MCP error: {exc}", style="#ef4444")
                )
            )

    def action_quit(self) -> None:
        # Stop the SSE listener thread before exiting
        stop = getattr(self._backend, "_event_stop", None)
        if stop is not None:
            stop.set()
        self.exit()

    def action_abort_or_focus(self) -> None:
        """Escape: abort → close info panel → close slash menu → close command panel → focus input."""
        from digitorn_cli.tui.widgets.info_panel import InfoPanel
        menu = self.query_one("#slash-menu", SlashMenu)
        sidebar = self.query_one("#sidebar", Sidebar)
        panel = self.query_one("#info-panel", InfoPanel)

        if self._busy:
            self._abort_generation()
        elif panel.is_visible:
            panel.close()
            self.query_one("#prompt-input", PromptInput).focus()
        elif menu.is_open:
            menu.hide()
        elif sidebar._command_panel is not None:
            sidebar.clear_command_panel()
        else:
            self.query_one("#prompt-input", PromptInput).focus()

    def _abort_generation(self) -> None:
        """Stop the current agent turn - clean abort with generation bump."""
        if hasattr(self._backend, "abort") and callable(self._backend.abort):
            self._backend.abort()
        # Bump generation so stale TurnComplete from the aborted turn is ignored
        self._generation += 1
        self._spinner.stop()
        self._busy = False
        chat = self.query_one("#chat-log", ChatLog)
        # Finalize any pending streams
        chat.end_thinking_stream()
        chat._end_streaming_if_active()
        # Show interruption message
        t = Text()
        t.append("\u25a0 ", style="bold #f59e0b")
        t.append("Interrupted by user", style="#f59e0b")
        chat._append(t)
        chat.add_separator()
        self.query_one("#status-footer", StatusFooter).set_busy(False)
        self.query_one("#prompt-input", PromptInput).focus()

    def on_paste(self, event) -> None:
        """Redirect paste events to the input widget, even when focus is elsewhere."""
        inp = self.query_one("#prompt-input", PromptInput)
        if not inp.has_focus:
            inp.focus()
            inp._on_paste(event)

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar", Sidebar)
        sidebar.toggle_class("hidden")

    def action_search(self) -> None:
        """Open search bar (Ctrl+F)."""
        from digitorn_cli.tui.views.search import SearchBar
        search = self.query_one("#search-bar", SearchBar)
        chat = self.query_one("#chat-log", ChatLog)

        def on_search(query: str) -> int:
            """Search all chat messages, return match count."""
            if not query:
                return 0
            count = 0
            q = query.lower()
            for child in chat.children:
                text = child.render().plain if hasattr(child.render(), 'plain') else str(child.render())
                if q in text.lower():
                    child.styles.background = "#1e3a5f"
                    count += 1
                else:
                    child.styles.background = None
            return count

        def on_navigate(direction: int) -> None:
            """Scroll to next/previous match."""
            q = search._query.lower()
            if not q:
                return
            children = list(chat.children)
            matches = [c for c in children if q in (
                c.render().plain if hasattr(c.render(), 'plain') else str(c.render())
            ).lower()]
            if not matches:
                return
            idx = min(search._current_match - 1, len(matches) - 1)
            matches[idx].scroll_visible()

        def on_close() -> None:
            """Clear highlights."""
            for child in chat.children:
                child.styles.background = None
            self.query_one("#prompt-input", PromptInput).focus()

        search.open(on_search=on_search, on_navigate=on_navigate, on_close=on_close)

    def action_bookmark(self) -> None:
        """Toggle bookmark on last message (Ctrl+D)."""
        self.query_one("#chat-log", ChatLog).toggle_bookmark()

    def action_next_bookmark(self) -> None:
        """Jump to next bookmark (F2)."""
        self.query_one("#chat-log", ChatLog).jump_to_bookmark(1)

    def action_toggle_preview(self) -> None:
        """Toggle code preview pane (Ctrl+P)."""
        from digitorn_cli.tui.widgets.code_preview import CodePreview
        self.query_one("#code-preview", CodePreview).toggle()

    def action_new_tab(self) -> None:
        """Create a new session tab (Ctrl+T)."""
        import uuid
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        tab_bar = self.query_one("#tab-bar", TabBar)
        new_sid = str(uuid.uuid4())
        tab_bar.add_tab(new_sid, "New")
        # Switch backend to new session
        self._backend._session_id = new_sid
        self.query_one("#chat-log", ChatLog).clear_all()
        chat = self.query_one("#chat-log", ChatLog)
        t = Text()
        t.append("  New session: ", style="dim")
        t.append(new_sid[:12], style="bold cyan")
        chat._append(t)

    def action_close_tab(self) -> None:
        """Close current tab (Ctrl+W)."""
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        tab_bar = self.query_one("#tab-bar", TabBar)
        if tab_bar.tab_count <= 1:
            self.exit()  # Last tab → quit
            return
        current = tab_bar.active_session_id
        new_active = tab_bar.close_tab(current)
        if new_active:
            self._backend._session_id = new_active
            self.query_one("#chat-log", ChatLog).clear_all()
            # TODO: restore chat history for the switched-to session

    def action_next_tab(self) -> None:
        """Switch to next tab (F3)."""
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        tab_bar = self.query_one("#tab-bar", TabBar)
        new_sid = tab_bar.next_tab()
        if new_sid:
            self._backend._session_id = new_sid
            self.query_one("#chat-log", ChatLog).clear_all()
            self.post_message(TabBar.TabSelected(new_sid))

    def action_prev_tab(self) -> None:
        """Switch to previous tab (F4)."""
        from digitorn_cli.tui.widgets.tab_bar import TabBar
        tab_bar = self.query_one("#tab-bar", TabBar)
        new_sid = tab_bar.prev_tab()
        if new_sid:
            self._backend._session_id = new_sid
            self.query_one("#chat-log", ChatLog).clear_all()
            self.post_message(TabBar.TabSelected(new_sid))

    def action_clear_chat(self) -> None:
        """Clear all messages from the chat log."""
        chat = self.query_one("#chat-log", ChatLog)
        chat.clear_all()
        t = Text()
        t.append("\u2713 ", style="#22c55e")
        t.append("Chat cleared", style="#64748b")
        chat._append(t)

    def action_show_help(self) -> None:
        """Show keyboard shortcuts and slash commands."""
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_help_panel()

    # ── Approval ──────────────────────────────────────────────────

    def on_approval_requested(self, msg: ApprovalRequested) -> None:
        self._spinner.stop()
        self._pending_approval = msg
        chat = self.query_one("#chat-log", ChatLog)
        chat.add_approval_request(msg.tool_name, msg.tool_params, msg.risk_level)

        # ── ask_user with content: show plan in sidebar ──────────
        if msg.tool_name == "ask_user" and msg.tool_params.get("content"):
            sidebar = self.query_one("#sidebar", Sidebar)
            # Make sidebar visible if hidden
            if sidebar.has_class("hidden"):
                sidebar.remove_class("hidden")
            # Push the plan content into sidebar as a reviewable plan
            plan_content = msg.tool_params["content"]
            plan_steps = [
                line.strip().lstrip("0123456789.- ")
                for line in plan_content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if plan_steps:
                sidebar._plan = plan_steps[:20]
                sidebar._plan_step = 0
                sidebar._rebuild()
            # Switch to approval mode with plan-specific prompt
            self.query_one("#prompt-icon", Static).update(
                Text(" \u2753 ", style="bold #3b82f6")
            )
            inp = self.query_one("#prompt-input", PromptInput)
            inp.placeholder = "y to approve plan, n to reject, or type feedback..."
            inp.focus()
            return

        # ── Standard approval request ────────────────────────────
        self.query_one("#prompt-icon", Static).update(
            Text(" \u26a0 ", style="bold #f59e0b")
        )
        inp = self.query_one("#prompt-input", PromptInput)
        inp.placeholder = "y to approve, n to deny, or type reason..."
        inp.focus()

    def _handle_approval_response(self, text: str) -> None:
        msg = self._pending_approval
        self._pending_approval = None
        # Restore normal input
        self.query_one("#prompt-icon", Static).update(
            Text(" \u276f ", style="bold #22c55e")
        )
        inp = self.query_one("#prompt-input", PromptInput)
        inp.placeholder = "Message..."

        lower = text.strip().lower()
        approved = lower in ("y", "yes", "ok", "approve")
        deny_msg = "" if approved else (
            text if lower not in ("n", "no", "deny", "reject") else ""
        )

        chat = self.query_one("#chat-log", ChatLog)
        chat.add_approval_result(approved, deny_msg)

        if approved:
            self._spinner.start(mode="tool_use", label="Executing approved action")
        self._backend.resolve_approval(msg.request_id, approved, deny_msg)

    # ── Undo ──────────────────────────────────────────────────────

    def action_undo(self) -> None:
        chat = self.query_one("#chat-log", ChatLog)
        try:
            if not hasattr(self._backend, "undo_last"):
                chat._append(Text("Undo not available in this mode.", style="#f59e0b"))
                return
            result = self._backend.undo_last()
            if result is None:
                chat._append(Text("Nothing to undo (no checkpoints).", style="#64748b"))
                return
            if result.get("error"):
                chat._append(Text(f"Undo failed: {result['error']}", style="#ef4444"))
                return
            path = result.get("path", "?")
            remaining = result.get("remaining", 0)
            t = Text()
            t.append("\u21b6 ", style="bold #3b82f6")
            t.append("Undone", style="bold #3b82f6")
            t.append(f"  {path}", style="#e2e8f0")
            if remaining > 0:
                t.append(f"  ({remaining} more)", style="#64748b")
            chat._append(t)
            self._poll_git_status()
        except Exception as exc:
            chat._append(Text(f"Undo error: {exc}", style="#ef4444"))

    # ── Git status polling ────────────────────────────────────────

    # Git status is now provided by the daemon in the result event's workspace_status.
    # No more local subprocess polling.
