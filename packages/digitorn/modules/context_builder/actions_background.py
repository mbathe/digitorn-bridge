"""Background task actions mixin for ContextBuilderModule."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from digitorn.modules.base import ActionResult
from digitorn.modules.context_builder.params import BackgroundRunParams
from digitorn.modules.decorators import action

logger = logging.getLogger(__name__)

_BG_SETTLE_SECONDS = 0.3

@dataclass
class UniversalBackgroundTask:
    """Tracks a background action launched via background_run."""

    task_id: str
    tool_name: str
    started_at: str
    _asyncio_task: asyncio.Task[Any] = field(repr=False)
    result: Any = None
    error: str | None = None
    finished_at: str | None = None
    session_id: str = "_standalone"
    _explicit_status: str | None = field(default=None, repr=False)

    @property
    def status(self) -> str:
        if self._explicit_status is not None:
            return self._explicit_status
        if self._asyncio_task.done():
            return "failed" if self.error else "completed"
        return "running"

    def set_status(self, value: str) -> None:
        self._explicit_status = value

    @property
    def elapsed_seconds(self) -> float:
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now(timezone.utc)
        return round((end - start).total_seconds(), 1)

    def to_summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "task_id": self.task_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }
        if self.status == "running":
            data["hint"] = (
                f"Task '{self.tool_name}' running for {self.elapsed_seconds}s. "
                f"Use BackgroundRun(task_id='{self.task_id}') to check again."
            )
        elif self.status == "completed":
            data["result"] = self.result
            data["hint"] = f"Task completed in {self.elapsed_seconds}s."
        elif self.status == "failed":
            data["result"] = self.result
            data["hint"] = f"Task failed: {self.error}"
        elif self.status == "cancelled":
            data["hint"] = "Task was cancelled."
        return data

class BackgroundActionsMixin:
    """Background task actions - 1 tool, 5 modes via hidden params."""

    def _bg_done_callback(self, task_id: str, session_id: str, asyncio_task: asyncio.Task) -> None:
        session_tasks = self._background_tasks.get(session_id, {})
        bg = session_tasks.get(task_id)
        if bg is None:
            return
        bg.finished_at = datetime.now(timezone.utc).isoformat()
        exc = asyncio_task.exception() if not asyncio_task.cancelled() else None
        if asyncio_task.cancelled():
            bg.error = "Task was cancelled."
        elif exc is not None:
            bg.error = str(exc)
        else:
            raw = asyncio_task.result()
            if isinstance(raw, ActionResult):
                if raw.success:
                    bg.result = raw.data
                else:
                    bg.error = raw.error
                    bg.result = raw.data
            else:
                bg.result = raw

        notification: dict[str, Any] = {
            "task_id": bg.task_id,
            "tool_name": bg.tool_name,
            "status": bg.status,
            "elapsed_seconds": bg.elapsed_seconds,
            "session_id": session_id,
        }
        if bg.error:
            notification["error"] = bg.error
        else:
            result_str = str(bg.result)
            if len(result_str) > 2000:
                notification["result_preview"] = result_str[:2000] + f"... ({len(result_str)} chars total)"
                notification["hint"] = (
                    f"Result truncated. Use BackgroundRun(task_id=\"{bg.task_id}\") "
                    "to get the full output."
                )
            else:
                notification["result_preview"] = result_str
                notification["hint"] = f"Use BackgroundRun(task_id='{bg.task_id}') for full result."

        # Route through push_module_notification so the SSE relay fires
        self.push_module_notification(notification)

    @action(
        description="Run any tool in the background - returns task_id immediately.",
        tool_prompt=(
            "Run a tool in the background. Returns a task_id immediately.\n"
            "You are automatically notified when the task completes or fails.\n"
            "\n"
            "## Modes\n"
            "- BackgroundRun(name='database.sql', params={query: '...'}) → launch\n"
            "- BackgroundRun(task_id='abc')                              → check status\n"
            "- BackgroundRun(task_id='abc', cancel=true)                 → cancel\n"
            "- BackgroundRun(task_id='abc', wait=true, timeout=120)      → block until done\n"
            "- BackgroundRun(list_tasks=true)                            → list all tasks\n"
            "\n"
            "## Auto-notification\n"
            "When ANY background task completes or fails, you receive a system message:\n"
            "  [BACKGROUND TASK COMPLETED] task_id=..., tool=..., elapsed=45s\n"
            "You do NOT need to poll. Continue working and you will be notified.\n"
            "\n"
            "## Note about shell commands\n"
            "For shell commands, use Bash(command='...', run_in_background=true) directly."
        ),
        params_model=BackgroundRunParams,
        risk_level="medium",
        tags=["execution", "background", "primitive"],
        side_effects=["state_mutation"],
        aliases=["arriere_plan", "lancer_tache", "bg_run", "async_run"],
        cli_label='Background',
        cli_param='name',
    )
    async def background_run(self, params: BackgroundRunParams) -> ActionResult:
        if params.list_tasks:
            return await self._bg_list()

        if params.task_id and params.cancel:
            return await self._bg_cancel(params.task_id)

        if params.task_id and params.wait:
            return await self._bg_wait(params.task_id, params.timeout)

        if params.task_id:
            return await self._bg_status(params.task_id)

        if params.name:
            return await self._bg_launch(params)

        return ActionResult(
            success=False,
            error=(
                "Provide either: name+params to launch, task_id to check/cancel/wait, "
                "or list_tasks=true to list all."
            ),
        )

    async def _bg_launch(self, params: BackgroundRunParams) -> ActionResult:
        tool, error = self._resolve_tool(params.name)
        if error:
            return ActionResult(success=False, error=error)

        task_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        coro = tool.module.execute(
            tool.action_name, dict(params.params), context=self._exec_context,
        )
        asyncio_task = asyncio.create_task(coro)

        sid = self._bg_session_id()
        bg = UniversalBackgroundTask(
            task_id=task_id,
            tool_name=tool.fqn,
            started_at=now,
            _asyncio_task=asyncio_task,
            session_id=sid,
        )
        self._session_bg_tasks(sid)[task_id] = bg

        asyncio_task.add_done_callback(
            lambda t: self._bg_done_callback(task_id, sid, t)
        )

        await asyncio.sleep(_BG_SETTLE_SECONDS)

        if bg.status == "failed":
            return ActionResult(
                success=False,
                error=f"Background task failed immediately: {bg.error}",
                data=bg.to_summary(),
            )

        logger.info("background_task_started id=%s tool=%s", task_id, tool.fqn)
        return ActionResult(success=True, data=bg.to_summary())

    async def _bg_status(self, task_id: str) -> ActionResult:
        bg = self._session_bg_tasks().get(task_id)
        if bg is None:
            return ActionResult(
                success=False,
                error=f"Task '{task_id}' not found. Use BackgroundRun(list_tasks=true) to see all.",
            )
        return ActionResult(success=True, data=bg.to_summary())

    async def _bg_cancel(self, task_id: str) -> ActionResult:
        bg = self._session_bg_tasks().get(task_id)
        if bg is None:
            return ActionResult(success=False, error=f"Task '{task_id}' not found.")

        if bg.status != "running":
            return ActionResult(success=True, data={
                "task_id": bg.task_id,
                "status": bg.status,
                "hint": f"Task already {bg.status}.",
            })

        if bg._asyncio_task is not None and not bg._asyncio_task.done():
            bg._asyncio_task.cancel()
            try:
                await bg._asyncio_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("bg_cancel_error id=%s: %s", bg.task_id, exc)

        bg.set_status("cancelled")
        bg.error = "Cancelled by agent."
        bg.finished_at = datetime.now(timezone.utc).isoformat()

        # Notify via SSE so frontend sees it too
        self.push_module_notification({
            "task_id": task_id,
            "tool_name": bg.tool_name,
            "status": "cancelled",
            "elapsed_seconds": bg.elapsed_seconds,
            "session_id": bg.session_id,
            "hint": f"Agent cancelled task '{task_id}' ({bg.tool_name}).",
        })

        logger.info("background_task_cancelled id=%s tool=%s", bg.task_id, bg.tool_name)
        return ActionResult(success=True, data={
            "task_id": bg.task_id,
            "status": "cancelled",
            "elapsed_seconds": bg.elapsed_seconds,
        })

    async def _bg_wait(self, task_id: str, timeout: float) -> ActionResult:
        bg = self._session_bg_tasks().get(task_id)
        if bg is None:
            return ActionResult(success=False, error=f"Task '{task_id}' not found.")

        if bg.status != "running":
            return ActionResult(success=True, data=bg.to_summary())

        try:
            await asyncio.wait_for(
                asyncio.shield(bg._asyncio_task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ActionResult(success=True, data={
                **bg.to_summary(),
                "hint": (
                    f"Timeout after {timeout}s - task still running. "
                    f"Use BackgroundRun(task_id='{task_id}', wait=true, timeout=300) "
                    "for a longer wait."
                ),
            })
        except (asyncio.CancelledError, Exception):
            pass

        return ActionResult(success=True, data=bg.to_summary())

    async def _bg_list(self) -> ActionResult:
        session_tasks = self._session_bg_tasks()
        sorted_tasks = sorted(
            session_tasks.values(),
            key=lambda t: (t.status != "running", t.started_at),
        )
        tasks = [t.to_summary() for t in sorted_tasks]
        running = sum(1 for t in session_tasks.values() if t.status == "running")
        completed = sum(1 for t in session_tasks.values() if t.status == "completed")
        failed = sum(1 for t in session_tasks.values() if t.status == "failed")

        return ActionResult(success=True, data={
            "tasks": tasks,
            "total": len(tasks),
            "running": running,
            "completed": completed,
            "failed": failed,
        })

    async def cancel_bg_task(self, session_id: str, task_id: str) -> dict[str, Any]:
        """Cancel a background task (user-initiated via API)."""
        session_tasks = self._background_tasks.get(session_id, {})
        bg = session_tasks.get(task_id)
        if bg is None:
            return {"success": False, "error": f"Task '{task_id}' not found."}
        if bg.status != "running":
            return {"success": True, "already_finished": True, "status": bg.status}

        if bg._asyncio_task is not None and not bg._asyncio_task.done():
            bg._asyncio_task.cancel()
            try:
                await bg._asyncio_task
            except (asyncio.CancelledError, Exception):
                pass

        bg.set_status("cancelled")
        bg.error = "Cancelled by user."
        bg.finished_at = datetime.now(timezone.utc).isoformat()

        self.push_module_notification({
            "task_id": task_id,
            "tool_name": bg.tool_name,
            "status": "cancelled",
            "elapsed_seconds": bg.elapsed_seconds,
            "session_id": session_id,
            "result_preview": "(cancelled by user)",
            "hint": f"User cancelled task '{task_id}' ({bg.tool_name}).",
        })

        return {"success": True, "task_id": task_id, "status": "cancelled"}

    def _prompt_sections_background(self) -> list[dict[str, Any]]:
        return [{
            "title": "",
            "content": (
                "## Background Tasks\n"
                "- **BackgroundRun**(name, params): Launch any tool in the background\n"
                "- **BackgroundRun**(task_id): Check task status and result\n"
                "- **BackgroundRun**(task_id, cancel=true): Cancel a running task\n"
                "- **BackgroundRun**(task_id, wait=true, timeout=60): Wait for completion\n"
                "- **BackgroundRun**(list_tasks=true): List all tasks\n"
                "\n"
                "**AUTO-NOTIFICATION**: When any background task completes or fails, "
                "you receive a system message automatically. No need to poll."
            ),
            "priority": 16,
        }]
