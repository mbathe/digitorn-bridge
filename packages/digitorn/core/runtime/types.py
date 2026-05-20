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
    """Everything the runtime needs to execute an agent."""

    agent_id: str
    role: str

    provider: Any
    fallback_provider: Any = None  # Optional fallback if primary fails after retries
    system_prompt: str = ""
    generation_params: dict[str, Any] = field(default_factory=dict)

    tools: list[dict[str, Any]] = field(default_factory=list)
    native_tool_use: bool = True
    tool_injection: str = "discovery"

    plan_first: bool = True
    watchers_enabled: bool = False
    context_config: ContextWindowConfig = field(default_factory=ContextWindowConfig)

    app_id: str | None = None
    session_id: str | None = None
    user_id: str = "admin"
    workspace: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)

    path_policy: Any = None

    approval_queue: ApprovalQueue | None = None
    security_profile: SecurityProfile | None = None
    app_middleware: Any = None

    memory_module: Any = None
    context_builder: Any = None
    runtime_config: Any = None
    sandbox_worker: Any = None
    lsp_module: Any = None
    preview_module: Any = None
    widget_module: Any = None
    workspace_module: Any = None

    compiled_constraints: dict[str, dict[str, Any]] = field(default_factory=dict)
    direct_modules_map: dict[str, str] = field(default_factory=dict)

    prompt_cache_control: dict[str, Any] | None = None
    setup_summary: list[str] = field(default_factory=list)
    channels_info: list[dict[str, Any]] = field(default_factory=list)
    default_channel: str | None = None

    last_compact_turn: int = -10
    completion_reminded: bool = False
    nudged_response: bool = False

    progress_relay: Any = None

    # Set by the background runtime so modules can push per-activation events to the dashboard timeline.
    activation_recorder: Any = None

    current_run_id: str | None = None

    user_jwt: str = ""

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
    # Terminal status: "", "cancelled", "loop_killed", "timeout", "interrupted".
    status: str = ""


WORKSPACE_PLACEHOLDER = "{WORKSPACE}"


def apply_workspace_override(
    ctx: AgentContext,
    workspace: str,
    yaml_workspace: str = "",
) -> None:
    """Apply a per-session workspace to an AgentContext (mutates in place); rebuilds constraints + path policy + strips WORKSPACE placeholder."""
    ctx.workspace = workspace
    resolved = workspace or yaml_workspace or ""

    if workspace:
        fs_constraints = dict(ctx.compiled_constraints.get("filesystem", {}))
        fs_constraints["paths"] = [workspace]
        ctx.compiled_constraints = dict(ctx.compiled_constraints)
        ctx.compiled_constraints["filesystem"] = fs_constraints

        from digitorn.core.path_policy import PathPolicy
        merged_constraints: dict[str, Any] = {}
        for mod_constraints in ctx.compiled_constraints.values():
            if not isinstance(mod_constraints, dict):
                continue
            if mod_constraints.get("unrestricted"):
                merged_constraints["unrestricted"] = True
            extras = mod_constraints.get("allowed_paths") or []
            if extras:
                acc = list(merged_constraints.get("allowed_paths", []))
                for e in extras:
                    if e and e not in acc:
                        acc.append(e)
                merged_constraints["allowed_paths"] = acc
        ctx.path_policy = PathPolicy.from_constraints(workspace, merged_constraints)

    if ctx.system_prompt:
        if yaml_workspace and workspace and yaml_workspace != workspace:
            ctx.system_prompt = ctx.system_prompt.replace(yaml_workspace, workspace)
        ctx.system_prompt = ctx.system_prompt.replace(WORKSPACE_PLACEHOLDER, resolved)


def apply_workspace_to_messages(
    messages: list[dict[str, Any]],
    workspace: str,
    yaml_workspace: str = "",
) -> None:
    """Update the system prompt in session messages with the actual workspace."""
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
