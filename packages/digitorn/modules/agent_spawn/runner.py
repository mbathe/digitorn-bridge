"""Isolated agent runner — executes a sub-agent in its own context.

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


async def run_isolated_agent(
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
        # AS14: preserve None semantics — None means inherit, {} means deny.
        compiled_constraints=compiled_constraints if compiled_constraints is not None else {},
        sandbox_worker=sandbox_worker,
        direct_modules_map=direct_modules_map or {},
        approval_queue=approval_queue,
        # AS12: only fall back to "admin" when explicitly None — preserve "" or
        # other intentional values from the parent context.
        user_id=user_id if user_id is not None else "admin",
    )

    # AS10: per-session module + index cache. Building a fresh registry +
    # ContextBuilder index per spawn costs 100-300 ms. We cache by session
    # so all sub-agents in the same session share the work; cleanup happens
    # in cleanup_session() in the parent module.
    cache_key = session_id or "_standalone"
    cache_entry: dict[str, Any] | None = None
    cache_owns_modules = False
    if session_module_cache is not None:
        cache_entry = session_module_cache.get(cache_key)

    isolated_modules: dict[str, Any] = {}
    cb = None
    cb_owned_by_cache = False

    if cache_entry is not None:
        isolated_modules = cache_entry.get("isolated_modules", {})
        cb = cache_entry.get("context_builder")
        cb_owned_by_cache = cb is not None
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
                    # Share the module instance — sub-agent uses same memory/web/lsp
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
            }
            cb_owned_by_cache = True

    if cb is not None:
        ctx.context_builder = cb

    try:
        # In discovery mode, inject direct tools (filesystem, shell, memory)
        # into the sub-agent's tool list — same as bootstrap does for the
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
    # Sub-agents run in background — nobody reads their verbose output.
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
        "- If the task says to search/read/explore — return facts with file paths and line numbers.\n"
        "- If the task says to implement/fix — make the changes, verify, report what changed.\n"
        "- Minimize tool calls. Grep before Read. Don't read entire files when you only need a section.\n"
        "- Do NOT create tasks (TodoAdd) or set goals — the coordinator handles that.\n"
        "\n"
    )
    effective_prompt = _agent_directives + system_prompt

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
