"""PreviewManager — supervises an app's dev server.

One manager instance per deployed app. Lifecycle:

1. Construct with ``PreviewConfig`` + ``bundle_dir``.
2. ``await manager.install()`` — runs the optional ``install_command``
   (e.g. ``npm install``) once. Idempotent via an ``.installed`` marker.
3. ``await manager.start()`` — spawns the process, polls the health
   endpoint, and begins supervising. Returns when the server is ready.
4. ``await manager.stop()`` — terminates gracefully, then kills on
   timeout, waits for the supervisor task to finish.

On unexpected process exit while ``restart_on_crash=True``, the manager
restarts the process with linear backoff (up to 3 retries per 60s
window). After the budget is exceeded the manager transitions to
``CRASHED`` and stops retrying.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from digitorn.core.app.schema import PreviewConfig
from digitorn.core.runtime.node_runtime import NodeRuntime, get_node_runtime

logger = logging.getLogger(__name__)


_LOG_BUFFER_SIZE = 500
_RESTART_WINDOW_SEC = 60.0
_RESTART_MAX = 3
_HEALTH_POLL_INTERVAL = 0.5
_STOP_GRACE_SEC = 5.0
_INSTALLED_MARKER = ".digitorn-preview-installed"


def _node_install_is_broken(cwd: Path) -> bool:
    """Heuristic: detect a half-broken ``npm install`` in ``cwd``.

    Returns True if ``package.json`` exists but ``node_modules/.bin``
    is missing or empty. That's the signature of an interrupted
    install (packages downloaded but binstubs never generated), which
    otherwise fails at ``npm run dev`` with ``'vite' is not
    recognized`` / ``command not found``.

    Non-Node preview dirs (no ``package.json``) always return False —
    they don't need binstubs at all.
    """
    try:
        if not (cwd / "package.json").is_file():
            return False
        bin_dir = cwd / "node_modules" / ".bin"
        if not bin_dir.is_dir():
            return True
        # Empty .bin = every dependency is present but no shim was
        # written. npm install will repair it.
        for _entry in bin_dir.iterdir():
            return False
        return True
    except OSError:
        return False


class PreviewState(str, Enum):
    STOPPED = "stopped"
    INSTALLING = "installing"
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    CRASHED = "crashed"


@dataclass
class PreviewStatus:
    state: PreviewState
    pid: int | None = None
    port: int | None = None
    version: str | None = None
    started_at: float | None = None
    last_exit_code: int | None = None
    restart_count: int = 0
    last_error: str | None = None
    logs_tail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "pid": self.pid,
            "port": self.port,
            "version": self.version,
            "started_at": self.started_at,
            "last_exit_code": self.last_exit_code,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "logs_tail": list(self.logs_tail),
        }


class PreviewManager:
    """Supervises a single app's dev server.

    The manager is self-contained: it owns the process, a log ring
    buffer, a health poller, and a restart supervisor task.
    """

    def __init__(
        self,
        config: PreviewConfig,
        *,
        bundle_dir: Path,
        app_id: str,
        owner_user_id: str | None = None,
        node_runtime: NodeRuntime | None = None,
    ) -> None:
        self._config = config
        self._bundle_dir = Path(bundle_dir)
        self._app_id = app_id
        self._owner_user_id = owner_user_id
        self._runtime = node_runtime or get_node_runtime()

        self._state: PreviewState = PreviewState.STOPPED
        self._proc: asyncio.subprocess.Process | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._log_reader_tasks: list[asyncio.Task[None]] = []
        self._logs: deque[str] = deque(maxlen=_LOG_BUFFER_SIZE)
        self._restart_times: deque[float] = deque(maxlen=_RESTART_MAX)
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._started_at: float | None = None
        self._stopping = False
        self._lock = asyncio.Lock()

    # ───────────────────────── public API ──────────────────────────

    @property
    def config(self) -> PreviewConfig:
        return self._config

    @property
    def state(self) -> PreviewState:
        return self._state

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def port(self) -> int:
        return self._config.port

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def status(self) -> PreviewStatus:
        return PreviewStatus(
            state=self._state,
            pid=self._proc.pid if self._proc else None,
            port=self._config.port,
            version=self._runtime.info.version if self._runtime.available else None,
            started_at=self._started_at,
            last_exit_code=self._last_exit_code,
            restart_count=len(self._restart_times),
            last_error=self._last_error,
            logs_tail=list(self._logs),
        )

    async def install(self, *, force: bool = False) -> None:
        """Run the optional one-shot ``install_command`` if declared.

        Creates a marker file so subsequent deploys skip reinstall
        unless ``force=True``.
        """
        if not self._config.install_command:
            return

        cwd = self._resolve_cwd()
        cwd.mkdir(parents=True, exist_ok=True)
        marker = cwd / _INSTALLED_MARKER
        if marker.exists() and not force:
            # Belt-and-braces: the marker can get out of sync with the
            # real state of node_modules (interrupted install, manual
            # rm -rf, half-applied update). If package.json declares a
            # node project but node_modules/.bin is empty/missing, we
            # KNOW the install is broken regardless of the marker, so
            # force a reinstall instead of blowing up later at
            # ``vite is not recognized``.
            if _node_install_is_broken(cwd):
                logger.warning(
                    "preview_install_reinstall app=%s reason=binstubs_missing "
                    "cwd=%s",
                    self._app_id, cwd,
                )
                try:
                    marker.unlink()
                except OSError:
                    pass
                # fall through to the install path below
            else:
                logger.info(
                    "preview_install_skipped app=%s reason=marker_exists",
                    self._app_id,
                )
                return

        self._state = PreviewState.INSTALLING
        self._append_log(
            f"[install] running: {' '.join(self._config.install_command)}"
        )

        try:
            await self._runtime.ensure_installed()
        except Exception as exc:
            self._last_error = f"Node unavailable: {exc}"
            self._state = PreviewState.CRASHED
            raise

        cmd, args = self._config.install_command[0], self._config.install_command[1:]
        env = self._runtime.env
        env.update(self._config.env)

        try:
            proc = await self._runtime.spawn(
                cmd, args, cwd=cwd, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            self._last_error = f"install command not found: {exc}"
            self._state = PreviewState.CRASHED
            self._append_log(f"[install] ERROR: {self._last_error}")
            raise

        async def _pump() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._append_log(line.decode("utf-8", errors="replace").rstrip())

        await _pump()
        rc = await proc.wait()
        if rc != 0:
            # Surface the last ~30 lines of captured output so the daemon
            # log makes the actual npm / yarn / pnpm error obvious.
            tail = list(self._logs)[-30:]
            tail_text = "\n    ".join(tail) if tail else "(no output)"
            self._last_error = (
                f"install command exited with code {rc}\n"
                f"  last {len(tail)} lines of output:\n    {tail_text}"
            )
            self._state = PreviewState.CRASHED
            self._append_log(f"[install] FAILED rc={rc}")
            logger.error(
                "preview_install_failed app=%s rc=%d cwd=%s\n%s",
                self._app_id, rc, cwd, self._last_error,
            )
            raise RuntimeError(self._last_error)

        marker.write_text("ok\n", encoding="utf-8")
        self._state = PreviewState.STOPPED
        self._append_log("[install] done")
        logger.info("preview_install_done app=%s cwd=%s", self._app_id, cwd)

    async def start(self) -> None:
        """Spawn the dev server and wait until ready.

        Idempotent if already running. Raises on startup failure.
        """
        async with self._lock:
            if self._state in (PreviewState.RUNNING, PreviewState.STARTING):
                return
            if not self._config.enabled:
                logger.info("preview_start_skipped app=%s reason=disabled", self._app_id)
                return

            try:
                await self._runtime.ensure_installed()
            except Exception as exc:
                self._last_error = f"Node unavailable: {exc}"
                self._state = PreviewState.CRASHED
                raise

            self._stopping = False
            self._state = PreviewState.STARTING
            try:
                await self._spawn_process()
                await self._wait_ready(self._config.startup_timeout)
            except Exception as exc:
                self._last_error = str(exc)
                self._state = PreviewState.CRASHED
                await self._kill_process(grace=False)
                raise

            self._state = PreviewState.RUNNING
            self._started_at = time.time()
            self._supervisor_task = asyncio.create_task(self._supervise())
            logger.info(
                "preview_started app=%s port=%d pid=%s",
                self._app_id, self._config.port, self._proc.pid if self._proc else None,
            )

    async def stop(self) -> None:
        """Terminate the dev server and cancel the supervisor."""
        async with self._lock:
            self._stopping = True
            if self._supervisor_task is not None:
                self._supervisor_task.cancel()
                try:
                    await asyncio.wait_for(self._supervisor_task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                self._supervisor_task = None

            await self._kill_process(grace=True)
            for task in self._log_reader_tasks:
                if not task.done():
                    task.cancel()
            self._log_reader_tasks.clear()
            self._state = PreviewState.STOPPED
            self._started_at = None
            logger.info("preview_stopped app=%s", self._app_id)

    async def restart(self) -> None:
        """Stop and re-start. Resets the restart budget."""
        await self.stop()
        self._restart_times.clear()
        await self.start()

    def get_logs(self, limit: int = 200) -> list[str]:
        if limit <= 0:
            return []
        if limit >= len(self._logs):
            return list(self._logs)
        return list(self._logs)[-limit:]

    # ───────────────────────── internals ──────────────────────────

    def _resolve_cwd(self) -> Path:
        """Resolve the preview ``cwd`` relative to the app bundle dir."""
        cwd = Path(self._config.cwd)
        if cwd.is_absolute():
            return cwd
        return (self._bundle_dir / cwd).resolve()

    async def _spawn_process(self) -> None:
        cwd = self._resolve_cwd()
        if not cwd.is_dir():
            raise RuntimeError(
                f"preview cwd does not exist: {cwd} "
                f"(relative to bundle {self._bundle_dir})"
            )
        logger.info(
            "preview_spawn_cwd app=%s bundle=%s resolved=%s has_pkg=%s",
            self._app_id, self._bundle_dir, cwd,
            (cwd / "package.json").is_file(),
        )

        cmd = self._config.command[0]
        args = list(self._config.command[1:])

        env = self._runtime.env
        # Ensure the dev server binds the requested port (Vite / Next
        # honor these as fallback when no explicit flag is passed).
        env.setdefault("PORT", str(self._config.port))
        env.setdefault("HOST", "127.0.0.1")
        env.update(self._config.env)

        logger.info(
            "preview_spawn app=%s cmd=%s args=%s cwd=%s port=%d",
            self._app_id, cmd, args, cwd, self._config.port,
        )

        self._proc = await self._runtime.spawn(
            cmd, args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        self._log_reader_tasks = [
            asyncio.create_task(
                self._pump_stream(self._proc.stdout, "stdout"),
            ),
            asyncio.create_task(
                self._pump_stream(self._proc.stderr, "stderr"),
            ),
        ]

    async def _pump_stream(
        self,
        stream: asyncio.StreamReader | None,
        label: str,
    ) -> None:
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                decoded = line.decode("utf-8", errors="replace").rstrip()
                self._append_log(f"[{label}] {decoded}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("preview_log_pump_error app=%s: %s", self._app_id, exc)

    async def _wait_ready(self, timeout: float) -> None:
        """Poll the health endpoint (or port) until reachable or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                # Give the log reader tasks a moment to drain the final
                # few bytes of stdout/stderr before we snapshot the tail.
                await asyncio.sleep(0.15)
                tail = list(self._logs)[-30:]
                tail_text = "\n    ".join(tail) if tail else "(no output)"
                logger.error(
                    "preview_startup_exit app=%s rc=%d\n  cwd=%s\n  "
                    "last %d log lines:\n    %s",
                    self._app_id, self._proc.returncode,
                    self._resolve_cwd(), len(tail), tail_text,
                )
                raise RuntimeError(
                    f"preview process exited during startup "
                    f"(rc={self._proc.returncode})\n"
                    f"  cwd: {self._resolve_cwd()}\n"
                    f"  last {len(tail)} log lines:\n    {tail_text}"
                )
            if await self._is_ready():
                return
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)

        # Timeout branch — also snapshot logs in case the process is
        # running but stuck / unreachable.
        tail = list(self._logs)[-30:]
        tail_text = "\n    ".join(tail) if tail else "(no output)"
        raise TimeoutError(
            f"preview did not become ready within {timeout:.0f}s\n"
            f"  cwd: {self._resolve_cwd()}\n"
            f"  last {len(tail)} log lines:\n    {tail_text}"
        )

    async def _is_ready(self) -> bool:
        """Check if the dev server accepts TCP connections on its port."""
        try:
            fut = asyncio.open_connection("127.0.0.1", self._config.port)
            reader, writer = await asyncio.wait_for(fut, timeout=1.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
            return False

    async def _supervise(self) -> None:
        """Watch the process; on unexpected exit, restart with backoff."""
        assert self._proc is not None
        try:
            rc = await self._proc.wait()
            self._last_exit_code = rc
            if self._stopping:
                return
            logger.warning(
                "preview_exited_unexpectedly app=%s rc=%s",
                self._app_id, rc,
            )
            if not self._config.restart_on_crash:
                self._state = PreviewState.CRASHED
                return
            await self._maybe_restart()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "preview_supervisor_error app=%s: %s", self._app_id, exc,
                exc_info=True,
            )
            self._state = PreviewState.CRASHED
            self._last_error = str(exc)

    async def _maybe_restart(self) -> None:
        """Enforce the 3-per-60s restart budget then relaunch."""
        now = time.time()
        cutoff = now - _RESTART_WINDOW_SEC
        while self._restart_times and self._restart_times[0] < cutoff:
            self._restart_times.popleft()

        if len(self._restart_times) >= _RESTART_MAX:
            self._state = PreviewState.CRASHED
            self._last_error = (
                f"preview crashed {_RESTART_MAX} times in "
                f"{_RESTART_WINDOW_SEC:.0f}s — giving up"
            )
            logger.error("preview_restart_budget_exceeded app=%s", self._app_id)
            return

        self._restart_times.append(now)
        backoff = 1.0 * len(self._restart_times)
        self._state = PreviewState.RESTARTING
        self._append_log(
            f"[manager] restarting in {backoff:.1f}s "
            f"(attempt {len(self._restart_times)}/{_RESTART_MAX})"
        )
        await asyncio.sleep(backoff)
        if self._stopping:
            return
        try:
            await self._spawn_process()
            await self._wait_ready(self._config.startup_timeout)
            self._state = PreviewState.RUNNING
            self._started_at = time.time()
            self._supervisor_task = asyncio.create_task(self._supervise())
        except Exception as exc:
            self._last_error = str(exc)
            self._state = PreviewState.CRASHED
            self._append_log(f"[manager] restart failed: {exc}")

    async def _kill_process(self, *, grace: bool) -> None:
        """Terminate the dev server process and **all its descendants**.

        On Windows we spawn via ``cmd.exe /c npm.cmd run dev`` which
        creates a four-level process tree:

            digitorn daemon
              └─ cmd.exe
                   └─ npm.cmd (cmd.exe subshell)
                        └─ node.exe (vite)

        ``proc.terminate()`` / ``proc.kill()`` only targets the top-level
        ``cmd.exe``; the grandchild ``node.exe`` survives as an orphan
        still bound to the dev-server TCP port. On the next deploy that
        orphan prevents the new Vite from binding and the whole preview
        crashes loop with rc=1 ``Port 5174 is already in use``.

        Fix: on Windows, after asking the process nicely, call
        ``taskkill /F /T /PID <pid>`` which walks the child tree and
        force-kills every descendant in one shot.
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        pid = proc.pid
        is_windows = sys.platform in ("win32", "cygwin")

        async def _tree_kill() -> None:
            """Force-kill the top-level process AND every descendant."""
            if is_windows:
                try:
                    tk = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(tk.wait(), timeout=_STOP_GRACE_SEC)
                except Exception as exc:
                    logger.debug(
                        "preview_taskkill_failed app=%s pid=%s: %s",
                        self._app_id, pid, exc,
                    )
            else:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        try:
            if is_windows:
                # Windows: go STRAIGHT to ``taskkill /T /F /PID``.
                # We must NOT call ``proc.terminate()`` first because
                # that kills cmd.exe, which breaks the parent→child
                # links the tree-walker relies on. Once cmd.exe is
                # gone, the grand-children (npm node.exe → vite
                # node.exe) become orphans with no reachable parent,
                # and taskkill /T can no longer walk to them — leaving
                # the dev-server node.exe holding the port forever.
                await _tree_kill()
            else:
                # Unix: the shell forwards SIGTERM to its process
                # group, so terminate() is enough for the graceful
                # path and kill() is the escape hatch.
                if grace and proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(
                            proc.wait(), timeout=_STOP_GRACE_SEC,
                        )
                    except ProcessLookupError:
                        pass
                    except asyncio.TimeoutError:
                        logger.warning(
                            "preview_terminate_timeout app=%s pid=%s",
                            self._app_id, pid,
                        )
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SEC)
            except asyncio.TimeoutError:
                logger.error(
                    "preview_kill_timeout app=%s pid=%s — leaving orphan",
                    self._app_id, pid,
                )
        except ProcessLookupError:
            pass

    def _append_log(self, line: str) -> None:
        self._logs.append(line)
