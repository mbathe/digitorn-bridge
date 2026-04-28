"""Tests for the workspace module - virtual filesystem over preview.

Covers: Write, Read, Edit (surgical), Glob, Grep, Delete, Config/Meta.
"""

from __future__ import annotations

import asyncio

import pytest

from digitorn.modules.preview.module import PreviewModule
from digitorn.modules.workspace.module import (
    DeleteParams,
    EditParams,
    GlobParams,
    GrepParams,
    ReadParams,
    WorkspaceConfig,
    WorkspaceModule,
    WriteParams,
)


class _MockBus:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    @staticmethod
    def session_key(app_id, session_id, user_id):
        return f"{app_id}:{session_id}:{user_id}"

    async def publish(self, key, data):
        self.published.append((key, data))


def _setup():
    preview = PreviewModule()
    preview.set_active_session("sess-1", user_id="u1")
    bus = _MockBus()
    preview._event_bus = bus
    preview._bus_app_id = "test-app"

    ws = WorkspaceModule()
    ws._preview = preview

    return ws, preview, bus


# ── Write ─────────────────────────────────────────────────────────────


def test_write_creates_file_and_emits():
    ws, preview, bus = _setup()

    async def go():
        r = await ws.write(WriteParams(path="src/App.tsx", content="export default () => <h1>Hi</h1>"))
        assert r.success
        assert r.data["path"] == "src/App.tsx"
        assert r.data["language"] == "tsx"
        assert r.data["lines"] == 1

        # Verify in preview store
        ch = preview._session().channel("files")
        assert "src/App.tsx" in ch
        assert ch["src/App.tsx"]["language"] == "tsx"

        # Socket.IO event emitted
        await asyncio.sleep(0.05)
        assert any(d["type"] == "preview:resource_set" for _, d in bus.published)

    asyncio.run(go())


def test_write_strips_dot_slash():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="./src/main.tsx", content="x"))
        r = await ws.read(ReadParams(path="src/main.tsx"))
        assert r.success

    asyncio.run(go())


def test_write_overwrite():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="v1"))
        await ws.write(WriteParams(path="f.txt", content="v2"))
        r = await ws.read(ReadParams(path="f.txt"))
        assert "v2" in r.data["content"]

    asyncio.run(go())


# ── Read ──────────────────────────────────────────────────────────────


def test_read_existing():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="readme.md", content="# Hello\nWorld"))
        r = await ws.read(ReadParams(path="readme.md"))
        assert r.success
        assert r.data["language"] == "markdown"
        # Content should have line numbers
        assert "1\t# Hello" in r.data["content"]
        assert "2\tWorld" in r.data["content"]

    asyncio.run(go())


def test_read_missing():
    ws, _, _ = _setup()

    async def go():
        r = await ws.read(ReadParams(path="nope.txt"))
        assert not r.success
        assert "not found" in r.error.lower()

    asyncio.run(go())


# ── Edit ──────────────────────────────────────────────────────────────


def test_edit_single_replacement():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="app.py", content="def hello():\n    return 'world'\n"))
        r = await ws.edit(EditParams(
            path="app.py",
            old_string="return 'world'",
            new_string="return 'universe'",
        ))
        assert r.success
        assert r.data["replacements"] == 1
        assert "diff" in r.data

        # Verify content updated
        r2 = await ws.read(ReadParams(path="app.py"))
        assert "universe" in r2.data["content"]
        assert "world" not in r2.data["content"]

    asyncio.run(go())


def test_edit_replace_all():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="foo bar foo baz foo"))
        r = await ws.edit(EditParams(
            path="f.txt", old_string="foo", new_string="qux", replace_all=True,
        ))
        assert r.success
        assert r.data["replacements"] == 3

        r2 = await ws.read(ReadParams(path="f.txt"))
        assert "foo" not in r2.data["content"]
        assert "qux" in r2.data["content"]

    asyncio.run(go())


