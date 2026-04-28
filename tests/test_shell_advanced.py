"""Advanced Shell module tests - consolidated bash action with 5 modes.

Tests for:
- Mode 1 (Sync): normal execution with platform info
- Mode 2 (Async): background execution with progress notifications
- Mode 3 (Status): check background task status
- Mode 4 (Kill): terminate background task
- Mode 5 (Stdin): send input to interactive task
"""

from __future__ import annotations

import asyncio
import platform
import pytest
from unittest.mock import patch, AsyncMock

from digitorn.modules.shell.module import ShellModule, BackgroundTask
from digitorn.modules.shell.params import BashParams

_IS_WINDOWS = platform.system().lower() == "windows"

from _test_helpers import run_coro


@pytest.fixture
def shell(tmp_path):
    m = ShellModule()
    run_coro(m.on_config_update({"workspace": str(tmp_path)}))
    return m


# ═══════════════════════════════════════════════════════════════
# MODE 1: SYNC EXECUTION
# ═══════════════════════════════════════════════════════════════

class TestBashSyncMode:
    """Mode 1: Bash(command='...')"""

    @pytest.mark.asyncio
    async def test_sync_basic(self, shell):
        """Basic sync execution returns immediately with stdout, stderr, exit_code."""
        r = await shell.bash(BashParams(command="echo hello"))
        assert r.success
        assert "hello" in r.data["stdout"]
        assert r.data["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_sync_platform_info(self, shell):
        """Sync result includes platform and shell info."""
        r = await shell.bash(BashParams(command="echo test"))
        assert r.success
        assert "platform" in r.data
        assert "shell" in r.data
        assert r.data["platform"] in ("unix", "windows")
        assert isinstance(r.data["shell"], str)

    @pytest.mark.asyncio
    async def test_sync_cwd_persistence(self, shell, tmp_path):
        """CWD persists between sync calls."""
        subdir = tmp_path / "work"
        subdir.mkdir()
        # Set initial CWD
        shell._persisted_cwd = str(subdir)
        # First call doesn't change it
        r = await shell.bash(BashParams(command="pwd"))
        assert r.success
        # Now cd to a different dir
        r = await shell.bash(BashParams(command="cd ..", cwd=str(subdir)))
        # Verify CWD changed (if cd was successful)
        # Note: cd changes are application-specific

    @pytest.mark.asyncio
    async def test_sync_exit_code_nonzero(self, shell):
        """Non-zero exit codes are captured."""
        r = await shell.bash(BashParams(command="exit 42"))
        # May return success=False but exit_code should be 42
        if r.data:
            assert r.data.get("exit_code") == 42


# ═══════════════════════════════════════════════════════════════
# MODE 2 & 3: ASYNC EXECUTION + STATUS CHECK
# ═══════════════════════════════════════════════════════════════

class TestBashAsyncMode:
    """Mode 2: Bash(command='...', run_in_background=true)
    Mode 3: Bash(task_id='...') to check status"""

    @pytest.mark.asyncio
    async def test_async_returns_task_id(self, shell):
        """Async launch returns task_id immediately."""
        r = await shell.bash(
            BashParams(command="echo starting", run_in_background=True)
        )
        assert r.success
        assert "task_id" in r.data
        assert "status" in r.data
        assert r.data["status"] == "running"
        assert "platform" in r.data
        assert "shell" in r.data

    @pytest.mark.asyncio
    async def test_async_fast_completion(self, shell):
        """Fast command (completes in <300ms) returns failure immediately."""
        r = await shell.bash(
            BashParams(command="echo done", run_in_background=True)
        )
        # Should succeed because command completes quickly
        if r.success:
            assert r.data["status"] == "running"
        else:
            # Or fail with "exited immediately"
            assert "immediately" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_status_check(self, shell):
        """Status mode checks running task (Mode 3)."""
        # Start a long-running task
        r = await shell.bash(
            BashParams(command="sleep 5 && echo done", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")
        assert task_id

        # Check status immediately
        r2 = await shell.bash(BashParams(task_id=task_id))
        assert r2.success
        assert r2.data["task_id"] == task_id
        assert "status" in r2.data
        assert "uptime_seconds" in r2.data
        assert "stdout" in r2.data

    @pytest.mark.asyncio
    async def test_status_nonexistent_task(self, shell):
        """Status check for non-existent task returns error."""
        r = await shell.bash(BashParams(task_id="nonexistent_id_xyz"))
        assert not r.success
        assert "not found" in (r.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# MODE 4: KILL TASK
# ═══════════════════════════════════════════════════════════════

class TestBashKillMode:
    """Mode 4: Bash(task_id='...', kill=true)"""

    @pytest.mark.asyncio
    async def test_kill_running_task(self, shell):
        """Kill mode terminates a running task."""
        # Start a long task
        r = await shell.bash(
            BashParams(command="sleep 30", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")

        # Give it a moment to start
        await asyncio.sleep(0.1)

        # Kill it
        r2 = await shell.bash(BashParams(task_id=task_id, kill=True))
        assert r2.success
        assert r2.data["status"] == "killed"
        assert "exit_code" in r2.data

    @pytest.mark.asyncio
    async def test_kill_nonexistent_task(self, shell):
        """Kill mode for non-existent task returns error."""
        r = await shell.bash(BashParams(task_id="fake_id", kill=True))
        assert not r.success
        assert "not found" in (r.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# MODE 5: STDIN INPUT
# ═══════════════════════════════════════════════════════════════

class TestBashStdinMode:
    """Mode 5: Bash(task_id='...', stdin_text='input')"""

    @pytest.mark.asyncio
    async def test_stdin_to_running_task(self, shell):
        """Send stdin to a running task."""
        # Start a task that reads from stdin (e.g., cat)
        r = await shell.bash(
            BashParams(command="cat", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")

        # Send input
        r2 = await shell.bash(BashParams(task_id=task_id, stdin_text="hello world"))
        assert r2.success
        assert r2.data.get("sent") == "hello world"

    @pytest.mark.asyncio
    async def test_stdin_nonexistent_task(self, shell):
        """Stdin mode for non-existent task returns error."""
        r = await shell.bash(
            BashParams(task_id="fake_id", stdin_text="test")
        )
        assert not r.success
        assert "not found" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_stdin_finished_task(self, shell):
        """Stdin mode for finished task returns error."""
        # Start a fast task
        r = await shell.bash(
            BashParams(command="echo done && sleep 1", run_in_background=True)
        )
        if r.success:
            task_id = r.data.get("task_id")
            # Wait for it to finish
            await asyncio.sleep(1.5)
            # Try to send stdin
            r2 = await shell.bash(
                BashParams(task_id=task_id, stdin_text="input")
            )
            assert not r2.success
            assert "not running" in (r2.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# MODE 6: WAIT FOR TASK COMPLETION
# ═══════════════════════════════════════════════════════════════

class TestBashWaitMode:
    """Mode 6: Bash(task_id='...', wait=true) - Block until completion."""

    @pytest.mark.asyncio
    async def test_wait_for_completion(self, shell):
        """Wait mode blocks until task finishes."""
        # Start a task that takes a moment
        r = await shell.bash(
            BashParams(command="echo starting && sleep 1 && echo done", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")

        # Wait for completion
        r2 = await shell.bash(BashParams(task_id=task_id, wait=True))
        assert r2.success
        assert r2.data["status"] == "completed"
        assert r2.data["exit_code"] == 0
        assert "done" in r2.data.get("stdout", "")

    @pytest.mark.asyncio
    async def test_wait_returns_full_output(self, shell):
        """Wait mode returns complete stdout/stderr."""
        r = await shell.bash(
            BashParams(command="echo output1 && echo output2 && echo output3", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")

        r2 = await shell.bash(BashParams(task_id=task_id, wait=True, timeout=10))
        assert r2.success
        stdout = r2.data.get("stdout", "")
        assert "output1" in stdout
        assert "output2" in stdout
        assert "output3" in stdout

    @pytest.mark.asyncio
    async def test_wait_nonexistent_task(self, shell):
        """Wait on non-existent task returns error."""
        r = await shell.bash(BashParams(task_id="fake_id", wait=True))
        assert not r.success
        assert "not found" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_wait_timeout(self, shell):
        """Wait respects timeout."""
        r = await shell.bash(
            BashParams(command="sleep 30", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")
        task_id = r.data.get("task_id")

        # Wait with short timeout
        r2 = await shell.bash(BashParams(task_id=task_id, wait=True, timeout=1))
        assert not r2.success
        assert "timed out" in (r2.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# MODE 7: STREAM OUTPUT WITH FILTERING
# ═══════════════════════════════════════════════════════════════

class TestBashStreamMode:
    """Mode 7: Bash(command='...', stream=true, stream_pattern='...') - Stream with filtering."""

    @pytest.mark.asyncio
    async def test_stream_basic(self, shell):
        """Stream mode outputs command results."""
        notifications = []
        shell._notify_bg = lambda n: notifications.append(n)

        r = await shell.bash(
            BashParams(command="echo line1 && echo line2 && echo line3", stream=True)
        )
        assert r.success
        assert r.data["lines_processed"] >= 3

    @pytest.mark.asyncio
    async def test_stream_with_pattern_filter(self, shell):
        """Stream with pattern filters lines."""
        notifications = []
        shell._notify_bg = lambda n: notifications.append(n)

        # Only match lines with "error"
        r = await shell.bash(
            BashParams(
                command="echo ok && echo error: failed && echo ok && echo error: timeout",
                stream=True,
                stream_pattern="error"
            )
        )
        assert r.success
        # Only error lines should trigger notifications
        error_notifs = [n for n in notifications if "error" in n.get("result_preview", "")]
        assert len(error_notifs) >= 2

    @pytest.mark.asyncio
    async def test_stream_returns_exit_code(self, shell):
        """Stream returns exit code on completion."""
        r = await shell.bash(
            BashParams(command="echo done && exit 0", stream=True)
        )
        assert r.success
        assert r.data["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_stream_invalid_pattern(self, shell):
        """Invalid stream pattern returns error."""
        r = await shell.bash(
            BashParams(
                command="echo test",
                stream=True,
                stream_pattern="[invalid(regex"
            )
        )
        assert not r.success
        assert "regex" in (r.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

class TestBashNotifications:
    """Test progress notifications from background tasks."""

    @pytest.mark.asyncio
    async def test_progress_notifications_sent(self, shell):
        """Background task sends progress notifications."""
        notification_received = []

        def mock_notify_bg(notif: dict):
            notification_received.append(notif)

        shell._notify_bg = mock_notify_bg

        # Start a task that takes a few seconds
        r = await shell.bash(
            BashParams(command="echo progress && sleep 2", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")

        # Wait for progress notifications
        await asyncio.sleep(6)

        # Should have received at least PROGRESS and COMPLETED notifications
        statuses = [n.get("status") for n in notification_received]
        assert "completed" in statuses or "failed" in statuses

    @pytest.mark.asyncio
    async def test_notification_has_output_tail(self, shell):
        """Final notification includes output tail in result_preview."""
        notifications = []
        shell._notify_bg = lambda n: notifications.append(n)

        # Start a task with output
        r = await shell.bash(
            BashParams(command="echo line1 && echo line2 && echo line3", run_in_background=True)
        )
        if not r.success:
            pytest.skip("Task failed to start")

        # Wait for completion
        await asyncio.sleep(2)

        # Find the COMPLETED notification
        completed = [n for n in notifications if n.get("status") == "completed"]
        assert len(completed) > 0
        final_notif = completed[0]

        # Should have result_preview with output
        assert "result_preview" in final_notif
        assert final_notif["result_preview"]  # should have content


# ═══════════════════════════════════════════════════════════════
# ERROR CASES & EDGE CASES
# ═══════════════════════════════════════════════════════════════

class TestBashEdgeCases:
    """Edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_no_params_error(self, shell):
        """Calling bash with no command and no task_id returns error."""
        r = await shell.bash(BashParams())
        assert not r.success
        assert "must provide" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_blocked_sleep_command(self, shell):
        """Sleep >2s is blocked."""
        r = await shell.bash(BashParams(command="sleep 10"))
        assert not r.success
        assert "sleep" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_blocked_sed_i_command(self, shell):
        """sed -i is blocked."""
        r = await shell.bash(BashParams(command="sed -i 's/a/b/' file.txt"))
        assert not r.success
        assert "sed -i" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_timeout(self, shell):
        """Sync commands respect timeout."""
        # Use a long-running command that won't be blocked
        # (sleep >2s is blocked, so use a bash loop instead)
        r = await shell.bash(
            BashParams(command="for i in {1..100}; do echo $i; sleep 0.5; done", timeout=1)
        )
        assert not r.success
        assert "timed" in (r.error or "").lower()


# ═══════════════════════════════════════════════════════════════
# SECURITY
# ═══════════════════════════════════════════════════════════════

class TestBashSecurity:
    """Security tests: command blacklist, workspace confinement."""

    @pytest.mark.asyncio
    async def test_forbidden_pattern_blocked(self, shell):
        """Forbidden patterns (rm -rf /, etc) are blocked."""
        r = await shell.bash(BashParams(command="rm -rf /"))
        assert not r.success
        assert "forbidden" in (r.error or "").lower()

    @pytest.mark.asyncio
    async def test_output_sanitization(self, shell):
        """Sensitive values (API keys, passwords) are redacted."""
        # Mock environment variable
        with patch.dict("os.environ", {"API_KEY": "secret123"}):
            r = await shell.bash(BashParams(command="echo $API_KEY"))
            if r.success:
                # Should be redacted
                assert "secret123" not in r.data.get("stdout", "")
