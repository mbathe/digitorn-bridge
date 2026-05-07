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
        # Per-session spawn locks. The single global lock used to be the
        # bottleneck under fan-out: 100 spawns from 5 sessions queued
        # serially through one mutex. Splitting per-session means
        # different sessions can spawn in parallel while still keeping
        # capacity checks atomic within a session.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._spawn_locks_guard = asyncio.Lock()
        self._session_module_cache: dict[str, dict[str, Any]] = {}
        self._agent_metrics: dict[str, dict[str, Any]] = {}
        # Incremental counters - O(1) lookup vs the previous O(N)
        # iterate on every spawn / status / list call. Bumped under the
        # session lock on spawn, decremented in the watchdog when the
        # asyncio task closes (success / failure / cancel - all paths).
        self._running_count_by_session: dict[str, int] = {}
        self._total_running_count: int = 0
        # Background cleanup task - replaces the inline ``_cleanup_completed``
        # call that used to run on every spawn. Set by ``_ensure_cleanup_task``
        # the first time a spawn happens.
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

    # ── Internals ─────────────────────────────────────────────

    async def _get_spawn_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session spawn lock, creating it on demand.

        The outer ``_spawn_locks_guard`` only serializes the dict-create
        race; the returned lock is held by ``_mode_spawn`` for the
        duration of capacity-check + counter-bump. Different sessions
        proceed in parallel with no contention.
        """
        async with self._spawn_locks_guard:
            lock = self._spawn_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._spawn_locks[session_id] = lock
            return lock

    def _ensure_cleanup_task(self) -> None:
        """Start the periodic cleanup task once, lazily.

        Lazy-start because the module is instantiated before any event
        loop exists; the first spawn (which always runs inside the
        loop) triggers task creation. Idempotent across spawns.
        """
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
        """Evict least-recently-used cache entries past ``max_cached_sessions``.

        ``cleanup_session`` is the primary cleanup hook (drops the
        entry on normal session end, awaits ``on_stop`` on owned
        modules). This is the LRU backstop: if a session never calls
        ``cleanup_session`` (daemon crash mid-session, orphaned
        sub-agent, partial cleanup path), entries accumulate and each
        holds 50-100 MB of context. Past the cap we drop the oldest
        ``last_used_at``-sorted entries and call ``on_stop`` so the
        same teardown contract applies as the explicit path.
        """
        cache = self._session_module_cache
        if len(cache) <= self._max_cached_sessions:
            return
        # Sort ascending by last_used_at — oldest first. Entries
        # without a timestamp (legacy / set during a partial write)
        # sort to the front so they get evicted first.
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
            # Drop the per-session build lock too — same scaling
            # concern as ``cleanup_session``.
            try:
                from digitorn.modules.agent_spawn.runner import _CACHE_BUILD_LOCKS
                _CACHE_BUILD_LOCKS.pop(sid, None)
            except Exception:
                pass
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
        """Re-run the entry-agent gateway resolver for a specialist.

        The bootstrap stores each specialist's YAML-deployed provider
        on ``spec["provider"]`` -- that's the cold-start instance the
        compiler built from ``brain.config`` (e.g. github_copilot with
        the YAML's GH token). Calling that provider directly skips the
        gateway's quota tracker + JWT gate. We fix it by routing the
        same way ``manager_v2/_chat.py`` does for the entry agent.

        Returns the deployed provider unchanged when:
          * the spec carries no ``brain`` (legacy app, registered
            before this fix shipped),
          * the user is anonymous / local / system,
          * BYOK is on for the (user, app) pair,
          * the gateway is disabled at the daemon level,
          * the brain's provider is in LOCAL_PROVIDERS (ollama, etc.),
          * the resolver itself raises (we log + keep the deployed
            provider so a resolver bug never breaks dispatch).
        """
        brain = spec.get("brain")
        if brain is None:
            return deployed_provider

        try:
            from digitorn.core.credentials.gateway_resolver import (
                resolve_session_provider,
            )
            from digitorn.core.credentials.byok_store import is_byok_enabled
            from digitorn.core.config import get_settings as _get_settings
        except Exception as exc:  # pragma: no cover - import only fails in tests
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

        # Wrap the brain so the resolver sees a stable shape - mirrors
        # how ``_chat.py`` calls the resolver for sub-class brains.
        agent_wrapper = type("_Wrap", (), {"brain": brain})()

        try:
            resolved = await resolve_session_provider(
                deployed_provider=deployed_provider,
                agent=agent_wrapper,
                user_id=user_id,
                app_id=app_id,
                modules=modules,
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
        """Daemon-wide running count - O(1) read of the maintained counter.

        Was O(N) iterate over all agents in all sessions. With 400
        active sub-agents and a spawn rate of 100/s that was 40k dict
        ops per second under the spawn lock. Now O(1).
        """
        return self._total_running_count

    def _session_running(self, session_id: str) -> int:
        """Per-session running count - O(1) read of the maintained counter.

        Same rationale as ``_total_running``: was O(N) iterate, now
        constant time. The actual ``_running_count_by_session`` dict is
        bumped in ``_mode_spawn`` and decremented in the watchdog
        ``_on_done`` callback - every terminal path goes through it
        (success, exception, cancellation, timeout) so the counter
        cannot drift.
        """
        return self._running_count_by_session.get(session_id, 0)

    def _bump_running(self, session_id: str) -> None:
        """Atomically increment the per-session + global counters."""
        self._running_count_by_session[session_id] = (
            self._running_count_by_session.get(session_id, 0) + 1
        )
        self._total_running_count += 1

    def _drop_running(self, session_id: str) -> None:
        """Atomically decrement the per-session + global counters.

        Called from the watchdog ``add_done_callback`` (single-shot per
        task). Defensive against double-decrement: clamps to zero.
        """
        cur = self._running_count_by_session.get(session_id, 0)
        if cur <= 1:
            self._running_count_by_session.pop(session_id, None)
        else:
            self._running_count_by_session[session_id] = cur - 1
        if self._total_running_count > 0:
            self._total_running_count -= 1

    # ── Metrics emission ──────────────────────────────────────────
    #
    # The internal counters above are O(1) for capacity checks. The
    # metrics module is the externally-observable channel - exposed
    # via /api/metrics in JSON and Prometheus formats. Both layers
    # are intentionally separate: the counters are a *gate* (capacity
    # check + admission control), the metrics are *telemetry*.

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
                # Watchdog raced ahead of result-finalisation. Fall
                # back to wall-clock so the histogram still gets a
                # data point - status will be ``unknown`` which
                # surfaces as a separate Prometheus label.
                duration = round(time.monotonic() - tracked.started_at, 1)
            spec = tracked.specialist or "generic"
            # One counter per terminal status (completed / failed /
            # cancelled / timeout / unknown). Keeps Prometheus queries
            # straightforward (sum by status) without status as a label.
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
                "Multi-agent orchestration -- 1 ultra-powerful Agent tool "
                "with 8 modes for spawning, monitoring, and managing sub-agents."
            ),
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        pass

    async def on_stop(self) -> None:
        # Cancel the periodic cleanup task first - it's an unbounded
        # ``while True`` loop and would otherwise survive the daemon
        # shutdown until the loop closes (which logs a noisy
        # ``Task was destroyed but it is pending!`` warning).
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
        # Clear counters so a subsequent on_start (test-suite reload)
        # starts from a clean slate. Without this, fast cycles between
        # on_stop / on_start in tests can leak counters across runs.
        self._running_count_by_session.clear()
        self._total_running_count = 0
        self._spawn_locks.clear()

    def _install_agent_watchdog(
        self,
        tracked: "TrackedAgent",
        session_id: str,
        app_id: str | None = None,
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

        Also fires the terminal Prometheus metrics (counter +
        duration histogram) — captured in the closure rather than
        passed through the runner so the metrics emit even when the
        runner crashed before producing a result.

        See _mode_spawn for the full motivation.
        """
        agent_id = tracked.agent_id

        def _on_done(task: asyncio.Task) -> None:
            # Drop the running counters EVERY time the task ends, even
            # if a finalised result already exists. This is the single
            # decrement point - all task-end paths (return, raise,
            # cancel) go through ``add_done_callback``, so the
            # session-counter and the global counter cannot drift.
            try:
                self._drop_running(session_id)
            except Exception as exc:
                logger.debug("agent_spawn drop_running failed: %s", exc)
            already_finalized = tracked.result is not None
            try:
                # Already finalized → skip synthesis (still emit metric
                # below).
                if already_finalized:
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
                # Use the structural primitive so any waiter on
                # ``result_event`` wakes up too - matches the runner's
                # normal happy-path semantics so a watchdog-synthesised
                # terminal state isn't observably worse than a runner-
                # written one.
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
                    except Exception:
                        # If even AgentResult() constructor blows up,
                        # nothing more we can do - the safety check
                        # in ``_mode_status`` (task done + result None
                        # for >5s) is the last fallback.
                        pass
            finally:
                # Single point that emits the terminal Prometheus
                # metric — covers BOTH the runner-finalised path
                # (already_finalized=True, returned early above) and
                # every synthesis branch (cancelled / failed / crashed).
                # The helper itself swallows exceptions so a faulty
                # metrics backend never breaks the watchdog.
                self._emit_terminal_metric(app_id, session_id, tracked)

        tracked.asyncio_task.add_done_callback(_on_done)

    async def cleanup_session(self, session_id: str) -> None:
        agents = self._agents.pop(session_id, {})
        tasks_to_wait: list[asyncio.Task] = []
        for agent in agents.values():
            was_running = agent.asyncio_task and not agent.asyncio_task.done()
            if was_running:
                # Cooperative cancel signal first (so the agent loop
                # bails at the next turn boundary), then hard cancel.
                agent.cancel_reason = "session aborted"
                if agent.cancel_event is not None:
                    try:
                        agent.cancel_event.set()
                    except Exception:
                        pass
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
        # Drop our own per-session spawn lock - same scaling concern
        # (long-running daemon with many short-lived sessions). The
        # lock is only held during the brief capacity-check window.
        try:
            async with self._spawn_locks_guard:
                self._spawn_locks.pop(session_id, None)
        except Exception:
            pass
        # Drop the per-session running counter. Defensive: if any agent
        # for this session was still tracked, the watchdog already
        # decremented when its task got cancelled above.
        self._running_count_by_session.pop(session_id, None)

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
        # Lazy-start the periodic cleanup task on first spawn (idempotent).
        # Replaces the previous per-spawn ``_cleanup_completed()`` that
        # iterated every agent across every session on each call.
        self._ensure_cleanup_task()

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

        # Per-session lock: capacity check + counter bump are atomic
        # within a session, but different sessions proceed in parallel
        # (no global serialisation).
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
                        f"agent, or raise ``agent_spawn.max_workers`` in the daemon "
                        f"config (max 500)."
                    ),
                )
            # Daemon-wide ceiling. ``max_workers_global`` is configured
            # explicitly (default 200, capped at 2000) so operators can
            # tune the safety valve independently of the per-session
            # pool. Without this a single session could spawn
            # ``max_workers`` × number_of_sessions agents and exhaust
            # RAM / DB pool / file descriptors.
            global_cap = self._max_workers_global
            running = self._total_running()
            if running >= global_cap:
                return ActionResult(
                    success=False,
                    error=(
                        f"Daemon pool full: {running}/{global_cap} total agents "
                        f"running across all sessions. Wait for some to finish "
                        f"or raise ``agent_spawn.max_workers_global`` in the "
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
                # Apply the same gateway routing the entry agent gets
                # at session-start. Without this, a specialist whose
                # YAML declares ``provider: github_copilot`` would hit
                # api.githubcopilot.com directly with its YAML key,
                # bypassing the JWT auth gate AND the Digitorn quota
                # tracker. We re-run ``resolve_session_provider`` so
                # the decision is consistent with what the entry agent
                # got: BYOK / local / anonymous keep the YAML provider,
                # everyone else routes via the gateway.
                base_provider = await self._resolve_specialist_provider(
                    spec, base_provider, parent_ctx, modules,
                )
            else:
                if self._coordinator_provider is None:
                    return ActionResult(
                        success=False,
                        error="No coordinator provider configured. Cannot spawn ad-hoc agents.",
                    )
                # Coordinator is ALREADY the session-start-resolved
                # provider (the entry agent's). It went through
                # ``resolve_session_provider`` in ``_chat.py`` so
                # ad-hoc spawns inherit the right routing without
                # extra work.
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
            # Lazy-create the result-signalling event NOW (we're inside
            # the running loop, so the asyncio.Event() constructor is
            # safe). Waiters in ``_mode_wait_one`` / ``_mode_wait_all``
            # await this event instead of the task itself - the event
            # fires the moment the runner stores a terminal result,
            # closing the previously-racy window between the runner's
            # ``finally`` and the asyncio task transitioning to ``done()``.
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

            # Snapshot the parent's working memory (goal, todos, key
            # facts) so the child can be told why it was spawned in
            # the first place. Without this, the child saw only its
            # own ``task`` field and was completely unaware of the
            # parent's broader objective. We snapshot here, under the
            # spawn lock, so the child sees a consistent point-in-time
            # view of the parent's memory even if the parent mutates
            # it concurrently while the child is starting up.
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
            # Atomic counter bump while still under the spawn lock.
            # Decrement happens in ``_install_agent_watchdog._on_done``,
            # which runs on every terminal task transition - so the
            # counter cannot drift even if the runner crashes between
            # ``create_task`` and the first ``await`` inside the task.
            self._bump_running(session_id)
            # Prometheus telemetry — paired with
            # ``_emit_terminal_metric`` in the watchdog. Captures
            # ``parent_ctx.app_id`` so /api/metrics can break down
            # spawns / completions / failures per app.
            _app_id_metric = getattr(parent_ctx, "app_id", None)
            self._emit_spawn_metric(
                _app_id_metric, session_id, params.specialist,
            )
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
            self._install_agent_watchdog(
                tracked, session_id, app_id=_app_id_metric,
            )

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
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        elapsed = round(time.monotonic() - tracked.started_at, 1)

        # The ``set_result_and_signal`` primitive in the runner now
        # writes ``tracked.result`` BEFORE the asyncio task transitions
        # to ``done()``, so a previous race-condition guard
        # (``await asyncio.sleep(0.05)`` here) is no longer needed.
        # The ghost-agent safety net below stays as a last-resort
        # fallback for the rare case where BOTH the runner finalizer
        # AND the watchdog fail to record terminal state.
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

        # Wait on the structural ``result_event`` rather than the
        # asyncio Task itself. The event is set inside the runner's
        # ``finally`` BEFORE the task transitions to done, so by the
        # time we wake there is guaranteed to be a result. ``shield``
        # is no longer needed - the event isn't tied to the task's
        # cancellation propagation.
        ev = tracked.ensure_event()
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Agent '{agent_id}' still running after {timeout}s. Check later with Agent(agent_id='{agent_id}').",
            )
        except asyncio.CancelledError:
            # Outer cancellation (session abort, parent turn cancel).
            # The runner's own cancellation handling sets a "cancelled"
            # result + signals the event, so we still get a clean read
            # below. If we got cancelled before the runner did, the
            # watchdog will synthesize one shortly.
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
                # Result is set by ``set_result_and_signal`` BEFORE the
                # task transitions to done, so by the time we get here
                # ``tracked.result`` is authoritative. The previous
                # ``await asyncio.sleep(0.05)`` race-guard is no longer
                # needed.
                if tracked.result:
                    already_done.append(tracked.result.to_dict())
                else:
                    already_done.append({"agent_id": aid, "status": "unknown"})
                continue
            tasks_to_wait.append((aid, tracked.asyncio_task))

        if tasks_to_wait:
            # Wait on the structural ``result_event`` for each agent,
            # not the asyncio task. The event fires the moment the
            # runner stores its terminal result - independent of how
            # asyncio wraps the task's lifecycle.
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
        """Mode 6: cancel a running agent."""
        tracked = self._session_agents().get(agent_id)
        if tracked is None:
            return ActionResult(success=False, error=f"Agent '{agent_id}' not found.")

        if tracked.asyncio_task and not tracked.asyncio_task.done():
            # Cooperative-cancel BEFORE the hard cancel: flip the
            # event so the agent loop's per-turn check (in
            # ``agent_turn``) bails out at the next natural boundary
            # if the asyncio cancellation signal gets swallowed by a
            # blocking call. Hard ``cancel()`` is still issued right
            # after as a fallback for cases where the agent has
            # already entered an awaitable that respects cancellation.
            tracked.cancel_reason = "cancelled by coordinator"
            if tracked.cancel_event is not None:
                try:
                    tracked.cancel_event.set()
                except Exception:
                    pass
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
    # BACKGROUND RUNNER
    # ═══════════════════════════════════════════════════════════

    def _capture_parent_memory_seed(
        self, parent_ctx: ExecutionContext | None,
    ) -> dict[str, Any] | None:
        """Snapshot the parent's working memory for the child to inherit.

        Read-only: never mutates the parent's store. The returned dict
        is a shallow copy of the fields most relevant to a sub-agent
        being briefed for a specific subtask:

        - ``original_request`` - what the user asked the coordinator
        - ``goal`` - top-level objective the parent is working toward
        - ``sub_goals`` - decomposition the parent has already done
        - ``todos`` - serialised list of pending / in-progress items
        - ``key_facts`` - facts the parent has marked important
          (capped to the most recent 7 to keep prompts compact)

        Sub-agents typically don't need ``notes``, ``content_cache``,
        ``checkpoints``, or ``active_entities`` - those are
        coordinator-level scratch space. We expose a focused snapshot
        instead of the full ``WorkingMemory`` to keep the child's
        prompt tight and the inheritance contract explicit.

        Returns ``None`` when the parent has no memory module wired
        (standalone tests, sandbox workers) - the child will run
        without an inherited context section.
        """
        if parent_ctx is None:
            return None
        memory_module = getattr(parent_ctx, "memory_module", None)
        if memory_module is None:
            return None
        try:
            store = memory_module.store
        except Exception:
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
        parent_memory_seed: dict[str, Any] | None = None,
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

        # v2: bump parent run's sub_agents_spawned counter + emit a
        # ``sub_agent`` event on the parent's run timeline. These are
        # fire-and-forget; the spawn path is not blocked by tracker I/O.
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
        except Exception:
            pass

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
            # Atomic ``result-set + signal``. ``set_result_and_signal``
            # is the single termination point for every path through
            # ``_run_agent`` (success, retried failure, raised
            # exception, cancellation, daemon shutdown). Any waiter
            # blocked on ``tracked.result_event`` wakes up on this
            # call - no race with the asyncio task's done() flag.
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
