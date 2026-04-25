"""Tests for new ecosystem features (Batches A-F).

Covers: multi_edit, patch, read images, write stale guard,
git diff stat_only, git worktrees, web fetch markdown,
lsp diagnostics, tracking, tool_hooks.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from digitorn.modules.base import ActionResult
from digitorn.modules.filesystem.module import FilesystemModule


@pytest.fixture
def tmp(tmp_path):
    sample = tmp_path / "hello.py"
    sample.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fs(tmp):
    m = FilesystemModule()
    m._checkpoint_enabled = True
    m._workspace_root = str(tmp)
    return m


# ── filesystem.read images ─────────────────────────────────────────


class TestReadImages:
    async def test_read_png(self, fs, tmp):
        # Create a minimal 1x1 PNG
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        png = tmp / "pixel.png"
        png.write_bytes(png_data)

        result = await fs.execute("read", {"path": str(png)})
        assert result.success
        assert result.data["is_image"] is True
        assert result.data["mime_type"] == "image/png"
        assert len(result.data["content"]) > 10  # base64 encoded

    async def test_read_jpg(self, fs, tmp):
        jpg = tmp / "test.jpg"
        jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # Fake JPEG header

        result = await fs.execute("read", {"path": str(jpg)})
        assert result.success
        assert result.data["is_image"] is True
        assert result.data["mime_type"] == "image/jpeg"

    async def test_read_text_not_image(self, fs, tmp):
        result = await fs.execute("read", {"path": str(tmp / "hello.py")})
        assert result.success
        assert result.data.get("is_image") is None or result.data.get("is_image") is False


# ── filesystem.write stale guard ───────────────────────────────────


class TestWriteStaleGuard:
    async def test_write_existing_requires_read(self, fs, tmp):
        # Create a file >500 bytes so the stale guard triggers
        big_file = tmp / "big.py"
        big_file.write_text("x" * 600, encoding="utf-8")
        path = str(big_file)
        result = await fs.execute("write", {"path": path, "content": "overwritten"})
        assert not result.success
        assert "not read" in result.error.lower()

    async def test_write_existing_after_read_ok(self, fs, tmp):
        path = str(tmp / "hello.py")
        await fs.execute("read", {"path": path})
        result = await fs.execute("write", {"path": path, "content": "overwritten"})
        assert result.success

    async def test_write_new_file_no_read_needed(self, fs, tmp):
        path = str(tmp / "brand_new.py")
        result = await fs.execute("write", {"path": path, "content": "new file"})
        assert result.success
        assert (tmp / "brand_new.py").read_text() == "new file"


# ── tracking ──────────────────────────────────────────────────────


class TestTracking:
    def test_session_usage_accumulate(self):
        from digitorn.core.runtime.tracking import SessionUsage

        usage = SessionUsage()
        assert usage.total_tokens == 0

        # Mock response with usage
        class FakeUsage:
            prompt_tokens = 100
            completion_tokens = 50

        class FakeResponse:
            usage = FakeUsage()

        usage.record_response(FakeResponse())
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

        usage.record_response(FakeResponse())
        assert usage.prompt_tokens == 200
        assert usage.total_tokens == 300

    def test_format_image_result(self):
        from digitorn.core.runtime.tracking import format_image_tool_result

        # Non-image returns None
        assert format_image_tool_result({"content": "text"}) is None

        # Image returns content blocks
        blocks = format_image_tool_result({
            "is_image": True,
            "mime_type": "image/png",
            "content": "abc123base64",
            "path": "test.png",
            "size_bytes": 1024,
        })
        assert blocks is not None
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "image_url"
        assert "data:image/png;base64," in blocks[1]["image_url"]["url"]


# ── tool_hooks ────────────────────────────────────────────────────


class TestToolHooks:
    def test_tool_match_exact(self):
        from digitorn.core.runtime.hooks import TurnState, get_condition
        from digitorn.core.runtime.tool_hooks import ToolCallContext

        cond = get_condition("tool_match")
        assert cond is not None

        state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0, agent_id="t")
        state.tool_context = ToolCallContext(tool_name="filesystem.edit")  # type: ignore

        assert cond(state, {"tools": ["filesystem.edit"]}) is True
        assert cond(state, {"tools": ["filesystem.write"]}) is False

    def test_tool_match_wildcard(self):
        from digitorn.core.runtime.hooks import TurnState, get_condition
        from digitorn.core.runtime.tool_hooks import ToolCallContext

        cond = get_condition("tool_match")
        state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0, agent_id="t")
        state.tool_context = ToolCallContext(tool_name="filesystem.multi_edit")  # type: ignore

        assert cond(state, {"tools": ["filesystem.*"]}) is True
        assert cond(state, {"tools": ["git.*"]}) is False

    def test_tool_match_no_context(self):
        from digitorn.core.runtime.hooks import TurnState, get_condition

        cond = get_condition("tool_match")
        state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0, agent_id="t")

        assert cond(state, {"tools": ["filesystem.edit"]}) is False


# ── lsp module ────────────────────────────────────────────────────


class TestLspModule:
    def test_module_registered(self):
        from digitorn.modules.lsp.module import LspModule

        m = LspModule()
        actions = list(m._action_registry.keys())
        assert "diagnostics" in actions
        assert "check" in actions
        assert "notify_change" in actions

    def test_manifest(self):
        from digitorn.modules.lsp.module import LspModule

        m = LspModule()
        mf = m.get_manifest()
        assert mf.module_id == "lsp"
        assert len(mf.actions) == 3

    async def test_diagnostics_no_linter(self):
        from digitorn.modules.lsp.module import LspModule

        m = LspModule()
        result = await m.execute("diagnostics", {"path": "nonexistent.xyz"})
        assert not result.success
        assert "No feedback server" in (result.error or "") or "No linter" in (result.error or "")


# ── workspace rules ───────────────────────────────────────────────


class TestWorkspaceRules:
    def test_load_rules_empty(self, tmp_path):
        from digitorn.core.workspace import WorkspaceLayout

        layout = WorkspaceLayout(str(tmp_path), "test-app")
        assert layout.load_rules() is None

    def test_load_rules_with_files(self, tmp_path):
        from digitorn.core.workspace import WorkspaceLayout

        rules_dir = tmp_path / ".digitorn" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "style.md").write_text("Use 4-space indentation.")

        layout = WorkspaceLayout(str(tmp_path), "test-app")
        rules = layout.load_rules()
        assert rules is not None
        assert "4-space indentation" in rules

    def test_load_rules_scoped(self, tmp_path):
        from digitorn.core.workspace import WorkspaceLayout

        rules_dir = tmp_path / ".digitorn" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "api.md").write_text('---\npaths: ["src/api/**"]\n---\nValidate inputs.')
        (rules_dir / "general.md").write_text("Be concise.")

        layout = WorkspaceLayout(str(tmp_path), "test-app")

        # Without filtering: both loaded
        all_rules = layout.load_rules()
        assert "Validate inputs" in all_rules
        assert "Be concise" in all_rules

        # With matching path: both (general has no scope)
        rules = layout.load_rules(active_paths=["src/api/auth.py"])
        assert "Validate inputs" in rules
        assert "Be concise" in rules

        # With non-matching path: only general
        rules = layout.load_rules(active_paths=["tests/test_foo.py"])
        assert "Be concise" in rules
        assert "Validate inputs" not in rules
