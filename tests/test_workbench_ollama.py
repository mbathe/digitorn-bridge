"""End-to-end workbench tests with local Ollama models.

Tests verify that local LLMs (qwen2.5:14b, mistral-nemo, qwen2.5-coder:7b)
can use the workbench toolchain correctly:
1. wb_write - create buffers
2. wb_edit - surgical edits (not regeneration)
3. wb_append - incremental building
4. wb_use - send buffer to another tool
5. wb_snapshot/wb_restore - versioning
6. Multi-turn: modify existing workbench content

Run with:
    python -m pytest tests/test_workbench_ollama.py -v -s
    python -m pytest tests/test_workbench_ollama.py -v -s -k "qwen14b"
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from digitorn.core.app.compiler import AppYAMLCompiler
from digitorn.core.loader import load_modules
from digitorn.core.runtime.agent_loop import agent_turn
from digitorn.core.runtime.bootstrap import bootstrap
from digitorn.modules.registry import ModuleRegistry


# ---------------------------------------------------------------------------
# Skip if Ollama not running
# ---------------------------------------------------------------------------

def _ollama_available() -> bool:
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/tags", timeout=3)
        return r.status_code == 200 and len(r.json().get("models", [])) > 0
    except Exception:
        return False


OLLAMA_UP = _ollama_available()
pytestmark = pytest.mark.skipif(not OLLAMA_UP, reason="Ollama not running")


# Models to test - best first
MODELS = [
    pytest.param("qwen2.5:14b-instruct-q4_K_M", id="qwen14b"),
    pytest.param("mistral-nemo:latest", id="mistral-nemo"),
    pytest.param("qwen2.5-coder:7b", id="qwen-coder-7b"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _boot(yaml_content: str, tmp_path: Path) -> dict[str, Any]:
    yaml_path = tmp_path / "app.yaml"
    yaml_path.write_text(yaml_content)
    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)
    return await bootstrap(compiled, registry)


async def _run(ctx, message: str, max_turns: int = 10) -> Any:
    messages = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": message},
    ]
    return await agent_turn(ctx, messages, max_turns=max_turns, timeout=180.0)


def _ctx(boot: dict, agent_id: str = "assistant"):
    return boot["contexts"][agent_id]


def _tool_names(result) -> set[str]:
    return {tc.name for tc in result.tool_calls}


def _wb_tools_used(result) -> set[str]:
    return {tc.name for tc in result.tool_calls if tc.name.startswith("wb_")}


def _tool_call_count(result, name: str) -> int:
    return sum(1 for tc in result.tool_calls if tc.name == name)


# ---------------------------------------------------------------------------
# YAML builders
# ---------------------------------------------------------------------------

def _wb_app(
    model: str,
    tmp_path: str,
    extra_modules: str = "hello: {}",
    native_tool_use: bool | None = None,
) -> str:
    ntu_line = ""
    if native_tool_use is not None:
        ntu_line = f"\n              native_tool_use: {'true' if native_tool_use else 'false'}"
    return textwrap.dedent(f"""\
        app:
          app_id: wb-ollama-test
          name: "Workbench Ollama Test"

        modules:
          {extra_modules}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: ollama
              model: "{model}"
              backend: openai_compat{ntu_line}
              config:
                base_url: "http://localhost:11434/v1"
                api_key: "ollama"
              context:
                max_tokens: 8192
            system_prompt: |
              You are a document builder assistant.
              You MUST use workbench tools to build content. The available tools are:
              - wb_write(key, content): Create or overwrite a buffer
              - wb_append(key, content): Append to an existing buffer
              - wb_edit(key, old, new): Replace text in a buffer
              - wb_read(key): Read buffer content
              - wb_overview(): List all buffers
              - wb_use(buffer, tool, param): Send buffer content to another tool
              - wb_snapshot(key, name): Save a named version
              - wb_restore(key, name): Restore a saved version

              RULES:
              - ALWAYS use wb_write first to create content in a buffer
              - NEVER output document content directly in your response
              - Use wb_edit for surgical fixes, NOT wb_write to regenerate
              - When asked to modify existing content, use wb_edit

        execution:
          mode: one_shot
          max_turns: 15
          workspace: "{tmp_path}"
          workbench: true
          workbench_reflection: true
          workbench_error_memory: true

        capabilities:
          default_policy: auto
    """)


def _wb_app_fs(model: str, tmp_path: str, native_tool_use: bool | None = None) -> str:
    return _wb_app(
        model, tmp_path,
        extra_modules=f'filesystem: {{}}',
        native_tool_use=native_tool_use,
    )


# ===========================================================================
# 1. BASIC: Can the model call wb_write at all?
# ===========================================================================

class TestOllamaBasicWorkbench:
    """Most fundamental test: does the LLM call wb_write?"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_wb_write_called(self, tmp_path, model):
        """Agent should call wb_write when explicitly asked."""
        # Force native tool calling for models that support it
        ntu = True if "qwen" in model else None
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=ntu), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Use the wb_write tool to create a buffer named 'greeting' "
            "with the content 'Hello, World!'.",
            max_turns=5,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, (
            f"[{model}] Agent should call wb_write. "
            f"Tools called: {_tool_names(result)}. "
            f"Response: {result.content[:200] if result.content else '(no content)'}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_wb_read_after_write(self, tmp_path, model):
        """Agent should write then read a buffer."""
        ntu = True if "qwen" in model else None
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=ntu), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Step 1: Call wb_write with key='data' and content='name: Alice\\nage: 30'.\n"
            "Step 2: Call wb_read with key='data' to show the content.",
            max_turns=6,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"[{model}] Missing wb_write: {_tool_names(result)}"
        assert "wb_read" in wb_used, f"[{model}] Missing wb_read: {_tool_names(result)}"


