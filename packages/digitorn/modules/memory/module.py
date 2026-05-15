"""Memory Module - cognitive memory system for Digitorn agents.

Provides 5 memory layers (all opt-in):
- **Working Memory**: goal, plan, todo-list, facts, entities (always in prompt)
- **Episodic Memory**: session summaries (persistent)
- **Semantic Memory**: facts + entity graph (vector + graph)
- **Procedural Memory**: learned patterns
- **Memory Runtime**: proactive injection, content cache, goal guardian

The memory is rendered as a single text block injected into the system
prompt by the context_builder. The agent sees everything at once -
no queries needed. Like opening your eyes and knowing.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, ExecutionContext

_SENSITIVE_PATTERNS = ["key", "secret", "password", "token", "auth", "credential", "private", "jwt"]


def _redact_secrets(text: str, extra_patterns: list[str] | None = None) -> str:
    """Redact values of known sensitive env vars from text before storing in memory."""
    patterns = _SENSITIVE_PATTERNS + (extra_patterns or [])
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if any(p in key.lower() for p in patterns):
            text = text.replace(value, "[REDACTED]")
    return text
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.memory.store import (
    CachedContent,
    Checkpoint,
    Episode,
    MemoryConfig,
    MemoryStore,
    Note,
    SemanticMemory,
    TodoStatus,
)

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class MemoryModuleConfig(BaseModel):
    """Pydantic config for the memory module (validated at compile time).

    Note: Runtime memory behavior is driven by ``MemoryConfig`` (a dataclass
    in ``memory/store.py``). This class only exists so the compiler can
    reject type errors on the well-known keys while still tolerating
    forward-compatible flags (e.g. ``auto_remember``).
    """

    model_config = {"extra": "allow"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
    working_memory: bool = Field(default=False)
    todo_list: bool = Field(default=False)
    checkpoint: bool = Field(default=False)
    episodic: bool = Field(default=False)
    semantic: dict[str, Any] | bool = Field(default_factory=dict)
    procedural: bool = Field(default=False)
    runtime: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    security: dict[str, Any] = Field(default_factory=dict)
    auto_remember: bool = Field(default=False)


# ── Parameter models (6 actions) ─────────────────────────────────


_HIDDEN = {"hidden": True}


# ── LLM-exposed params (Claude Code compatible) ─────────

class TaskCreateParams(BaseModel):
    """Create a task to track progress on a step of your work."""
    subject: str = Field(..., description="Brief title for the task. Example: 'Fix authentication bug'.")
    description: str = Field("", description="What needs to be done.")


class TaskUpdateParams(BaseModel):
    """Update a task's status as you work."""
    taskId: str = Field(..., description="The task ID to update. Example: 't1'.")
    status: str = Field(..., description="New status: 'pending', 'in_progress', 'completed', 'blocked'.")


# ── Internal params (kept for API/TUI, NOT exposed to LLM) ──

class SetGoalParams(BaseModel):
    """Set the main goal for this session.

    IMPORTANT: The only parameter is 'goal'. Do NOT use 'objective', 'task', or 'description'.

    Examples:
        set_goal(goal="Fix the authentication bug in src/auth/validate.py")
        set_goal(goal="Implement OAuth2 support for the API")
    """
    goal: str = Field(..., description="The goal to set. REQUIRED. Example: set_goal(goal=\"Fix the auth bug\")")


class RememberParams(BaseModel):
    """Store a fact that survives context compaction.

    IMPORTANT: The only parameter is 'content'. Do NOT use 'what', 'when', 'fact', or 'text'.

    Examples:
        remember(content="Auth bug is in src/auth/validate.py:42")
        remember(content="Test command: pytest tests/ -v")
        remember(content="Project uses FastAPI + SQLAlchemy + Alembic")
    """
    content: str = Field(..., description="The fact to remember. REQUIRED. Example: remember(content=\"Test command: pytest tests/ -v\")")


