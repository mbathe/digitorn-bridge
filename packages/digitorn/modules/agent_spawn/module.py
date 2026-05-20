"""Agent Spawn Module - Agent tool with mode dispatch."""

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
        # Per-session spawn locks so different sessions don't contend.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._spawn_locks_guard = asyncio.Lock()
        self._session_module_cache: dict[str, dict[str, Any]] = {}
        self._agent_metrics: dict[str, dict[str, Any]] = {}
        # Incremental running counters (O(1) capacity checks); decremented
        # by the watchdog on every terminal task transition.
        self._running_count_by_session: dict[str, int] = {}
        self._total_running_count: int = 0
        self._cleanup_task: asyncio.Task[None] | None = None

        try:
            from digitorn.core.config import get_settings
            _cfg = get_settings().agent_spawn
            self._max_workers: int = _cfg.max_workers
            self._max_workers_global: int = _cfg.max_workers_global
            self._cleanup_age: float = _cfg.cleanup_age
            self._cleanup_interval: float = _cfg.cleanup_interval
            self._max_cached_sessions: int = getattr(_cfg, "max_cached_sessions", 100)
        except Exception:
            self._max_workers: int = 20
            self._max_workers_global: int = 200
            self._cleanup_age: float = 300.0
            self._cleanup_interval: float = 30.0
            self._max_cached_sessions: int = 100

    async def _get_spawn_lock(self, session_id: str) -> asyncio.Lock:
        async with self._spawn_locks_guard:
            lock = self._spawn_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._spawn_locks[session_id] = lock
            return lock

    def _ensure_cleanup_task(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def _periodic() -> None:
            while True:
                try:
                    await asyncio.sleep(self._cleanup_interval)
                except asyncio.CancelledError:
                    return
                try:
                    self._cleanup_completed()
                except Exception as exc:
                    logger.debug("agent_spawn periodic cleanup failed: %s", exc)
                try:
                    await self._evict_cache_lru()
                except Exception as exc:
                    logger.debug(
                        "agent_spawn cache LRU eviction failed: %s", exc,
                    )
        self._cleanup_task = loop.create_task(
            _periodic(), name="agent_spawn:periodic_cleanup",
        )

    async def _evict_cache_lru(self) -> None:
        cache = self._session_module_cache
        if len(cache) <= self._max_cached_sessions:
            return
        ordered = sorted(
            cache.items(),
            key=lambda kv: kv[1].get("last_used_at", 0.0),
        )
        evict_count = len(cache) - self._max_cached_sessions
        victims = ordered[:evict_count]
        for sid, cached in victims:
            cache.pop(sid, None)
            if not cached.get("owns_modules"):
                continue
            iso_modules = cached.get("isolated_modules", {})
            for mid, mod in iso_modules.items():
                try:
                    await mod.on_stop()
                except Exception as exc:
                    logger.debug(
                        "agent_spawn LRU evict: on_stop failed sid=%s mid=%s: %s",
                        sid, mid, exc,
                    )
            try:
                from digitorn.modules.agent_spawn.runner import _CACHE_BUILD_LOCKS
                _CACHE_BUILD_LOCKS.pop(sid, None)
            except Exception as exc:
                logger.debug("agent_spawn LRU evict: lock cleanup failed sid=%s: %s", sid, exc)
        if victims:
            logger.info(
                "agent_spawn LRU evicted %d cache entries (cap=%d)",
                len(victims), self._max_cached_sessions,
            )

    def _session_id(self) -> str:
        ctx = self._context_var.get()
        if ctx is not None and ctx.session_id:
            return ctx.session_id
        return "_standalone"

    async def _resolve_specialist_provider(
        self,
        spec: dict[str, Any],
        deployed_provider: Any,
        parent_ctx: Any,
        modules: dict[str, Any],
    ) -> Any:
        brain = spec.get("brain")
        if brain is None:
            return deployed_provider

        try:
            from digitorn.core.credentials.gateway_resolver import (
                resolve_session_provider,
            )
            from digitorn.core.credentials.byok_store import is_byok_enabled
            from digitorn.core.config import get_settings as _get_settings
        except Exception as exc:
            logger.debug(
                "agent_spawn: gateway_resolver unavailable, keeping deployed: %s",
                exc,
            )
            return deployed_provider

        user_id = getattr(parent_ctx, "user_id", None)
        app_id = getattr(parent_ctx, "app_id", None) or "_unknown"

        try:
            byok_on = await is_byok_enabled(user_id, app_id) if user_id else False
        except Exception as exc:
            logger.debug(
                "agent_spawn: is_byok_enabled failed (assuming False): %s", exc,
            )
            byok_on = False

        agent_wrapper = type("_Wrap", (), {"brain": brain})()

        # inject `llm_provider` (infra singleton) into the resolver
        # input so it can decide KEEP vs ROUTE; specialists' filtered
        # modules list never declares it.
        resolver_modules = dict(modules)
        if "llm_provider" not in resolver_modules:
            cached_llm = spec.get("llm_module")
            if cached_llm is not None:
                resolver_modules["llm_provider"] = cached_llm

        try:
            resolved = await resolve_session_provider(
                deployed_provider=deployed_provider,
                agent=agent_wrapper,
                user_id=user_id,
                app_id=app_id,
                modules=resolver_modules,
                settings=_get_settings(),
                byok_enabled=byok_on,
            )
        except Exception as exc:
            logger.warning(
                "agent_spawn: resolver crashed for specialist=%s, "
                "keeping deployed provider: %s",
                spec.get("specialty", "?"), exc, exc_info=True,
            )
            return deployed_provider

        logger.info(
            "agent_spawn._resolve_specialist_provider: specialty=%s user=%s app=%s "
            "byok=%s deployed.hint=%s resolved.hint=%s same_object=%s",
            spec.get("specialty", "?"), user_id, app_id, byok_on,
            getattr(deployed_provider, "provider_hint", "?"),
            getattr(resolved, "provider_hint", "?"),
            resolved is deployed_provider,
        )

        if resolved is not deployed_provider:
            logger.info(
                "agent_spawn: specialist=%s ROUTE-VIA-GATEWAY (user=%s app=%s)",
                spec.get("specialty", "?"), user_id, app_id,
            )
        return resolved

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
        return self._total_running_count

    def _session_running(self, session_id: str) -> int:
        return self._running_count_by_session.get(session_id, 0)

    def _bump_running(self, session_id: str) -> None:
        self._running_count_by_session[session_id] = (
            self._running_count_by_session.get(session_id, 0) + 1
        )
        self._total_running_count += 1

    def _drop_running(self, session_id: str) -> None:
        cur = self._running_count_by_session.get(session_id, 0)
        if cur <= 1:
            self._running_count_by_session.pop(session_id, None)
        else:
            self._running_count_by_session[session_id] = cur - 1
        if self._total_running_count > 0:
            self._total_running_count -= 1

    def _emit_spawn_metric(
        self,
        app_id: str | None,
        session_id: str,
        specialist: str | None,
    ) -> None:
        try:
            from digitorn.core.metrics import metrics
            spec = specialist or "generic"
            metrics.inc("agent_spawn_total", app_id=app_id, specialist=spec)
            metrics.inc_gauge(
                "agent_running", 1.0,
                app_id=app_id, session_id=session_id,
            )
        except Exception as exc:
            logger.debug("agent_spawn metrics (spawn) failed: %s", exc)

    def _emit_terminal_metric(
        self,
        app_id: str | None,
        session_id: str,
        tracked: "TrackedAgent",
    ) -> None:
        try:
            from digitorn.core.metrics import metrics
            status = "unknown"
            duration = 0.0
            if tracked.result is not None:
                status = tracked.result.status or "unknown"
                duration = float(tracked.result.duration_seconds or 0.0)
            else:
                duration = round(time.monotonic() - tracked.started_at, 1)
            spec = tracked.specialist or "generic"
            metrics.inc(
                f"agent_{status}_total",
                app_id=app_id, specialist=spec,
            )
            metrics.inc_gauge(
                "agent_running", -1.0,
                app_id=app_id, session_id=session_id,
            )
            metrics.observe(
                "agent_duration_seconds", duration,
                specialist=spec, status=status,
            )
        except Exception as exc:
            logger.debug("agent_spawn metrics (terminal) failed: %s", exc)

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Multi-agent orchestration: one Agent tool "
                "with 8 modes for spawning, monitoring, and managing sub-agents."
            ),
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
        self._cleanup_task = None
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
        self._running_count_by_session.clear()
        self._total_running_count = 0
        self._spawn_locks.clear()

    def _install_agent_watchdog(
        self,
        tracked: "TrackedAgent",
        session_id: str,
        app_id: str | None = None,
    ) -> None:
        agent_id = tracked.agent_id

        def _on_done(task: asyncio.Task) -> None:
            try:
                self._drop_running(session_id)
            except Exception as exc:
                logger.debug("agent_spawn drop_running failed: %s", exc)
            already_finalized = tracked.result is not None
            try:
                if already_finalized:
                    return
                # a cancelled task's `exception()` raises, so
                # `cancelled()` must be checked first.
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
                        status = "failed"
                        err_msg = (
                            "Sub-agent finished but never produced a "
                            "result - the runner's finalizer probably "
                            "raised. Check daemon logs."
                        )
                tracked.set_result_and_signal(AgentResult(
                    agent_id=agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status=status,
                    duration_seconds=round(
                        time.monotonic() - tracked.started_at, 1,
                    ),
                    errors=[err_msg],
                ))
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
                            "_synthetic": True,
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
                if tracked.result is None:
                    try:
                        tracked.set_result_and_signal(AgentResult(
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
                        ))
                    except Exception as emit_exc:
                        logger.debug("agent_spawn watchdog crash event emit failed: %s", emit_exc)
            finally:
                self._emit_terminal_metric(app_id, session_id, tracked)

        tracked.asyncio_task.add_done_callback(_on_done)

    async def cleanup_session(self, session_id: str) -> None:
        agents = self._agents.pop(session_id, {})
        tasks_to_wait: list[asyncio.Task] = []
        for agent in agents.values():
            was_running = agent.asyncio_task and not agent.asyncio_task.done()
            if was_running:
                agent.cancel_reason = "session aborted"
                if agent.cancel_event is not None:
                    agent.cancel_event.set()
                agent.asyncio_task.cancel()
                tasks_to_wait.append(agent.asyncio_task)
            self._agent_metrics.pop(agent.agent_id, None)
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
                except Exception as notify_exc:
                    logger.debug("agent_cancel notify failed: %s", notify_exc)
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
        try:
            from digitorn.modules.agent_spawn.runner import _CACHE_BUILD_LOCKS
            _CACHE_BUILD_LOCKS.pop(session_id, None)
        except Exception as exc:
            logger.debug("cleanup_session lock cleanup failed sid=%s: %s", session_id, exc)
        try:
            async with self._spawn_locks_guard:
                self._spawn_locks.pop(session_id, None)
        except Exception as exc:
            logger.debug("cleanup_session spawn_locks pop failed sid=%s: %s", session_id, exc)
        self._running_count_by_session.pop(session_id, None)

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

        if params.list_agents:
            return await self._mode_list()

        if params.agent_id and params.cancel:
            return await self._mode_cancel(params.agent_id)

        if params.agent_id and params.reassign:
            return await self._mode_reassign(params.agent_id, params.reassign)

        if params.agent_ids is not None:
            return await self._mode_wait_all(params.agent_ids, params.timeout)

        if params.agent_id and params.wait:
            return await self._mode_wait_one(params.agent_id, params.timeout)

        if params.agent_id and not params.prompt:
            return await self._mode_status(params.agent_id)

        if params.prompt:
            return await self._mode_spawn(params)

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

    async def _mode_spawn(self, params: AgentParams) -> ActionResult:
        self._ensure_cleanup_task()

        parent_ctx = self._context_var.get()

        if parent_ctx is not None and getattr(parent_ctx, "role", "") == "worker":
            return ActionResult(
                success=False,
                error=(
                    "Sub-agents cannot spawn sub-agents. Only the coordinator "
                    "agent may launch workers via the Agent tool."
                ),
            )

        session_id_for_cap = self._session_id()
        spawn_lock = await self._get_spawn_lock(session_id_for_cap)
        async with spawn_lock:
            session_running = self._session_running(session_id_for_cap)
            if session_running >= self._max_workers:
                return ActionResult(
                    success=False,
                    error=(
                        f"Session pool full: {session_running}/{self._max_workers} agents "
                        f"running for this session. Wait for one to finish, cancel an "
                        f"agent, or raise `agent_spawn.max_workers` in the daemon "
                        f"config (max 500)."
                    ),
                )
            global_cap = self._max_workers_global
            running = self._total_running()
            if running >= global_cap:
                return ActionResult(
                    success=False,
                    error=(
                        f"Daemon pool full: {running}/{global_cap} total agents "
                        f"running across all sessions. Wait for some to finish "
                        f"or raise `agent_spawn.max_workers_global` in the "
                        f"daemon config (max 2000)."
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
                # re-run gateway routing so specialists honour the
                # quota tracker + JWT gate just like the entry agent.
                base_provider = await self._resolve_specialist_provider(
                    spec, base_provider, parent_ctx, modules,
                )
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

            # per-agent SDK clone so each gets its own httpx pool;
            # 50 agents queueing on a shared pool would serialise.
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
            tracked.ensure_event()

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

            # snapshot under the spawn lock so the child sees a
            # consistent point-in-time view of the parent's memory.
            parent_memory_seed = self._capture_parent_memory_seed(
                parent_ctx,
            )

            tracked.asyncio_task = asyncio.create_task(
                self._run_agent(
                    tracked, provider, system_prompt, tools,
                    modules, native_tool_use, tool_injection,
                    session_id=session_id,
                    parent_ctx=parent_ctx,
                    parent_memory_seed=parent_memory_seed,
                ),
                name=f"agent_spawn:{agent_id}",
            )

            self._session_agents(session_id)[agent_id] = tracked
            self._bump_running(session_id)
            _app_id_metric = getattr(parent_ctx, "app_id", None)
            self._emit_spawn_metric(
                _app_id_metric, session_id, params.specialist,
            )
            self._install_agent_watchdog(
                tracked, session_id, app_id=_app_id_metric,
            )

        logger.info(
            "agent_spawn: launched %s specialist=%s task=%s",
            agent_id, params.specialist, (params.prompt or "")[:60],
        )

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
            except Exception as exc:
                logger.debug("spawn_agent notify failed: %s", exc)

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
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        elapsed = round(time.monotonic() - tracked.started_at, 1)

        # Last-resort: task done but no terminal result for >5s -- both
        # the runner finalizer and the watchdog failed; synthesise one.
        _GHOST_AGENT_GRACE_S = 5.0
        if (
            tracked.result is None
            and tracked.asyncio_task and tracked.asyncio_task.done()
            and (time.monotonic() - tracked.started_at) > _GHOST_AGENT_GRACE_S
        ):
            tracked.set_result_and_signal(AgentResult(
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
            ))
        if tracked.result:
            data = tracked.result.to_dict()
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
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        if tracked.result is not None:
            data = tracked.result.to_dict()
            data["description"] = tracked.description
            # keep failed/cancelled for reassign; only drop completed.
            if tracked.result.status == "completed":
                self._session_agents().pop(agent_id, None)
                self._agent_metrics.pop(agent_id, None)
            return ActionResult(success=True, data=data)

        ev = tracked.ensure_event()
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Agent '{agent_id}' still running after {timeout}s. Check later with Agent(agent_id='{agent_id}').",
            )
        except asyncio.CancelledError:
            pass

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
            if tracked.result.status == "completed":
                self._session_agents().pop(agent_id, None)
                self._agent_metrics.pop(agent_id, None)
            return ActionResult(success=True, data=data)

        return ActionResult(success=False, error=f"Agent '{agent_id}' finished but no result captured.")

    async def _mode_wait_all(self, agent_ids: list[str] | None, timeout: float) -> ActionResult:
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
                if tracked.result:
                    already_done.append(tracked.result.to_dict())
                else:
                    already_done.append({"agent_id": aid, "status": "unknown"})
                continue
            tasks_to_wait.append((aid, tracked.asyncio_task))

        if tasks_to_wait:
            aws = [tracked_for_aid.ensure_event().wait()
                   for tracked_for_aid in (
                       session_agents.get(aid) for aid, _ in tasks_to_wait
                   ) if tracked_for_aid is not None]
            if aws:
                await asyncio.wait(
                    [asyncio.create_task(aw, name="agent_wait_event") for aw in aws],
                    timeout=timeout,
                    return_when=asyncio.ALL_COMPLETED,
                )

        results = list(already_done)
        timed_out = []

        for aid, task in tasks_to_wait:
            tracked = session_agents.get(aid)
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
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        if tracked.asyncio_task and not tracked.asyncio_task.done():
            # flip cooperative cancel before the hard cancel so
            # the loop bails at the next turn even if cancel() is swallowed.
            tracked.cancel_reason = "cancelled by coordinator"
            if tracked.cancel_event is not None:
                tracked.cancel_event.set()
            tracked.asyncio_task.cancel()
            try:
                await asyncio.wait_for(tracked.asyncio_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

            if tracked.result is None:
                tracked.set_result_and_signal(AgentResult(
                    agent_id=agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status="cancelled",
                ))

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
                except Exception as exc:
                    logger.debug("agent_cancel notify failed: %s", exc)

            return ActionResult(success=True, data={
                "agent_id": agent_id,
                "status": "cancelled",
            })

        return ActionResult(success=False, error=f"Agent '{agent_id}' is not running.")

    async def _mode_reassign(self, agent_id: str, new_task: str) -> ActionResult:
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

    def _capture_parent_memory_seed(
        self, parent_ctx: ExecutionContext | None,
    ) -> dict[str, Any] | None:
        if parent_ctx is None:
            return None
        memory_module = getattr(parent_ctx, "memory_module", None)
        if memory_module is None:
            return None
        try:
            store = memory_module.store
        except AttributeError:
            return None
        if store is None:
            return None
        working = getattr(store, "working", None)
        if working is None:
            return None
        try:
            todos_serialised: list[dict[str, Any]] = []
            for item in (working.todos or [])[:20]:
                try:
                    todos_serialised.append({
                        "id": getattr(item, "id", ""),
                        "content": getattr(item, "content", ""),
                        "status": getattr(
                            getattr(item, "status", None), "value", "",
                        ) or str(getattr(item, "status", "")),
                    })
                except Exception:
                    continue
            return {
                "original_request": (working.original_request or "")[:500],
                "goal": (working.goal or "")[:300],
                "sub_goals": list(working.sub_goals or [])[:10],
                "todos": todos_serialised,
                "key_facts": list(working.key_facts or [])[-7:],
            }
        except Exception as exc:
            logger.debug(
                "agent_spawn: parent memory snapshot failed: %s", exc,
            )
            return None

    def _build_metrics_relay(
        self,
        agent_id: str,
        session_id: str | None,
        specialist: str | None,
        task: str,
    ) -> Any:
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
                preview = ""
                if etype == "tool_call":
                    tool_name = event.get("name", "")
                    preview = f"Called {tool_name}" if tool_name else ""
                elif etype == "turn_complete":
                    preview = event.get("content", "")[:200] if event.get("content") else ""

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
        parent_memory_seed: dict[str, Any] | None = None,
    ) -> None:
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

        _parent_ws = getattr(parent_ctx, "workspace", None)
        effective_workspace = _parent_ws or self.workspace
        if not _parent_ws and effective_workspace:
            logger.info(
                "agent_spawn: %s parent_ctx.workspace missing - falling back to %s",
                tracked.agent_id, effective_workspace,
            )

        await self._fire_agent_hook(
            "agent_spawn", parent_ctx, tracked, session_id,
        )

        try:
            from digitorn.core.runtime import run_tracker as _runs
            _parent_run = getattr(parent_ctx, "current_run_id", None)
            if _parent_run:
                _runs.increment_sub_agents_spawned(_parent_run)
                _runs.emit_event(_parent_run, "sub_agent", {
                    "event": "spawned",
                    "child_agent_id": tracked.agent_id,
                    "specialist": tracked.specialist,
                    "task_preview": (tracked.task or "")[:200],
                })
        except Exception as exc:
            logger.debug("agent_spawn start notify failed: %s", exc)

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
                        parent_run_id=getattr(parent_ctx, "current_run_id", None),
                        app_id=getattr(parent_ctx, "app_id", None),
                        parent_memory_seed=parent_memory_seed,
                        tracked=tracked,
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
                            except Exception as exc:
                                logger.debug("agent_retrying notify failed: %s", exc)
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
                tracked.set_result_and_signal(final_result)
            elif tracked.result is None:
                tracked.set_result_and_signal(AgentResult(
                    agent_id=tracked.agent_id,
                    task=tracked.task,
                    specialist=tracked.specialist,
                    status="failed",
                    errors=["agent finished with no result"],
                ))
            await self._fire_agent_hook(
                "agent_complete", parent_ctx, tracked, session_id,
                result=tracked.result,
            )
            # only close clones; closing the shared coordinator
            # provider would break every other agent and the parent turn.
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
        try:
            cb = getattr(parent_ctx, "context_builder", None)
            hook_runner = getattr(cb, "hook_runner", None)
            if hook_runner is None:
                return
            from digitorn.core.runtime.hooks import TurnState
            from types import SimpleNamespace
            tool_result = None
            if result is not None:
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