# ===========================================================================
# 2. EDIT: Surgical edits instead of regeneration
# ===========================================================================

class TestOllamaWorkbenchEdit:
    """Test that the model can do surgical edits."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_wb_edit_typo_fix(self, tmp_path, model):
        """Agent should fix a typo with wb_edit, not rewrite."""
        ntu = True if "qwen" in model else None
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=ntu), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Step 1: Call wb_write with key='doc' and content='The quik brown fox jumps over the lazy dog.'\n"
            "Step 2: Call wb_edit with key='doc', old='quik', new='quick' to fix the typo.\n"
            "Step 3: Call wb_read with key='doc' to verify the fix.",
            max_turns=8,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"[{model}] Missing wb_write"
        assert "wb_edit" in wb_used, f"[{model}] Should use wb_edit for typo fix: {_tool_names(result)}"

        # Verify only 1 wb_write (no regeneration)
        write_count = _tool_call_count(result, "wb_write")
        assert write_count <= 1, (
            f"[{model}] Should NOT regenerate - got {write_count} wb_write calls"
        )


# ===========================================================================
# 3. APPEND: Incremental building
# ===========================================================================

class TestOllamaWorkbenchAppend:
    """Test incremental content building with wb_append."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_wb_append_sections(self, tmp_path, model):
        """Agent builds a document section by section."""
        ntu = True if "qwen" in model else None
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=ntu), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Build a document in 3 steps:\n"
            "Step 1: Call wb_write with key='report' and content='# Report\\n\\n## Introduction\\nThis is the intro.'\n"
            "Step 2: Call wb_append with key='report' and content='\\n## Analysis\\nThis is the analysis section.'\n"
            "Step 3: Call wb_append with key='report' and content='\\n## Conclusion\\nThis is the conclusion.'\n"
            "Then call wb_read with key='report' to show the full document.",
            max_turns=10,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"[{model}] Missing wb_write"
        assert "wb_append" in wb_used, f"[{model}] Missing wb_append: {_tool_names(result)}"

        append_count = _tool_call_count(result, "wb_append")
        assert append_count >= 2, (
            f"[{model}] Should append at least 2 times, got {append_count}"
        )


# ===========================================================================
# 4. WB_USE: Send buffer to another tool
# ===========================================================================