def test_edit_not_unique():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="aaa\naaa\n"))
        r = await ws.edit(EditParams(path="f.txt", old_string="aaa", new_string="bbb"))
        assert not r.success
        assert "2 times" in r.error

    asyncio.run(go())


def test_edit_not_found_with_suggestion():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="function handleClick() {\n  console.log('click');\n}\n"))
        r = await ws.edit(EditParams(
            path="f.txt",
            old_string="function handleClck() {",  # typo
            new_string="function handleTap() {",
        ))
        assert not r.success
        assert "not found" in r.error.lower()
        # Should suggest the closest match
        assert "Closest match" in r.error

    asyncio.run(go())


def test_edit_missing_file():
    ws, _, _ = _setup()

    async def go():
        r = await ws.edit(EditParams(path="nope.txt", old_string="a", new_string="b"))
        assert not r.success
        assert "not found" in r.error.lower()

    asyncio.run(go())


def test_edit_identical_strings():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="hello"))
        r = await ws.edit(EditParams(path="f.txt", old_string="hello", new_string="hello"))
        assert not r.success
        assert "identical" in r.error.lower()

    asyncio.run(go())


# ── Glob ──────────────────────────────────────────────────────────────


def test_glob_pattern():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="src/App.tsx", content="a"))
        await ws.write(WriteParams(path="src/main.tsx", content="b"))
        await ws.write(WriteParams(path="src/lib/utils.ts", content="c"))
        await ws.write(WriteParams(path="readme.md", content="d"))

        r = await ws.glob(GlobParams(pattern="src/*.tsx"))
        assert r.success
        assert r.data["count"] == 2
        assert set(f["path"] for f in r.data["files"]) == {"src/App.tsx", "src/main.tsx"}

    asyncio.run(go())


def test_glob_star_star():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="a.ts", content=""))
        await ws.write(WriteParams(path="src/b.ts", content=""))
        await ws.write(WriteParams(path="src/lib/c.ts", content=""))

        r = await ws.glob(GlobParams(pattern="**/*.ts"))
        assert r.success
        # fnmatch ** works on the full path
        assert r.data["count"] >= 2

    asyncio.run(go())


def test_glob_no_match():
    ws, _, _ = _setup()

    async def go():
        r = await ws.glob(GlobParams(pattern="*.xyz"))
        assert r.success
        assert r.data["count"] == 0

    asyncio.run(go())


# ── Grep ──────────────────────────────────────────────────────────────


def test_grep_finds_matches():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="a.py", content="import os\nimport sys\nprint(os.getcwd())\n"))
        await ws.write(WriteParams(path="b.py", content="import json\n"))

        r = await ws.grep(GrepParams(pattern="import os"))
        assert r.success
        assert r.data["count"] == 1
        assert r.data["matches"][0]["path"] == "a.py"
        assert r.data["matches"][0]["line"] == 1

    asyncio.run(go())


def test_grep_regex():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.tsx", content="const App = () => <div>Hello</div>;\nconst Btn = () => <button/>;\n"))

        r = await ws.grep(GrepParams(pattern=r"const \w+ = \(\)"))
        assert r.success
        assert r.data["count"] == 2

    asyncio.run(go())


def test_grep_with_glob_filter():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="a.py", content="import os"))
        await ws.write(WriteParams(path="b.ts", content="import os"))

        r = await ws.grep(GrepParams(pattern="import os", glob="*.py"))
        assert r.success
        assert r.data["count"] == 1
        assert r.data["matches"][0]["path"] == "a.py"

    asyncio.run(go())


def test_grep_no_match():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="f.txt", content="hello world"))
        r = await ws.grep(GrepParams(pattern="zzz_nope"))
        assert r.success
        assert r.data["count"] == 0

    asyncio.run(go())