class MemoryModule(BaseModule):
    """Cognitive memory for Digitorn agents.

    Memory is scoped by app + session:
    - **Per-session**: working memory, todos, episodic, content cache
    - **Per-app** (shared): semantic facts, graph, procedural patterns

    The module maintains a dict of session stores. ``get_session_store()``
    returns the store for a given session (creating it on demand).
    The ``store`` property returns the current active session store
    (set by the agent loop via ``set_active_session()``).
    """

    MODULE_ID = "memory"
    VERSION = "1.0.0"
    CONFIG_MODEL = MemoryModuleConfig

    def __init__(self) -> None:
        super().__init__()
        self._config = MemoryConfig()
        self._app_id: str | None = None
        self._redact_secrets: bool = True
        self._extra_sensitive_patterns: list[str] = []

        self._app_semantic = SemanticMemory()
        self._app_procedures: list = []

        self._session_stores: dict[str, MemoryStore] = {}

        self._active_session_id: str | None = None
        self._default_store = MemoryStore()

    def set_active_session(self, session_id: str | None) -> None:
        """Set the active session for subsequent operations."""
        self._active_session_id = session_id

    def get_session_store(self, session_id: str | None = None) -> MemoryStore:
        """Get or create the memory store for a session.

        Per-session data (working memory, todos, episodes) is isolated
        by `(user_id, session_id)` - previously we keyed by `session_id`
        only, which meant two concurrent sessions that happened to use
        the same sid prefix (or the `_default_store` fallback) ended up
        sharing `working.goal`. Race confirmed by the concurrent-session
        test that interleaved three SetGoal calls from the same user.

        Shared data (semantic, procedural) is per-user (loaded lazily
        from KV at first access) - stops the cross-user fact leak the
        audit flagged as CVE-level.
        """
        ctx = None
        try:
            ctx = self._context_var.get()
        except Exception:
            pass
        uid_now = getattr(ctx, "user_id", "") if ctx else ""
        sid_now = session_id or (
            getattr(ctx, "session_id", "") if ctx else ""
        ) or self._active_session_id
        # Compound key - isolates users AND sessions.
        sid = f"{uid_now}::{sid_now}" if sid_now else None

        if sid is None:
            self._default_store.semantic = self._app_semantic
            self._default_store.procedures = self._app_procedures
            return self._default_store

        if sid not in self._session_stores:
            store = MemoryStore(self._config)
            # Store the plain session id (what clients passed), not
            # the compound (uid::sid) bucket key.
            store.session_id = sid_now or ""
            store.app_id = self._app_id

            # Per-user semantic memory - previously we shared a single
            # `_app_semantic` across ALL sessions / ALL users of the
            # same app, which leaked "f1, f2, f3…" facts from one user
            # into a brand-new session belonging to another user. Now
            # each user gets its own semantic block, loaded lazily from
            # the KV backend with the user-scoped key.
            uid = uid_now

            user_semantic = None
            if hasattr(self, "_user_semantic"):
                user_semantic = self._user_semantic.get(uid)
            else:
                self._user_semantic = {}

            if user_semantic is None:
                user_semantic = SemanticMemory()
                backend = getattr(self, "_kv_backend", None)
                if backend is not None:
                    try:
                        tmp = MemoryStore(self._config)
                        tmp.semantic = user_semantic
                        tmp.restore(backend, self._app_id, user_id=uid)
                    except Exception as exc:
                        logger.debug("memory restore failed app=%s user=%s: %s",
                                     self._app_id, uid, exc)
                self._user_semantic[uid] = user_semantic

            store.semantic = user_semantic
            store.procedures = self._app_procedures
            self._session_stores[sid] = store

        return self._session_stores[sid]

    @property
    def store(self) -> MemoryStore:
        """The active session's memory store."""
        return self.get_session_store()

    async def cleanup_session(self, session_id: str) -> None:
        """Remove all per-session state for a session.

        Called by the session manager when a session ends. Prevents
        unbounded growth of _session_stores across the daemon's lifetime.
        Semantic memory and procedures (app-level) are NOT cleared -
        only the working/episodic state of this specific session.
        """
        # The dict is keyed by ``f"{user_id}::{session_id}"`` (see
        # ``get_store`` line 188) but ``cleanup_session`` receives only
        # the plain session_id. A direct ``pop(session_id)`` never
        # matched -- every session that ended left its MemoryStore
        # alive in the dict, leaking ~10KB per session forever. Session
        # UUIDs are globally unique, so collecting ALL keys ending in
        # ``::{session_id}`` is safe (matches the one compound entry).
        stores_to_clean: list[Any] = []
        keys_to_drop = [
            k for k in self._session_stores
            if k == session_id or k.endswith(f"::{session_id}")
        ]
        for k in keys_to_drop:
            s = self._session_stores.pop(k, None)
            if s is not None:
                stores_to_clean.append(s)
        store = stores_to_clean[0] if stores_to_clean else None
        if store is not None:
            try:
                # Clear working memory (goal, todos) and episodic
                if hasattr(store, "working") and store.working is not None:
                    store.working.todos.clear()
                    store.working.goal = None
                if hasattr(store, "episodic") and store.episodic is not None:
                    if hasattr(store.episodic, "events"):
                        store.episodic.events.clear()
            except Exception as exc:
                logger.debug("memory_cleanup_session_partial session=%s error=%s", session_id, exc)
        logger.debug("memory_cleanup_session session=%s", session_id)

    @property
    def memory_config(self) -> MemoryConfig:
        return self._config

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        from digitorn.modules.memory.hooks import (
            build_memory_prompt_section,
            build_memory_instructions,
        )
        sections: list[dict[str, Any]] = []
        snapshot = build_memory_prompt_section(self)
        if snapshot:
            sections.append({
                "title": "MEMORY",
                "content": snapshot,
                "priority": 5,
            })
        instructions = build_memory_instructions(self)
        if instructions:
            sections.append({
                "title": "",
                "content": instructions,
                "priority": 6,
            })
        return sections

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Cognitive memory system - working memory, episodic, semantic, "
                "procedural. Makes agents conscious of their environment."
            ),
            "author": "Digitorn Team",
        })

    async def on_start(self) -> None:
        # We DON'T restore at `on_start` anymore - that fires once at
        # daemon boot and loaded whatever user happened to be first,
        # then every session inherited that user's facts. Instead,
        # facts now load lazily per session with the correct user_id
        # (see `_ensure_store_for_session`).
        pass

    async def on_stop(self) -> None:
        backend = getattr(self, "_kv_backend", None)
        app_id = getattr(self, "_app_id", "default")
        if backend is not None:
            # Persist per-user if we can infer it from the current
            # context; otherwise fall back to app-wide key for the
            # legacy daemon-shutdown path.
            ctx = self._context_var.get() if hasattr(self, "_context_var") else None
            uid = getattr(ctx, "user_id", "") if ctx else ""
            self.store.persist(backend, app_id, "", user_id=uid)
            logger.info("memory_persisted app=%s user=%s", app_id, uid or "<none>")

    async def on_config_update(self, config: dict[str, Any]) -> None:
        await super().on_config_update(config)
        security = config.get("security", {})
        self._redact_secrets = security.get("redact_secrets", True)
        self._extra_sensitive_patterns = security.get("sensitive_patterns", [])
        self._config = MemoryConfig.from_dict(config)
        self._default_store = MemoryStore(self._config)
        self._default_store.semantic = self._app_semantic
        self._default_store.procedures = self._app_procedures
        self._session_stores.clear()
        logger.info(
            "memory_config working=%s todo=%s episodic=%s semantic_v=%s "
            "semantic_g=%s procedural=%s runtime_inject=%s cache=%s guardian=%s",
            self._config.working_memory, self._config.todo_list,
            self._config.episodic, self._config.semantic_vector,
            self._config.semantic_graph, self._config.procedural,
            self._config.runtime_proactive_injection,
            self._config.runtime_content_cache,
            self._config.runtime_goal_guardian,
        )


    # ── LLM-exposed actions (Claude Code compatible) ────────

    @action(
        description="Create a task to checkpoint your work (resume protocol).",
        tool_prompt=(
            "Checkpoint your intent as a task. Tasks are the source of "
            "truth for what you are doing — both for the USER (visible in "
            "the side panel) and for the DAEMON'S RESUME MECHANISM. If a "
            "turn crashes mid-stream (network blip, daemon restart, abort, "
            "context overflow), the runtime re-injects your tasks into the "
            "next turn and asks you to continue from your in_progress entry.\n"
            "Tasks ARE your resume protocol. A plan that exists only in "
            "your head dies with the process.\n"
            "\n"
            "## When to create tasks\n"
            "- Work that has AT LEAST 2 distinct steps — create a task "
            "for EACH step BEFORE acting. Tasks come in batches, not "
            "solo. A single isolated task is a code smell: either the "
            "work was trivial (no tasks needed) or the breakdown was "
            "lazy (it had sub-steps you skipped).\n"
            "- After a non-trivial user message: break the ask into "
            "≥ 2 tasks, then execute. The breakdown is your contract "
            "with your future self.\n"
            "- Before a slow operation (long shell command, large edit, "
            "sub-agent spawn): create a task so the user sees the intent "
            "AND so a resume can continue past the interruption point — "
            "but only if there's at least one more step queued after it.\n"
            "\n"
            "## When NOT to create a task\n"
            "- One-shot trivial answer (\"what time is it\", \"explain "
            "this concept in one paragraph\") — just answer.\n"
            "- ONE-STEP work in general — if the entire user request is a "
            "single tool call followed by an answer, no task. The UI "
            "panel hides single-task lists anyway because a 1/1 progress "
            "bar is noise.\n"
            "- Sub-agents NEVER create tasks — the coordinator owns the plan.\n"
            "\n"
            "## Make tasks resumable\n"
            "- ``subject``: imperative one-liner the user understands at a "
            "glance. Example: \"Fix JWT verification in auth/client.py\".\n"
            "- ``description``: WHY plus enough context for another you "
            "(cold start, no prior memory) to continue. Include file paths, "
            "function names, key parameters. A poor description = lost work "
            "on resume. Example: \"User reported 'kid mismatch' on verify. "
            "Suspect stale JWKS cache. Add refresh_jwks() before first "
            "verify; add unit test for kid rotation.\"\n"
            "- One task = one logical step. Not one per file. Not one per line.\n"
            "\n"
            "## Lifecycle\n"
            "After creating, immediately TaskUpdate to ``in_progress`` "
            "when you START the work, ``completed`` when DONE AND "
            "VERIFIED. See TaskUpdate for the discipline."
        ),
        params_model=TaskCreateParams,
        risk_level="low",
        tags=["memory", "todo"],
        cli_label="+Task",
        cli_param="subject",
        display_hidden=True,
        display_channel="tasks",
    )
    async def task_create(self, params: TaskCreateParams) -> ActionResult:
        content = params.subject
        if params.description:
            content = f"{params.subject}: {params.description}"
        item = self.store.working.add_todo(content)
        self._emit_todo_event("todo_added", item)
        # Persist to the WAL so a daemon crash + resume restores
        # this todo without re-running the action.
        await self._persist_event(
            type="memory_task_create",
            payload={
                "id": getattr(item, "id", ""),
                "content": content,
                "status": str(getattr(item, "status", "pending")).split(".")[-1].lower(),
            },
        )
        snapshot = self._build_todo_snapshot()
        return ActionResult(success=True, data=snapshot)

    @action(
        description="Update a task's status (drives user UI + resume protocol).",
        tool_prompt=(
            "Update a task's status as you work. This drives two things:\n"
            "1. The user's progress bar in the side panel (live feedback).\n"
            "2. The DAEMON'S RESUME PROTOCOL. If your turn is interrupted "
            "mid-work, the runtime reads which tasks are ``in_progress`` "
            "and re-injects them into the next turn so you can continue. "
            "Honest statuses = smooth resume. Lying statuses = duplicated "
            "or skipped work on recovery.\n"
            "\n"
            "## Statuses\n"
            "- ``pending``     not started yet\n"
            "- ``in_progress`` actively working RIGHT NOW (only ONE at a time)\n"
            "- ``completed``   fully done AND verified\n"
            "- ``blocked``     waiting on something external\n"
            "\n"
            "## Rules — not suggestions, contract\n"
            "- Set ``in_progress`` BEFORE the first tool call of that task. "
            "The resumer treats this flag as ground truth: ``in_progress`` "
            "means \"I was here when I crashed, please continue\".\n"
            "- Set ``completed`` ONLY after verifying the result (test "
            "passed, file written, command exited 0, response received). A "
            "premature ``completed`` lies to the resumer and the work is "
            "lost — the resumer skips the task assuming it is done.\n"
            "- Only ONE task ``in_progress`` at any moment. If you switch "
            "focus, bump the previous one back to ``pending`` (or to "
            "``blocked`` with a reason in the task description) FIRST.\n"
            "- ``blocked`` is for genuine external dependencies (user "
            "approval, third-party API outage, missing credential). It is "
            "NOT for \"I don't feel like doing it right now\". Always "
            "record the blocking reason — resume reads it to decide retry "
            "vs escalate.\n"
            "\n"
            "## Crash example\n"
            "You marked task ``t2`` as ``in_progress``, started a 30s "
            "shell command, then the daemon restarted. On the next turn, "
            "the runtime sees ``t2 in_progress`` and asks you to continue. "
            "You check whether the command effect is observable (file "
            "exists? state changed?), then either mark ``completed`` or "
            "re-run. This only works if the status was honest."
        ),
        params_model=TaskUpdateParams,
        risk_level="low",
        tags=["memory", "todo"],
        cli_label="Task",
        cli_param="taskId",
        display_hidden=True,
        display_channel="tasks",
    )
    async def task_update(self, params: TaskUpdateParams) -> ActionResult:
        # Map 'completed' to 'done' for internal compatibility
        status_str = params.status
        if status_str == "completed":
            status_str = "done"

        status_map = {
            "pending": TodoStatus.PENDING,
            "in_progress": TodoStatus.IN_PROGRESS,
            "done": TodoStatus.DONE,
            "blocked": TodoStatus.BLOCKED,
        }
        ts = status_map.get(status_str)
        if ts is None:
            return ActionResult(success=False, error=f"Invalid status: {params.status}. Use: pending, in_progress, completed, blocked.")

        found = False
        for item in self.store.working.todos:
            if item.id == params.taskId:
                item.status = ts
                if ts == TodoStatus.DONE:
                    import time
                    item.completed_at = time.monotonic()
                found = True
                self._emit_todo_event("todo_updated", item)
                break

        if not found:
            available = [t.id for t in self.store.working.todos]
            return ActionResult(success=False, error=f"Task '{params.taskId}' not found. Available: {available}")

        # Persist the status flip to the WAL.
        await self._persist_event(
            type="memory_task_update",
            payload={
                "id": params.taskId,
                "status": str(ts).split(".")[-1].lower(),
            },
        )
        snapshot = self._build_todo_snapshot()
        return ActionResult(success=True, data=snapshot)

    # ── Internal actions (API/TUI only) ──────────────────────

    @action(
        description="Set the main goal for this session. Internal - use Remember for goals.",
        tool_prompt="Set the main goal visible in memory at every turn. Use at the start of any non-trivial task.",
        params_model=SetGoalParams,
        risk_level="low",
        tags=["memory", "internal"],
        cli_label="Goal",
        cli_param="goal",
        display_hidden=True,
        display_channel="memory",
    )
    async def set_goal(self, params: SetGoalParams) -> ActionResult:
        self.store.working.goal = params.goal
        logger.info("memory_goal_set goal=%s", params.goal)
        self._notify_bg({"type": "goal_set", "goal": params.goal})
        await self._persist_event(
            type="memory_goal_set",
            payload={"goal": params.goal},
        )
        return ActionResult(success=True, data={"goal": params.goal})

    # ── remember ─────────────────────────────────────────────

    @action(
        description="Store a fact that survives context compaction.",
        tool_prompt=(
            "Store a fact that survives context compaction. Your long-term memory.\n"
            "\n"
            "## When to use\n"
            "- Key findings: 'Auth bug is in src/auth/validate.py:42 - missing null check'\n"
            "- Architecture decisions: 'Project uses FastAPI + SQLAlchemy + Alembic'\n"
            "- Important commands: 'Test command: pytest tests/ -v --tb=short'\n"
            "- After receiving sub-agent results - store the key findings\n"
            "- After context compaction - re-remember critical info you'll need\n"
            "- Project structure: 'Entry point: src/main.py, config: src/config.yaml'\n"
            "\n"
            "## When NOT to use\n"
            "- Trivial facts you won't need later\n"
            "- Entire file contents - remember the location, not the content\n"
            "- Sub-agents should NOT remember goals/tasks - only facts\n"
            "\n"
            "## Rules\n"
            "- Keep facts concise (1-2 sentences max)\n"
            "- Include file paths and line numbers when relevant\n"
            "- Duplicates are auto-detected and skipped\n"
            "- Secrets are auto-redacted from stored facts\n"
            "- Remember AFTER completing work, not before - store results, not plans"
        ),
        params_model=RememberParams,
        risk_level="low",
        tags=["memory"],
        cli_label="Remember",
        cli_param="content",
        display_hidden=True,
        display_channel="memory",
    )
    async def remember(self, params: RememberParams) -> ActionResult:
        content = _redact_secrets(params.content, self._extra_sensitive_patterns) if self._redact_secrets else params.content

        # Deduplicate
        for existing in self.store.semantic.facts:
            if existing.content.lower() == content.lower():
                return ActionResult(success=True, data={
                    "id": existing.id, "action": "already_stored",
                })

        fact = self.store.semantic.add_fact(content, category="", importance=1.0, source="agent")
        self._notify_bg({"type": "fact_added", "id": fact.id, "content": fact.content})
        await self._persist_event(
            type="memory_fact_added",
            payload={
                "id": fact.id,
                "content": fact.content,
                "category": getattr(fact, "category", "") or "",
                "importance": float(getattr(fact, "importance", 1.0) or 1.0),
                "source": getattr(fact, "source", "agent") or "agent",
            },
        )
        return ActionResult(success=True, data={"id": fact.id, "content": fact.content})

    # ── Helper methods ───────────────────────────────────────

    def _build_todo_snapshot(self) -> dict[str, Any]:
        """Build a summary of the current todo state."""
        progress = self.store.working.get_progress()
        todos = [t.to_dict() for t in self.store.working.todos]
        return {
            "goal": self.store.working.goal,
            "todos": todos,
            "progress": progress,
        }

    def _emit_todo_event(self, event_type: str, item: Any) -> None:
        """Emit a todo event for the UI (frontend expects full todo list)."""
        self._notify_bg({
            "type": event_type,
            "todo": item.to_dict() if hasattr(item, "to_dict") else str(item),
            "todos": [t.to_dict() for t in self.store.working.todos],
            "goal": self.store.working.goal,
            "progress": self.store.working.get_progress(),
        })

    async def _persist_event(
        self, *, type: str, payload: dict[str, Any],
    ) -> None:
        """Persist a working-memory mutation as a session event.

        Routes through the session-store bridge so the event lands in
        the WAL (``events.jsonl``) atomically. The projection family
        ``memory_*`` reads it back at session resume and rebuilds
        ``state.goal`` / ``state.todos`` / ``state.semantic_facts``.

        Cost on the hot path: a dict lookup + seq alloc + queue push
        (sub-millisecond, in-memory). The actual disk write happens
        asynchronously in the DiskFlusher worker.

        Best-effort — never raises. Memory mutations succeed in RAM
        regardless of whether the bridge is configured. The caller's
        ``ActionResult`` is unaffected on persistence failure.
        """
        try:
            ctx = self._context_var.get()
        except Exception:
            ctx = None
        sid = getattr(ctx, "session_id", "") if ctx else ""
        if not sid:
            # No session context (CLI / test / module preload) — the
            # in-memory mutation is the canonical state for this run.
            return
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is None:
                return
            await bridge.record(
                kind="event",
                type=type,
                app_id=getattr(ctx, "app_id", "") or "",
                session_id=sid,
                user_id=getattr(ctx, "user_id", "") or "",
                payload=dict(payload),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "memory_persist_event_failed type=%s sid=%s err=%s",
                type, sid, exc,
            )
