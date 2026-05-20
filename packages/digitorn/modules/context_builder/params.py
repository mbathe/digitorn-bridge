"""Pydantic parameter models for context_builder actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

class SearchToolsParams(BaseModel):
    """Search for tools by natural-language description."""

    query: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "Natural-language description of what you want to do. "
            "Examples: 'read a file', 'execute SQL query', 'take screenshot'."
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of results to return.",
    )

class GetToolParams(BaseModel):
    """Get the full schema and metadata for a specific tool."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Fully qualified tool name in 'module.action' format "
            "(e.g. 'database.fetch_results', 'filesystem.read_file')."
        ),
    )

class ExecuteToolParams(BaseModel):
    """Execute a tool with the given parameters."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        alias="name",
        validation_alias=None,  # accept both 'name' and 'tool_name'
        description=(
            "Fully qualified tool name in 'module.action' format "
            "(e.g. 'database.fetch_results')."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters to pass to the tool. Must match the tool's schema.",
    )

    def __init__(self, **data: Any) -> None:
        # Accept 'tool_name' as alias for 'name'
        if "tool_name" in data and "name" not in data:
            data["name"] = data.pop("tool_name")
        super().__init__(**data)

class ListCategoriesParams(BaseModel):
    """List all available tool categories (modules)."""

class BrowseCategoryParams(BaseModel):
    """Browse tools in a specific category (module)."""

    category: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Category (module) ID to browse "
            "(e.g. 'database', 'filesystem', 'browser')."
        ),
    )
    page: int = Field(
        default=1,
        ge=1,
        description="Page number for pagination (20 tools per page).",
    )

class ParallelAction(BaseModel):
    """A single action within a parallel batch."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description=(
            "Fully qualified tool name (module.action). "
            "Example: 'filesystem.read', 'http.get', 'database.execute_query'."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for this action (must match the tool's schema).",
    )

class RunParallelParams(BaseModel):
    """Execute multiple actions in parallel."""

    actions: list[ParallelAction] = Field(
        ...,
        min_length=1,
        max_length=50,
        description=(
            "List of actions to execute concurrently. "
            "Each runs independently; failures don't cancel others."
        ),
    )

_HIDDEN = {"hidden": True}

class BackgroundRunParams(BaseModel):
    """Run any tool in the background - returns task_id immediately."""

    name: str | None = Field(
        default=None,
        max_length=256,
        description="Tool name to run in the background (e.g. 'database.sql').",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the tool.",
    )

    task_id: str | None = Field(default=None, json_schema_extra=_HIDDEN,
        description="Task ID - for status/cancel/wait.")
    cancel: bool = Field(default=False, json_schema_extra=_HIDDEN,
        description="Cancel the task (requires task_id).")
    wait: bool = Field(default=False, json_schema_extra=_HIDDEN,
        description="Wait for completion (requires task_id).")
    list_tasks: bool = Field(default=False, json_schema_extra=_HIDDEN,
        description="List all background tasks.")
    timeout: float = Field(default=60.0, ge=1.0, le=3600.0, json_schema_extra=_HIDDEN,
        description="Max seconds to wait (for wait mode).")

# Legacy aliases - kept for backward compat with existing API consumers
BackgroundTaskIdParams = BackgroundRunParams
BackgroundWaitParams = BackgroundRunParams
BackgroundListParams = BackgroundRunParams