def test_grep_invalid_regex():
    ws, _, _ = _setup()

    async def go():
        r = await ws.grep(GrepParams(pattern="[invalid"))
        assert not r.success
        assert "regex" in r.error.lower()

    asyncio.run(go())


# ── Delete ────────────────────────────────────────────────────────────


def test_delete_existing():
    ws, _, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="tmp.py", content="x = 1"))
        r = await ws.delete(DeleteParams(path="tmp.py"))
        assert r.success
        assert r.data["deleted"] is True

        r2 = await ws.read(ReadParams(path="tmp.py"))
        assert not r2.success

    asyncio.run(go())


def test_delete_missing():
    ws, _, _ = _setup()

    async def go():
        r = await ws.delete(DeleteParams(path="nope.txt"))
        assert r.success
        assert r.data["deleted"] is False

    asyncio.run(go())


# ── List ──────────────────────────────────────────────────────────────


# ── Language detection ────────────────────────────────────────────────


def test_language_detection():
    ws, _, _ = _setup()

    async def go():
        cases = {
            "app.py": "python", "style.css": "css", "doc.tex": "latex",
            "readme.md": "markdown", "data.json": "json", "config.yaml": "yaml",
            "main.go": "go", "lib.rs": "rust", "index.html": "html",
            "unknown.xyz": "text",
        }
        for path, expected in cases.items():
            r = await ws.write(WriteParams(path=path, content="x"))
            assert r.data["language"] == expected, f"{path}: {r.data['language']} != {expected}"

    asyncio.run(go())


# ── Config & metadata ────────────────────────────────────────────────


def test_auto_detect_react():
    """First write of .tsx → render_mode auto-detected as 'react'."""
    ws, preview, bus = _setup()

    async def go():
        await ws.write(WriteParams(path="src/App.tsx", content="export default () => <h1/>"))
        state = preview._session().state
        assert "workspace" in state
        meta = state["workspace"]
        assert meta["render_mode"] == "react"
        assert meta["entry_file"] == "src/App.tsx"

    asyncio.run(go())


def test_auto_detect_latex():
    """First write of .tex → render_mode auto-detected as 'latex'."""
    ws, preview, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="main.tex", content="\\documentclass{article}"))
        meta = preview._session().state["workspace"]
        assert meta["render_mode"] == "latex"
        assert meta["entry_file"] == "main.tex"

    asyncio.run(go())


def test_explicit_config():
    """Explicit render_mode + entry_file from on_config_update."""
    ws, preview, _ = _setup()

    async def go():
        await ws.on_config_update({"render_mode": "slides", "entry_file": "deck/intro.md", "title": "My Deck"})
        await ws.write(WriteParams(path="deck/intro.md", content="# Slide 1"))
        meta = preview._session().state["workspace"]
        assert meta["render_mode"] == "slides"
        assert meta["entry_file"] == "deck/intro.md"
        assert meta["title"] == "My Deck"

    asyncio.run(go())


def test_meta_published_once():
    """Metadata is set in preview state only once (first write)."""
    ws, preview, _ = _setup()

    async def go():
        await ws.write(WriteParams(path="a.tsx", content="a"))
        meta1 = preview._session().state.get("workspace")
        assert meta1 is not None
        assert meta1["render_mode"] == "react"

        # Second write should NOT reset the metadata
        assert ws._meta_published is True
        await ws.write(WriteParams(path="b.tsx", content="b"))
        # Still the same metadata (not re-published)
        meta2 = preview._session().state["workspace"]
        assert meta2 == meta1

    asyncio.run(go())


def test_config_reset_republishes():
    """Calling on_config_update resets the flag so next write re-publishes."""
    ws, preview, bus = _setup()

    async def go():
        await ws.write(WriteParams(path="a.tsx", content="a"))
        assert preview._session().state["workspace"]["render_mode"] == "react"

        await ws.on_config_update({"render_mode": "latex", "entry_file": "main.tex"})
        await ws.write(WriteParams(path="main.tex", content="\\doc"))
        assert preview._session().state["workspace"]["render_mode"] == "latex"

    asyncio.run(go())


