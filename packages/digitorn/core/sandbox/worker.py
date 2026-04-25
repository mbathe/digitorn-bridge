"""Daemon-side proxy for a sandboxed tool executor subprocess.

The worker only executes individual tool actions — it does NOT run
agent_turn, manage sessions, or call the LLM. The daemon handles all
of that and sends "exec" requests for tool calls that need OS isolation.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import sys
from typing import Any

from .ipc import (
    IPCRequest,
    IPCResponse,
    decode_response,
    encode_request,
)

logger = logging.getLogger(__name__)

_WORKER_MODULE = "digitorn.core.sandbox.worker_main"


class WorkerState(enum.Enum):
    SPAWNING = "spawning"
    READY = "ready"
    WARM = "warm"
    SANDBOXED = "sandboxed"
    TAINTED = "tainted"
    DEAD = "dead"


class SandboxWorker:
    """Lightweight proxy to a sandboxed tool executor process."""

    def __init__(
        self,
        app_id: str,
        module_ids: list[str],
        workspace: str = "",
        allowed_paths: list[str] | None = None,
        sandbox_config: dict[str, Any] | None = None,
    ) -> None:
        self._app_id = app_id
        self._module_ids = module_ids
        self._workspace = workspace
        self._allowed_paths = allowed_paths or []
        self._sandbox_config = sandbox_config or {}
        self._state = WorkerState.DEAD
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[IPCResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        self._state = WorkerState.SPAWNING

        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in sys.path if p
        )

        preexec = _make_preexec()

        self._process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=preexec,
        )

        init_msg = json.dumps({
            "id": 0,
            "app_id": self._app_id,
            "modules": self._module_ids,
            "workspace": self._workspace,
            "allowed_paths": self._allowed_paths,
            "sandbox": self._sandbox_config,
        }).encode() + b"\n"

        self._process.stdin.write(init_msg)
        await self._process.stdin.drain()

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=60,
            )
        except asyncio.TimeoutError:
            self._state = WorkerState.DEAD
            stderr = await self._read_stderr()
            raise RuntimeError(f"Worker startup timeout:\n{stderr}")

        if not line or not line.strip():
            self._state = WorkerState.DEAD
            stderr = await self._read_stderr()
            raise RuntimeError(f"Worker crashed during startup:\n{stderr}")

        resp = decode_response(line)
        if not resp.success:
            self._state = WorkerState.DEAD
            raise RuntimeError(f"Worker init failed: {resp.error}")

        self._state = WorkerState.READY
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("sandbox_worker_ready app=%s pid=%d modules=%s",
                     self._app_id, self._process.pid, self._module_ids)

    async def send_exec(
        self,
        module: str,
        action: str,
        params: dict[str, Any],
        workspace: str = "",
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await self._request(
            "exec",
            module=module,
            action=action,
            params=params,
            workspace=workspace or self._workspace,
            constraints=constraints or {},
        )
        if resp.success:
            return resp.data
        raise RuntimeError(resp.error)

    def update_workspace(self, workspace: str) -> None:
        self._workspace = workspace

    def mark_tainted(self) -> None:
        if self._state == WorkerState.READY:
            self._state = WorkerState.TAINTED

    async def stop(self) -> None:
        if not self.is_alive:
            self._state = WorkerState.DEAD
            return

        try:
            await self._send(IPCRequest(id=0, method="shutdown"))
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._process.kill()

        self._state = WorkerState.DEAD

        if self._reader_task:
            self._reader_task.cancel()

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("Worker stopped"))
        self._pending.clear()

    async def _request(self, method: str, **payload: Any) -> IPCResponse:
        if self._state != WorkerState.READY:
            raise RuntimeError(
                f"Worker not ready (state={self._state.value})"
            )

        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        req = IPCRequest(id=req_id, method=method, payload=payload)
        fut: asyncio.Future[IPCResponse] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut

        await self._send(req)
        return await fut

    async def _send(self, req: IPCRequest) -> None:
        if not self.is_alive:
            raise RuntimeError(f"Worker {self._app_id} is not running")
        data = encode_request(req)
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        try:
            while self.is_alive:
                line = await self._process.stdout.readline()
                if not line:
                    break

                try:
                    resp = decode_response(line)
                except Exception:
                    continue

                fut = self._pending.pop(resp.id, None)
                if fut and not fut.done():
                    fut.set_result(resp)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("worker_reader_error app=%s: %s", self._app_id, exc)
        finally:
            if self._state != WorkerState.DEAD:
                self._state = WorkerState.DEAD

    async def _read_stderr(self) -> str:
        if not self._process or not self._process.stderr:
            return "(no stderr)"
        try:
            data = await asyncio.wait_for(self._process.stderr.read(), timeout=5)
            lines = data.decode(errors="replace").strip().splitlines()
            return "\n".join(lines[-5:]) if lines else "(empty)"
        except Exception:
            return "(read error)"


class AppSandboxWorker:
    """Worker with deferred sandbox — starts warm, sandboxed later per-session.

    Unlike ``SandboxWorker`` which applies the OS sandbox immediately at
    startup, ``AppSandboxWorker`` boots into a WARM state (modules loaded,
    no sandbox) and waits for ``apply_sandbox()`` to lock it down for a
    specific session's workspace.

    This enables warm worker pools where bootstrap cost (~2-5s) is paid
    once, and per-session sandbox application is near-instant (~0.1ms).
    """

    def __init__(
        self,
        compiled: Any,
        app_id: str,
        *,
        warm_pool: bool = False,
    ) -> None:
        self._compiled = compiled
        self._app_id = app_id
        self._warm_pool = warm_pool
        self._workspace = ""
        self._session_id = ""
        self._state = WorkerState.DEAD
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[IPCResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Spawn and bootstrap the worker.

        If warm_pool=True, the worker enters WARM state (no sandbox).
        Otherwise behaves identically to SandboxWorker — immediate sandbox.
        """
        self._state = WorkerState.SPAWNING

        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in sys.path if p
        )

        preexec = _make_preexec()

        self._process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            preexec_fn=preexec,
        )

        # Build init message from compiled app
        module_ids = list(self._compiled.modules.keys())
        workspace = getattr(
            getattr(self._compiled, "execution", None), "workspace", "",
        ) or ""

        init_msg = json.dumps({
            "id": 0,
            "app_id": self._app_id,
            "modules": module_ids,
            "workspace": workspace,
            "allowed_paths": [],
            "sandbox": {},
            "warm_pool": self._warm_pool,
        }).encode() + b"\n"

        self._process.stdin.write(init_msg)
        await self._process.stdin.drain()

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=60,
            )
        except asyncio.TimeoutError:
            self._state = WorkerState.DEAD
            stderr = await self._read_stderr()
            raise RuntimeError(f"Worker startup timeout:\n{stderr}")

        if not line or not line.strip():
            self._state = WorkerState.DEAD
            stderr = await self._read_stderr()
            raise RuntimeError(f"Worker crashed during startup:\n{stderr}")

        resp = decode_response(line)
        if not resp.success:
            self._state = WorkerState.DEAD
            raise RuntimeError(f"Worker init failed: {resp.error}")

        if self._warm_pool and resp.data.get("warm"):
            self._state = WorkerState.WARM
        else:
            self._state = WorkerState.READY

        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info(
            "app_sandbox_worker_%s app=%s pid=%d modules=%s",
            self._state.value, self._app_id,
            self._process.pid, module_ids,
        )

    async def apply_sandbox(
        self,
        workspace: str,
        session_id: str,
        namespaces: set[str] | None = None,
        hardening: dict[str, Any] | None = None,
        audit: bool = False,
    ) -> None:
        """Apply the OS sandbox for a specific session.

        Transitions WARM -> SANDBOXED. Raises if worker is not WARM.
        """
        if self._state != WorkerState.WARM:
            raise RuntimeError(
                f"Cannot apply sandbox: worker not warm "
                f"(state={self._state.value})"
            )

        from .ipc import make_sandbox_request
        req = make_sandbox_request(
            workspace=workspace,
            session_id=session_id,
            namespaces=namespaces,
            hardening=hardening,
            audit=audit,
        )

        resp = await self._request_raw(req)

        if not resp.success:
            self._state = WorkerState.DEAD
            raise RuntimeError(f"Sandbox apply failed: {resp.error}")

        if resp.data.get("sandbox_ready"):
            self._workspace = workspace
            self._session_id = session_id
            self._state = WorkerState.SANDBOXED
            logger.info(
                "app_sandbox_worker_sandboxed app=%s session=%s workspace=%s active=%s",
                self._app_id, session_id, workspace,
                resp.data.get("active", []),
            )
        else:
            self._state = WorkerState.DEAD
            raise RuntimeError("Worker did not confirm sandbox_ready")

    async def health(self) -> dict[str, Any]:
        """Query the worker's health stats."""
        from .ipc import make_health_request
        req = make_health_request()
        resp = await self._request_raw(req)
        if resp.success:
            return resp.data
        raise RuntimeError(f"Health check failed: {resp.error}")

    async def send_exec(
        self,
        module: str,
        action: str,
        params: dict[str, Any],
        workspace: str = "",
        constraints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a tool action in the sandboxed worker."""
        if self._state not in (WorkerState.READY, WorkerState.SANDBOXED):
            raise RuntimeError(
                f"Worker not ready (state={self._state.value})"
            )

        resp = await self._request(
            "exec",
            module=module,
            action=action,
            params=params,
            workspace=workspace or self._workspace,
            constraints=constraints or {},
        )
        if resp.success:
            return resp.data
        raise RuntimeError(resp.error)

    def mark_tainted(self) -> None:
        if self._state in (WorkerState.READY, WorkerState.SANDBOXED):
            self._state = WorkerState.TAINTED

    async def stop(self) -> None:
        if not self.is_alive:
            self._state = WorkerState.DEAD
            return

        try:
            await self._send(IPCRequest(id=0, method="shutdown"))
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._process.kill()

        self._state = WorkerState.DEAD

        if self._reader_task:
            self._reader_task.cancel()

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("Worker stopped"))
        self._pending.clear()

    # ── Internal ─────────────────────────────────────────────────

    async def _request_raw(self, req: IPCRequest) -> IPCResponse:
        """Send a pre-built IPCRequest and await the response."""
        fut: asyncio.Future[IPCResponse] = asyncio.get_event_loop().create_future()
        self._pending[req.id] = fut
        await self._send(req)
        return await fut

    async def _request(self, method: str, **payload: Any) -> IPCResponse:
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        req = IPCRequest(id=req_id, method=method, payload=payload)
        fut: asyncio.Future[IPCResponse] = asyncio.get_event_loop().create_future()
        self._pending[req.id] = fut
        await self._send(req)
        return await fut

    async def _send(self, req: IPCRequest) -> None:
        if not self.is_alive:
            raise RuntimeError(f"Worker {self._app_id} is not running")
        data = encode_request(req)
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_loop(self) -> None:
        try:
            while self.is_alive:
                line = await self._process.stdout.readline()
                if not line:
                    break

                try:
                    resp = decode_response(line)
                except Exception:
                    continue

                fut = self._pending.pop(resp.id, None)
                if fut and not fut.done():
                    fut.set_result(resp)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("worker_reader_error app=%s: %s", self._app_id, exc)
        finally:
            if self._state != WorkerState.DEAD:
                self._state = WorkerState.DEAD

    async def _read_stderr(self) -> str:
        if not self._process or not self._process.stderr:
            return "(no stderr)"
        try:
            data = await asyncio.wait_for(self._process.stderr.read(), timeout=5)
            lines = data.decode(errors="replace").strip().splitlines()
            return "\n".join(lines[-5:]) if lines else "(empty)"
        except Exception:
            return "(read error)"


def _make_preexec():
    import platform
    if platform.system() != "Linux":
        return None

    def preexec():
        import signal
        import ctypes
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(1, signal.SIGKILL)
        except Exception:
            pass

    return preexec
