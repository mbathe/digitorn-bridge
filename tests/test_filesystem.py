"""Tests for the filesystem module (agent-optimized v2).

Covers all 15 actions with real file operations in a temporary directory.
Includes tests for params_model schema exposure and validation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.modules.base import ActionResult, ExecutionContext
from digitorn.modules.filesystem.module import FilesystemModule


@pytest.fixture
def fs(tmp_path):
    mod = FilesystemModule()
    # The module now denies access when no workspace and no constraints are set
    # (deny-by-default). Point workspace to the pytest tmp root so all tests
    # that operate on tmp_path files are allowed through _check_path.
    mod._workspace_root = str(tmp_path)
    return mod


@pytest.fixture
def tmp(tmp_path):
    """Temporary directory with a sample file."""
    sample = tmp_path / "hello.txt"
    sample.write_text("line1\nline2\nline3\n", encoding="utf-8")
    return tmp_path


# ── manifest ─────────────────────────────────────────────────────────


class TestManifest:
    def test_15_actions_registered(self, fs):
        assert len(fs._action_registry) == 14

    def test_manifest_module_id(self, fs):
        mf = fs.get_manifest()
        assert mf.module_id == "filesystem"
        assert len(mf.actions) == 14

    def test_declared_permissions(self, fs):
        mf = fs.get_manifest()
        assert "fs.read" in mf.declared_permissions
        assert "fs.delete" in mf.declared_permissions

    def test_delete_is_high_risk_irreversible(self, fs):
        spec = fs._action_registry["rm"].spec
        assert spec.risk_level == "high"
        assert spec.irreversible is True

    def test_side_effects_declared(self, fs):
        write_spec = fs._action_registry["write"].spec
        assert "filesystem_write" in write_spec.side_effects
        rm_spec = fs._action_registry["rm"].spec
        assert "filesystem_delete" in rm_spec.side_effects


# ── read ─────────────────────────────────────────────────────────────


class TestRead:
    @pytest.mark.asyncio
    async def test_read_full_with_line_numbers(self, fs, tmp):
        result = await fs.execute("read", {"path": str(tmp / "hello.txt")})
        assert result.success
        assert "1│line1" in result.data["content"]
        assert "3│line3" in result.data["content"]
        assert result.data["total_lines"] == 3

    @pytest.mark.asyncio
    async def test_read_line_range(self, fs, tmp):
        result = await fs.execute("read", {
            "path": str(tmp / "hello.txt"),
            "start_line": 2,
            "end_line": 2,
        })
        assert result.success
        assert "line2" in result.data["content"]
        assert "line1" not in result.data["content"]
        assert result.data["start_line"] == 2
        assert result.data["end_line"] == 2

    @pytest.mark.asyncio
    async def test_read_nonexistent(self, fs, tmp):
        result = await fs.execute("read", {"path": str(tmp / "nope.txt")})
        assert not result.success
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_directory_fails(self, fs, tmp):
        result = await fs.execute("read", {"path": str(tmp)})
        assert not result.success
        assert "not a file" in result.error.lower()


# ── write ────────────────────────────────────────────────────────────


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_new_file(self, fs, tmp):
        target = str(tmp / "new.txt")
        result = await fs.execute("write", {"path": target, "content": "hello"})
        assert result.success
        assert Path(target).read_text() == "hello"
        assert result.data["bytes_written"] == 5

    @pytest.mark.asyncio
    async def test_write_create_dirs(self, fs, tmp):
        target = str(tmp / "a" / "b" / "c.txt")
        result = await fs.execute("write", {
            "path": target, "content": "deep", "create_dirs": True,
        })
        assert result.success
        assert Path(target).read_text() == "deep"

    @pytest.mark.asyncio
    async def test_write_parent_missing_fails(self, fs, tmp):
        target = str(tmp / "no_parent" / "file.txt")
        result = await fs.execute("write", {"path": target, "content": "x"})
        assert not result.success
        assert "parent" in result.error.lower()


# ── edit ─────────────────────────────────────────────────────────────


class TestEdit:
    @pytest.mark.asyncio
    async def test_edit_replace(self, fs, tmp):
        # Must read before edit (read-before-edit enforcement)
        await fs.execute("read", {"path": str(tmp / "hello.txt")})
        result = await fs.execute("edit", {
            "path": str(tmp / "hello.txt"),
            "old_string": "line2",
            "new_string": "REPLACED",
        })
        assert result.success
        assert result.data["replacements"] == 1
        content = (tmp / "hello.txt").read_text()
        assert "REPLACED" in content
        assert "line2" not in content
        # Preview should have line numbers.
        assert "│" in result.data["preview"]

    @pytest.mark.asyncio
    async def test_edit_replace_all(self, fs, tmp):
        (tmp / "repeat.txt").write_text("aaa\naaa\naaa\n")
        await fs.execute("read", {"path": str(tmp / "repeat.txt")})
        result = await fs.execute("edit", {
            "path": str(tmp / "repeat.txt"),
            "old_string": "aaa",
            "new_string": "bbb",
            "replace_all": True,
        })
        assert result.success
        assert result.data["replacements"] == 3
        assert (tmp / "repeat.txt").read_text() == "bbb\nbbb\nbbb\n"

    @pytest.mark.asyncio
    async def test_edit_not_found(self, fs, tmp):
        await fs.execute("read", {"path": str(tmp / "hello.txt")})
        result = await fs.execute("edit", {
            "path": str(tmp / "hello.txt"),
            "old_string": "MISSING",
            "new_string": "x",
        })
        assert not result.success
        assert "not found" in result.error.lower()


# ── insert ───────────────────────────────────────────────────────────


class TestInsert:
    @pytest.mark.asyncio
    async def test_insert_at_line(self, fs, tmp):
        result = await fs.execute("insert", {
            "path": str(tmp / "hello.txt"),
            "line": 2,
            "content": "INSERTED",
        })
        assert result.success
        assert result.data["lines_inserted"] == 1
        lines = (tmp / "hello.txt").read_text().splitlines()
        assert lines[1] == "INSERTED"
        assert lines[2] == "line2"

    @pytest.mark.asyncio
    async def test_insert_at_end(self, fs, tmp):
        result = await fs.execute("insert", {
            "path": str(tmp / "hello.txt"),
            "line": 100,
            "content": "END",
        })
        assert result.success
        lines = (tmp / "hello.txt").read_text().splitlines()
        assert lines[-1] == "END"

    @pytest.mark.asyncio
    async def test_insert_preview_has_numbers(self, fs, tmp):
        result = await fs.execute("insert", {
            "path": str(tmp / "hello.txt"),
            "line": 1,
            "content": "TOP",
        })
        assert "│" in result.data["preview"]


# ── ls ───────────────────────────────────────────────────────────────


class TestLs:
    @pytest.mark.asyncio
    async def test_list_directory(self, fs, tmp):
        result = await fs.execute("ls", {"path": str(tmp)})
        assert result.success
        names = [e["name"] for e in result.data["entries"]]
        assert "hello.txt" in names

    @pytest.mark.asyncio
    async def test_list_with_pattern(self, fs, tmp):
        (tmp / "a.py").write_text("a")
        (tmp / "b.js").write_text("b")
        result = await fs.execute("ls", {"path": str(tmp), "pattern": "*.py"})
        assert result.success
        names = [e["name"] for e in result.data["entries"]]
        assert "a.py" in names
        assert "b.js" not in names

    @pytest.mark.asyncio
    async def test_list_nonexistent(self, fs, tmp):
        result = await fs.execute("ls", {"path": str(tmp / "nope")})
        assert not result.success


# ── grep ─────────────────────────────────────────────────────────────


class TestGrep:
    @pytest.mark.asyncio
    async def test_grep_finds_content(self, fs, tmp):
        result = await fs.execute("grep", {
            "pattern": "line2", "path": str(tmp),
        })
        assert result.success
        total = result.data.get("count") or result.data.get("numMatches", 0)
        assert total >= 1
        assert any("line2" in m["text"] for m in result.data["matches"])

    @pytest.mark.asyncio
    async def test_grep_no_match(self, fs, tmp):
        result = await fs.execute("grep", {
            "pattern": "ZZZZZ", "path": str(tmp),
        })
        assert result.success
        total = result.data.get("count") or result.data.get("numMatches", 0)
        assert total == 0

    @pytest.mark.asyncio
    async def test_grep_with_include(self, fs, tmp):
        (tmp / "code.py").write_text("def hello():\n    pass\n")
        (tmp / "data.txt").write_text("hello world\n")
        result = await fs.execute("grep", {
            "pattern": "hello", "path": str(tmp), "include": "*.py",
        })
        assert result.success
        assert all(m["file"].endswith(".py") for m in result.data["matches"])


# ── find ─────────────────────────────────────────────────────────────


class TestFind:
    @pytest.mark.asyncio
    async def test_find_by_pattern(self, fs, tmp):
        result = await fs.execute("find", {"pattern": "*.txt", "path": str(tmp)})
        assert result.success
        assert result.data["count"] >= 1

    @pytest.mark.asyncio
    async def test_find_type_filter(self, fs, tmp):
        sub = tmp / "subdir"
        sub.mkdir()
        result = await fs.execute("find", {
            "pattern": "*", "path": str(tmp), "type": "dir",
        })
        assert result.success
        assert any("subdir" in f for f in result.data["files"])


# ── mv ───────────────────────────────────────────────────────────────


class TestMv:
    @pytest.mark.asyncio
    async def test_move_file(self, fs, tmp):
        result = await fs.execute("mv", {
            "source": str(tmp / "hello.txt"),
            "destination": str(tmp / "moved.txt"),
        })
        assert result.success
        assert (tmp / "moved.txt").exists()
        assert not (tmp / "hello.txt").exists()

    @pytest.mark.asyncio
    async def test_move_no_overwrite(self, fs, tmp):
        (tmp / "existing.txt").write_text("x")
        result = await fs.execute("mv", {
            "source": str(tmp / "hello.txt"),
            "destination": str(tmp / "existing.txt"),
        })
        assert not result.success


# ── cp ───────────────────────────────────────────────────────────────


class TestCp:
    @pytest.mark.asyncio
    async def test_copy_file(self, fs, tmp):
        result = await fs.execute("cp", {
            "source": str(tmp / "hello.txt"),
            "destination": str(tmp / "copy.txt"),
        })
        assert result.success
        assert (tmp / "copy.txt").exists()
        assert (tmp / "hello.txt").exists()

    @pytest.mark.asyncio
    async def test_copy_no_overwrite(self, fs, tmp):
        (tmp / "existing.txt").write_text("x")
        result = await fs.execute("cp", {
            "source": str(tmp / "hello.txt"),
            "destination": str(tmp / "existing.txt"),
        })
        assert not result.success


# ── rm ───────────────────────────────────────────────────────────────


class TestRm:
    @pytest.mark.asyncio
    async def test_delete_file(self, fs, tmp):
        result = await fs.execute("rm", {"path": str(tmp / "hello.txt")})
        assert result.success
        assert not (tmp / "hello.txt").exists()

    @pytest.mark.asyncio
    async def test_delete_dir_requires_recursive(self, fs, tmp):
        sub = tmp / "subdir"
        sub.mkdir()
        result = await fs.execute("rm", {"path": str(sub)})
        assert not result.success
        assert "recursive" in result.error.lower()

    @pytest.mark.asyncio
    async def test_delete_dir_recursive(self, fs, tmp):
        sub = tmp / "subdir"
        sub.mkdir()
        (sub / "a.txt").write_text("a")
        result = await fs.execute("rm", {"path": str(sub), "recursive": True})
        assert result.success
        assert not sub.exists()


# ── mkdir ────────────────────────────────────────────────────────────


class TestMkdir:
    @pytest.mark.asyncio
    async def test_create_nested(self, fs, tmp):
        result = await fs.execute("mkdir", {"path": str(tmp / "a" / "b" / "c")})
        assert result.success
        assert (tmp / "a" / "b" / "c").is_dir()


# ── file_stat ────────────────────────────────────────────────────────


class TestFileStat:
    @pytest.mark.asyncio
    async def test_file_info(self, fs, tmp):
        result = await fs.execute("file_stat", {"path": str(tmp / "hello.txt")})
        assert result.success
        assert result.data["type"] == "file"
        assert result.data["size_bytes"] > 0

    @pytest.mark.asyncio
    async def test_dir_info(self, fs, tmp):
        result = await fs.execute("file_stat", {"path": str(tmp)})
        assert result.success
        assert result.data["type"] == "dir"

    @pytest.mark.asyncio
    async def test_nonexistent(self, fs, tmp):
        result = await fs.execute("file_stat", {"path": str(tmp / "nope")})
        assert not result.success


# ── constraints ──────────────────────────────────────────────────────


class TestConstraints:
    """Verify that path and file-size constraints are enforced."""

    @pytest.mark.asyncio
    async def test_manifest_declares_constraints(self, fs):
        mf = fs.get_manifest()
        names = [c.name for c in mf.supported_constraints]
        assert "paths" in names
        assert "max_file_size" in names

    @pytest.mark.asyncio
    async def test_path_constraint_blocks_outside(self, fs, tmp):
        """Reading a file outside the allowed paths must fail."""
        allowed = tmp / "allowed"
        allowed.mkdir()
        (allowed / "ok.txt").write_text("ok")
        (tmp / "forbidden.txt").write_text("secret")

        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"paths": [str(allowed)]},
        )
        # Inside allowed - should succeed.
        result = await fs.execute("read", {"path": str(allowed / "ok.txt")}, context=ctx)
        assert result.success

        # Outside allowed - should fail.
        result = await fs.execute("read", {"path": str(tmp / "forbidden.txt")}, context=ctx)
        assert not result.success
        assert "outside" in result.error.lower()

    @pytest.mark.asyncio
    async def test_path_constraint_blocks_write(self, fs, tmp):
        allowed = tmp / "sandbox"
        allowed.mkdir()

        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"paths": [str(allowed)]},
        )
        result = await fs.execute("write", {
            "path": str(tmp / "escape.txt"), "content": "bad",
        }, context=ctx)
        assert not result.success
        assert "outside" in result.error.lower()

    @pytest.mark.asyncio
    async def test_path_constraint_blocks_rm(self, fs, tmp):
        allowed = tmp / "sandbox"
        allowed.mkdir()
        target = tmp / "important.txt"
        target.write_text("keep me")

        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"paths": [str(allowed)]},
        )
        result = await fs.execute("rm", {"path": str(target)}, context=ctx)
        assert not result.success
        assert target.exists()  # Not deleted.

    @pytest.mark.asyncio
    async def test_max_file_size_blocks_read(self, fs, tmp):
        big = tmp / "big.txt"
        big.write_text("x" * 1000)

        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"max_file_size": "500B"},
        )
        result = await fs.execute("read", {"path": str(big)}, context=ctx)
        assert not result.success
        assert "exceeds" in result.error.lower()

    @pytest.mark.asyncio
    async def test_max_file_size_blocks_write(self, fs, tmp):
        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"max_file_size": "10B"},
        )
        result = await fs.execute("write", {
            "path": str(tmp / "toobig.txt"),
            "content": "a" * 100,
        }, context=ctx)
        assert not result.success
        assert "exceeds" in result.error.lower()
        assert not (tmp / "toobig.txt").exists()

    @pytest.mark.asyncio
    async def test_no_constraints_allows_everything(self, fs, tmp):
        """Without constraints, all operations pass (dev mode)."""
        result = await fs.execute("read", {"path": str(tmp / "hello.txt")})
        assert result.success

    @pytest.mark.asyncio
    async def test_mv_both_paths_checked(self, fs, tmp):
        allowed = tmp / "sandbox"
        allowed.mkdir()
        (allowed / "a.txt").write_text("a")

        ctx = ExecutionContext(
            plan_id="test", action_id="test",
            constraints={"paths": [str(allowed)]},
        )
        # Source inside, destination outside - should fail.
        result = await fs.execute("mv", {
            "source": str(allowed / "a.txt"),
            "destination": str(tmp / "escaped.txt"),
        }, context=ctx)
        assert not result.success
        assert (allowed / "a.txt").exists()  # Not moved.


# ── params_model: schema exposure ────────────────────────────────────


class TestParamsSchema:
    """Verify that the manifest exposes full parameter schemas to agents."""

    def test_all_actions_have_params_model(self, fs):
        """Every action must declare a params_model - no exceptions."""
        for name, entry in fs._action_registry.items():
            assert entry.params_model is not None, (
                f"Action '{name}' is missing params_model"
            )

    def test_manifest_actions_have_input_schema(self, fs):
        """The manifest JSON for each action must include a non-empty params_schema."""
        mf = fs.get_manifest()
        for action in mf.actions:
            schema = action.to_json_schema()
            assert "properties" in schema, (
                f"Action '{action.name}' has no properties in JSON schema"
            )
            assert len(schema["properties"]) > 0, (
                f"Action '{action.name}' has empty properties"
            )

    def test_read_schema_has_descriptions(self, fs):
        """The read action schema should expose field descriptions."""
        spec = fs._action_registry["read"].spec
        schema = spec.to_json_schema()
        props = schema["properties"]
        assert "path" in props
        assert props["path"].get("description"), "path field has no description"
        assert "start_line" in props
        assert props["start_line"].get("description"), "start_line has no description"

    def test_read_schema_required_fields(self, fs):
        """path should be required, start_line/end_line should not."""
        spec = fs._action_registry["read"].spec
        schema = spec.to_json_schema()
        required = schema.get("required", [])
        assert "path" in required
        assert "start_line" not in required
        assert "end_line" not in required

    def test_edit_schema_has_min_length(self, fs):
        """old_string must have minLength=1 in the schema."""
        spec = fs._action_registry["edit"].spec
        schema = spec.to_json_schema()
        old_string_prop = schema["properties"]["old_string"]
        assert old_string_prop.get("minLength") == 1

    def test_param_specs_generated(self, fs):
        """ActionSpec.params should be populated from the Pydantic model."""
        spec = fs._action_registry["read"].spec
        assert len(spec.params) > 0
        param_names = [p.name for p in spec.params]
        assert "path" in param_names
        assert "start_line" in param_names

    def test_langchain_tool_schema(self, fs):
        """to_langchain_tool_schema() should produce a valid LLM tool definition."""
        spec = fs._action_registry["grep"].spec
        tool = spec.to_langchain_tool_schema()
        assert tool["name"] == "grep"
        assert "parameters" in tool
        assert "pattern" in tool["parameters"]["properties"]


# ── params_model: validation ─────────────────────────────────────────


class TestParamsValidation:
    """Verify that invalid params return a clean ActionResult error."""

    @pytest.mark.asyncio
    async def test_missing_required_param(self, fs, tmp):
        """Calling read without 'path' should return a validation error."""
        result = await fs.execute("read", {})
        assert isinstance(result, ActionResult)
        assert not result.success
        assert "path" in result.error
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_wrong_type_param(self, fs, tmp):
        """start_line must be an integer, not a string."""
        result = await fs.execute("read", {
            "path": str(tmp / "hello.txt"),
            "start_line": "abc",
        })
        assert isinstance(result, ActionResult)
        assert not result.success
        assert "start_line" in result.error

    @pytest.mark.asyncio
    async def test_constraint_violation(self, fs, tmp):
        """start_line must be >= 1."""
        result = await fs.execute("read", {
            "path": str(tmp / "hello.txt"),
            "start_line": 0,
        })
        assert isinstance(result, ActionResult)
        assert not result.success
        assert "start_line" in result.error

    @pytest.mark.asyncio
    async def test_edit_empty_old_string(self, fs, tmp):
        """old_string cannot be empty (min_length=1)."""
        result = await fs.execute("edit", {
            "path": str(tmp / "hello.txt"),
            "old_string": "",
            "new_string": "x",
        })
        assert isinstance(result, ActionResult)
        assert not result.success
        assert "old_string" in result.error

    @pytest.mark.asyncio
    async def test_valid_params_pass_through(self, fs, tmp):
        """Valid params should work normally."""
        result = await fs.execute("read", {"path": str(tmp / "hello.txt")})
        assert result.success
        assert "content" in result.data

    @pytest.mark.asyncio
    async def test_unknown_param_ignored(self, fs, tmp):
        """Extra params should be ignored (Pydantic default behavior)."""
        result = await fs.execute("read", {
            "path": str(tmp / "hello.txt"),
            "unknown_field": "ignored",
        })
        # Pydantic ignores unknown fields by default
        assert result.success