class TestOllamaWorkbenchUse:
    """Test wb_use to pipe buffer content to filesystem."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [
        pytest.param("qwen2.5-coder:7b", id="qwen-coder-7b"),
    ])
    async def test_wb_use_to_filesystem(self, tmp_path, model):
        """Agent writes to workbench then sends to filesystem via wb_use."""
        boot = await _boot(_wb_app_fs(model, str(tmp_path), native_tool_use=True), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            f"Step 1: Call wb_write with key='script' and content='print(\"hello\")'\n"
            f"Step 2: Call wb_use with buffer='script', tool='filesystem.write', "
            f"param='content', extra={{\"path\": \"{tmp_path}/hello.py\"}}\n"
            f"This will write the buffer content to a file.",
            max_turns=10,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"[{model}] Missing wb_write"
        assert "wb_use" in wb_used, (
            f"[{model}] Should use wb_use to send to filesystem. Used: {_tool_names(result)}"
        )


# ===========================================================================
# 5. SNAPSHOT: Versioning
# ===========================================================================

class TestOllamaWorkbenchSnapshot:
    """Test snapshot/restore versioning."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [
        pytest.param("qwen2.5-coder:7b", id="qwen-coder-7b"),
    ])
    async def test_snapshot_and_restore(self, tmp_path, model):
        """Agent creates a snapshot, modifies, then restores."""
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=True), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Follow these exact steps:\n"
            "Step 1: Call wb_write with key='doc' and content='Version 1 content'\n"
            "Step 2: Call wb_snapshot with key='doc' and name='v1'\n"
            "Step 3: Call wb_edit with key='doc', old='Version 1', new='Version 2'\n"
            "Step 4: Call wb_restore with key='doc' and name='v1'\n"
            "Step 5: Call wb_read with key='doc' to verify it shows 'Version 1 content'",
            max_turns=10,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"[{model}] Missing wb_write"
        assert "wb_snapshot" in wb_used, f"[{model}] Missing wb_snapshot: {_tool_names(result)}"

        # At least snapshot should be called
        snap_count = _tool_call_count(result, "wb_snapshot")
        assert snap_count >= 1, f"[{model}] Should call wb_snapshot at least once"


# ===========================================================================
# 6. MULTI-TURN: Modify existing content across turns
# ===========================================================================

class TestOllamaMultiTurn:
    """Test that agent modifies existing workbench content in turn 2."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [
        pytest.param("qwen2.5-coder:7b", id="qwen-coder-7b"),
    ])
    async def test_edit_across_turns(self, tmp_path, model):
        """Turn 1: create buffer. Turn 2: edit it (not regenerate)."""
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=True), tmp_path)
        ctx = _ctx(boot)

        # ── Turn 1: Create content ──
        result1 = await _run(
            ctx,
            "Call wb_write with key='config' and content='{\"host\": \"localhost\", \"port\": 3000}'",
            max_turns=5,
        )
        wb_used_t1 = _wb_tools_used(result1)
        assert "wb_write" in wb_used_t1, f"[{model}] Turn 1 should use wb_write"

        # ── Turn 2: Edit existing ──
        messages_t2 = [
            {"role": "system", "content": ctx.system_prompt},
            {"role": "user", "content": "Create a config in the workbench"},
            {"role": "assistant", "content": result1.content or "Done, buffer 'config' created."},
            {"role": "user", "content": (
                "The config buffer 'config' already exists in the workbench. "
                "Use wb_edit to change the port from 3000 to 8080. "
                "Do NOT use wb_write - the buffer already exists, just edit it."
            )},
        ]

        result2 = await agent_turn(ctx, messages_t2, max_turns=8, timeout=180.0)
        wb_used_t2 = _wb_tools_used(result2)
        all_tools_t2 = _tool_names(result2)

        assert "wb_edit" in wb_used_t2, (
            f"[{model}] Turn 2 should use wb_edit. Used: {all_tools_t2}"
        )

        write_count_t2 = _tool_call_count(result2, "wb_write")
        assert write_count_t2 == 0, (
            f"[{model}] Turn 2 should NOT regenerate with wb_write. "
            f"wb_write called {write_count_t2} times"
        )


# ===========================================================================
# 7. COMPARISON: Model ranking
# ===========================================================================

class TestOllamaModelComparison:
    """Run the same task across all models and report success rate."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", MODELS)
    async def test_three_step_workflow(self, tmp_path, model):
        """Standard workflow: write → edit → read. Reports which models pass."""
        ntu = True if "qwen" in model else None
        boot = await _boot(_wb_app(model, str(tmp_path), native_tool_use=ntu), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Do exactly these 3 tool calls in order:\n"
            "1. wb_write(key='test', content='Hello World')\n"
            "2. wb_edit(key='test', old='World', new='Ollama')\n"
            "3. wb_read(key='test')\n"
            "Do nothing else.",
            max_turns=8,
        )
        wb_used = _wb_tools_used(result)

        # Score: how many of the 3 steps did the model complete?
        steps = 0
        if "wb_write" in wb_used:
            steps += 1
        if "wb_edit" in wb_used:
            steps += 1
        if "wb_read" in wb_used:
            steps += 1

        print(f"\n{'='*60}")
        print(f"MODEL: {model}")
        print(f"SCORE: {steps}/3")
        print(f"Tools used: {_tool_names(result)}")
        print(f"WB tools: {wb_used}")
        if result.content:
            print(f"Response: {result.content[:300]}")
        print(f"{'='*60}\n")

        # At minimum, the model should call wb_write
        assert "wb_write" in wb_used, (
            f"[{model}] Cannot even call wb_write - score {steps}/3"
        )


