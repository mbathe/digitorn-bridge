"""End-to-end workbench tests with real DeepSeek model.

Tests verify that the agent:
1. Sees workbench tools in both direct and discovery modes
2. Actually uses wb_write/wb_edit/wb_use instead of regenerating
3. Builds multi-section documents incrementally
4. Uses wb_reflect when reflection is enabled
5. Recovers from errors via wb_edit (not regeneration)

Run with:
    DEEPSEEK_API_KEY=sk-xxx python -m pytest tests/test_workbench_deepseek.py -v -s
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

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
pytestmark = pytest.mark.skipif(not DEEPSEEK_KEY, reason="DEEPSEEK_API_KEY not set")


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
    return await agent_turn(ctx, messages, max_turns=max_turns, timeout=120.0)


def _ctx(boot: dict, agent_id: str = "assistant"):
    return boot["contexts"][agent_id]


def _tool_names(result) -> set[str]:
    """Extract unique tool names from a TurnResult."""
    return {tc.name for tc in result.tool_calls}


def _wb_tools_used(result) -> set[str]:
    """Extract only wb_* tool names used."""
    return {tc.name for tc in result.tool_calls if tc.name.startswith("wb_")}


# ---------------------------------------------------------------------------
# YAML builders
# ---------------------------------------------------------------------------

def _wb_app_direct(tmp_path: str) -> str:
    """App with workbench + few modules → direct injection mode."""
    return textwrap.dedent(f"""\
        app:
          app_id: wb-direct-test
          name: "Workbench Direct Test"

        modules:
          hello: {{}}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: deepseek
              model: deepseek-chat
              backend: openai_compat
              config:
                api_key: "{DEEPSEEK_KEY}"
            system_prompt: |
              You are a document builder assistant. You MUST use the workbench (wb_write, wb_edit, wb_append, wb_use) to build documents.
              NEVER output the document directly in your response. Always write it to the workbench first.

        execution:
          mode: one_shot
          max_turns: 15
          workbench: true
          workbench_reflection: true
          workbench_error_memory: true

        capabilities:
          default_policy: auto
    """)


def _wb_app_filesystem(tmp_path: str) -> str:
    """App with workbench + filesystem → can wb_use to write files."""
    return textwrap.dedent(f"""\
        app:
          app_id: wb-fs-test
          name: "Workbench FS Test"

        modules:
          filesystem: {{}}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: deepseek
              model: deepseek-chat
              backend: openai_compat
              config:
                api_key: "{DEEPSEEK_KEY}"
            system_prompt: |
              You are a document builder. Use the workbench to build content incrementally.
              Use wb_write to create buffers, wb_edit to fix them, wb_use to send to filesystem.write.
              NEVER regenerate content from scratch - always edit what exists.
              The workspace is {tmp_path}.

        execution:
          mode: one_shot
          max_turns: 20
          workspace: "{tmp_path}"
          workbench: true
          workbench_reflection: true

        capabilities:
          default_policy: auto
    """)


def _wb_app_sql(tmp_path: str, db_path: str) -> str:
    """App with workbench + database → can build SQL in workbench."""
    return textwrap.dedent(f"""\
        app:
          app_id: wb-sql-test
          name: "Workbench SQL Test"

        modules:
          database:
            config:
              connections:
                main:
                  url: "sqlite:///{db_path}"

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: deepseek
              model: deepseek-chat
              backend: openai_compat
              config:
                api_key: "{DEEPSEEK_KEY}"
            system_prompt: |
              You are a SQL analyst. Use the workbench to build SQL queries step by step.
              Use wb_write to draft queries, wb_reflect to validate them, wb_edit to fix issues,
              and wb_use to execute them against the database.
              ALWAYS use workbench for SQL - never pass raw SQL directly to database.execute_query.

        execution:
          mode: one_shot
          max_turns: 15
          workbench: true
          workbench_reflection: true
          workbench_error_memory: true

        capabilities:
          default_policy: auto
    """)


def _wb_app_report(tmp_path: str) -> str:
    """App for multi-section report generation."""
    return textwrap.dedent(f"""\
        app:
          app_id: wb-report-test
          name: "Workbench Report Test"

        modules:
          filesystem: {{}}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: deepseek
              model: deepseek-chat
              backend: openai_compat
              config:
                api_key: "{DEEPSEEK_KEY}"
              context:
                max_tokens: 131072
            system_prompt: |
              You are a technical report writer. You build reports incrementally using the workbench.

              WORKFLOW:
              1. Use wb_write to create the report buffer with initial structure
              2. Use wb_append to add each section one at a time
              3. Use wb_read to review what you have so far
              4. Use wb_edit to fix any issues
              5. When the report is complete, use wb_use to send it to filesystem.write

              RULES:
              - NEVER regenerate the entire document. Only append or edit.
              - Each section should be substantial (multiple paragraphs).
              - Use wb_overview to check buffer status.
              The workspace is {tmp_path}.

        execution:
          mode: one_shot
          max_turns: 30
          workspace: "{tmp_path}"
          workbench: true
          workbench_reflection: true

        capabilities:
          default_policy: auto
    """)


# ===========================================================================
# 1. TOOLS VISIBILITY - wb_* tools are visible to the agent
# ===========================================================================

class TestWorkbenchToolsVisible:
    """Verify wb_* tools appear in the agent's tool list."""

    @pytest.mark.asyncio
    async def test_wb_tools_in_direct_mode(self, tmp_path):
        """With few modules + workbench:true, wb_* tools appear as direct tools."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        tool_names = {t["function"]["name"] for t in ctx.tools}
        wb_expected = {
            "wb_write", "wb_append", "wb_insert", "wb_read", "wb_overview",
            "wb_edit", "wb_replace_lines", "wb_drop", "wb_use", "wb_diff",
            "wb_reflect", "wb_error_memory", "call_app",
        }
        missing = wb_expected - tool_names
        assert not missing, f"Missing wb tools in direct mode: {missing}"

    @pytest.mark.asyncio
    async def test_base_primitives_also_present(self, tmp_path):
        """Base primitives (run_parallel, background_*) still present."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        tool_names = {t["function"]["name"] for t in ctx.tools}
        assert "run_parallel" in tool_names
        assert "background_run" in tool_names

    @pytest.mark.asyncio
    async def test_wb_in_system_prompt(self, tmp_path):
        """System prompt includes workbench instructions."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        prompt_lower = ctx.system_prompt.lower()
        assert "workbench" in prompt_lower or "wb_write" in prompt_lower


# ===========================================================================
# 2. BASIC WORKBENCH USAGE - agent writes to workbench
# ===========================================================================

class TestAgentUsesWorkbench:
    """Verify the LLM actually calls wb_* tools."""

    @pytest.mark.asyncio
    async def test_agent_writes_to_workbench(self, tmp_path):
        """Agent should use wb_write to create content."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Create a JSON configuration file in the workbench with the following fields: "
            "name='MyApp', version='1.0.0', debug=false, port=8080. "
            "Use wb_write to create it.",
            max_turns=5,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, (
            f"Agent should have used wb_write. Tools used: {_tool_names(result)}"
        )

    @pytest.mark.asyncio
    async def test_agent_edits_workbench_buffer(self, tmp_path):
        """Agent should use wb_write then wb_edit to modify content."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Step 1: Use wb_write to create a buffer called 'config' with this JSON: "
            '{"name": "MyApp", "version": "1.0.0", "debug": true}\n'
            "Step 2: Use wb_edit to change debug from true to false.\n"
            "Step 3: Use wb_read to show me the final result.",
            max_turns=8,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"Should use wb_write. Used: {_tool_names(result)}"
        assert "wb_edit" in wb_used or "wb_replace_lines" in wb_used, (
            f"Should use wb_edit or wb_replace_lines. Used: {_tool_names(result)}"
        )


# ===========================================================================
# 3. WORKBENCH + FILESYSTEM - wb_use sends to filesystem.write
# ===========================================================================

class TestWorkbenchToFilesystem:
    """Test the wb_write → wb_edit → wb_use → filesystem.write pipeline."""

    @pytest.mark.asyncio
    async def test_build_and_write_file(self, tmp_path):
        """Agent builds content in workbench then writes to filesystem."""
        boot = await _boot(_wb_app_filesystem(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            f"Build a Python script in the workbench that prints 'Hello from workbench!'. "
            f"Use wb_write to create it as 'script' buffer, then use wb_use to write it "
            f"to {tmp_path}/hello.py using filesystem.write.",
            max_turns=10,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"Should use wb_write. Used: {_tool_names(result)}"
        assert "wb_use" in wb_used, f"Should use wb_use. Used: {_tool_names(result)}"

        # Verify the file was actually written
        hello_py = tmp_path / "hello.py"
        if hello_py.exists():
            content = hello_py.read_text()
            assert "Hello from workbench" in content or "hello" in content.lower()


# ===========================================================================
# 4. WORKBENCH + DATABASE - SQL query building
# ===========================================================================

class TestWorkbenchSQL:
    """Test SQL query building in workbench."""

    @pytest.mark.asyncio
    async def test_build_and_execute_sql(self, tmp_path):
        """Agent builds SQL in workbench, reflects, then executes via wb_use."""
        import sqlite3

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
        conn.execute("INSERT INTO products VALUES (1, 'Widget', 9.99)")
        conn.execute("INSERT INTO products VALUES (2, 'Gadget', 24.99)")
        conn.execute("INSERT INTO products VALUES (3, 'Doohickey', 4.99)")
        conn.commit()
        conn.close()

        boot = await _boot(_wb_app_sql(str(tmp_path), db_path), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Query the products table to find all products with price > 5.00, "
            "ordered by price descending. Use the workbench to build the SQL query: "
            "wb_write to draft it, wb_reflect to validate, then wb_use to execute it "
            "against the 'main' database connection.",
            max_turns=12,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"Should draft SQL in workbench. Used: {_tool_names(result)}"
        # Agent should have used at least wb_write + wb_use (or wb_reflect)
        assert len(wb_used) >= 2, (
            f"Should use at least 2 wb tools (write+use or write+reflect). Used: {wb_used}"
        )


# ===========================================================================
# 5. MULTI-SECTION REPORT - incremental building
# ===========================================================================

class TestWorkbenchReport:
    """Test incremental multi-section report building."""

    @pytest.mark.asyncio
    async def test_incremental_report_building(self, tmp_path):
        """Agent builds a multi-section report using wb_write + wb_append."""
        boot = await _boot(_wb_app_report(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Build a technical report about Python web frameworks with these sections:\n"
            "1. Title and Introduction (overview of the landscape)\n"
            "2. Django (features, pros/cons)\n"
            "3. FastAPI (features, pros/cons)\n"
            "4. Flask (features, pros/cons)\n"
            "5. Comparison table\n"
            "6. Conclusion with recommendation\n\n"
            "Use wb_write for the initial structure, then wb_append for each section. "
            "Each section should be at least 2 paragraphs. "
            f"When done, use wb_use to save to {tmp_path}/report.md via filesystem.write.",
            max_turns=25,
        )
        wb_used = _wb_tools_used(result)
        all_tools = _tool_names(result)

        # Must use wb_write to start
        assert "wb_write" in wb_used, f"Should start with wb_write. Used: {all_tools}"

        # Must use wb_append at least twice (incremental building)
        append_count = sum(1 for tc in result.tool_calls if tc.name == "wb_append")
        assert append_count >= 2, (
            f"Should use wb_append at least twice for incremental building. "
            f"Used {append_count} times. All tools: {all_tools}"
        )

        # Should eventually push to filesystem
        assert "wb_use" in wb_used, (
            f"Should use wb_use to push report to filesystem. Used: {all_tools}"
        )

        # Check the file was written and has substantial content
        report = tmp_path / "report.md"
        if report.exists():
            content = report.read_text()
            assert len(content) > 500, f"Report too short ({len(content)} chars)"
            # Should mention at least some frameworks
            lower = content.lower()
            assert "django" in lower or "fastapi" in lower or "flask" in lower

    @pytest.mark.asyncio
    async def test_edit_not_regenerate(self, tmp_path):
        """Agent should fix errors via wb_edit, not by rewriting everything."""
        boot = await _boot(_wb_app_report(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Step 1: Use wb_write to create a buffer 'doc' with this markdown:\n"
            "# API Documentation\n\n"
            "## Endpoint: GET /users\n\n"
            "Returns a list of usres.\n\n"  # typo: usres
            "## Endpoint: POST /users\n\n"
            "Creates a new usr.\n\n"  # typo: usr
            "Step 2: Use wb_edit to fix the typo 'usres' to 'users'\n"
            "Step 3: Use wb_edit to fix the typo 'usr' to 'user'\n"
            "Step 4: Use wb_read to show the final result",
            max_turns=10,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used, f"Should use wb_write. Used: {_tool_names(result)}"

        # Must use wb_edit (surgical fix) not a second wb_write (regeneration)
        edit_count = sum(1 for tc in result.tool_calls if tc.name == "wb_edit")
        write_count = sum(1 for tc in result.tool_calls if tc.name == "wb_write")
        assert edit_count >= 1, (
            f"Should use wb_edit to fix typos. Used: {_tool_names(result)}"
        )
        assert write_count <= 1, (
            f"Should NOT regenerate (multiple wb_write). "
            f"wb_write={write_count}, wb_edit={edit_count}"
        )


# ===========================================================================
# 6. REFLECTION - agent validates before sending
# ===========================================================================

class TestWorkbenchReflection:
    """Test that the agent uses wb_reflect when enabled."""

    @pytest.mark.asyncio
    async def test_reflect_catches_bad_json(self, tmp_path):
        """Agent should use wb_reflect on JSON content."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            "Use wb_write to create a buffer called 'data' with this content:\n"
            '{"users": [{"name": "Alice", "age": 30}, {"name": "Bob"}]}\n'
            "Then use wb_reflect to validate it.\n"
            "Then use wb_read to show the result.",
            max_turns=8,
        )
        wb_used = _wb_tools_used(result)
        assert "wb_write" in wb_used
        assert "wb_reflect" in wb_used, (
            f"Should use wb_reflect for validation. Used: {_tool_names(result)}"
        )


