"""Tests for the CLI run command and UI components.

Tests the UI helpers, input resolution, and the overall
run command flow (with mocked bootstrap/provider).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.core.cli.ui import (
    print_agent_response,
    print_app_header,
    print_error,
    print_goodbye,
    print_greeting,
    print_info,
    print_one_shot_result,
    print_success,
    print_tool_call,
)


# ===========================================================================
# Tests — UI Components (smoke tests — they just shouldn't crash)
# ===========================================================================


class TestUIComponents:

    def test_print_app_header(self, capsys):
        """App header renders without error."""
        print_app_header(
            app_name="Test App",
            agent_id="coordinator",
            mode="one_shot",
            total_tools=42,
            total_categories=5,
        )

    def test_print_greeting(self, capsys):
        print_greeting("Welcome to the app!")

    def test_print_tool_call_success(self, capsys):
        result = MagicMock()
        result.success = True
        print_tool_call("database.fetch", {"query": "SELECT 1"}, result)

    def test_print_tool_call_error(self, capsys):
        result = MagicMock()
        result.success = False
        result.error = "Connection failed"
        print_tool_call("database.fetch", {"query": "SELECT 1"}, result)

    def test_print_tool_call_no_params(self, capsys):
        print_tool_call("list_categories", {}, None)

    def test_print_tool_call_long_params(self, capsys):
        # Params longer than 120 chars should be truncated
        long_params = {"query": "x" * 200}
        print_tool_call("test.action", long_params, None)

    def test_print_agent_response(self, capsys):
        print_agent_response("Here is the **result**:\n- Item 1\n- Item 2")

    def test_print_agent_response_empty(self, capsys):
        print_agent_response("")
        print_agent_response("  ")

    def test_print_one_shot_text(self, capsys):
        print_one_shot_result("Plain text output", "text")

    def test_print_one_shot_json(self, capsys):
        print_one_shot_result('{"score": 95}', "json")

    def test_print_one_shot_invalid_json(self, capsys):
        # Should fall back to plain text
        print_one_shot_result("not json {", "json")

    def test_print_error(self, capsys):
        print_error("Something went wrong")

    def test_print_success(self, capsys):
        print_success("All good")

    def test_print_info(self, capsys):
        print_info("FYI")

    def test_print_goodbye(self, capsys):
        print_goodbye()


# ===========================================================================
# Tests — Input resolution for one_shot
# ===========================================================================


class TestInputResolution:

    def test_text_message(self):
        from digitorn.core.runtime.modes.one_shot import _build_user_message
        msg = _build_user_message("Hello", "text")
        assert msg["content"] == "Hello"

    def test_any_type_treated_as_text(self):
        from digitorn.core.runtime.modes.one_shot import _build_user_message
        msg = _build_user_message("Hello", "any")
        assert msg["content"] == "Hello"

    def test_json_string_input(self):
        from digitorn.core.runtime.modes.one_shot import _build_json_message
        msg = _build_json_message('{"key": "value"}')
        assert "key" in msg["content"]
        assert "```json" in msg["content"]

    def test_image_nonexistent_path(self):
        from digitorn.core.runtime.modes.one_shot import _build_user_message
        msg = _build_user_message("http://example.com/image.png", "image")
        assert msg["role"] == "user"
        # Should create multimodal content
        assert isinstance(msg["content"], list)

    def test_file_nonexistent(self):
        from digitorn.core.runtime.modes.one_shot import _build_file_message
        msg = _build_file_message("/nonexistent/path.txt")
        assert "File path:" in msg["content"]

    def test_file_exists(self, tmp_path):
        from digitorn.core.runtime.modes.one_shot import _build_file_message
        f = tmp_path / "test.txt"
        f.write_text("Hello from file")
        msg = _build_file_message(str(f))
        assert "Hello from file" in msg["content"]
        assert "test.txt" in msg["content"]

    def test_json_file(self, tmp_path):
        from digitorn.core.runtime.modes.one_shot import _build_json_message
        f = tmp_path / "data.json"
        f.write_text('{"x": 42}')
        msg = _build_json_message(str(f))
        assert "42" in msg["content"]
        assert "```json" in msg["content"]


# ===========================================================================
# Tests — Output extraction
# ===========================================================================


class TestOutputExtraction:

    def test_json_in_code_block(self):
        from digitorn.core.runtime.modes.one_shot import _extract_json_output
        from digitorn.core.runtime.types import TurnResult

        result = TurnResult(
            content='Analysis:\n```json\n{"bugs": 3, "score": 72}\n```\nEnd.',
            tool_calls_count=1, turns_used=2,
        )
        extracted = _extract_json_output(result)
        parsed = json.loads(extracted.content)
        assert parsed["bugs"] == 3
        assert parsed["score"] == 72

    def test_raw_json_object(self):
        from digitorn.core.runtime.modes.one_shot import _extract_json_output
        from digitorn.core.runtime.types import TurnResult

        result = TurnResult(content='  {"ok": true}  ', tool_calls_count=0, turns_used=1)
        extracted = _extract_json_output(result)
        assert json.loads(extracted.content)["ok"] is True

    def test_raw_json_array(self):
        from digitorn.core.runtime.modes.one_shot import _extract_json_output
        from digitorn.core.runtime.types import TurnResult

        result = TurnResult(content='[1, 2, 3]', tool_calls_count=0, turns_used=1)
        extracted = _extract_json_output(result)
        assert json.loads(extracted.content) == [1, 2, 3]

    def test_no_json_passthrough(self):
        from digitorn.core.runtime.modes.one_shot import _extract_json_output
        from digitorn.core.runtime.types import TurnResult

        result = TurnResult(content="Just plain text", tool_calls_count=0, turns_used=1)
        extracted = _extract_json_output(result)
        assert extracted.content == "Just plain text"