def test_default_entry_file_from_render_mode():
    """When entry_file is not set, it defaults based on render_mode."""
    ws, preview, _ = _setup()

    async def go():
        await ws.on_config_update({"render_mode": "html"})
        await ws.write(WriteParams(path="style.css", content="body{}"))
        meta = preview._session().state["workspace"]
        assert meta["render_mode"] == "html"
        assert meta["entry_file"] == "index.html"

    asyncio.run(go())


# ── Dynamic tool prompts ──────────────────────────────────────────────


def test_dynamic_prompts_default():
    """Without instructions, base prompts are returned."""
    ws, _, _ = _setup()
    prompts = ws.get_dynamic_tool_prompts()
    assert "workspace.write" in prompts
    assert "workspace.edit" in prompts
    assert "workspace.read" in prompts
    assert "workspace.glob" in prompts
    assert "workspace.grep" in prompts
    assert "workspace.delete" in prompts
    # Base prompt for write
    assert "Write a file" in prompts["workspace.write"]


def test_dynamic_prompts_with_instructions():
    """Global instructions are prepended to all tool prompts."""
    ws, _, _ = _setup()

    async def go():
        await ws.on_config_update({
            "render_mode": "latex",
            "instructions": "You write LaTeX documents. Main file is main.tex.",
        })
        prompts = ws.get_dynamic_tool_prompts()
        for fqn, prompt in prompts.items():
            assert "LaTeX documents" in prompt, f"{fqn} missing instructions"
        # Base prompt still present after instructions
        assert "Write a file" in prompts["workspace.write"]

    asyncio.run(go())


def test_dynamic_prompts_per_tool_override():
    """Per-tool instructions replace the base prompt."""
    ws, _, _ = _setup()

    async def go():
        await ws.on_config_update({
            "render_mode": "slides",
            "instructions": "You create slide decks.",
            "tool_instructions": {
                "write": "Create a new slide as slides/NN.md. Use # for title.",
            },
        })
        prompts = ws.get_dynamic_tool_prompts()
        # write got a per-tool override (not the base)
        assert "slides/NN.md" in prompts["workspace.write"]
        assert "Write a file" not in prompts["workspace.write"]
        # Global instructions still prepended
        assert "slide decks" in prompts["workspace.write"]
        # Other tools keep base + global instructions
        assert "slide decks" in prompts["workspace.edit"]
        assert "Read a file" in prompts["workspace.read"]

    asyncio.run(go())


def test_prompt_py_picks_up_dynamic_prompts():
    """build_system_prompt uses get_dynamic_tool_prompts from modules."""
    from digitorn.modules.context_builder.prompt import build_system_prompt
    from digitorn.modules.context_builder.types import IndexedTool, ToolIndex

    # Create a minimal index with a workspace.write tool (empty static prompt)
    tool = IndexedTool(
        fqn="workspace.write", module_id="workspace", action_name="write",
        description="Write a file.", risk_level="low", tags=[], params_schema={},
        examples=[], module=None, policy_decision="auto",
        tool_prompt="",  # empty - dynamic should fill it
    )
    index = ToolIndex(tools={"workspace.write": tool})

    # A fake module that provides dynamic prompts
    class _FakeMod:
        def get_dynamic_tool_prompts(self):
            return {"workspace.write": "You build React apps with TypeScript."}

    prompt = build_system_prompt(
        agent_id="main", role="worker", user_prompt="",
        index=index, modules={"workspace": _FakeMod()},
    )
    assert "React apps with TypeScript" in prompt


# ── Error: no preview ─────────────────────────────────────────────────


def test_requires_preview():
    ws = WorkspaceModule()

    async def go():
        with pytest.raises(RuntimeError, match="preview"):
            await ws.write(WriteParams(path="x.txt", content="x"))

    asyncio.run(go())
