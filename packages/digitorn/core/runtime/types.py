"""Runtime types - data structures used during execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from digitorn.core.runtime.approval import ApprovalQueue
    from digitorn.core.security import SecurityProfile


@dataclass
class ContextWindowConfig:
    """Context window management parameters."""

    max_tokens: int = 128_000
    output_reserved: int = 4096
    strategy: str = "summarize"
    keep_recent: int = 10
    compression_trigger: float = 0.75
    summary_max_tokens: int = 1024
    auto_compact: bool = True

    @property
    def effective_max(self) -> int:
        return max(self.max_tokens - self.output_reserved, 1)


@dataclass
class AgentContext:
    """Everything the runtime needs to execute an agent.

    Built by bootstrap, consumed by agent_turn().
    """

    # ── Identity ────────────────────────────────────────────────────────
    agent_id: str
    role: str

    # ── LLM ─────────────────────────────────────────────────────────────
    provider: Any
    fallback_provider: Any = None  # Optional fallback if primary fails after retries
    system_prompt: str = ""
    generation_params: dict[str, Any] = field(default_factory=dict)

    # ── Tools ───────────────────────────────────────────────────────────
    tools: list[dict[str, Any]] = field(default_factory=list)
    native_tool_use: bool = True
    tool_injection: str = "discovery"

    # ── Behaviour ───────────────────────────────────────────────────────
    plan_first: bool = True
    watchers_enabled: bool = False
    context_config: ContextWindowConfig = field(default_factory=ContextWindowConfig)

    # ── Session ─────────────────────────────────────────────────────────
    app_id: str | None = None
    session_id: str | None = None
    user_id: str = "admin"
    workspace: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ── Security & middleware ───────────────────────────────────────────
    approval_queue: ApprovalQueue | None = None
    security_profile: SecurityProfile | None = None
    app_middleware: Any = None

    # ── Modules injected by bootstrap ───────────────────────────────────
    memory_module: Any = None
    context_builder: Any = None
    runtime_config: Any = None
    sandbox_worker: Any = None
    lsp_module: Any = None
    preview_module: Any = None
    widget_module: Any = None
    workspace_module: Any = None

    # ── Mappings injected by bootstrap ──────────────────────────────────
    compiled_constraints: dict[str, dict[str, Any]] = field(default_factory=dict)
    direct_modules_map: dict[str, str] = field(default_factory=dict)

    # ── Prompt metadata ─────────────────────────────────────────────────
    prompt_cache_control: dict[str, Any] | None = None
    setup_summary: list[str] = field(default_factory=list)
    channels_info: list[dict[str, Any]] = field(default_factory=list)
    default_channel: str | None = None

    # ── Mutable loop state ──────────────────────────────────────────────
    last_compact_turn: int = -10
    completion_reminded: bool = False
    nudged_response: bool = False

    # ── Sub-agent live-progress relay ───────────────────────────────────
    # Optional callback the runtime invokes with structured events
    # (token_usage, tool_call, turn_complete) so a parent coordinator
    # can stream sub-agent metrics in real time.
    progress_relay: Any = None

    # ── Background activation recorder ──────────────────────────────────
    # Set by the background runtime when it starts a new activation.
    # Modules (mostly channels) can call
    # ``ctx.activation_recorder.record_channel_sent(...)`` to push
    # events into the per-activation timeline so the dashboard drawer
    # can show "📧 email sent to alice@x.com" on the correct row.
    # ``None`` in any non-background context - code that uses this must
    # check for None first.
    activation_recorder: Any = None

    # ── Agent run tracking (v2 schema) ──────────────────────────────────
    # ``current_run_id`` is set by ``agent_turn`` for the duration of the
    # call (and restored to its previous value on exit). Sub-agents pick
    # it up as their parent_run_id so the dashboard can build the
    # spawn tree. ``None`` when the run isn't being tracked (write
    # failure or no DB).
    current_run_id: str | None = None

    # ── User JWT for outbound LLM calls via the gateway ─────────────────
    # When the runtime routes a brain's LLM call through the gateway
    # (``http://127.0.0.1:8002/v1`` or ``https://gateway.digitorn.ai/v1``),
    # the gateway authenticates the call via the user's JWT - same token
    # that authenticated the inbound HTTP request that started the
    # session. The provider's HTTP layer reads this from the
    # ``RequestContext`` ContextVar and uses it as the bearer for the
    # gateway. Empty string when the session has no authenticated user
    # (legacy ``user_id="local"`` path) - the gateway will reject the
    # call as 401, surfacing the missing-auth error to the caller.
    user_jwt: str = ""

    # ── Cooperative cancellation ────────────────────────────────────────
    # Optional ``asyncio.Event`` checked at the top of every turn in
    # ``agent_turn``. Setters (currently the agent_spawn module's
    # ``_mode_cancel``) flip it BEFORE issuing a hard ``Task.cancel()``
    # so the agent loop bails at the next natural point even when the
    # asyncio cancellation signal gets swallowed by a blocking call.
    # ``None`` for the main coordinator agent - it can't be soft-cancelled
    # this way (use the session-abort path instead).
    cancel_event: Any = None
    cancel_reason: str = ""


@dataclass
class HookEvent:
    """Notification emitted when a hook action fires."""

    hook_id: str
    action_type: str
    phase: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallInfo:
    """Lightweight record of a single tool call."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str = ""


