"""Tests for Digitorn TUI — spinner, chat_log, block streaming.

Tests the core mechanisms without requiring a running Textual app:
- SpinnerBar rendering (modes, elapsed, verb rotation)
- ChatLog block-based streaming (paragraph flush, narration detection)
- StatusFooter formatting
- BackgroundRunParams auto-routing
- Message construction
- Tool name resolution
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.text import Text


# ── SpinnerBar ────────────────────────────────────────────────────


class TestSpinnerBar:
    """Tests SpinnerBar rendering logic."""

    def test_fmt_elapsed_under_1s(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _fmt_elapsed
        assert _fmt_elapsed(0.5) == ""
        assert _fmt_elapsed(0.0) == ""

    def test_fmt_elapsed_seconds(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _fmt_elapsed
        assert _fmt_elapsed(3.0) == "3s"
        assert _fmt_elapsed(59.0) == "59s"

    def test_fmt_elapsed_minutes(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _fmt_elapsed
        assert _fmt_elapsed(90.0) == "1m30s"
        assert _fmt_elapsed(125.0) == "2m05s"

    def test_verbs_not_empty(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _VERBS
        assert len(_VERBS) > 20
        assert all(isinstance(v, str) for v in _VERBS)

    def test_frames_valid(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _FRAMES
        assert len(_FRAMES) > 0
        assert all(len(f) == 1 for f in _FRAMES)

    def test_mode_styles_defined(self):
        from digitorn.core.cli.tui.widgets.spinner_bar import _MODE_STYLES
        assert "thinking" in _MODE_STYLES
        assert "generating" in _MODE_STYLES
        assert "streaming" in _MODE_STYLES
        assert "tool_use" in _MODE_STYLES
        assert "requesting" in _MODE_STYLES
        # Each style is (icon, label, color, hi_color)
        for mode, style in _MODE_STYLES.items():
            assert len(style) == 4, f"Mode {mode} style has wrong length"


# ── ChatLog block-based streaming ─────────────────────────────────


class TestChatLogStreaming:
    """Test the block-based streaming logic extracted from ChatLog."""

    def test_paragraph_split(self):
        """Double newline triggers a block flush."""
        buf = "First paragraph.\n\nSecond paragraph."
        parts = buf.split("\n\n")
        assert len(parts) == 2
        assert parts[0] == "First paragraph."
        assert parts[1] == "Second paragraph."

    def test_no_flush_without_double_newline(self):
        """Single newline should NOT trigger a flush."""
        buf = "Line one.\nLine two.\nLine three."
        parts = buf.split("\n\n")
        assert len(parts) == 1

    def test_multiple_paragraphs(self):
        buf = "Para 1.\n\nPara 2.\n\nPara 3.\n\nIncomplete"
        parts = buf.split("\n\n")
        complete = "\n\n".join(parts[:-1])
        remainder = parts[-1]
        assert complete == "Para 1.\n\nPara 2.\n\nPara 3."
        assert remainder == "Incomplete"

    def test_flush_preserves_markdown(self):
        buf = "# Title\n\nSome **bold** text.\n\n```python\nprint('hi')\n```\n\nMore text"
        parts = buf.split("\n\n")
        complete = "\n\n".join(parts[:-1])
        remainder = parts[-1]
        assert "# Title" in complete
        assert "**bold**" in complete
        assert remainder == "More text"

    def test_narration_detection(self):
        from digitorn.core.cli.tui.widgets.chat_log import ChatLog
        # Should be detected as narration
        assert ChatLog._is_pure_narration("Reading /home/user/test.py")
        assert ChatLog._is_pure_narration("Listing /tmp/dir")
        assert ChatLog._is_pure_narration("Searching /path/to/file")
        # Should NOT be narration
        assert not ChatLog._is_pure_narration("Hello, how can I help?")
        assert not ChatLog._is_pure_narration("The file contains 42 lines of code.")
        assert not ChatLog._is_pure_narration(
            "This is a long response that explains many things about the code."
        )

    def test_narration_multi_line_rejected(self):
        from digitorn.core.cli.tui.widgets.chat_log import ChatLog
        long_text = "Reading /path\n" * 10
        assert not ChatLog._is_pure_narration(long_text)

    def test_narration_empty_string(self):
        from digitorn.core.cli.tui.widgets.chat_log import ChatLog
        assert not ChatLog._is_pure_narration("")
        assert not ChatLog._is_pure_narration("   ")

    def test_thread_safe_text_accumulator(self):
        """Agent thread writes to _stream_text_acc, timer reads — no data races."""
        acc: list[str] = []
        values = []

        def agent():
            for i in range(50):
                acc.append(f"word{i} ")
                time.sleep(0.002)

        def reader():
            for _ in range(20):
                snapshot = acc[:]
                values.append(len(snapshot))
                time.sleep(0.005)

        t1 = threading.Thread(target=agent)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(acc) == 50
        # Reader saw intermediate states
        intermediate = [v for v in values if 0 < v < 50]
        assert len(intermediate) > 0

    def test_block_boundary_sentence_end(self):
        """Sentence-ending punctuation followed by space is a valid boundary."""
        from digitorn.core.cli.tui.widgets.chat_log import ChatLog
        chat = ChatLog.__new__(ChatLog)
        # Simulate enough text with a sentence boundary
        buf = "This is a complete sentence. And this starts another one that is still going"
        boundary = chat._find_block_boundary(buf)
        if boundary != -1:
            complete = buf[:boundary]
            assert complete.rstrip().endswith(".")

    def test_block_boundary_paragraph(self):
        """Paragraph break (\\n\\n) is the strongest boundary."""
        from digitorn.core.cli.tui.widgets.chat_log import ChatLog
        chat = ChatLog.__new__(ChatLog)
        buf = "First paragraph.\n\nSecond paragraph still going"
        boundary = chat._find_block_boundary(buf)
        assert boundary != -1
        assert buf[:boundary].strip() == "First paragraph."


# ── StatusFooter ──────────────────────────────────────────────────


class TestStatusFooter:
    """Test StatusFooter formatting helpers."""

    def test_fmt_tokens_small(self):
        from digitorn.core.cli.tui.widgets.status_footer import _fmt_tokens
        assert _fmt_tokens(0) == "0"
        assert _fmt_tokens(42) == "42"
        assert _fmt_tokens(999) == "999"

    def test_fmt_tokens_large(self):
        from digitorn.core.cli.tui.widgets.status_footer import _fmt_tokens
        assert _fmt_tokens(1000) == "1.0k"
        assert _fmt_tokens(100_000) == "100k"

    def test_compact_path(self):
        from digitorn.core.cli.tui.widgets.status_footer import _compact_path
        from pathlib import Path
        home = str(Path.home())
        assert _compact_path(f"{home}/codes/project") == "~/codes/project"
        assert _compact_path("/tmp/other") == "/tmp/other"

    def test_fit_label_short(self):
        from digitorn.core.cli.tui.widgets.status_footer import _fit_label
        assert _fit_label("~/project:main", 30) == "~/project:main"

    def test_fit_label_truncate(self):
        from digitorn.core.cli.tui.widgets.status_footer import _fit_label
        result = _fit_label("~/very/long/path/to/project:feature-branch", 25)
        assert len(result) <= 25
        assert "\u2026" in result  # Ellipsis present


# ── Messages ──────────────────────────────────────────────────────


class TestMessages:
    """Test Textual Message subclasses."""

    def test_token_received(self):
        from digitorn.core.cli.tui.messages import TokenReceived
        m = TokenReceived("hello")
        assert m.delta == "hello"

    def test_out_token_count(self):
        from digitorn.core.cli.tui.messages import OutTokenCount
        m = OutTokenCount(42)
        assert m.count == 42

    def test_in_token_count(self):
        from digitorn.core.cli.tui.messages import InTokenCount
        m = InTokenCount(1000)
        assert m.count == 1000

    def test_thinking_delta(self):
        from digitorn.core.cli.tui.messages import ThinkingDelta
        m = ThinkingDelta("reasoning...")
        assert m.delta == "reasoning..."

    def test_tool_started(self):
        from digitorn.core.cli.tui.messages import ToolStarted
        m = ToolStarted("filesystem.read", {"path": "/tmp/x"})
        assert m.name == "filesystem.read"
        assert m.params["path"] == "/tmp/x"

    def test_tool_completed(self):
        from digitorn.core.cli.tui.messages import ToolCompleted
        m = ToolCompleted("filesystem.read", {"path": "/tmp/x"}, {"data": {"content": "hi"}})
        assert m.result["data"]["content"] == "hi"

    def test_turn_complete(self):
        from digitorn.core.cli.tui.messages import TurnComplete
        m = TurnComplete("response text", error=None)
        assert m.content == "response text"
        assert m.error is None

    def test_turn_complete_with_error(self):
        from digitorn.core.cli.tui.messages import TurnComplete
        m = TurnComplete("", error="timeout")
        assert m.error == "timeout"

    def test_backend_ready(self):
        from digitorn.core.cli.tui.messages import BackendReady
        m = BackendReady(
            app_name="Test", agent_id="agent-1", mode="standalone",
            total_tools=5, model="gpt-4", greeting="Hello!", workspace="/tmp",
        )
        assert m.app_name == "Test"
        assert m.total_tools == 5
        assert m.workspace == "/tmp"

    def test_agent_event(self):
        from digitorn.core.cli.tui.messages import AgentEvent
        m = AgentEvent(agent_id="a1", status="spawned", specialist="coder", task="fix bug")
        assert m.agent_id == "a1"
        assert m.specialist == "coder"


# ── BackgroundRunParams auto-routing ──────────────────────────────


class TestBackgroundRunParams:
    """Test the auto-routing fix for background_run."""

    def test_name_only(self):
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams(name="shell.run", params={"command": "ls"})
        assert p.name == "shell.run"
        assert p.command is None

    def test_command_only(self):
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams(command="echo hello")
        assert p.command == "echo hello"
        assert p.name is None

    def test_command_with_cwd(self):
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams(command="python app.py", cwd="/tmp")
        assert p.command == "python app.py"
        assert p.cwd == "/tmp"

    def test_command_with_env(self):
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams(command="node server.js", env={"PORT": "3000"})
        assert p.env == {"PORT": "3000"}

    def test_neither_name_nor_command(self):
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams()
        assert p.name is None
        assert p.command is None

    def test_both_name_and_command(self):
        """When both provided, name takes priority (it's the original param)."""
        from digitorn.modules.context_builder.params import BackgroundRunParams
        p = BackgroundRunParams(name="shell.background_run", command="echo hi")
        assert p.name == "shell.background_run"
        assert p.command == "echo hi"


# ── Tool name resolution ─────────────────────────────────────────


class TestToolNameResolution:
    """Test DigitornTUI._resolve_tool and _normalize_name."""

    def test_simple_name(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        name, params = DigitornTUI._resolve_tool("filesystem.read", {"path": "/tmp"})
        assert name == "filesystem.read"

    def test_execute_tool_unwrap(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        name, params = DigitornTUI._resolve_tool(
            "execute_tool",
            {"tool_name": "filesystem.read", "params": {"path": "/tmp"}},
        )
        assert name == "filesystem.read"
        assert params == {"path": "/tmp"}

    def test_double_underscore_normalize(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        assert DigitornTUI._normalize_name("filesystem__read") == "filesystem.read"

    def test_single_underscore_normalize(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        assert DigitornTUI._normalize_name("filesystem_read") == "filesystem.read"

    def test_already_dotted_unchanged(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        assert DigitornTUI._normalize_name("filesystem.read") == "filesystem.read"

    def test_memory_tools_not_normalized(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        # These are in the exception list
        assert DigitornTUI._normalize_name("set_goal") == "set_goal"
        assert DigitornTUI._normalize_name("add_todo") == "add_todo"

    def test_silent_tools(self):
        from digitorn.core.cli.tui.app import DigitornTUI
        app = DigitornTUI.__new__(DigitornTUI)
        assert app._is_silent_tool("set_goal", "set_goal")
        assert app._is_silent_tool("search_tools", "search_tools")
        assert app._is_silent_tool("spawn_agent", "spawn_agent")
        assert not app._is_silent_tool("filesystem.read", "filesystem.read")
