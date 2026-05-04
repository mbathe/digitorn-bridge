"""Agent Spawn Module - 1 ultra-powerful Agent tool with mode dispatch.

Single tool, 8 modes (like Shell):
  1. Spawn sync:   Agent(prompt='...')                    → run, wait, return result
  2. Spawn async:  Agent(prompt='...', wait=false)        → launch background, return agent_id
  3. Status:       Agent(agent_id='...')                   → check agent status
  4. Wait one:     Agent(agent_id='...', wait=true)        → block until done
  5. Wait all:     Agent(agent_ids=[...])                  → wait for multiple agents
  6. Cancel:       Agent(agent_id='...', cancel=true)      → terminate agent
  7. Reassign:     Agent(agent_id='...', reassign='task')  → respawn failed agent
  8. List:         Agent(list=true)                        → list all agents
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.agent_spawn.params import AgentParams
from digitorn.modules.agent_spawn.runner import (
    AgentResult,
    TrackedAgent,
    run_isolated_agent,
)

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class AgentSpawnConfig(BaseModel):
    """Pydantic config for the agent_spawn module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")


class AgentSpawnModule(BaseModule):
    """Multi-agent orchestration - 1 tool, 8 modes."""

    MODULE_ID = "agent_spawn"
    VERSION = "2.0.0"
    CONFIG_MODEL = AgentSpawnConfig

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        specialists = getattr(self, "_specialists", {})
        max_workers = getattr(self, "_max_workers", 3)
        if not specialists and max_workers <= 0:
            return []

        lines = [
            "You have sub-agents that run in parallel with their own isolated context. "
            "Each agent has its own context window - work it does does NOT consume yours.",
            "",
            "## When to delegate",
            "",
            "- You have 2+ independent tasks - call Agent multiple times in one turn, they run concurrently",
            "- A task requires reading many files or exploring a large codebase - the agent reads in ITS context",
            "- Your conversation is getting long - delegate to protect your context window",
            "- A task is self-contained and doesn't need back-and-forth with the user",
            "",
            "## When NOT to delegate",
            "",
            "- Simple operations (one grep, one file read, one shell command) - overhead isn't worth it",
            "- Tasks that need the user's conversation history - agents can't see it",
            "- Tasks where you need to make judgment calls based on the user's intent",
            "- When you only have one task - just do it yourself",
        ]

        if specialists:
            lines.append("")
            lines.append("## Available specialists")
            lines.append("")
            for sid, spec in specialists.items():
                specialty = spec.get("specialty", "general")
                lines.append(f"  - **{sid}**: {specialty}")

        lines.append("")
        lines.append("## Writing good prompts")
        lines.append("")
        lines.append(
            "Brief the agent like a colleague who just walked into the room - "
            "it has NO context from your conversation."
        )
        lines.append("")
        lines.append("Include in every prompt:")
        lines.append("- What to do and why")
        lines.append("- Relevant file paths, function names, error messages")
        lines.append("- What you've already tried or ruled out")
        lines.append("- Whether it should write code or just research")
        lines.append("")
        lines.append("Bad:  Agent(prompt='fix the bug')")
        lines.append("Good: Agent(prompt='The function parse_config() in src/config.py:42 raises "
                      "KeyError on empty YAML files. Read the function, find why it fails on "
                      "empty input, and fix it. Run pytest tests/test_config.py to verify.')")
        lines.append("")
        lines.append("**Never delegate understanding.** Don't say 'based on your findings, fix it.' "
                      "Instead, gather the findings yourself, synthesize them, THEN delegate "
                      "the specific fix with full context.")
        lines.append("")
        lines.append("## Modes")
        lines.append("")
        lines.append("**Background (default):** Agent(prompt='...') - launches in background, returns agent_id instantly")
        lines.append("**Parallel:** call multiple Agent() in one turn - they all launch concurrently, then collect with Agent(agent_ids=[...])")
        lines.append("**Blocking:** Agent(prompt='...', wait=true) - blocks until agent finishes, returns result directly")
        lines.append("**Collect:** Agent(agent_ids=[id1, id2]) - wait for background agents and get all results")
        lines.append("**Status:** Agent(agent_id='...') - check progress without blocking")
        lines.append("**Cancel:** Agent(agent_id='...', cancel=true) - stop a running agent")
        lines.append("")
        lines.append("## After receiving results")
        lines.append("")
        lines.append("- Read the result carefully - verify it makes sense before using it")
        lines.append("- Store key findings with Remember so they survive context compaction")
        lines.append("- If an agent failed, you can reassign: Agent(agent_id='...', reassign='new task')")
        lines.append("")
        lines.append(f"Max parallel agents: {max_workers}")

        return [{
            "title": "Agent Pool",
            "content": "\n".join(lines),
            "priority": 40,
        }]

    def __init__(self) -> None:
        super().__init__()
        self._agents: dict[str, dict[str, TrackedAgent]] = {}
        self._specialists: dict[str, dict[str, Any]] = {}
        self._coordinator_provider: Any = None
        self._coordinator_tools: list[dict[str, Any]] = []
        self._coordinator_modules: dict[str, Any] = {}
        self._coordinator_native_tool_use: bool = True
        self._coordinator_tool_injection: str = "discovery"
        self._relay_progress: bool = True
        self._auto_retry: int = 0
        self._notify_fn: Any | None = None
        self._spawn_lock: asyncio.Lock | None = None
        self._session_module_cache: dict[str, dict[str, Any]] = {}
        self._agent_metrics: dict[str, dict[str, Any]] = {}

        try:
            from digitorn.core.config import get_settings
            _cfg = get_settings().agent_spawn
            self._max_workers: int = _cfg.max_workers
            self._cleanup_age: float = _cfg.cleanup_age
        except Exception:
            self._max_workers: int = 3
            self._cleanup_age: float = 300.0

    # ── Internals ─────────────────────────────────────────────

    def _get_spawn_lock(self) -> asyncio.Lock:
        if self._spawn_lock is None:
            self._spawn_lock = asyncio.Lock()
        return self._spawn_lock

    def _session_id(self) -> str:
        ctx = self._context_var.get()
        if ctx is not None and ctx.session_id:
            return ctx.session_id
        return "_standalone"

    def _session_agents(self, session_id: str | None = None) -> dict[str, TrackedAgent]:
        sid = session_id or self._session_id()
        return self._agents.setdefault(sid, {})

    def _cleanup_completed(self, max_age: float | None = None) -> None:
        if max_age is None:
            max_age = self._cleanup_age
        now = time.monotonic()
        for sid in list(self._agents.keys()):
            session_agents = self._agents[sid]
            expired = [
                aid for aid, a in session_agents.items()
                if a.result is not None and (now - a.started_at) > max_age
            ]
            for aid in expired:
                session_agents.pop(aid, None)
            if not session_agents:
                self._agents.pop(sid, None)

    def _total_running(self) -> int:
        """Count agents currently running across ALL sessions (daemon-wide).

        Used as a hard ceiling so a runaway session can't exhaust the
        whole daemon. The per-session cap below (``_session_running``)
        is the primary scaling knob - this just prevents one session
        from monopolizing resources.
        """
        total = 0
        for session_agents in list(self._agents.values()):
            for a in list(session_agents.values()):
                if a.asyncio_task and not a.asyncio_task.done():
                    total += 1
        return total

    def _session_running(self, session_id: str) -> int:
        """Count running agents for a specific session.

        ``max_workers`` semantics: BEFORE this method existed, the cap was
        enforced globally - if Session A had 30 agents running, Session B
        could only spawn 20 more even with a 50-worker config. With this,
        each session gets up to ``max_workers`` independently and the
        global cap (``_total_running`` × small headroom) only fires when
        the whole daemon is under pressure.
        """
        agents = self._agents.get(session_id) or {}
        return sum(
            1 for a in agents.values()
            if a.asyncio_task and not a.asyncio_task.done()
        )

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Multi-agent orchestration -- 1 ultra-powerful Agent tool "
                "with 8 modes for spawning, monitoring, and managing sub-agents."
            ),
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        tasks_to_wait: list[asyncio.Task] = []
        for session_agents in list(self._agents.values()):
            for agent in list(session_agents.values()):
                if agent.asyncio_task and not agent.asyncio_task.done():
                    agent.asyncio_task.cancel()
                    tasks_to_wait.append(agent.asyncio_task)
        if tasks_to_wait:
            try:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            except Exception as exc:
                logger.debug("agent_spawn on_stop gather error: %s", exc)
        self._agents.clear()
        self._session_module_cache.clear()
        self._agent_metrics.clear()

    def _install_agent_watchdog(
        self, tracked: "TrackedAgent", session_id: str,
    ) -> None:
        """Backstop terminal state for a tracked agent.

        Registered as ``asyncio.Task.add_done_callback``: runs the
        moment the agent's task closes for any reason (return, raise,
        cancel). If the runner's normal happy-path already set
        ``tracked.result`` and emitted an ``agent_result`` /
        ``agent_cancel`` notification, this is a no-op. Otherwise we
        synthesize the missing terminal state from the task's
        exception/cancel signal and emit an event so the frontend's
        AgentGroup never stays stuck on "running".

        See _mode_spawn for the full motivation.
        """
        agent_id = tracked.agent_id

        def _on_done(task: asyncio.Task) -> None:
            try:
                # Already finalized → nothing to do.
                if tracked.result is not None:
                    return
                # Determine what happened from the task's terminal
                # state. ``cancelled()`` is always checked first
                # because a cancelled task's ``exception()`` raises.
                status: str
                err_msg: str
                if task.cancelled():
                    status = "cancelled"
                    err_msg = (
                        "Sub-agent task was cancelled before it could "
                        "report a result (likely a daemon shutdown, "
                        "session abort, or parent-turn cancel)."
                    )
                else:
                    exc = task.exception()
                    if exc is not None:
                        status = "failed"
                        err_msg = f"{type(exc).__name__}: {exc}"
                    else:
                        # Returned without an exception but never
                        # touched tracked.result. The runner's finally
                        # block likely raised silently.
                        status = "failed"
                        err_msg = (
                            "Sub-agent finished but never produced a "
                            "result - the runner's finalizer probably "
                            "raised. Check daemon logs."
                        )
                tracked.result = AgentResult(
                    agent_id=agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status=status,
                    duration_seconds=round(
                        time.monotonic() - tracked.started_at, 1,
                    ),
                    errors=[err_msg],
                )
                # Emit a synthetic terminal event so the frontend
                # AgentGroup transitions out of "running". Mirrors
                # the shape the runner's notify_fn would have built.
                if self._notify_fn is not None:
                    try:
                        self._notify_fn({
                            "type": f"agent_{status}",
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "status": status,
                            "specialist": tracked.specialist or "",
                            "task": tracked.task[:200],
                            "duration_seconds":
                                tracked.result.duration_seconds,
                            "tool_calls_count": 0,
                            "preview": "",
                            "result_summary": "",
                            "error": err_msg,
                            "_synthetic": True,  # debug marker
                        })
                    except Exception as exc:
                        logger.debug(
                            "agent watchdog: notify_fn failed for %s: %s",
                            agent_id, exc,
                        )
                logger.warning(
                    "agent_spawn watchdog synthesized %s for %s "
                    "(runner skipped result): %s",
                    status, agent_id, err_msg,
                )
            except Exception as exc:
                logger.exception(
                    "agent_spawn watchdog crashed for %s: %s",
                    agent_id, exc,
                )
                # Final safety net: if we crashed BEFORE setting
                # ``tracked.result``, the polling endpoint
                # ``_mode_status`` would return "running" forever for
                # this agent (the asyncio task is done but result is
                # still None). Force-set a synthetic "failed" result
                # so the frontend's AgentGroup transitions out of
                # "running" no matter what happened above.
                if tracked.result is None:
                    try:
                        tracked.result = AgentResult(
                            agent_id=agent_id,
                            task=tracked.task,
                            specialist=tracked.specialist,
                            status="failed",
                            duration_seconds=round(
                                time.monotonic() - tracked.started_at, 1,
                            ),
                            errors=[
                                f"watchdog crashed without finalizing: "
                                f"{type(exc).__name__}: {exc}"
                            ],
                        )
                    except Exception:
                        # If even AgentResult() constructor blows up,
                        # nothing more we can do - the safety check
                        # in ``_mode_status`` (task done + result None
                        # for >5s) is the last fallback.
                        pass

        tracked.asyncio_task.add_done_callback(_on_done)

    async def cleanup_session(self, session_id: str) -> None:
        agents = self._agents.pop(session_id, {})
        tasks_to_wait: list[asyncio.Task] = []
        for agent in agents.values():
            was_running = agent.asyncio_task and not agent.asyncio_task.done()
            if was_running:
                agent.asyncio_task.cancel()
                tasks_to_wait.append(agent.asyncio_task)
            self._agent_metrics.pop(agent.agent_id, None)
            # Emit cancel event so the frontend updates sidebar/chat
            if was_running and self._notify_fn:
                try:
                    elapsed = round(time.monotonic() - agent.started_at, 1)
                    self._notify_fn({
                        "type": "agent_cancel",
                        "agent_id": agent.agent_id,
                        "session_id": session_id,
                        "status": "cancelled",
                        "specialist": agent.specialist,
                        "task": agent.task[:200],
                        "reason": "Session aborted by user",
                        "duration_seconds": elapsed,
                    })
                except Exception:
                    pass
        if tasks_to_wait:
            try:
                await asyncio.gather(*tasks_to_wait, return_exceptions=True)
            except Exception as exc:
                logger.debug("agent_spawn cleanup_session gather error: %s", exc)
        cached = self._session_module_cache.pop(session_id, None)
        if cached:
            iso_modules = cached.get("isolated_modules", {})
            for mid, mod in iso_modules.items():
                try:
                    await mod.on_stop()
                except Exception as exc:
                    logger.debug(
                        "agent_spawn cache module on_stop failed mid=%s: %s",
                        mid, exc,
                    )
        # Drop the per-session build lock so the dict doesn't grow
        # unboundedly on a long-running daemon. The lock object only
        # ever held during sub-agent spawn and is safe to drop now.
        try:
            from digitorn.modules.agent_spawn.runner import _CACHE_BUILD_LOCKS
            _CACHE_BUILD_LOCKS.pop(session_id, None)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════
    # THE SINGLE TOOL - Agent
    # ═══════════════════════════════════════════════════════════

    @action(
        description="Launch a sub-agent to work on a task.",
        tool_prompt=(
            "Launch an isolated sub-agent with its own context window.\n"
            "The agent shares your workspace, filesystem, shell, and memory - "
            "but cannot see your conversation.\n"
            "\n"
            "## Default: background (non-blocking)\n"
            "\n"
            "Agents run in background by default. You get an agent_id back instantly.\n"
            "Launch multiple agents in one turn - they all run concurrently:\n"
            "  Agent(prompt='Search auth code for vulnerabilities')\n"
            "  Agent(prompt='Search database code for SQL injection')\n"
            "  Agent(prompt='Search API routes for missing validation')\n"
            "Then collect all results:\n"
            "  Agent(agent_ids=['agent_abc', 'agent_def', 'agent_ghi'])\n"
            "\n"
            "## Blocking mode (wait=true)\n"
            "\n"
            "Use wait=true when you need the result immediately before continuing:\n"
            "  Agent(prompt='Read src/auth.py and explain the OAuth flow', wait=true)\n"
            "\n"
            "## Other modes\n"
            "\n"
            "Status:   Agent(agent_id='agent_abc')               → check progress\n"
            "Cancel:   Agent(agent_id='agent_abc', cancel=true)   → stop it\n"
            "Collect:  Agent(agent_ids=['id1', 'id2'])            → wait for multiple\n"
            "Reassign: Agent(agent_id='agent_abc', reassign='Try differently: ...')\n"
            "List:     Agent(list=true)\n"
            "\n"
            "## Prompt writing rules\n"
            "\n"
            "The agent starts with ZERO context. Your prompt is everything it knows.\n"
            "\n"
            "Always include:\n"
            "- What to do and why (goal + motivation)\n"
            "- File paths, line numbers, error messages, function names\n"
            "- What you already know or ruled out\n"
            "- Whether to write code or just research\n"
            "\n"
            "Bad:  Agent(prompt='fix the bug')\n"
            "Good: Agent(prompt='parse_config() in src/config.py:42 raises KeyError on "
            "empty YAML. Read the function, fix it, run pytest tests/test_config.py.')\n"
            "\n"
            "Never delegate understanding - gather info, synthesize it yourself, "
            "then delegate the specific action with full context.\n"
        ),
        params_model=AgentParams,
        risk_level="medium",
        tags=["multi-agent", "spawn"],
        cli_label='Agent',
        cli_param='prompt',
    )
    async def agent(self, params: AgentParams) -> ActionResult:
        """Single entry point - dispatch to the right mode."""

        # ── Mode 8: List all agents ──────────────────────────
        if params.list_agents:
            return await self._mode_list()

        # ── Mode 6: Cancel agent ─────────────────────────────
        if params.agent_id and params.cancel:
            return await self._mode_cancel(params.agent_id)

        # ── Mode 7: Reassign agent ───────────────────────────
        if params.agent_id and params.reassign:
            return await self._mode_reassign(params.agent_id, params.reassign)

        # ── Mode 5: Wait for multiple agents ─────────────────
        if params.agent_ids is not None:
            return await self._mode_wait_all(params.agent_ids, params.timeout)

        # ── Mode 4: Wait for one agent ───────────────────────
        if params.agent_id and params.wait:
            return await self._mode_wait_one(params.agent_id, params.timeout)

        # ── Mode 3: Check agent status ───────────────────────
        if params.agent_id and not params.prompt:
            return await self._mode_status(params.agent_id)

        # ── Mode 1 & 2: Spawn agent (sync or background) ────
        if params.prompt:
            return await self._mode_spawn(params)

        # ── No valid mode ────────────────────────────────────
        return ActionResult(
            success=False,
            error=(
                "Must provide either:\n"
                "  - prompt (to spawn an agent)\n"
                "  - agent_id (to check/wait/cancel)\n"
                "  - agent_ids (to wait for multiple)\n"
                "  - list=true (to list all agents)"
            ),
        )

    # ═══════════════════════════════════════════════════════════
    # MODE IMPLEMENTATIONS
    # ═══════════════════════════════════════════════════════════

    async def _mode_spawn(self, params: AgentParams) -> ActionResult:
        """Mode 1 (sync) / Mode 2 (background): spawn a new agent."""
        self._cleanup_completed()

        parent_ctx = self._context_var.get()

        # Sub-agents cannot spawn sub-agents
        if parent_ctx is not None and getattr(parent_ctx, "role", "") == "worker":
            return ActionResult(
                success=False,
                error=(
                    "Sub-agents cannot spawn sub-agents. Only the coordinator "
                    "agent may launch workers via the Agent tool."
                ),
            )

        # Atomic capacity check - per-session cap is the primary knob,
        # global cap is a safety net so one runaway session can't burn
        # all the daemon's worker slots.
        async with self._get_spawn_lock():
            session_id_for_cap = self._session_id()
            session_running = self._session_running(session_id_for_cap)
            if session_running >= self._max_workers:
                return ActionResult(
                    success=False,
                    error=(
                        f"Session pool full: {session_running}/{self._max_workers} agents "
                        f"running for this session. Wait for one to finish, cancel an "
                        f"agent, or raise ``agent_spawn.max_workers`` in the daemon "
                        f"config (max 50)."
                    ),
                )
            # Daemon-wide ceiling: ``max_workers`` × max-sessions worth of
            # agents would otherwise pile up. Cap at 4× the per-session
            # limit so 4 active sessions can each saturate without
            # blocking each other, but a 5th wave of activity is throttled.
            global_cap = max(self._max_workers * 4, 100)
            running = self._total_running()
            if running >= global_cap:
                return ActionResult(
                    success=False,
                    error=(
                        f"Daemon pool full: {running}/{global_cap} total agents "
                        f"running across all sessions. Wait for some to finish "
                        f"before spawning more."
                    ),
                )

            agent_id = f"agent_{uuid.uuid4().hex[:8]}"

            if params.specialist and params.specialist in self._specialists:
                spec = self._specialists[params.specialist]
                base_provider = spec["provider"]
                system_prompt = spec["system_prompt"]
                tools = list(spec["tools"])
                modules = spec["modules"]
                native_tool_use = spec.get("native_tool_use", True)
                tool_injection = spec.get("tool_injection", "discovery")
            else:
                if self._coordinator_provider is None:
                    return ActionResult(
                        success=False,
                        error="No coordinator provider configured. Cannot spawn ad-hoc agents.",
                    )
                base_provider = self._coordinator_provider
                system_prompt = params.system_prompt or "You are a helpful assistant."
                tools = list(self._coordinator_tools)
                modules = self._coordinator_modules
                native_tool_use = self._coordinator_native_tool_use
                tool_injection = self._coordinator_tool_injection

            # Clone the provider so each sub-agent gets its own SDK client
            # (and therefore its own httpx connection pool). The shared
            # parent provider's pool is bounded - 50 agents queueing on
            # one 100-connection pool serialize at the network layer
            # even though they're separate asyncio tasks. A per-agent
            # provider lets every agent saturate its own bandwidth.
            # ``clone()`` is cheap (no I/O) - the client is built lazily
            # on the first ``initialize()`` / ``chat()`` call.
            try:
                if hasattr(base_provider, "clone"):
                    provider = base_provider.clone(provider_id_suffix=agent_id)
                else:
                    provider = base_provider
            except Exception as exc:
                logger.warning(
                    "agent_spawn: provider.clone() failed for %s, "
                    "falling back to shared provider: %s",
                    agent_id, exc,
                )
                provider = base_provider

            tracked = TrackedAgent(
                agent_id=agent_id,
                task=params.prompt,
                specialist=params.specialist,
                max_turns=params.max_turns,
                timeout=params.timeout,
                description=params.description,
            )

            session_id = self._session_id()

            self._agent_metrics[agent_id] = {
                "agent_id": agent_id,
                "session_id": session_id,
                "specialist": params.specialist,
                "task": (params.prompt or "")[:80],
                "tokens_in": 0,
                "tokens_out": 0,
                "tool_calls": 0,
                "turns": 0,
                "started_at": time.monotonic(),
            }

            tracked.asyncio_task = asyncio.create_task(
                self._run_agent(
                    tracked, provider, system_prompt, tools,
                    modules, native_tool_use, tool_injection,
                    session_id=session_id,
                    parent_ctx=parent_ctx,
                ),
                name=f"agent_spawn:{agent_id}",
            )

            self._session_agents(session_id)[agent_id] = tracked
            # ── Watchdog: synthesize a terminal result whenever the
            # asyncio task ends without one. The runner's normal path
            # sets ``tracked.result`` and emits an ``agent_*`` event,
            # but several failure modes bypass it:
            #   1. Crash BEFORE the try/except block (rare, but seen
            #      with bad provider auth that throws on `await`).
            #   2. asyncio.shield around result-set, then daemon
            #      shutdown / cancel before the finally block runs.
            #   3. Generic Exception during the `finally` block itself
            #      (memory cleanup raising, etc), preventing
            #      ``notify_fn`` from firing.
            # In all three the agent appeared "running" forever in
            # the chat / sidebar. The done-callback below runs on
            # ANY task termination (success, exception, cancellation)
            # and patches up missing state + emits a synthetic
            # ``agent_result`` so the frontend always reaches a
            # terminal state. 1:1 with the asyncio.Task.add_done_callback
            # contract: invoked on the event loop thread post-close.
            self._install_agent_watchdog(tracked, session_id)

        logger.info(
            "agent_spawn: launched %s specialist=%s task=%s",
            agent_id, params.specialist, (params.prompt or "")[:60],
        )

        # Emit spawn event for frontend
        if self._notify_fn:
            try:
                self._notify_fn({
                    "type": "agent_spawn",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "status": "spawned",
                    "specialist": params.specialist,
                    "task": (params.prompt or "")[:200],
                    "description": params.description,
                    "parent_agent": getattr(parent_ctx, "agent_id", None) if parent_ctx else None,
                })
            except Exception:
                pass

        spawn_data = {
            "agent_id": agent_id,
            "specialist": params.specialist,
            "description": params.description,
            "task": params.prompt,
            "status": "running",
            "running_agents": running + 1,
            "max_workers": self._max_workers,
        }

        # Mode 2: background - return immediately
        if not params.wait:
            return ActionResult(success=True, data=spawn_data)

        # Mode 1: sync - wait for completion
        return await self._mode_wait_one(agent_id, params.timeout)

    async def _mode_status(self, agent_id: str) -> ActionResult:
        """Mode 3: check agent status."""
        self._cleanup_completed()
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        elapsed = round(time.monotonic() - tracked.started_at, 1)

        # Already done → return full result
        # Race condition guard: task done but finally block hasn't stored result yet
        if tracked.result is None and tracked.asyncio_task and tracked.asyncio_task.done():
            await asyncio.sleep(0.05)
        # Last-resort safety net: task done for ``GHOST_AGENT_GRACE_S``+
        # but result still None means BOTH the runner finalizer AND
        # the watchdog (``_install_agent_watchdog._on_done``) failed
        # to record terminal state. Synthesize one ourselves so the
        # UI doesn't show this agent as "running" forever.
        _GHOST_AGENT_GRACE_S = 5.0
        if (
            tracked.result is None
            and tracked.asyncio_task and tracked.asyncio_task.done()
            and (time.monotonic() - tracked.started_at) > _GHOST_AGENT_GRACE_S
        ):
            tracked.result = AgentResult(
                agent_id=agent_id,
                task=tracked.task,
                specialist=tracked.specialist,
                status="failed",
                duration_seconds=round(
                    time.monotonic() - tracked.started_at, 1,
                ),
                errors=[
                    "agent task finished but neither the runner nor "
                    "the watchdog recorded a terminal result; "
                    "synthesizing failed status from _mode_status"
                ],
            )
        if tracked.result:
            data = tracked.result.to_dict()
            # Include live metrics
            metrics = self._agent_metrics.get(agent_id)
            if metrics:
                data["metrics"] = {
                    "tokens_in": metrics["tokens_in"],
                    "tokens_out": metrics["tokens_out"],
                    "tool_calls": metrics["tool_calls"],
                    "turns": metrics["turns"],
                }
            return ActionResult(success=True, data=data)

        is_running = tracked.asyncio_task and not tracked.asyncio_task.done()
        data: dict[str, Any] = {
            "agent_id": agent_id,
            "status": "running" if is_running else "unknown",
            "task": tracked.task,
            "specialist": tracked.specialist,
            "elapsed_seconds": elapsed,
        }
        metrics = self._agent_metrics.get(agent_id)
        if metrics:
            data["metrics"] = {
                "tokens_in": metrics["tokens_in"],
                "tokens_out": metrics["tokens_out"],
                "tool_calls": metrics["tool_calls"],
                "turns": metrics["turns"],
            }
        return ActionResult(success=True, data=data)

    async def _mode_wait_one(self, agent_id: str, timeout: float) -> ActionResult:
        """Mode 4: block until one agent finishes."""
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        # Already done
        if tracked.result is not None:
            data = tracked.result.to_dict()
            data["description"] = tracked.description
            # Only clean up completed agents - keep failed/cancelled for reassign
            if tracked.result.status == "completed":
                self._session_agents().pop(agent_id, None)
                self._agent_metrics.pop(agent_id, None)
            return ActionResult(success=True, data=data)

        if tracked.asyncio_task is None or tracked.asyncio_task.done():
            # Race condition: task done but finally block hasn't stored result yet
            if tracked.result is None:
                await asyncio.sleep(0.05)
            if tracked.result:
                data = tracked.result.to_dict()
                data["description"] = tracked.description
                return ActionResult(success=True, data=data)
            return ActionResult(success=False, error=f"Agent '{agent_id}' has no result.")

        try:
            await asyncio.wait_for(
                asyncio.shield(tracked.asyncio_task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Agent '{agent_id}' still running after {timeout}s. Check later with Agent(agent_id='{agent_id}').",
            )
        except asyncio.CancelledError:
            pass

        # Race condition: task done but finally block hasn't stored result yet
        if tracked.result is None and tracked.asyncio_task and tracked.asyncio_task.done():
            await asyncio.sleep(0.05)

        if tracked.result is not None:
            data = tracked.result.to_dict()
            data["description"] = tracked.description
            metrics = self._agent_metrics.get(agent_id)
            if metrics:
                data["metrics"] = {
                    "tokens_in": metrics["tokens_in"],
                    "tokens_out": metrics["tokens_out"],
                    "tool_calls": metrics["tool_calls"],
                    "turns": metrics["turns"],
                }
            # Only clean up completed agents - keep failed/cancelled for reassign
            if tracked.result.status == "completed":
                self._session_agents().pop(agent_id, None)
                self._agent_metrics.pop(agent_id, None)
            return ActionResult(success=True, data=data)

        return ActionResult(success=False, error=f"Agent '{agent_id}' finished but no result captured.")

    async def _mode_wait_all(self, agent_ids: list[str] | None, timeout: float) -> ActionResult:
        """Mode 5: wait for multiple agents."""
        session_agents = self._session_agents()

        if agent_ids:
            target_ids = agent_ids
        else:
            target_ids = [
                aid for aid, tracked in session_agents.items()
                if tracked.result is None
                and tracked.asyncio_task is not None
                and not tracked.asyncio_task.done()
            ]

        if not target_ids:
            return ActionResult(success=True, data={
                "message": "No running agents to wait for.",
                "results": [],
                "completed": 0,
            })

        tasks_to_wait: list[tuple[str, asyncio.Task]] = []
        already_done: list[dict[str, Any]] = []

        for aid in target_ids:
            tracked = session_agents.get(aid)
            if tracked is None:
                already_done.append({"agent_id": aid, "status": "not_found"})
                continue
            if tracked.result is not None:
                already_done.append(tracked.result.to_dict())
                continue
            if tracked.asyncio_task is None or tracked.asyncio_task.done():
                # Task finished but result may not be stored yet (race condition
                # between asyncio marking task done and the finally block running).
                # Give the finally block a chance to execute.
                if tracked.result is None:
                    await asyncio.sleep(0.05)
                if tracked.result:
                    already_done.append(tracked.result.to_dict())
                else:
                    already_done.append({"agent_id": aid, "status": "unknown"})
                continue
            tasks_to_wait.append((aid, tracked.asyncio_task))

        if tasks_to_wait:
            aws = [asyncio.shield(task) for _, task in tasks_to_wait]
            await asyncio.wait(aws, timeout=timeout, return_when=asyncio.ALL_COMPLETED)

        results = list(already_done)
        timed_out = []

        for aid, task in tasks_to_wait:
            tracked = session_agents.get(aid)
            # Race condition guard: task may be done() but the finally block
            # in _run_agent hasn't stored tracked.result yet. Yield once.
            if tracked and tracked.result is None and task.done():
                await asyncio.sleep(0.05)
            if tracked and tracked.result is not None:
                data = tracked.result.to_dict()
                metrics = self._agent_metrics.get(aid)
                if metrics:
                    data["metrics"] = {
                        "tokens_in": metrics["tokens_in"],
                        "tokens_out": metrics["tokens_out"],
                        "tool_calls": metrics["tool_calls"],
                        "turns": metrics["turns"],
                    }
                results.append(data)
            elif task.done():
                results.append({"agent_id": aid, "status": "finished_no_result"})
            else:
                timed_out.append(aid)
                results.append({
                    "agent_id": aid,
                    "status": "still_running",
                    "message": f"Still running after {timeout}s",
                })

        completed = sum(1 for r in results if r.get("status") == "completed")
        failed = sum(1 for r in results if r.get("status") in ("failed", "timeout"))

        return ActionResult(success=True, data={
            "results": results,
            "total": len(results),
            "completed": completed,
            "failed": failed,
            "timed_out": timed_out,
            "message": (
                f"{completed}/{len(results)} agents completed"
                + (f", {len(timed_out)} still running" if timed_out else "")
            ),
        })

    async def _mode_cancel(self, agent_id: str) -> ActionResult:
        """Mode 6: cancel a running agent."""
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        if tracked.asyncio_task and not tracked.asyncio_task.done():
            tracked.asyncio_task.cancel()
            try:
                await asyncio.wait_for(tracked.asyncio_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            if tracked.result is None:
                tracked.result = AgentResult(
                    agent_id=agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status="cancelled",
                )

            # Emit cancel event for frontend
            elapsed = round(time.monotonic() - tracked.started_at, 1)
            if self._notify_fn:
                try:
                    self._notify_fn({
                        "type": "agent_cancel",
                        "agent_id": agent_id,
                        "session_id": self._session_id(),
                        "status": "cancelled",
                        "specialist": tracked.specialist,
                        "task": tracked.task[:200],
                        "reason": "Cancelled by coordinator",
                        "duration_seconds": elapsed,
                    })
                except Exception:
                    pass

            return ActionResult(success=True, data={
                "agent_id": agent_id,
                "status": "cancelled",
            })

        return ActionResult(success=False, error=f"Agent '{agent_id}' is not running.")

    async def _mode_reassign(self, agent_id: str, new_task: str) -> ActionResult:
        """Mode 7: respawn a failed/cancelled agent with a new task."""
        old = self._session_agents().get(agent_id)
        if old is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        if old.asyncio_task and not old.asyncio_task.done():
            old.asyncio_task.cancel()
            try:
                await asyncio.wait_for(old.asyncio_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self._session_agents().pop(agent_id, None)
        self._agent_metrics.pop(agent_id, None)

        return await self._mode_spawn(AgentParams(
            prompt=new_task,
            specialist=old.specialist,
            max_turns=old.max_turns,
            timeout=old.timeout,
        ))

    async def _mode_list(self) -> ActionResult:
        """Mode 8: list all agents."""
        self._cleanup_completed()
        agents = []
        for agent_id, tracked in self._session_agents().items():
            if tracked.result:
                status = tracked.result.status
            elif tracked.asyncio_task and not tracked.asyncio_task.done():
                status = "running"
            else:
                status = "unknown"

            entry: dict[str, Any] = {
                "agent_id": agent_id,
                "task": tracked.task[:80],
                "specialist": tracked.specialist,
                "description": tracked.description,
                "status": status,
            }
            metrics = self._agent_metrics.get(agent_id)
            if metrics:
                entry["metrics"] = {
                    "tokens_in": metrics["tokens_in"],
                    "tokens_out": metrics["tokens_out"],
                    "tool_calls": metrics["tool_calls"],
                    "turns": metrics["turns"],
                }
            agents.append(entry)

        running = sum(1 for a in agents if a["status"] == "running")
        return ActionResult(success=True, data={
            "agents": agents,
            "total": len(agents),
            "running": running,
            "max_workers": self._max_workers,
        })

    # ═══════════════════════════════════════════════════════════
    # BACKGROUND RUNNER (unchanged from v1)
    # ═══════════════════════════════════════════════════════════

    def _build_metrics_relay(
        self,
        agent_id: str,
        session_id: str | None,
        specialist: str | None,
        task: str,
    ) -> Any:
        """Build a relay callback for live metrics (tokens, tool_calls, turns)."""
        notify_fn = self._notify_fn
        if notify_fn is None:
            return None

        metrics = self._agent_metrics.get(agent_id)

        started_at = metrics.get("started_at", time.monotonic()) if metrics else time.monotonic()

        def _relay(event: dict[str, Any]) -> None:
            etype = event.get("type", "")
            try:
                if metrics is not None:
                    if etype == "token_usage":
                        metrics["tokens_in"] += int(event.get("input_tokens", 0) or 0)
                        metrics["tokens_out"] += int(event.get("output_tokens", 0) or 0)
                    elif etype == "tool_call":
                        metrics["tool_calls"] += 1
                    elif etype == "turn_complete":
                        metrics["turns"] += 1
                elapsed = round(time.monotonic() - started_at, 1)
                tc = metrics["tool_calls"] if metrics else 0
                tin = metrics["tokens_in"] if metrics else 0
                tout = metrics["tokens_out"] if metrics else 0
                turns = metrics["turns"] if metrics else 0
                # Build preview from event context
                preview = ""
                if etype == "tool_call":
                    tool_name = event.get("name", "")
                    preview = f"Called {tool_name}" if tool_name else ""
                elif etype == "turn_complete":
                    preview = event.get("content", "")[:200] if event.get("content") else ""

                # Full metrics payload so the UI can display live token /
                # tool / turn counters per sub-agent without polling a
                # separate metrics endpoint. ``event_type`` lets the
                # client filter which fields to highlight (e.g. flash
                # the token counter on token_usage, the tool counter on
                # tool_call). ``tokens_total`` is precomputed for the
                # common "show one number" UI path.
                payload = {
                    "type": "agent_progress",
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "status": "running",
                    "specialist": specialist,
                    "task": task[:200],
                    "preview": preview,
                    "duration_seconds": elapsed,
                    "tool_calls_count": tc,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "tokens_total": tin + tout,
                    "turns": turns,
                    "event_type": etype,
                }
                notify_fn(payload)
            except Exception as exc:
                logger.debug("agent_spawn metrics relay failed: %s", exc)

        return _relay

    async def _run_agent(
        self,
        tracked: TrackedAgent,
        provider: Any,
        system_prompt: str,
        tools: list[dict[str, Any]],
        modules: dict[str, Any],
        native_tool_use: bool,
        tool_injection: str,
        session_id: str | None = None,
        parent_ctx: ExecutionContext | None = None,
    ) -> None:
        """Run an agent in background. Auto-retries on failure if configured."""
        attempts = 1 + self._auto_retry
        final_result: AgentResult | None = None

        relay_fn = self._build_metrics_relay(
            agent_id=tracked.agent_id,
            session_id=session_id,
            specialist=tracked.specialist,
            task=tracked.task,
        )

        compiled_constraints = getattr(parent_ctx, "compiled_constraints", None)
        sandbox_worker = getattr(parent_ctx, "sandbox_worker", None)
        if sandbox_worker is not None:
            logger.debug(
                "agent_spawn: %s inheriting sandbox_worker from parent",
                tracked.agent_id,
            )

        # Workspace inheritance: prefer parent_ctx.workspace, then the
        # module's own session-scoped workspace (ContextVar), finally the
        # bootstrap default. Passing a blank workspace to the sub-agent
        # made filesystem/shell modules resolve against an unintended
        # too-wide root.
        _parent_ws = getattr(parent_ctx, "workspace", None)
        effective_workspace = _parent_ws or self.workspace
        if not _parent_ws and effective_workspace:
            logger.info(
                "agent_spawn: %s parent_ctx.workspace missing - falling back to %s",
                tracked.agent_id, effective_workspace,
            )

        # Fire `agent_spawn` hook - lets apps log, notify, or inject
        # context before the sub-agent runs.
        await self._fire_agent_hook(
            "agent_spawn", parent_ctx, tracked, session_id,
        )

        try:
            for attempt in range(attempts):
                try:
                    result = await run_isolated_agent(
                        task=tracked.task,
                        provider=provider,
                        system_prompt=system_prompt,
                        tools=tools,
                        modules=modules,
                        agent_id=tracked.agent_id,
                        specialist=tracked.specialist,
                        max_turns=tracked.max_turns,
                        timeout=tracked.timeout,
                        native_tool_use=native_tool_use,
                        tool_injection=tool_injection,
                        notify_fn=self._notify_fn,
                        relay_fn=relay_fn,
                        relay_progress=self._relay_progress,
                        session_id=session_id,
                        workspace=effective_workspace,
                        compiled_constraints=compiled_constraints,
                        sandbox_worker=sandbox_worker,
                        direct_modules_map=getattr(parent_ctx, "direct_modules_map", None),
                        approval_queue=getattr(parent_ctx, "approval_queue", None),
                        user_id=getattr(parent_ctx, "user_id", None),
                        security_profile=getattr(parent_ctx, "security_profile", None),
                        session_module_cache=self._session_module_cache,
                    )

                    if result.status == "completed" or attempt >= attempts - 1:
                        final_result = result
                        break

                    if result.status in ("timeout", "failed"):
                        logger.info(
                            "agent_spawn: %s %s on attempt %d/%d - retrying",
                            tracked.agent_id, result.status, attempt + 1, attempts,
                        )
                        if self._notify_fn:
                            try:
                                self._notify_fn({
                                    "type": "agent_retrying",
                                    "agent_id": tracked.agent_id,
                                    "session_id": session_id,
                                    "attempt": attempt + 2,
                                    "max_attempts": attempts,
                                    "reason": result.status,
                                })
                            except Exception:
                                pass
                        continue

                    final_result = result
                    break

                except asyncio.CancelledError:
                    final_result = AgentResult(
                        agent_id=tracked.agent_id,
                        task=tracked.task,
                        specialist=tracked.specialist,
                        status="cancelled",
                    )
                    raise
                except Exception as exc:
                    if attempt >= attempts - 1:
                        final_result = AgentResult(
                            agent_id=tracked.agent_id,
                            task=tracked.task,
                            specialist=tracked.specialist,
                            status="failed",
                            errors=[str(exc)],
                        )
                        logger.warning("agent_spawn: %s crashed: %s", tracked.agent_id, exc)
                        break
                    logger.info(
                        "agent_spawn: %s crashed on attempt %d/%d - retrying: %s",
                        tracked.agent_id, attempt + 1, attempts, exc,
                    )
        finally:
            if final_result is not None:
                tracked.result = final_result
            elif tracked.result is None:
                tracked.result = AgentResult(
                    agent_id=tracked.agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status="failed",
                    errors=["agent finished with no result"],
                )
            # Fire `agent_complete` hook with the final result attached
            # to the tool_context so `{{tool.result.status}}` templates
            # can route on success / failure / cancellation.
            await self._fire_agent_hook(
                "agent_complete", parent_ctx, tracked, session_id,
                result=tracked.result,
            )
            # Close the cloned provider's HTTP client so the per-agent
            # httpx pool is released. Skip if it's the shared coordinator
            # provider (no ``_is_clone`` flag) - closing that would break
            # every other agent and the parent turn.
            if getattr(provider, "_is_clone", False) and hasattr(provider, "close"):
                try:
                    await provider.close()
                except Exception as exc:
                    logger.debug(
                        "agent_spawn: provider.close() failed for %s: %s",
                        tracked.agent_id, exc,
                    )

    async def _fire_agent_hook(
        self,
        event: str,
        parent_ctx: Any,
        tracked: "TrackedAgent",
        session_id: str | None,
        result: Any = None,
    ) -> None:
        """Fire an agent-lifecycle hook (agent_spawn / agent_complete).

        Reaches the hook runner via the parent's context_builder - the
        same path bootstrap.py attaches it on. No-op when no hook runner
        is wired (standalone tests, sandbox workers).
        """
        try:
            cb = getattr(parent_ctx, "context_builder", None)
            hook_runner = getattr(cb, "hook_runner", None)
            if hook_runner is None:
                return
            from digitorn.core.runtime.hooks import TurnState
            from types import SimpleNamespace
            tool_result = None
            if result is not None:
                # Serialise AgentResult to a plain dict for templates.
                tool_result = {
                    "agent_id": getattr(result, "agent_id", ""),
                    "specialist": getattr(result, "specialist", ""),
                    "task": getattr(result, "task", ""),
                    "status": getattr(result, "status", ""),
                    "errors": list(getattr(result, "errors", []) or []),
                    "summary": getattr(result, "summary", "") or "",
                }
            tool_ctx = SimpleNamespace(
                tool_name=f"agent.{tracked.specialist or 'sub'}",
                tool_params={
                    "agent_id": tracked.agent_id,
                    "specialist": tracked.specialist,
                    "task": tracked.task,
                },
                tool_result=tool_result,
                tool_ok=(
                    getattr(result, "status", "") == "completed"
                    if result is not None else True
                ),
                tool_elapsed=0.0,
            )
            state = TurnState(
                messages=[],
                turn=0, max_turns=0, tool_calls_count=0,
                agent_id=tracked.agent_id,
            )
            state.tool_context = tool_ctx  # type: ignore[attr-defined]
            await hook_runner.run(event, state)
        except Exception as exc:
            logger.debug("%s hook failed: %s", event, exc)