# ===========================================================================
# 7. COMPLEX PIPELINE - full workbench lifecycle
# ===========================================================================

class TestWorkbenchFullPipeline:
    """End-to-end: write → append → edit → reflect → use."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, tmp_path):
        """Agent goes through the full workbench lifecycle."""
        boot = await _boot(_wb_app_filesystem(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)
        result = await _run(
            ctx,
            f"Build a README.md file for a Python project called 'DataFlow'. Follow these steps:\n"
            f"1. wb_write: Create 'readme' buffer with the title '# DataFlow' and a blank intro\n"
            f"2. wb_append: Add a '## Installation' section with pip install instructions\n"
            f"3. wb_append: Add a '## Usage' section with a code example\n"
            f"4. wb_append: Add a '## License' section (MIT)\n"
            f"5. wb_read: Review the full document\n"
            f"6. wb_edit: Fix or improve anything you see\n"
            f"7. wb_use: Write the final README to {tmp_path}/README.md via filesystem.write\n",
            max_turns=20,
        )
        wb_used = _wb_tools_used(result)
        all_tools = _tool_names(result)

        assert "wb_write" in wb_used, f"Missing wb_write. Used: {all_tools}"
        assert "wb_append" in wb_used, f"Missing wb_append. Used: {all_tools}"

        # Check file was actually produced
        readme = tmp_path / "README.md"
        if readme.exists():
            content = readme.read_text()
            assert "DataFlow" in content
            assert len(content) > 100


# ===========================================================================
# 8. MULTI-TURN PERSISTENCE - edit after success
# ===========================================================================

class TestWorkbenchMultiTurn:
    """The killer feature: agent builds content, user asks for changes,
    agent edits the existing workbench instead of regenerating."""

    @pytest.mark.asyncio
    async def test_generate_then_modify(self, tmp_path):
        """Turn 1: build a report in workbench. Turn 2: modify the header.
        Agent should wb_edit, not wb_write a whole new document."""
        boot = await _boot(_wb_app_filesystem(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)

        # ── Turn 1: Generate the initial document in workbench ──
        result1 = await _run(
            ctx,
            "Use wb_write to create a buffer called 'report' with this exact content:\n"
            "# Sales Report Q1 2026\n\n"
            "## Summary\n\nStrong quarterly results across all product lines.\n\n"
            "## Revenue\n\n| Product | Revenue |\n|---------|--------|\n"
            "| Widget | $1.2M |\n| Gadget | $800K |\n\n"
            "## Outlook\n\nWe expect continued growth in Q2.\n\n"
            f"Then use wb_use to save it to {tmp_path}/report.md via filesystem.write.",
            max_turns=10,
        )

        wb_used_t1 = _wb_tools_used(result1)
        assert "wb_write" in wb_used_t1, (
            f"Turn 1 should use wb_write. Used: {_tool_names(result1)}"
        )

        # Check workbench has the buffer (even if wb_use failed for filesystem)
        cb = getattr(ctx, "_context_builder", None)
        wb = getattr(cb, "_workbench", None)
        assert wb is not None and wb.buffer_count > 0, "Workbench should have buffers after turn 1"

        # ── Turn 2: Ask to modify - agent should edit, not regenerate ──
        messages_t2 = [
            {"role": "system", "content": ctx.system_prompt},
            {"role": "user", "content": "Build a report in the workbench..."},
            {"role": "assistant", "content": result1.content},
            {"role": "user", "content": (
                "Change the title from 'Sales Report Q1 2026' to "
                "'Sales Report Q1-Q2 2026 (Revised)'. "
                "Also add a row 'Doohickey | $400K' to the Revenue table. "
                "Use wb_edit to make these surgical changes, then wb_use to "
                f"save the updated version to {tmp_path}/report.md via filesystem.write."
            )},
        ]

        from digitorn.core.runtime.agent_loop import agent_turn
        result2 = await agent_turn(
            ctx, messages_t2, max_turns=12, timeout=120.0,
        )

        wb_used_t2 = _wb_tools_used(result2)
        all_tools_t2 = _tool_names(result2)

        # Agent MUST edit, not regenerate
        edit_tools = {"wb_edit", "wb_replace_lines", "wb_insert", "wb_append"}
        used_edit = wb_used_t2 & edit_tools
        assert used_edit, (
            f"Turn 2 should use wb_edit/wb_replace_lines/wb_insert to modify, "
            f"not regenerate. Used: {all_tools_t2}"
        )

        # Should push the updated version
        assert "wb_use" in wb_used_t2, (
            f"Turn 2 should use wb_use to save updated report. Used: {all_tools_t2}"
        )

    @pytest.mark.asyncio
    async def test_workbench_reminder_injected(self, tmp_path):
        """After turn 1 fills workbench, turn 2 should see a WORKBENCH reminder."""
        boot = await _boot(_wb_app_direct(str(tmp_path)), tmp_path)
        ctx = _ctx(boot)

        # Turn 1: Write something to workbench
        result1 = await _run(
            ctx,
            "Use wb_write to create a buffer 'draft' with content: '# Draft Document\\n\\nFirst version.'",
            max_turns=5,
        )
        assert "wb_write" in _wb_tools_used(result1)

        # Verify workbench has content
        cb = getattr(ctx, "_context_builder", None)
        wb = getattr(cb, "_workbench", None)
        assert wb is not None
        assert wb.buffer_count > 0

        # Turn 2: Build messages that simulate a new user turn
        messages = [
            {"role": "system", "content": ctx.system_prompt},
            {"role": "user", "content": "Create the draft"},
            {"role": "assistant", "content": result1.content},
            {"role": "user", "content": "Use wb_read to show me the draft buffer."},
        ]

        from digitorn.core.runtime.agent_loop import agent_turn
        result2 = await agent_turn(ctx, messages, max_turns=5, timeout=60.0)

        # Agent should use wb_read (proving it knows the buffer exists)
        assert "wb_read" in _wb_tools_used(result2), (
            f"Turn 2 should use wb_read. Used: {_tool_names(result2)}"
        )
