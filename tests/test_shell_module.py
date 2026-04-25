"""Shell module tests — covers bash, bash_background, bash_status, constraints, security."""

from __future__ import annotations

import asyncio
import platform

import pytest

from digitorn.modules.shell.module import ShellConfig, ShellModule
from digitorn.modules.shell.params import (
    BashBackgroundParams,
    BashParams,
    BashStatusParams,
)

_IS_WINDOWS = platform.system().lower() == "windows"


from _test_helpers import run_coro


@pytest.fixture
def shell(tmp_path):
    m = ShellModule()
    run_coro(m.on_config_update({"workspace": str(tmp_path)}))
    return m


# ═══════════════════════════════════════════════════════════════
# CONFIG_MODEL
# ═══════════════════════════════════════════════════════════════


class TestShellConfig:
    def test_defaults(self):
        c = ShellConfig()
        assert c.timeout == 30
        assert c.max_output_bytes == 1_000_000
        assert c.sanitize_output is True

    def test_custom(self):
        c = ShellConfig(timeout=60, max_output_bytes=5_000_000, sanitize_output=False)
        assert c.timeout == 60
        assert c.sanitize_output is False

    def test_validation(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ShellConfig(timeout=0)
        with pytest.raises(ValidationError):
            ShellConfig(timeout=9999)

    def test_config_model_set(self):
        assert ShellModule.CONFIG_MODEL is ShellConfig


# ═══════════════════════════════════════════════════════════════
# BASH
# ═══════════════════════════════════════════════════════════════


class TestBash:
    @pytest.mark.asyncio
    async def test_echo(self, shell, tmp_path):
        cmd = "echo hello"
        r = await shell.bash(BashParams(command=cmd))
        assert r.success
        assert "hello" in r.data["stdout"]

    @pytest.mark.asyncio
    async def test_nonexistent_command(self, shell, tmp_path):
        r = await shell.bash(BashParams(command="__nonexistent_cmd_xyz__"))
        assert not r.success or r.data.get("exit_code", 0) != 0

    @pytest.mark.asyncio
    async def test_exit_code(self, shell, tmp_path):
        # Shell module uses Git Bash on all platforms, so use bash syntax
        r = await shell.bash(BashParams(command="exit 42"))
        # bash action may return success=False for non-zero exit codes
        # but should always populate data with exit_code
        if r.data is not None:
            assert r.data["exit_code"] == 42
        else:
            # If data is None, the error message should mention the exit code
            assert "42" in (r.error or "")

    @pytest.mark.asyncio
    async def test_cwd(self, shell, tmp_path):
        sub = tmp_path / "subdir"
        sub.mkdir()
        # Set the persisted cwd so the command runs in the subdir
        shell._persisted_cwd = str(sub)
        # Shell module uses Git Bash on all platforms, so use pwd
        r = await shell.bash(BashParams(command="pwd"))
        assert r.success
        assert "subdir" in r.data["stdout"].lower()


# ═══════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════


@pytest.mark.skipif(_IS_WINDOWS, reason="Background tasks use Unix sleep")
class TestBackgroundTasks:
    @pytest.mark.asyncio
    async def test_run_and_status(self, shell, tmp_path):
        r = await shell.bash_background(BashBackgroundParams(command="sleep 10"))
        assert r.success
        task_id = r.data["task_id"]

        r2 = await shell.bash_status(BashStatusParams(task_id=task_id))
        assert r2.success
        assert r2.data["status"] == "running"

        await shell.bash_status(BashStatusParams(task_id=task_id, kill=True))

    @pytest.mark.asyncio
    async def test_task_output(self, shell, tmp_path):
        r = await shell.bash_background(BashBackgroundParams(command="echo bg_output && sleep 1"))
        task_id = r.data["task_id"]
        await asyncio.sleep(0.5)

        r2 = await shell.bash_status(BashStatusParams(task_id=task_id))
        assert r2.success

        await shell.bash_status(BashStatusParams(task_id=task_id, kill=True))

    @pytest.mark.asyncio
    async def test_task_kill(self, shell, tmp_path):
        r = await shell.bash_background(BashBackgroundParams(command="sleep 60"))
        task_id = r.data["task_id"]

        r2 = await shell.bash_status(BashStatusParams(task_id=task_id, kill=True))
        assert r2.success


# ═══════════════════════════════════════════════════════════════
# SECURITY
# ═══════════════════════════════════════════════════════════════


class TestSecurity:
    @pytest.mark.asyncio
    @pytest.mark.skipif(_IS_WINDOWS, reason="Unix forbidden commands")
    async def test_forbidden_command_blocked(self, shell, tmp_path):
        r = await shell.bash(BashParams(command="rm -rf /"))
        assert not r.success
        assert "forbidden" in (r.error or "").lower() or "blocked" in (r.error or "").lower()

    def test_manifest(self, shell):
        m = shell.get_manifest()
        assert m.module_id == "shell"
        constraint_names = [c.name for c in (m.supported_constraints or [])]
        assert "allowed_commands" in constraint_names
        assert "blocked_commands" in constraint_names
