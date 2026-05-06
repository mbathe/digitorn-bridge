"""Isolated agent runner - executes a sub-agent in its own context.

Each sub-agent gets:
- Its own provider instance (or shared client with isolated state)
- Its own message history
- Its own tool set (filtered by specialist config)
- Its own memory store (isolated)

True parallelism: multiple runners execute as concurrent asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Per-session-id locks coordinate concurrent module-cache builds.
# Without this, spawning N sub-agents simultaneously made each one
# rebuild the isolated module set + ContextBuilder (~200ms × N),
# wasted CPU, and grew memory linearly with N. The lock means only
# one runner does the expensive build; the rest wait and reuse the
# cache entry.
_CACHE_BUILD_LOCKS: dict[str, asyncio.Lock] = {}
_CACHE_BUILD_LOCKS_GUARD = asyncio.Lock()


async def _get_cache_build_lock(cache_key: str) -> asyncio.Lock:
    async with _CACHE_BUILD_LOCKS_GUARD:
        lock = _CACHE_BUILD_LOCKS.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            _CACHE_BUILD_LOCKS[cache_key] = lock
        return lock


# Per-section caps for the inherited-context briefing. The capture
# pass in ``module.py::_capture_parent_memory_seed`` already does a
# coarse first cut (top-N items per section, single-string lengths);
# these caps are the second wall - they decide how much the sub-agent
# actually SEES in its system prompt.
_MAX_SEED_SUB_GOALS = 5
_MAX_SEED_TODOS = 8
_MAX_SEED_FACTS = 7
_MAX_SEED_ITEM_CHARS = 300       # Per-bullet cap (one fact / one todo)
_MAX_SEED_HEADER_CHARS = 500     # original_request / goal cap
_DEFAULT_SEED_TOTAL_CHARS = 4000  # Hard ceiling on the whole block


def _seed_total_chars_cap() -> int:
    """Resolve the configured global cap; fall back to default."""
    try:
        from digitorn.core.config import get_settings
        return get_settings().agent_spawn.max_seed_total_chars
    except Exception:
        return _DEFAULT_SEED_TOTAL_CHARS


def _truncate(text: str, limit: int) -> str:
    """Return ``text`` clipped to ``limit`` chars with ``…`` marker."""
    if not text:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _format_parent_memory_seed(seed: dict[str, Any] | None) -> str:
    """Render the parent-memory snapshot as a system-prompt section.

    Builds a compact ``## Inherited context`` block the sub-agent
    sees BEFORE its specialist prompt. Empty string when no seed was
    captured (parent had no memory module, standalone test, etc).

    The format is deliberately terse: prose summary at the top, then
    bulleted lists for goals / sub-goals / todos / facts. The agent
    should treat this as read-only background briefing - the
    directives section above explicitly forbids it from setting goals
    or creating todos (those are the coordinator's responsibility).

    All strings are double-capped: per-item via ``_truncate`` and the
    final block via ``_seed_total_chars_cap`` so a parent with 200
    todos / multi-KB facts can never blow up the child's prompt.
    """
    if not seed:
        return ""
    parts: list[str] = ["## Inherited context (from coordinator)\n"]
    original_request = _truncate(seed.get("original_request") or "", _MAX_SEED_HEADER_CHARS)
    if original_request:
        parts.append(f"Original user request: {original_request}\n")
    goal = _truncate(seed.get("goal") or "", _MAX_SEED_HEADER_CHARS)
    if goal:
        parts.append(f"Coordinator goal: {goal}\n")
    sub_goals = seed.get("sub_goals") or []
    if sub_goals:
        parts.append("Sub-goals:")
        for sg in sub_goals[:_MAX_SEED_SUB_GOALS]:
            parts.append(f"  - {_truncate(sg, _MAX_SEED_ITEM_CHARS)}")
        if len(sub_goals) > _MAX_SEED_SUB_GOALS:
            parts.append(f"  - … ({len(sub_goals) - _MAX_SEED_SUB_GOALS} more)")
        parts.append("")
    todos = seed.get("todos") or []
    if todos:
        parts.append("Coordinator's todo list (do NOT modify):")
        for t in todos[:_MAX_SEED_TODOS]:
            status = t.get("status", "")
            content = _truncate(t.get("content", ""), _MAX_SEED_ITEM_CHARS)
            parts.append(f"  - [{status}] {content}")
        if len(todos) > _MAX_SEED_TODOS:
            parts.append(f"  - … ({len(todos) - _MAX_SEED_TODOS} more)")
        parts.append("")
    facts = seed.get("key_facts") or []
    if facts:
        parts.append("Key facts the coordinator has gathered:")
        # Take the most recent N - older facts are usually superseded.
        kept = facts[-_MAX_SEED_FACTS:]
        for f in kept:
            parts.append(f"  - {_truncate(f, _MAX_SEED_ITEM_CHARS)}")
        if len(facts) > _MAX_SEED_FACTS:
            parts.append(f"  - … ({len(facts) - _MAX_SEED_FACTS} older facts dropped)")
        parts.append("")
    parts.append(
        "This briefing is read-only. Do not call SetGoal, TodoAdd, or "
        "TodoUpdate - the coordinator owns the global plan. Use these "
        "facts as context for your specific task below.\n"
    )
    rendered = "\n".join(parts) + "\n"
    # Final hard cap - belt-and-braces against runaway concatenation
    # if any of the contributing fields slipped past the per-item cap
    # (e.g. a custom seed dict carrying non-string values).
    total_cap = _seed_total_chars_cap()
    if len(rendered) > total_cap:
        truncation_marker = (
            f"\n\n[inherited context truncated at {total_cap} chars - "
            f"parent memory was larger; see full state in coordinator]\n"
        )
        keep = max(0, total_cap - len(truncation_marker))
        rendered = rendered[:keep] + truncation_marker
    return rendered


@dataclass
class AgentResult:
    """Structured result from a completed sub-agent."""

    agent_id: str
    task: str
    specialist: str | None = None
    status: str = "running"
    content: str = ""
    turns_used: int = 0
    duration_seconds: float = 0.0
    memory: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task": self.task,
            "specialist": self.specialist,
            "status": self.status,
            "content": self.content,
            "turns_used": self.turns_used,
            "duration_seconds": round(self.duration_seconds, 1),
            "memory": self.memory,
            "errors": self.errors,
            "tool_calls": self.tool_calls,
        }


@dataclass
class TrackedAgent:
    """A spawned agent being tracked by the pool."""

    agent_id: str
    task: str
    specialist: str | None
    asyncio_task: asyncio.Task | None = None
    result: AgentResult | None = None
    started_at: float = field(default_factory=time.monotonic)
    max_turns: int = 100
    timeout: float = 900.0
    description: str = ""  # Short label for frontend display

    # ── Structural-result primitive ──────────────────────────────
    # ``result_event`` is set the moment a terminal ``AgentResult`` is
    # written to ``self.result`` - REGARDLESS of whether the asyncio
    # Task itself has transitioned to ``done()``. This eliminates the
    # ``await asyncio.sleep(0.05)`` race-condition guards that the
    # callers of ``_mode_status`` / ``_mode_wait_one`` / ``_mode_wait_all``
    # used to need. Waiters now ``await tracked.result_event.wait()``
    # which fires AS SOON AS the runner's finally block stores the
    # result, instead of relying on observability of the task's
    # internal state.
    #
    # Lazy-init in ``ensure_event`` because dataclass default_factory
    # can't construct an asyncio object outside a running loop, and
    # ``TrackedAgent`` is built from any thread / sync context.
    result_event: asyncio.Event | None = field(default=None, repr=False)
    # ── Cooperative cancel ───────────────────────────────────────
    # ``asyncio.Task.cancel()`` is best-effort: if the agent is in a
    # blocking sync section or an uninterruptible await, the cancel
    # message gets queued but the agent runs to completion. Setting
    # ``cancel_event`` (an ``asyncio.Event``) lets the agent loop
    # bail out at every natural cancellation point (between turns)
    # without depending on the cooperative-cancel signal firing at
    # exactly the right ``await``. Lazy-created by the runner on
    # entry so it is bound to the running event loop.
    cancel_event: asyncio.Event | None = field(default=None, repr=False)
    cancel_reason: str = ""

    def ensure_event(self) -> asyncio.Event:
        """Return the ``result_event``, creating it lazily if needed.

        Must be called from within a running event loop (the typical
        path: the runner does this on entry, before the first
        ``await``). Idempotent.
        """
        if self.result_event is None:
            self.result_event = asyncio.Event()
        return self.result_event

    def set_result_and_signal(self, result: "AgentResult") -> None:
        """Atomic ``self.result = result`` + signal waiters.

        Called from the runner's ``finally`` block. Signal-after-set
        ordering guarantees a waiter that wakes on the event and
        immediately reads ``self.result`` always sees the assignment.
        """
        self.result = result
        ev = self.result_event
        if ev is not None and not ev.is_set():
            ev.set()


async def run_isolated_agent(
    task: str,
    provider: Any,
    system_prompt: str,
    tools: list[dict[str, Any]],
    modules: dict[str, Any],
    *,
    agent_id: str = "",
    specialist: str | None = None,
    parent_run_id: str | None = None,
    app_id: str | None = None,
    **kwargs: Any,
) -> AgentResult:
    """Public entrypoint — wraps the implementation in a trace span.

    The span is opened in the parent's TraceContext (propagated via
    contextvar; ``asyncio.create_task`` inherits contextvars). On the
    parent's trace dump the sub-agent appears as a child span of the
    coordinator's ``agent_turn``, so the full fan-out tree is visible.

    Final span attributes (``status``, ``turns_used``, ``tool_calls_count``,
    ``duration_seconds``) are populated from the returned ``AgentResult``
    so a single span captures both timing and outcome without needing
    manual instrumentation deep inside the runner body.
    """
    from digitorn.core.tracing import tracer

    if not agent_id:
        agent_id = f"sub_{uuid.uuid4().hex[:8]}"

    with tracer.span(
        "sub_agent_run",
        agent_id=agent_id,
        specialist=specialist or "generic",
        parent_run_id=parent_run_id or "",
        app_id=app_id or "",
    ) as span:
        result = await _run_isolated_agent_impl(
            task, provider, system_prompt, tools, modules,
            agent_id=agent_id,
            specialist=specialist,
            parent_run_id=parent_run_id,
            app_id=app_id,
            **kwargs,
        )
        try:
            span.attributes["status"] = result.status
            span.attributes["turns_used"] = result.turns_used
            span.attributes["tool_calls_count"] = len(result.tool_calls)
            span.attributes["duration_seconds"] = round(result.duration_seconds, 1)
            if result.errors:
                span.attributes["errors_count"] = len(result.errors)
                # Surface a graceful failure as a span error so trace
                # dashboards highlight it without forcing the runner
                # to ``raise`` (and lose the structured result).
                if result.status in ("failed", "timeout"):
                    span.status = "error"
        except Exception:
            pass
    return result


async def _run_isolated_agent_impl(
    task: str,
    provider: Any,
    system_prompt: str,
    tools: list[dict[str, Any]],
    modules: dict[str, Any],
    *,
    agent_id: str = "",
    specialist: str | None = None,
    max_turns: int = 100,
    timeout: float = 900.0,
    native_tool_use: bool = True,
    tool_injection: str = "discovery",
    notify_fn: Any | None = None,
    relay_fn: Any | None = None,
    relay_progress: bool = False,
    security_profile: Any = None,
    session_id: str | None = None,
    workspace: str | None = None,
    compiled_constraints: dict[str, dict[str, Any]] | None = None,
    sandbox_worker: Any = None,
    direct_modules_map: dict[str, str] | None = None,
    approval_queue: Any = None,
    user_id: str | None = None,
    session_module_cache: dict[str, dict[str, Any]] | None = None,
    parent_run_id: str | None = None,
    app_id: str | None = None,
    parent_memory_seed: dict[str, Any] | None = None,
    tracked: "TrackedAgent | None" = None,
) -> AgentResult:
    """Run a sub-agent in complete isolation.

    Creates its own AgentContext, messages, and runs agent_turn().
    Returns a structured AgentResult when done.
    """
    from digitorn.core.runtime.agent_loop import agent_turn
    from digitorn.core.runtime.types import AgentContext, ContextWindowConfig

    if not agent_id:
        agent_id = f"sub_{uuid.uuid4().hex[:8]}"

    result = AgentResult(
        agent_id=agent_id,
        task=task,
        specialist=specialist,
    )

    start = time.monotonic()

    # Strip {WORKSPACE} placeholder baked into the specialist prompt at bootstrap.
    if system_prompt:
        from digitorn.core.runtime.types import WORKSPACE_PLACEHOLDER
        system_prompt = system_prompt.replace(WORKSPACE_PLACEHOLDER, workspace or "")

    # Cooperative-cancel primitive. Lazy-created here (we are inside
    # the running event loop so ``asyncio.Event()`` is safe). The
    # parent module's ``_mode_cancel`` flips it BEFORE issuing the
    # hard ``Task.cancel()`` so the loop bails at the next turn
    # boundary even if the cancellation signal gets swallowed.
    if tracked is not None:
        tracked.cancel_event = asyncio.Event()

    ctx = AgentContext(
        agent_id=agent_id,
        role="worker",
        provider=provider,
        system_prompt=system_prompt,
        tools=tools,
        native_tool_use=native_tool_use,
        tool_injection=tool_injection,
        plan_first=True,
        generation_params={},
        context_config=ContextWindowConfig(
            max_tokens=getattr(provider, "_context_max", 50000) if hasattr(provider, "_context_max") else 50000,
            output_reserved=4096,
            strategy="summarize",
            keep_recent=6,
            auto_compact=True,
        ),
        security_profile=security_profile,
        session_id=session_id,
        workspace=workspace,
        # AS14: preserve None semantics - None means inherit, {} means deny.
        compiled_constraints=compiled_constraints if compiled_constraints is not None else {},
        sandbox_worker=sandbox_worker,
        direct_modules_map=direct_modules_map or {},
        approval_queue=approval_queue,
        # AS12: only fall back to "admin" when explicitly None - preserve "" or
        # other intentional values from the parent context.
        user_id=user_id if user_id is not None else "admin",
        # v2: link this sub-agent's run to the parent's so the dashboard
        # can render the spawn tree. agent_turn reads current_run_id as
        # the parent_run_id when it opens this sub-agent's agent_runs row.
        current_run_id=parent_run_id,
        app_id=app_id,
        cancel_event=tracked.cancel_event if tracked is not None else None,
    )

    # AS10: per-session module + index cache. Building a fresh registry +
    # ContextBuilder index per spawn costs 100-300 ms. We cache by session
    # so all sub-agents in the same session share the work; cleanup happens
    # in cleanup_session() in the parent module.
    cache_key = session_id or "_standalone"
    cache_entry: dict[str, Any] | None = None
    cache_owns_modules = False
    cache_entry = None
    cache_lock = None
    if session_module_cache is not None:
        cache_entry = session_module_cache.get(cache_key)
        if cache_entry is None:
            # Serialize the expensive build so 50 concurrent spawns share
            # ONE module set + ONE ContextBuilder instead of racing to
            # build 50 of each. The lock is per-cache-key so different
            # sessions don't block each other.
            cache_lock = await _get_cache_build_lock(cache_key)
            await cache_lock.acquire()
            # Re-check under the lock (double-check pattern).
            cache_entry = session_module_cache.get(cache_key)

    isolated_modules: dict[str, Any] = {}
    cb = None
    cb_owned_by_cache = False

    try:
        if cache_entry is not None:
            isolated_modules = cache_entry.get("isolated_modules", {})
            cb = cache_entry.get("context_builder")
            cb_owned_by_cache = cb is not None
            # Bump LRU recency on hit so an actively-used session never
            # gets evicted by ``_evict_cache_lru`` in the periodic
            # cleanup task. Cache misses set this field at write time
            # below.
            cache_entry["last_used_at"] = time.monotonic()
        else:
            try:
                from digitorn.modules.registry import ModuleRegistry
                from digitorn.core.loader import load_modules

                registry = ModuleRegistry()
                load_modules(registry, load_all=True)

                # Modules that should be SHARED (not recreated) because they carry state
                # that the sub-agent needs (e.g. memory store, config, connections)
                _SHARE_MODULES = {"memory", "web", "lsp", "filesystem", "shell"}

                for mid, mod in modules.items():
                    if mid in ("context_builder", "llm_provider", "index", "agent_spawn"):
                        continue
                    if mid in _SHARE_MODULES:
                        # Share the module instance - sub-agent uses same memory/web/lsp
                        isolated_modules[mid] = mod
                        continue
                    try:
                        fresh = registry.create(mid)
                        if hasattr(mod, "_config") and mod._config is not None:
                            try:
                                await fresh.on_start()
                                await fresh.on_config_update(
                                    mod._config if isinstance(mod._config, dict) else {}
                                )
                            except Exception as exc:
                                logger.warning(
                                    "agent_spawn %s: module %r on_start/on_config_update failed: %s",
                                    agent_id, mid, exc, exc_info=True,
                                )
                                result.errors.append(
                                    f"module '{mid}' init failed: {exc}"
                                )
                        isolated_modules[mid] = fresh
                        cache_owns_modules = True
                    except Exception as exc:
                        logger.warning(
                            "agent_spawn %s: registry.create(%r) failed, falling back to shared instance: %s",
                            agent_id, mid, exc, exc_info=True,
                        )
                        result.errors.append(
                            f"module '{mid}' create failed ({exc}); using shared fallback"
                        )
                        isolated_modules[mid] = mod
            except Exception as exc:
                logger.warning("agent_spawn: module isolation failed for %s, using shared: %s", agent_id, exc)
                isolated_modules = {
                    k: v for k, v in modules.items()
                    if k not in ("context_builder", "llm_provider", "index", "agent_spawn")
                }

            try:
                from digitorn.modules.context_builder.module import ContextBuilderModule
                from digitorn.modules.context_builder.builder import build_index

                cb = ContextBuilderModule()
                await cb.on_start()
                cb._index = build_index(isolated_modules, security_profile)
            except Exception as exc:
                logger.warning("agent_spawn: failed to build context for %s: %s", agent_id, exc)
                cb = None

            # Persist into the session cache for subsequent spawns.
            if session_module_cache is not None and cb is not None:
                session_module_cache[cache_key] = {
                    "isolated_modules": isolated_modules if cache_owns_modules else {},
                    "context_builder": cb,
                    "owns_modules": cache_owns_modules,
                    # Recency stamp drives LRU eviction past
                    # ``max_cached_sessions``. Bumped on each hit
                    # above; missed entries get the freshest stamp.
                    "last_used_at": time.monotonic(),
                }
                cb_owned_by_cache = True
    finally:
        # Always release the build lock so concurrent waiters proceed
        # regardless of build success/failure - leaving it held would
        # deadlock every subsequent spawn for this session.
        if cache_lock is not None and cache_lock.locked():
            cache_lock.release()

    if cb is not None:
        ctx.context_builder = cb

    try:
        # In discovery mode, inject direct tools (filesystem, shell, memory)
        # into the sub-agent's tool list - same as bootstrap does for the
        # main agent. Without this, the sub-agent wastes turns on
        # search_tools/get_tool before doing real work.
        if tool_injection == "discovery" and tools:
            from digitorn.core.runtime.bootstrap import _build_module_tools_schema
            _DIRECT_MODULES = {"filesystem", "shell", "memory"}
            existing = {t.get("name") or (t.get("function") or {}).get("name") for t in tools}
            for mod_id in _DIRECT_MODULES:
                mod = isolated_modules.get(mod_id)
                if mod is not None:
                    try:
                        direct_tools = _build_module_tools_schema(mod, prefix=mod_id, use_short_names=True)
                        for dt in direct_tools:
                            dt_name = dt.get("name") or (dt.get("function") or {}).get("name")
                            if dt_name not in existing:
                                tools.append(dt)
                                existing.add(dt_name)
                    except Exception:
                        pass

        memory_module = isolated_modules.get("memory")
        if memory_module is not None:
            memory_module.set_active_session(agent_id)
            ctx.memory_module = memory_module

        # AS16: relay_fn (built by parent) handles live token/tool/turn metrics.
        # Fallback to the legacy todo-only relay_progress for callers that
        # haven't migrated yet.
        active_relay = None
        if relay_fn is not None:
            active_relay = relay_fn
        elif notify_fn is not None and relay_progress:
            def _legacy_relay(event: dict) -> None:
                event_type = event.get("type", "")
                if event_type in ("todo_updated", "todo_added", "goal_set"):
                    notify_fn({
                        "type": "agent_progress",
                        "agent_id": agent_id,
                        "specialist": specialist,
                        "task": task[:60],
                        "event": event,
                    })
            active_relay = _legacy_relay

        if active_relay is not None:
            for mod in isolated_modules.values():
                if hasattr(mod, "_bg_notify"):
                    mod._bg_notify = active_relay
            # Hook into the AgentContext too so streaming/tool layers can
            # publish token_usage / tool_call / turn_complete events.
            try:
                ctx.progress_relay = active_relay
            except Exception:
                pass

    except Exception as exc:
        logger.warning("agent_spawn: failed to wire context for %s: %s", agent_id, exc)

    # Inject universal sub-agent directives before the specialist prompt.
    # Sub-agents run in background - nobody reads their verbose output.
    # They must be fast, precise, and output-focused.
    _agent_directives = (
        "You are a background sub-agent. Your output goes to a coordinator, not a human. "
        "Follow these rules strictly:\n"
        "\n"
        "- Be FAST. Do not narrate, explain your reasoning, or describe what you're about to do. "
        "Just do it.\n"
        "- No filler text. No greetings. No summaries of the task. No 'Let me...' or 'I'll now...'. "
        "Go straight to tool calls.\n"
        "- Execute the task with precision. Follow the instructions exactly.\n"
        "- When done, return ONLY the key findings or results. No commentary.\n"
        "- If the task says to search/read/explore - return facts with file paths and line numbers.\n"
        "- If the task says to implement/fix - make the changes, verify, report what changed.\n"
        "- Minimize tool calls. Grep before Read. Don't read entire files when you only need a section.\n"
        "- Do NOT create tasks (TodoAdd) or set goals - the coordinator handles that.\n"
        "\n"
    )
    # Build the inherited-context section from the parent's working
    # memory snapshot. The child sees the coordinator's goal, plan,
    # and key facts WITHOUT having any access to the parent's
    # conversation history. Read-only briefing - the child can't
    # mutate the parent's memory through this section. Empty when no
    # seed was provided (standalone tests, sandbox workers).
    inherited_section = _format_parent_memory_seed(parent_memory_seed)

    effective_prompt = _agent_directives + inherited_section + system_prompt

    messages = [
        {"role": "system", "content": effective_prompt},
        {"role": "user", "content": task},
    ]

    try:
        turn_result = await asyncio.wait_for(
            agent_turn(
                ctx,
                messages,
                max_turns=max_turns,
                timeout=timeout,
            ),
            timeout=timeout + 10,
        )

        result.status = "completed"
        result.content = turn_result.content or ""
        result.turns_used = turn_result.turns_used
        result.tool_calls = [
            {"name": c.name, "params": c.params, "success": c.success}
            for c in turn_result.tool_calls
        ]
        if turn_result.error:
            result.errors.append(turn_result.error)
            if not turn_result.content:
                result.status = "failed"

    except asyncio.TimeoutError:
        result.status = "timeout"
        result.errors.append(f"Agent timed out after {timeout}s")
    except asyncio.CancelledError:
        result.status = "cancelled"
    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))
        logger.warning("agent_spawn: agent %s failed: %s", agent_id, exc)

    result.duration_seconds = time.monotonic() - start

    try:
        if ctx.memory_module is not None and hasattr(ctx.memory_module, "store") and ctx.memory_module.store:
            store = ctx.memory_module.store
            mem: dict[str, Any] = {}
            try:
                mem["goal"] = store.working.goal
            except Exception:
                mem["goal"] = None
            try:
                mem["facts"] = store.working.key_facts[:10]
            except Exception:
                mem["facts"] = []
            try:
                mem["todos"] = [t.to_dict() for t in store.working.todos]
            except Exception:
                mem["todos"] = []
            try:
                mem["notes"] = [{"id": n.id, "content": n.content} for n in store.working.pending_notes()]
            except Exception:
                mem["notes"] = []
            try:
                mem["entities"] = list(store.working.active_entities.keys())[:10]
            except Exception:
                mem["entities"] = []
            result.memory = mem
    except Exception:
        logger.debug("failed to collect spawned agent memory snapshot", exc_info=True)

    # AS13: scrub the memory entries the sub-agent created so they don't
    # bleed into the next agent on the same shared memory module.
    try:
        memory_module = isolated_modules.get("memory")
        if memory_module is not None and hasattr(memory_module, "cleanup_session"):
            try:
                await memory_module.cleanup_session(agent_id)
            except TypeError:
                # Some memory modules expose a sync version
                memory_module.cleanup_session(agent_id)
    except Exception as exc:
        logger.debug("agent_spawn: memory cleanup_session failed for %s: %s", agent_id, exc)

    # AS2 + AS10: only stop modules WE created, and only when not cached.
    # Cached modules belong to the session and are released by
    # AgentSpawnModule.cleanup_session().
    if not cb_owned_by_cache:
        for mid, mod in isolated_modules.items():
            try:
                await mod.on_stop()
            except Exception as exc:
                logger.warning("agent_spawn: module on_stop failed for %s: %s", mid, exc)
        # AS2: drop the index reference so the embeddings/payloads can be GC'd.
        if cb is not None:
            try:
                cb._index = None
                await cb.on_stop()
            except Exception as exc:
                logger.debug("agent_spawn: ContextBuilder cleanup failed for %s: %s", agent_id, exc)

    if notify_fn is not None:
        try:
            # Build result_summary: first non-empty line of content, capped
            content_preview = result.content[:200] if result.content else ""
            result_summary = ""
            if result.content:
                for line in result.content.strip().split("\n"):
                    line = line.strip()
                    if line:
                        result_summary = line[:120]
                        break

            notification: dict[str, Any] = {
                "type": f"agent_{result.status}",
                "agent_id": agent_id,
                "session_id": session_id,
                "task": task[:200],
                "specialist": specialist,
                "status": result.status,
                "duration_seconds": round(result.duration_seconds, 1),
                "tool_calls_count": len(result.tool_calls),
                "preview": content_preview,
                "result_summary": result_summary,
            }
            if result.status == "failed":
                if result.errors:
                    notification["error"] = "; ".join(result.errors[:3])
                else:
                    notification["error"] = (
                        f"Sub-agent {agent_id} (specialist={specialist or 'generic'}) "
                        f"ended with status=failed but produced no diagnostic. "
                        f"Turns used: {result.turns_used}. Check daemon logs for stack trace."
                    )
            notify_fn(notification)
        except Exception as exc:
            logger.warning("agent_spawn: notification callback failed: %s", exc)

    logger.info(
        "agent_spawn: %s %s task=%s turns=%d duration=%.1fs",
        agent_id, result.status, task[:50], result.turns_used, result.duration_seconds,
    )

    return result