# ===========================================================================
# 8. NATIVE vs TEXT-BASED: Compare tool calling modes
# ===========================================================================

class TestNativeToolUseOverride:
    """Test the native_tool_use YAML override for Ollama models."""

    @pytest.mark.asyncio
    async def test_native_mode_qwen_coder(self, tmp_path):
        """qwen2.5-coder with native_tool_use: true should use native tool calls."""
        model = "qwen2.5-coder:7b"
        boot = await _boot(
            _wb_app(model, str(tmp_path), native_tool_use=True), tmp_path,
        )
        ctx = _ctx(boot)

        # Verify the override was applied
        assert ctx.native_tool_use is True, "native_tool_use should be True from YAML override"

        result = await _run(
            ctx,
            "Call wb_write with key='test' and content='native mode works'.",
            max_turns=5,
        )
        wb_used = _wb_tools_used(result)
        print(f"\n[NATIVE MODE] Tools: {_tool_names(result)}, WB: {wb_used}")
        assert "wb_write" in wb_used, (
            f"[native mode] Agent should call wb_write. Tools: {_tool_names(result)}"
        )

    @pytest.mark.asyncio
    async def test_text_based_mode_override(self, tmp_path):
        """native_tool_use: false forces text-based mode even if model supports native."""
        model = "qwen2.5-coder:7b"
        boot = await _boot(
            _wb_app(model, str(tmp_path), native_tool_use=False), tmp_path,
        )
        ctx = _ctx(boot)
        assert ctx.native_tool_use is False, "native_tool_use should be False from YAML override"

        # In text-based mode, tools are NOT passed as API tools but in the system prompt
        # The system prompt should contain tool descriptions as text
        assert "wb_write" in ctx.system_prompt, "Text-based mode should embed tools in prompt"

    @pytest.mark.asyncio
    async def test_default_mode_is_text_based(self, tmp_path):
        """Without override, Ollama should default to text-based (native_tool_use=False)."""
        model = "qwen2.5-coder:7b"
        boot = await _boot(
            _wb_app(model, str(tmp_path)), tmp_path,  # No override
        )
        ctx = _ctx(boot)
        assert ctx.native_tool_use is False, (
            "Ollama should default to text-based tool calling"
        )

    @pytest.mark.asyncio
    async def test_native_three_step_workflow(self, tmp_path):
        """Full write→edit→read with native_tool_use: true on qwen-coder."""
        model = "qwen2.5-coder:7b"
        boot = await _boot(
            _wb_app(model, str(tmp_path), native_tool_use=True), tmp_path,
        )
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Do exactly these 3 tool calls in order:\n"
            "1. wb_write(key='test', content='Hello World')\n"
            "2. wb_edit(key='test', old='World', new='Ollama')\n"
            "3. wb_read(key='test')\n"
            "Do nothing else.",
            max_turns=8,
        )
        wb_used = _wb_tools_used(result)

        steps = 0
        if "wb_write" in wb_used: steps += 1
        if "wb_edit" in wb_used: steps += 1
        if "wb_read" in wb_used: steps += 1

        print(f"\n[NATIVE 3-STEP] Score: {steps}/3, Tools: {_tool_names(result)}")

        assert "wb_write" in wb_used, f"Score {steps}/3 - at least wb_write expected"
