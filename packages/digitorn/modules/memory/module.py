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
        store = self._session_stores.pop(session_id, None)
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
        description="Create a task to track your progress.",
        tool_prompt=(
            "Create a task to track progress. The user sees tasks in a dedicated panel.\n"
            "\n"
            "## When to use\n"
            "- Complex multi-step work (3+ steps) - create one task per step\n"
            "- After receiving new instructions - break down requirements into tasks\n"
            "- Before starting implementation - plan your work as tasks\n"
            "\n"
            "## When NOT to use\n"
            "- Single trivial operations - just do them directly\n"
            "- Sub-agents should NEVER create tasks - the coordinator handles tracking\n"
            "\n"
            "## Rules\n"
            "- Create tasks BEFORE starting work, then update status as you go\n"
            "- Keep subjects brief and actionable: 'Fix auth bug', 'Add input validation'\n"
            "- One task per logical step - not one per file or one per line\n"
            "- Update to in_progress before starting, completed when done"
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
        snapshot = self._build_todo_snapshot()
        return ActionResult(success=True, data=snapshot)

    @action(
        description="Update a task's status.",
        tool_prompt=(
            "Update task status in real-time as you work.\n"
            "\n"
            "## Statuses\n"
            "- pending: not started yet\n"
            "- in_progress: currently working on it\n"
            "- completed: fully done\n"
            "- blocked: waiting on something\n"
            "\n"
            "## Rules\n"
            "- Mark as in_progress BEFORE starting work on a task\n"
            "- Mark as completed IMMEDIATELY after finishing\n"
            "- Only ONE task should be in_progress at a time\n"
            "- Only mark completed when FULLY accomplished - not if tests fail"
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