class WatchStartParams(BaseModel):
    """Start a persistent watcher; notifications fire only when `notify_when` triggers."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Fully qualified tool name to call periodically (module.action).",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for each check invocation.",
    )
    interval: float = Field(
        default=30.0,
        ge=5.0,
        le=3600.0,
        description="Seconds between checks (min 5, max 3600).",
    )
    label: str = Field(
        default="",
        max_length=256,
        description="Human-readable description of what is being monitored.",
    )
    max_checks: int = Field(
        default=0,
        ge=0,
        le=10000,
        description=(
            "Maximum number of checks before auto-stopping. "
            "0 = unlimited (default). "
            "Use 1 for a one-shot delayed action (timer/reminder)."
        ),
    )
    notify_when: str = Field(
        default="on_change",
        description=(
            "When to notify the LLM. One of: "
            "'on_change' (result differs from previous - default), "
            "'on_error' (only on errors or recovery), "
            "'on_threshold' (expression evaluates to true), "
            "'summary' (batch N checks then send summary), "
            "'always' (every check - debug only)."
        ),
    )
    notify_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Extra config for the notify strategy. "
            "For 'on_threshold': {\"expression\": \"result.status_code != 200\"}. "
            "For 'summary': {\"batch_size\": 10}."
        ),
    )

class WatcherIdParams(BaseModel):
    """Identify a watcher by its ID."""

    watcher_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Watcher ID returned by watch_start.",
    )

class WatchHistoryParams(BaseModel):
    """Get the last N check results from a watcher."""

    watcher_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Watcher ID returned by watch_start.",
    )
    last_n: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of recent check results to return.",
    )

class WatchListParams(BaseModel):
    """List all watchers."""

class SendNotificationParams(BaseModel):
    """Send a notification through an output channel (email, webhook, log, etc.)."""

    channel: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Channel instance name as declared in the app YAML "
            "(e.g. 'email_alerts', 'slack_ops', 'audit_log')."
        ),
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="The notification message body (plain text).",
    )
    title: str = Field(
        default="",
        max_length=500,
        description="Subject line or title for the notification.",
    )
    priority: str = Field(
        default="normal",
        description="Priority: 'low', 'normal', 'high', 'critical'.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Optional tags for filtering/routing.",
    )
    structured_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional machine-readable JSON payload.",
    )
    output_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-delivery channel config - REQUIRED when targeting a specific "
            "recipient. For email: {\"to_address\": \"user@example.com\"}. "
            "For SMS: {\"to_number\": \"+33...\"}. "
            "Without this, the channel's user_resolver must be able to "
            "resolve the target automatically."
        ),
    )

class UseSkillParams(BaseModel):
    """Load a skill to get detailed instructions for a specific workflow."""

    command: str = Field(
        ...,
        description="Skill command to load (e.g. '/commit', '/review')",
    )

class CallAppParams(BaseModel):
    """Parameters for calling another deployed app."""
    app_id: str = Field(..., description="The app_id of the deployed app to call.")
    input: str = Field(..., description="The input to send to the app.")
    timeout: float = Field(default=120.0, description="Timeout in seconds.")

class AskUserParams(BaseModel):
    """Ask the user a question (blocks until they answer)."""

    question: str = Field(
        ...,
        description=(
            "The question or message to show the user. Be specific about what you need. "
            "Example: 'Should I proceed with this plan?'"
        ),
    )
    content: str | None = Field(
        default=None,
        description=(
            "Optional content for the user to review/edit (plan, code, config, etc.). "
            "Displayed in a reviewable format. The user can modify it before approving. "
            "The (possibly edited) content is returned in the response. "
            "Example: '## Plan\\n1. Create auth middleware\\n2. Add routes'"
        ),
    )
    choices: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of choices for the user to select from. "
            "The client displays these as clickable buttons or a dropdown. "
            "The user's selection is returned as the response message. "
            "Example: ['FastAPI', 'Django', 'Flask']"
        ),
    )
    allow_multiple: bool = Field(
        default=False,
        description=(
            "If true with choices, the user can select multiple options. "
            "The response will contain all selected choices comma-separated."
        ),
    )
    form: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Optional structured form for complex user input. Each field: "
            "{type, name, label, options?, placeholder?, default?, required?}. "
            "Types: 'select', 'text', 'textarea', 'checkbox', 'toggle', 'number'. "
            "The user's responses are returned as a JSON object. "
            "Example: [{'type': 'select', 'name': 'framework', 'label': 'Framework', 'options': ['FastAPI', 'Django']}, "
            "{'type': 'text', 'name': 'name', 'label': 'Project name', 'placeholder': 'my-app'}]"
        ),
    )
    timeout: float = Field(
        default=300.0, ge=10.0, le=1800.0,
        description="Max seconds to wait for user response. Default: 300 (5 min).",
    )