@dataclass
class TurnResult:
    """Result of a single agent turn."""

    content: str
    tool_calls_count: int = 0
    turns_used: int = 0
    truncated: bool = False
    error: str | None = None
    tool_calls: list[ToolCallInfo] = field(default_factory=list)
    context_usage: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Structured terminal status: "" (default = completed normally),
    # "cancelled" (user abort / cooperative cancel), "loop_killed"
    # (loop_guard hard kill after consecutive failures), "timeout",
    # "interrupted". Used by callers that need to distinguish HOW a
    # turn ended without parsing ``error`` strings.
    status: str = ""


WORKSPACE_PLACEHOLDER = "{WORKSPACE}"


def apply_workspace_override(
    ctx: AgentContext,
    workspace: str,
    yaml_workspace: str = "",
) -> None:
    """Apply a per-session workspace to an AgentContext (mutates in place).

    Updates ctx.workspace, filesystem constraints, and system prompt.
    Used by both the daemon (manager.py) and sandbox workers (worker_main.py).

    Always strips ``{WORKSPACE}`` from the system prompt - never leaks a
    literal placeholder to the agent. Falls back to the yaml workspace,
    then to an empty string, when no session workspace is provided.
    """
    ctx.workspace = workspace
    resolved = workspace or yaml_workspace or ""

    if workspace:
        fs_constraints = dict(ctx.compiled_constraints.get("filesystem", {}))
        fs_constraints["paths"] = [workspace]
        ctx.compiled_constraints = dict(ctx.compiled_constraints)
        ctx.compiled_constraints["filesystem"] = fs_constraints

    if ctx.system_prompt:
        if yaml_workspace and workspace and yaml_workspace != workspace:
            ctx.system_prompt = ctx.system_prompt.replace(yaml_workspace, workspace)
        ctx.system_prompt = ctx.system_prompt.replace(WORKSPACE_PLACEHOLDER, resolved)


def apply_workspace_to_messages(
    messages: list[dict[str, Any]],
    workspace: str,
    yaml_workspace: str = "",
) -> None:
    """Update the system prompt in session messages with the actual workspace.

    Always strips ``{WORKSPACE}`` from the system message so the agent never
    sees the literal placeholder. Falls back to yaml workspace / empty string
    when no session workspace is provided.
    """
    if not messages or messages[0].get("role") != "system":
        return
    prompt = messages[0]["content"]
    resolved = workspace or yaml_workspace or ""
    if WORKSPACE_PLACEHOLDER not in prompt and (not yaml_workspace or yaml_workspace not in prompt):
        return
    if yaml_workspace and workspace and yaml_workspace != workspace:
        prompt = prompt.replace(yaml_workspace, workspace)
    prompt = prompt.replace(WORKSPACE_PLACEHOLDER, resolved)
    messages[0]["content"] = prompt
