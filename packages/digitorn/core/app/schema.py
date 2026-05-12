"""Pydantic models defining the app YAML structure.

These models are the **parse target** - the YAML is validated directly
against ``AppDefinition``.  They also serve as the documentation: every
field has a description that can be rendered in ``digitorn app schema``.

The structure is intentionally generic.  The ``modules`` block maps
module IDs to ``ModuleBlock``s, and each block contains:

- ``setup``: ordered list of action calls (action name + params)
- ``constraints``: runtime restrictions (validated against the module's
  ``ConstraintSpec`` declarations)

No module-specific knowledge is baked in - any module with ``@action``
methods is automatically configurable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from digitorn.core.app.typed_models import (
    AgentPoolConfig,
    CoordinationBlock,
    IncludeBlock,
    InstructionsBlock,
    QuickPrompt,
    SkillEntry,
    SlashCommand,
)


class AppMeta(BaseModel):
    """Top-level application identity."""

    model_config = {"extra": "forbid"}

    app_id: str = Field(..., description="Unique application identifier.")
    name: str = Field(..., description="Human-readable application name.")
    short_name: str = Field(
        default="",
        description=(
            "Compact label used in tight UI slots (dashboard chip, tab "
            "header, mobile drawer). One word, or two SHORT words; "
            "longer names overflow the 68 px chip and overlap their "
            "neighbors. When empty, clients fall back to `name` "
            "(truncated with ellipsis)."
        ),
    )
    version: str = Field(default="1.0", description="Application version string.")
    schema_version: str = Field(default="1", description="YAML schema version for forward compatibility.")
    description: str = Field(default="", description="Optional description.")
    author: str = Field(default="", description="Application author.")
    tags: list[str] = Field(default_factory=list, description="Searchable tags.")

    # ── Visual / UI metadata ─────────────────────────────────────
    icon: str = Field(
        default="",
        description=(
            "App icon. Can be: emoji ('💻'), icon name ('code'), "
            "URL to an image ('https://...'), or base64 data URI. "
            "If empty, the client generates a colored circle from app_id."
        ),
    )
    color: str = Field(
        default="",
        description=(
            "Accent color for the app card/header. Hex format: '#8B5CF6'. "
            "If empty, auto-generated from app_id hash."
        ),
    )
    category: str = Field(
        default="general",
        description=(
            "App category for grouping in the UI. "
            "Examples: 'coding', 'writing', 'research', 'data', 'devops', "
            "'design', 'communication', 'automation', 'general'."
        ),
    )
    quick_prompts: list[QuickPrompt] = Field(
        default_factory=list,
        description=(
            "Suggested prompts shown as clickable buttons when the user opens the app. "
            "Each entry: {label: 'short text', message: 'full prompt', icon: 'emoji'}. "
            "If empty, the client shows just the input field."
        ),
    )
    attachments: list[Literal["image", "document", "audio", "video"]] | Literal["*"] | None = Field(
        default=None,
        description=(
            "Attachment types the chat composer accepts.\n"
            "  - ``image``    PNG / JPG / GIF / WEBP / HEIC, routed to a "
            "vision-capable LLM\n"
            "  - ``document`` PDF / TXT / MD / CSV / XLSX / DOCX / code "
            "files, extracted as text before being handed to the model\n"
            "  - ``audio``    MP3 / WAV / M4A / OGG, transcribed via STT\n"
            "  - ``video``    MP4 / MOV / WEBM, accepted by a few vision "
            "models (Gemini, Sonnet)\n\n"
            "Pass the string ``*`` to enable every supported type. "
            "Leaving the field empty (``null`` / unset) disables "
            "attachments entirely: the composer's ``+`` menu collapses "
            "to slash-commands + snippets only. Apps must opt in - the "
            "default is no attachments."
        ),
    )
    attachments_mode: Literal["direct", "tool"] = Field(
        default="direct",
        description=(
            "How the agent sees attached files. Two modes only.\n"
            "  - ``direct`` (default) Full extracted text is prepended "
            "to the user message. Agent answers immediately, no tool "
            "call needed. Use for chat apps.\n"
            "  - ``tool``   Files are mirrored into the workspace "
            "under ``attachments/`` and the agent is told to use "
            "WsRead / WsGlob / WsGrep to inspect them. Use when you "
            "want the agent to read partially, iterate, or edit big "
            "files - requires the ``workspace`` module loaded and "
            "``workspace.read`` granted to the agent."
        ),
    )
    # ── Nested client-UI mirrors (for clients that look under app.*) ──
    # These mirror the top-level AppDefinition.features / .theme fields so a
    # YAML that nests them under app: also parses cleanly. The compiler
    # merges top-level + nested when assembling the manifest summary.
    features: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Client-UI feature toggles (same contract as top-level features:). "
            "Keys: voice, attachments, tools_panel, snippets, tasks_panel, "
            "memory_panel, context_ring, markdown, slash_commands, "
            "message_actions, status_pills, token_badges. "
            "Unspecified keys default to true on the client."
        ),
    )
    theme: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Client theme overrides. Keys: accent (hex), background (hex). "
            "accent overrides app.color for fine-grained control."
        ),
    )


class SetupStep(BaseModel):
    """A single action call to execute during app bootstrap.

    Maps directly to ``module.execute(action, params)``.
    The ``params`` dict is validated at compile time against the action's
    ``params_model`` (Pydantic JSON Schema).
    """

    model_config = {"extra": "forbid"}

    action: str = Field(..., description="Action name on the target module.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the action. May contain {{variables}}.",
    )


class CapabilityGrant(BaseModel):
    """An explicit grant or deny for module actions."""

    model_config = {"extra": "forbid"}

    module: str = Field(..., description="Target module ID.")
    actions: list[str] = Field(
        default_factory=list,
        description="Action names. Empty = all actions on the module.",
    )
    reason: str = Field(default="", description="Human-readable reason (for deny).")


class BehaviorCustomRule(BaseModel):
    """Legacy custom rule format. Kept for backward compatibility.
    Prefer ``rule_definitions`` for new apps.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(default="custom")
    rule: str = Field(...)
    enforce: str = Field(default="pre_tool")
    trigger: str = Field(default="")
    condition: dict[str, Any] = Field(default_factory=dict)
    action: str = Field(default="warn")
    message: str = Field(default="")


class BehaviorRuleDefinition(BaseModel):
    """A fully declarative behavioral rule - works for ANY action.

    Example::

        rule_definitions:
          - id: read_before_edit
            description: "Must read a file before editing it"
            trigger: [edit]
            when: pre_tool
            action: warn
            condition:
              target_not_in_set: read_files
            message: "You are editing '{target}' without reading it first."

          - id: no_sql_injection
            description: "Block raw SQL in user-facing queries"
            trigger: [database.execute]
            when: pre_tool
            action: block
            condition:
              param_matches:
                param: query
                pattern: ".*;\\s*(DROP|DELETE|TRUNCATE)"
            message: "Dangerous SQL detected. Use parameterized queries."
    """

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Unique rule identifier.")
    description: str = Field(default="", description="Human-readable description (shown in prompt).")
    trigger: list[str] | str = Field(
        default="*",
        description="Tool name(s) that trigger this rule. '*' = all tools.",
    )
    when: str = Field(
        default="pre_tool",
        description="When to check: 'pre_tool', 'post_tool', 'on_text' (agent text output).",
    )
    action: str = Field(
        default="warn",
        description="What to do: 'block' (prevent), 'warn' (inject message), 'remind' (post-tool hint).",
    )
    condition: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "When the rule fires. Condition types:\n"
            "  target_not_in_set: <set_name>    - target param NOT in tracked set\n"
            "  target_in_set: <set_name>         - target param IS in tracked set\n"
            "  counter_gte: {name, value}         - counter >= threshold\n"
            "  param_matches: {param, pattern}    - param matches regex\n"
            "  param_contains: {param, value}     - param contains string\n"
            "  flag_is: {name, value}             - flag equals value\n"
            "  no_text_before_tools: true         - agent didn't explain before tools\n"
            "  consecutive_gte: <N>               - same tool called N+ times\n"
            "  all: [conditions...]               - all must match\n"
            "  any: [conditions...]               - at least one matches\n"
            "  not: <condition>                   - negation"
        ),
    )
    message: str = Field(
        default="",
        description=(
            "Message template. Placeholders:\n"
            "  {target}              - file_path or primary target param\n"
            "  {tool}                - current tool name\n"
            "  {param:<name>}        - any param value\n"
            "  {counter:<name>}      - counter value\n"
            "  {set_count:<name>}    - size of a tracked set\n"
            "  {turn}                - current turn number"
        ),
    )


class StateTrackingSetConfig(BaseModel):
    """Configure a named set that tracks targets per tool."""

    model_config = {"extra": "forbid"}

    add_on: list[str] = Field(..., description="Tool names that add to this set.")
    target: str = Field(default="file_path", description="Param name to extract as the target value.")
    aliases: list[str] = Field(default_factory=list, description="Alternative param names (path, filepath, etc.).")


class StateTrackingCounterConfig(BaseModel):
    """Configure a named counter."""

    model_config = {"extra": "forbid"}

    increment_on: list[str] = Field(default_factory=list, description="Tool names that increment this counter.")
    reset_on: list[str] = Field(default_factory=list, description="Tool names that reset this counter to 0.")
    reset_when: dict[str, str] = Field(
        default_factory=dict,
        description="Reset when a param matches: {tool, param, matches}.",
    )

    @model_validator(mode="after")
    def _validate_has_trigger(self):
        if not self.increment_on:
            raise ValueError(
                "counter needs at least one 'increment_on' tool name "
                "(counters that never fire are useless)"
            )
        return self


class StateTrackingFlagConfig(BaseModel):
    """Configure a named boolean flag."""

    model_config = {"extra": "forbid"}

    set_on: list[str] = Field(default_factory=list, description="Tool names that set this flag to True.")
    unset_on: list[str] = Field(default_factory=list, description="Tool names that set this flag to False.")

    @model_validator(mode="after")
    def _validate_has_set_on(self):
        if not self.set_on:
            raise ValueError(
                "flag needs at least one 'set_on' tool name "
                "(flags that never flip are useless)"
            )
        return self


class StateTrackingConfig(BaseModel):
    """Configure what the session state tracks - fully declarative.

    Example::

        state_tracking:
          sets:
            read_files:
              add_on: [read, filesystem.read]
              target: file_path
            fetched_urls:
              add_on: [web.fetch]
              target: url
          counters:
            changes_since_test:
              increment_on: [edit, write]
              reset_on: [bash]
              reset_when:
                tool: bash
                param: command
                matches: "pytest|npm test"
          flags:
            has_web_searched:
              set_on: [web.search, search]
    """

    model_config = {"extra": "forbid"}

    sets: dict[str, StateTrackingSetConfig] = Field(default_factory=dict)
    counters: dict[str, StateTrackingCounterConfig] = Field(default_factory=dict)
    flags: dict[str, StateTrackingFlagConfig] = Field(default_factory=dict)


class ClassifierContextConfig(BaseModel):
    """What context the classifier receives about the agent's state."""

    model_config = {"extra": "forbid"}

    tool_inventory: bool = Field(
        default=True,
        description="Send the agent's tool names + descriptions.",
    )
    session_state: bool = Field(
        default=True,
        description="Send session state: files read/edited, searches, violations, turn number.",
    )
    workspace_info: bool = Field(
        default=True,
        description="Send workspace metadata: project type, languages, file count.",
    )
    recent_history: bool = Field(
        default=True,
        description="Send recent messages with tool calls and results.",
    )
    history_depth: int = Field(
        default=8,
        description="How many recent messages to include.",
    )


class ClassifierConfig(BaseModel):
    """Configuration for the semantic classifier LLM.

    The classifier is a generic pre-turn analysis engine. Each app
    configures what it analyzes, when it runs, and what it produces.

    Example::

        behavior:
          classify_turns: true
          classifier:
            frequency: every_turn
            timeout: 15
            complexity_levels: [trivial, simple, moderate, complex, critical]
            approaches: [direct, explore_first, plan_and_confirm, delegate, research_first]
            risk_levels: [none, low, medium, high]
            max_directives: 5
            system_prompt: "{{prompt.classifier}}"
    """

    model_config = {"extra": "forbid"}

    # ── When to run ──
    frequency: Literal[
        "every_turn", "first_turn", "every_n_turns", "on_new_message",
    ] = Field(
        default="every_turn",
        description=(
            "When to run the classifier:\n"
            "  'every_turn'    - before every agent turn (classifier can skip via skip_reason)\n"
            "  'first_turn'    - only on the first turn of a session\n"
            "  'every_n_turns' - every N turns (set frequency_n)\n"
            "  'on_new_message'- only when the user sent a new message (skip tool-only turns)"
        ),
    )
    frequency_n: int = Field(
        default=3,
        description="For 'every_n_turns': run every N turns.",
    )
    skip_followups: bool = Field(
        default=True,
        description=(
            "Auto-skip classification for simple follow-ups: "
            "'yes', 'ok', 'continue', 'go ahead', etc. "
            "Saves a classifier LLM call."
        ),
    )
    timeout: int = Field(
        default=15,
        description="Max seconds to wait for the classifier LLM response.",
    )

    # ── Output schema - what the classifier produces ──
    complexity_levels: list[str | dict[str, str]] = Field(
        default_factory=lambda: ["trivial", "simple", "moderate", "complex", "critical"],
        description=(
            "Ordered list of complexity levels. Each entry is either a plain string\n"
            "or a dict with {name, when, behavior} for full customization:\n\n"
            "  complexity_levels:\n"
            "    - name: trivial\n"
            "      when: '1 action, obvious answer'\n"
            "      behavior: 'Just do it, no planning'\n"
            "    - name: deep\n"
            "      when: 'Cross-cutting concern, 10+ files'\n"
            "      behavior: 'Full plan, user approval, phased execution'"
        ),
    )
    approaches: list[str | dict[str, str]] = Field(
        default_factory=lambda: ["direct", "explore_first", "plan_and_confirm", "delegate", "research_first"],
        description=(
            "Approach strategies. Each entry is either a plain string\n"
            "or a dict with {name, label, when, behavior} for full customization:\n\n"
            "  approaches:\n"
            "    - name: direct\n"
            "      label: 'Execute directly'\n"
            "      when: 'Task is trivial or simple, clear path'\n"
            "      behavior: 'Go straight to tool calls, minimal text'\n"
            "    - name: ask_expert\n"
            "      label: 'Needs human expertise'\n"
            "      when: 'Domain knowledge required that the agent lacks'\n"
            "      behavior: 'Ask the user with AskUser, explain what you need to know'"
        ),
    )
    risk_levels: list[str | dict[str, str]] = Field(
        default_factory=lambda: ["none", "low", "medium", "high"],
        description=(
            "Risk levels. Same format as approaches - string or dict:\n\n"
            "  risk_levels:\n"
            "    - name: safe\n"
            "      when: 'Read-only, no side effects'\n"
            "    - name: destructive\n"
            "      when: 'Deletes data, drops tables, force-pushes'\n"
            "      behavior: 'MUST confirm with user, explain what will be lost'"
        ),
    )
    max_directives: int = Field(
        default=5,
        description="Maximum number of directives the classifier should produce.",
    )

    # ── What context to include ──
    context: ClassifierContextConfig = Field(
        default_factory=ClassifierContextConfig,
        description="What context the classifier receives.",
    )

    # ── The behavioral model (system prompt) ──
    system_prompt: str | None = Field(
        default=None,
        description=(
            "Custom system prompt for the classifier LLM.\n"
            "Supports {{prompt.X}} references to load from ./prompts/.\n"
            "When null, uses the built-in default behavioral model.\n"
            "The prompt receives the output schema dynamically - you don't\n"
            "need to hardcode complexity/approach values in your prompt."
        ),
    )

    # ── Directive format ──
    directive_prefix: str = Field(
        default="[BEHAVIOR DIRECTIVE - {complexity} complexity, {risk} risk]",
        description=(
            "Format string for the directive header. Available placeholders:\n"
            "{complexity}, {approach}, {risk}, {approach_label}"
        ),
    )
    high_risk_warning: str = Field(
        default=(
            "Risk level: {risk}. "
            "Confirm destructive or irreversible actions with the user before proceeding."
        ),
        description="Warning appended when risk >= high_risk_threshold. Use {risk} placeholder.",
    )
    high_risk_threshold: str = Field(
        default="medium",
        description="Risk level (from risk_levels) at or above which high_risk_warning is appended.",
    )
    directive_footer: str = Field(
        default=(
            "Follow these directives. They are based on your behavioral rules "
            "and the current session state. Violations are detected in real-time."
        ),
        description="Text appended at the end of every directive message.",
    )


class BehaviorConfig(BaseModel):
    """Behavioral enforcement rules - actively monitored at runtime.

    Define a profile preset and/or individual rules. All enabled rules
    are enforced by the behavior engine on every tool call.

    Example::

        behavior:
          profile: coding
          classify_turns: true
          classifier:
            frequency: every_turn
            timeout: 15
            approaches: [direct, plan_and_confirm, delegate]
          rules:
            read_before_edit: true
            test_after_changes: true
          custom:
            - id: protect_migrations
              rule: "Never modify migration files without asking"
              trigger: edit
              action: block
    """

    model_config = {"extra": "forbid"}

    profile: str | None = Field(
        default=None,
        description="Preset profile: 'dev', 'coding', 'research', 'data', 'creative', 'assistant', or '{{behavior.X}}'.",
    )
    rules: dict[str, Any] = Field(
        default_factory=dict,
        description="Rule overrides. Keys are rule IDs (read_before_edit, test_after_changes, etc.), values are bool or int.",
    )
    custom: list[BehaviorCustomRule] = Field(
        default_factory=list,
        description="Legacy custom rules (backward compat). Prefer rule_definitions.",
    )
    rule_definitions: list[BehaviorRuleDefinition] = Field(
        default_factory=list,
        description="Fully declarative rules - works for ANY action. See BehaviorRuleDefinition.",
    )
    state_tracking: StateTrackingConfig | None = Field(
        default=None,
        description="What the session state tracks. When null, uses defaults from profile.",
    )
    classify_turns: bool = Field(
        default=False,
        description=(
            "Enable semantic classification. A small LLM analyzes each user message "
            "BEFORE the main agent acts and injects behavioral directives."
        ),
    )
    classifier: ClassifierConfig = Field(
        default_factory=ClassifierConfig,
        description="Configuration for the semantic classifier LLM.",
    )
    brain: "AgentBrain | None" = Field(
        default=None,
        description=(
            "LLM for semantic classification. Uses the same AgentBrain format as agents.\n"
            "If omitted, uses the coordinator's brain.\n"
            "Tip: use a fast/cheap model (haiku, deepseek-chat) for minimal latency."
        ),
    )
    use_agent_brain: bool = Field(
        default=True,
        description=(
            "When brain is not set, reuse the coordinator's brain for classification. "
            "Set to false to disable classification when no brain is configured."
        ),
    )


class CapabilitiesConfig(BaseModel):
    """Application-level security capabilities."""

    model_config = {"extra": "forbid"}

    default_policy: Literal["auto", "approve", "block"] = Field(
        default="approve",
        description="Default action policy: 'auto', 'approve', or 'block'.",
    )
    max_risk_level: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Maximum allowed risk level: 'low', 'medium', or 'high'.",
    )
    grant: list[CapabilityGrant] = Field(
        default_factory=list,
        description="Explicit action grants per module.",
    )
    approve: list[CapabilityGrant] = Field(
        default_factory=list,
        description="Actions requiring explicit user approval before execution.",
    )
    deny: list[CapabilityGrant] = Field(
        default_factory=list,
        description="Explicit action denies per module.",
    )
    approval_timeout: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Seconds to wait for user approval before auto-denying (30–3600).",
    )
    hidden_modules: list[str] = Field(
        default_factory=list,
        description=(
            "Module IDs to hide from the agent's tool index. "
            "Hidden modules are still loaded and can be used by setup steps, "
            "hooks, and channels - but the agent cannot see or call their tools. "
            "Example: ['filesystem'] to prevent the agent from accessing files."
        ),
    )
    hidden_actions: list[CapabilityGrant] = Field(
        default_factory=list,
        description=(
            "Specific actions to hide from the agent's tool index. "
            "Unlike 'deny', hidden actions are invisible but still executable "
            "by setup steps, hooks, and channels. Use this to declutter the "
            "agent's toolset without breaking internal automation."
        ),
    )


class MCPServerSandbox(BaseModel):
    """Per-MCP-server OS-level sandbox permissions.

    Every MCP server must explicitly declare what it needs.
    No declaration = no OS-level rights (deny-by-default).

    Example::

        mcp:
          config:
            servers:
              github:
                command: npx @modelcontextprotocol/server-github
                sandbox:
                  permissions: [process.exec, net.http]
                  paths:
                    read: ['{{workspace}}']
                    write: []
                  allowed_hosts: [api.github.com]

    Permission categories::

        process.exec     - spawn subprocesses (required for stdio transport)
        process.*        - all process permissions (exec + spawn_daemon)
        net.http         - outbound HTTP (required for SSE/HTTP transport)
        net.socket       - raw socket access
        net.listen       - bind/listen on a port
        net.*            - all network permissions
        fs.read          - read files beyond workspace
        fs.write         - write files beyond workspace
        fs.delete        - delete files beyond workspace
        fs.*             - all filesystem permissions
    """

    model_config = {"extra": "forbid"}

    permissions: list[str] = Field(
        default_factory=list,
        description=(
            "OS-level permissions this server needs. "
            "Example: ['process.exec', 'net.http', 'fs.read']"
        ),
    )
    paths: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Filesystem paths beyond workspace. "
            "Keys: 'read' (read-only), 'write' (read-write). "
            "Supports {{workspace}} and ~ expansion."
        ),
    )
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed network hosts for outbound connections. "
            "Only effective if 'net.http' or 'net.socket' is granted."
        ),
    )


class ModuleBlock(BaseModel):
    """Configuration block for a single module in the app YAML.

    Three sections:

    - ``config``: Static module configuration - pushed via
      ``module.on_config_update(config)`` at bootstrap time.  Validated
      against the module's ``CONFIG_MODEL`` (Pydantic) if declared.

    - ``setup``: Ordered list of action calls executed at bootstrap time.

    - ``constraints``: Runtime restrictions applied during the app's lifetime.

    Example::

        perception:
          config:
            enabled: false
            capture_after: true
            ocr_enabled: false
            timeout_seconds: 10
            actions:
              browser.take_screenshot:
                capture_after: true
                ocr_enabled: true
          setup:
            - action: register_handler
              params: { ... }
          constraints:
            allowed_actions: [capture_screen, parse_screen]
    """

    model_config = {"extra": "forbid"}

    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Static module configuration. Pushed to the module via "
            "on_config_update() at bootstrap time. Validated against "
            "the module's CONFIG_MODEL if declared.\n\n"
            "For MCP servers and third-party modules, an optional "
            "'sandbox' key declares OS-level permissions:\n"
            "  sandbox:\n"
            "    permissions: [fs.read, net.http]\n"
            "    paths:\n"
            "      read: ['{{workspace}}']\n"
            "      write: []\n"
            "    allowed_hosts: [api.github.com]"
        ),
    )
    setup: list[SetupStep] = Field(
        default_factory=list,
        description="Ordered list of actions to execute at app bootstrap.",
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Runtime constraints. 'allowed_actions' and 'blocked_actions' are "
            "universal; other keys are validated against the module's "
            "ConstraintSpec declarations."
        ),
    )
    middleware: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Module-level middleware pipeline. Each entry is a middleware "
            "name with optional config: [{audit: {log_params: true}}, {retry: {max_attempts: 3}}]"
        ),
    )
    credential: Any = Field(
        default=None,
        description=(
            "Reference to a user-vault credential bound to this module.\n"
            "Two shapes (compact and explicit):\n"
            "  credential: openai_main\n"
            "  credential: { ref: openai_main, scope: per_user }\n"
            "Resolved at activation time. The module's CredentialSlot "
            "declares which fields are injected and where in `config`."
        ),
    )

    @field_validator("credential", mode="before")
    @classmethod
    def _normalise_credential(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (str, dict)):
            return v
        raise ValueError(
            "credential must be a string (compact ref) or a mapping "
            "{ref, scope, provider?}",
        )


class ContextConfig(BaseModel):
    """Context management configuration for the agent loop.

    Controls how the context window is managed to prevent overflow errors.
    When the context fills up, the runtime can automatically compact it
    using the configured strategy.

    Can be set at two levels:
    - ``execution.context`` - default for all agents
    - ``agent.brain.context`` - per-brain override (multi-agent apps)

    Example::

        brain:
          provider: deepseek
          model: deepseek-chat
          context:
            max_tokens: 131072
            strategy: summarize
            keep_recent: 30
            compression_trigger: 0.70
    """

    model_config = {"extra": "forbid"}

    max_tokens: int = Field(
        default=0,
        ge=0,
        le=2_000_000,
        description=(
            "Context window size in tokens. 0 = auto-detect from provider. "
            "Override if the provider doesn't report it."
        ),
    )
    output_reserved: int = Field(
        default=4096,
        description="Tokens reserved for output generation. Subtracted from max_tokens for pressure calculation.",
    )
    strategy: Literal["truncate", "summarize"] = Field(
        default="summarize",
        description="Compaction strategy: 'truncate' or 'summarize'.",
    )
    keep_recent: int = Field(
        default=10,
        description="Number of recent messages to preserve during compaction.",
    )
    compression_trigger: float = Field(
        default=0.75,
        description="Token pressure ratio (0.0-1.0) at which automatic compaction triggers.",
    )
    summary_max_tokens: int = Field(
        default=1024,
        description="Maximum tokens for the summary when using 'summarize' strategy.",
    )
    auto_compact: bool = Field(
        default=True,
        description=(
            "Enable automatic compaction. When true, the runtime injects a "
            "context_pressure hook automatically if none is declared."
        ),
    )
    summary_brain: "AgentBrain | None" = Field(
        default=None,
        description=(
            "Optional separate brain for summarization during compaction. "
            "Use a fast/cheap model for summaries instead of the main brain. "
            "If not set, the agent's main brain is used."
        ),
    )


class AgentBrain(BaseModel):
    """LLM brain configuration for an agent.

    Two modes:

    1. **Inline** - full provider config embedded in the agent::

        brain:
          provider: deepseek
          model: deepseek-chat
          temperature: 0.2
          config:
            api_key: "{{secret.DEEPSEEK_API_KEY}}"
            base_url: "https://api.deepseek.com/v1"

    2. **Reference** - points to a named provider in ``modules.llm_provider``::

        brain:
          provider_id: deepseek_main
          temperature: 0.2
    """

    model_config = {"extra": "forbid"}

    provider_id: str | None = Field(
        default=None,
        description=(
            "Reference to a named provider declared in "
            "modules.llm_provider.config.providers. "
            "If set, provider/model/config are ignored."
        ),
    )

    provider: str | None = Field(
        default=None,
        description=(
            "Provider hint for auto-resolving base URL. Known values: "
            "anthropic, openai, deepseek, groq, mistral, together, ollama, "
            "lm_studio, vllm, google-gemini, gemini, xai, grok, cerebras, "
            "perplexity, fireworks, github_copilot."
        ),
    )

    @field_validator("provider", mode="after")
    @classmethod
    def _validate_provider_name(cls, v):
        if v is None:
            return v
        _KNOWN = {
            "openai", "deepseek", "groq", "mistral", "together",
            "lm_studio", "vllm", "ollama", "anthropic",
            "google-gemini", "gemini", "xai", "grok",
            "cerebras", "perplexity", "fireworks",
            "github_copilot",
        }
        if v.lower() not in _KNOWN:
            import difflib as _df
            sug = _df.get_close_matches(v.lower(), _KNOWN, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
            raise ValueError(
                f"unknown provider '{v}'. Built-in: {sorted(_KNOWN)}.{hint}"
            )
        return v

    model: str | None = Field(
        default=None,
        description="Model identifier (e.g. 'deepseek-chat', 'claude-sonnet-4-20250514').",
    )
    backend: Literal["openai_compat", "anthropic", "github_copilot"] = Field(
        default="openai_compat",
        description=(
            "Provider backend: 'anthropic', 'openai_compat', or "
            "'github_copilot' (uses your Copilot subscription via "
            "api.githubcopilot.com)."
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific config (api_key, base_url, etc.).",
    )

    credential: Any = Field(
        default=None,
        description=(
            "Reference to a user-vault credential. Two YAML shapes:\n"
            "  - string (compact): `credential: openai_main`\n"
            "  - mapping (explicit): `credential: { ref: openai_main, scope: per_user }`\n"
            "The runtime resolves the reference at activation time and "
            "injects the credential's fields into `config` (api_key, "
            "base_url, etc.) replacing any inline values. "
            "Recommended over inline `{{secret.X}}` templates."
        ),
    )

    @field_validator("credential", mode="before")
    @classmethod
    def _normalise_credential(cls, v: Any) -> Any:
        # Accept None, string (compact), or dict (explicit). Type
        # check only - structural validation (ref slug, scope value)
        # happens in `parse_credential_ref` at compile time.
        if v is None:
            return None
        if isinstance(v, (str, dict)):
            return v
        raise ValueError(
            "credential must be a string (compact ref) or a mapping "
            "{ref, scope, provider?}",
        )

    temperature: float | None = Field(default=None, description="Sampling temperature.")
    max_tokens: int | None = Field(default=None, description="Max tokens to generate.")
    top_p: float | None = Field(default=None, description="Nucleus sampling threshold.")
    timeout: float | None = Field(default=None, description="Request timeout in seconds.")

    native_tool_use: bool | None = Field(
        default=None,
        description=(
            "Override native tool calling detection. "
            "By default, auto-detected from provider (e.g. Ollama defaults to text-based). "
            "Set to true to force native OpenAI-style tool_calls (e.g. qwen2.5-coder on Ollama). "
            "Set to false to force text-based tool calling."
        ),
    )

    context: ContextConfig | None = Field(
        default=None,
        description=(
            "Context window management for this brain. "
            "If not set, inherits from execution.context. "
            "Useful in multi-agent apps where each brain uses a different model."
        ),
    )

    fallback: "AgentBrain | None" = Field(
        default=None,
        description=(
            "Fallback brain used when the primary provider returns a billing "
            "or credit error (402, insufficient balance). Lets apps gracefully "
            "degrade to a cheaper/free model instead of failing. Example:\n"
            "  fallback:\n"
            "    provider: anthropic\n"
            "    model: claude-haiku-4-5\n"
            "    config:\n"
            "      api_key: \"claude-code\""
        ),
    )

    # ── Multimodal capabilities ──────────────────────────
    vision: bool | None = Field(
        default=None,
        description=(
            "Whether this model supports image input (vision). "
            "null = auto-detect from model name. true = force enabled. "
            "false = convert images to text descriptions."
        ),
    )
    image_generation: bool = Field(
        default=False,
        description=(
            "Whether this model can generate images. "
            "If true, the framework handles image output in tool results "
            "and SSE events. Models like DALL-E, Stable Diffusion via MCP."
        ),
    )
    image_detail: str = Field(
        default="auto",
        description=(
            "Image resolution for vision. "
            "'auto' = provider decides. 'low' = 512px (cheaper, faster). "
            "'high' = native resolution (more accurate, more tokens)."
        ),
    )
    max_images_per_turn: int = Field(
        default=5, ge=0, le=100,
        description="Max images sent to the model per turn (0 = unlimited).",
    )

    @model_validator(mode="after")
    def _validate_credential_source(self) -> "AgentBrain":
        # Skip credential check when model is missing - the compiler
        # later raises a clearer "model is required" error and we don't
        # want to mask it with auth advice.
        if not self.model:
            return self

        # Reference brains delegate auth to a named provider in
        # modules.llm_provider, no local check needed.
        if self.provider_id is not None:
            return self

        # Local providers run without auth. Skip the check.
        _LOCAL_PROVIDERS = {"ollama", "lm_studio", "vllm"}
        if self.provider in _LOCAL_PROVIDERS:
            return self

        # Explicit base_url override pointing at localhost is treated
        # as local. Covers `provider: openai` + `base_url: http://localhost:*`.
        base_url = (self.config or {}).get("base_url", "") or ""
        if isinstance(base_url, str) and (
            "localhost" in base_url or "127.0.0.1" in base_url or "://0.0.0.0" in base_url
        ):
            return self

        # claude-code sentinel reads the OAuth token from the local
        # Claude Code installation. No credential needed in YAML.
        api_key = (self.config or {}).get("api_key", "")
        if api_key == "claude-code":
            return self

        # No provider declared at all + no credential ref + no provider_id
        # is suspicious but not strictly invalid (provider_id can be
        # resolved later, model alone might match a default). Skip strict
        # validation when both provider and credential are absent so the
        # error stays actionable.
        if self.provider is None and self.credential is None and not api_key:
            return self

        # Cloud provider with no auth: hard error.
        if self.provider is not None and self.credential is None and not api_key:
            raise ValueError(
                f"brain.provider='{self.provider}' is a cloud provider that "
                f"requires authentication, but no credential source was "
                f"declared. Add one of:\n"
                f"  credential: <vault_ref>           # recommended\n"
                f"  provider_id: <named_provider>     # references modules.llm_provider\n"
                f"  config: {{ api_key: '{{{{env.X}}}}' }}     # inline (dev-only)\n"
                f"Local providers (ollama, lm_studio, vllm) and `api_key: \"claude-code\"` "
                f"are exempt."
            )
        return self

    @model_validator(mode="after")
    def _validate_vision_model_match(self) -> "AgentBrain":
        # Only complain when the user EXPLICITLY enabled vision on a
        # model that has no known vision capability. ``vision=None``
        # (auto-detect) and ``vision=False`` (force off) are fine.
        if self.vision is True and self.model:
            model = self.model.lower()
            _VISION_MODELS = {
                "claude-sonnet", "claude-opus", "claude-haiku",
                "gpt-4o", "gpt-4-turbo", "gpt-4-vision", "gpt-5",
                "gemini", "llava", "deepseek-vl",
                "pixtral", "qwen-vl", "internvl",
            }
            if not any(v in model for v in _VISION_MODELS):
                raise ValueError(
                    f"vision=true was set but model '{self.model}' has no "
                    f"known vision capability. Either set a vision-capable "
                    f"model (claude-sonnet-*, claude-opus-*, claude-haiku-*, "
                    f"gpt-4o*, gemini-*, pixtral-*, qwen-vl-*, ...) or "
                    f"remove `vision: true`."
                )
        return self

    @property
    def is_reference(self) -> bool:
        """True if this brain references a named provider."""
        return self.provider_id is not None

    @property
    def supports_vision(self) -> bool:
        """Detect if this model supports vision (images as input)."""
        if self.vision is not None:
            return self.vision
        # Auto-detect from model name
        model = (self.model or "").lower()
        _VISION_MODELS = {
            "claude-sonnet", "claude-opus", "claude-haiku",
            "gpt-4o", "gpt-4-turbo", "gpt-4-vision",
            "gemini", "llava", "deepseek-vl",
            "pixtral", "qwen-vl", "internvl",
        }
        return any(v in model for v in _VISION_MODELS)


class InputConfig(BaseModel):
    """Input contract for one_shot mode.

    Defines what the application expects as input and how the CLI
    should present it to the agent.

    Example::

        input:
          type: text
          description: "Code source to analyse"
          required: true
    """

    model_config = {"extra": "forbid"}

    type: str = Field(
        default="text",
        description=(
            "Input type: 'text', 'image', 'audio', 'video', 'file', 'json', 'any'. "
            "Must be supported by the agent's brain model. "
            "For example, 'image' requires a vision-capable model (GPT-4o, Claude Sonnet, Gemini)."
        ),
    )
    accept: list[str] = Field(
        default_factory=list,
        description=(
            "Accepted MIME types. Empty = infer from type. "
            "Examples: ['image/png', 'image/jpeg'], ['audio/wav', 'audio/mp3'], "
            "['application/pdf'], ['video/mp4']."
        ),
    )
    max_size: str = Field(
        default="",
        description="Maximum input size. Examples: '10MB', '500KB'. Empty = no limit.",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the expected input.",
    )
    required: bool = Field(
        default=True,
        description="Whether input is mandatory.",
    )


class OutputConfig(BaseModel):
    """Output contract for one_shot mode.

    Defines what the application produces and how the CLI should
    format it.

    Example::

        output:
          type: json
          description: "Structured analysis report"
          schema:
            type: object
            properties:
              bugs: { type: array }
              score: { type: integer }
    """

    model_config = {"extra": "forbid"}

    type: str = Field(
        default="text",
        description=(
            "Output type: 'text', 'json', 'markdown', 'file', 'image', 'audio'. "
            "Determines how the CLI and API format the response."
        ),
    )
    format: str = Field(
        default="",
        description=(
            "Output format hint. For 'json': a JSON Schema. "
            "For 'file': the file extension. For 'image': 'png', 'svg', etc."
        ),
    )
    description: str = Field(
        default="",
        description="Human-readable description of the output.",
    )
    schema_def: dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        description="Optional JSON Schema for the expected output structure.",
    )


class HookConditionConfig(BaseModel):
    """Condition configuration for an internal hook.

    Built-in conditions:
    - ``context_pressure``: fires when token usage exceeds threshold
    - ``turn_count``: fires at a specific turn number or every N turns
    - ``tool_calls``: fires when tool call count exceeds threshold
    - ``message_count``: fires when message count exceeds threshold
    - ``always``: fires every time (useful with cooldown)

    Example::

        condition:
          type: context_pressure
          threshold: 0.75
          max_tokens: 128000
    """

    type: str = Field(..., description="Condition type (registered name).")
    model_config = {"extra": "allow"}


class HookActionConfig(BaseModel):
    """Action configuration for an internal hook.

    Built-in actions:
    - ``compact_context``: intelligently compact message history
    - ``inject_message``: inject a message into the conversation
    - ``module_action``: call any module action
    - ``log``: log a message (debugging)

    Example::

        action:
          type: compact_context
          strategy: summarize
          keep_last: 10
    """

    type: str = Field(..., description="Action type (registered name).")
    model_config = {"extra": "allow"}


_HOOK_EVENTS: frozenset[str] = frozenset({
    "turn_start", "turn_end",
    "tool_start", "tool_end",
    "pre_tool_use", "post_tool_use",
    "user_prompt",
    "session_start", "session_end",
    "pre_compact",
    "error",
    "approval_request",
    "agent_spawn", "agent_complete",
    "activation",
})


class HookConfig(BaseModel):
    """An internal hook: condition → action, evaluated during the agent loop.

    Example::

        hooks:
          - id: context_compaction
            "on": turn_end
            condition:
              type: context_pressure
              threshold: 0.75
            action:
              type: compact_context
              strategy: summarize
              keep_last: 10
            cooldown: 30

    IMPORTANT: YAML 1.1 parses unquoted ``on`` as boolean ``True``.
    Always quote it: ``"on": tool_end``. This schema rejects any
    non-string value on that field.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Unique hook identifier.")
    on: str = Field(
        default="turn_end",
        description=(
            "When to evaluate. One of: "
            + ", ".join(sorted(_HOOK_EVENTS))
            + ". MUST be quoted in YAML ('on' is a YAML 1.1 boolean keyword)."
        ),
    )

    @field_validator("on", mode="before")
    @classmethod
    def _validate_on(cls, v: Any) -> str:
        if isinstance(v, bool):
            raise ValueError(
                "Hook 'on' field was parsed as boolean. Likely cause: "
                "you wrote `on: tool_end` without quoting `on`. YAML 1.1 "
                "parses unquoted `on` as True. Use `\"on\": tool_end` instead."
            )
        if not isinstance(v, str):
            raise ValueError(f"Hook 'on' must be a string, got {type(v).__name__}")
        if v not in _HOOK_EVENTS:
            import difflib
            suggestions = difflib.get_close_matches(v, _HOOK_EVENTS, n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(
                f"Hook event '{v}' is unknown. Valid: {sorted(_HOOK_EVENTS)}.{hint}"
            )
        return v
    condition: HookConditionConfig = Field(
        ..., description="Condition that must be true for the hook to fire.",
    )
    action: HookActionConfig = Field(
        ..., description="Action to execute when the condition is met.",
    )
    cooldown: float = Field(
        default=0.0,
        description="Minimum seconds between fires (0 = no cooldown).",
    )
    max_fires: int = Field(
        default=0,
        ge=0,
        description=(
            "Max times this hook can fire per app lifetime. 0 = unlimited. "
            "Useful for one-shot setup hooks or for bounding runaway triggers."
        ),
    )
    priority: int = Field(
        default=100,
        description=(
            "Evaluation order among hooks on the same event. Lower runs first. "
            "Same priority → YAML order is preserved. Default 100."
        ),
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Feature flag. When False the hook is loaded but never fires - "
            "lets apps A/B gate new behavior without YAML surgery."
        ),
    )
    timeout: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Max seconds the hook action is allowed to run before being "
            "cancelled. Protects the event loop from a runaway hook "
            "(catastrophic regex, infinite loop in transform_params, "
            "stuck shell, …). Default 30s - enough for compaction. Lower "
            "for cheap hooks (log, notify, gate) so a misconfig can't "
            "stall the loop. Cancellation surfaces as an ``error`` "
            "event for the hook, the turn keeps going."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form tags for grouping / querying hooks. Not used by the "
            "runtime - surfaced in /api/apps/{id}/hooks for introspection."
        ),
    )


class TriggerConfig(BaseModel):
    """A trigger for background mode.

    Example::

        triggers:
          - id: new_csv
            type: watch
            paths: ["./inbox/*.csv"]
            message: "New file: {{event.path}}"
    """

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Unique trigger identifier.")
    type: str = Field(
        ...,
        description="Trigger type: 'cron', 'watch', 'http'.",
    )
    schedule: str = Field(default="", description="Cron expression (cron type only).")
    paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns to watch (watch type only).",
    )
    path: str = Field(default="", description="HTTP endpoint path (http type only).")
    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"] = Field(
        default="POST", description="HTTP method (http type only).",
    )
    port: int = Field(default=9100, ge=1024, le=65535, description="Port for HTTP trigger listener (default 9100).")
    message: str = Field(
        default="",
        description="Message template sent to the agent. Supports {{event.*}}.",
    )
    routing: str = Field(
        default="broadcast",
        description=(
            "How this trigger routes to sessions: "
            "'broadcast' (all active sessions), "
            "'user' (all sessions of the identified user), "
            "'session' (one specific session)."
        ),
    )
    routing_key: str = Field(
        default="",
        description=(
            "Template to extract the routing identifier from the event payload. "
            "For routing='user': identifies which user (e.g. '{{event.chat_id}}'). "
            "For routing='session': identifies which session (e.g. '{{event.header.X-Session-Id}}')."
        ),
    )


class SandboxConfig(BaseModel):
    """OS-level sandbox configuration for per-session isolation.

    Levels (presets):
        - off: no sandbox (current non-sandbox path)
        - standard: Landlock + seccomp + cgroups (single worker)
        - strict: + warm pool + user/PID namespaces + capability drop + MDWE
        - maximum: + network namespace + seccomp-notify audit + workspace snapshot

    Example::

        execution:
          sandbox:
            level: strict
            pool_size: 4
            namespaces: [user, pid, net]
    """

    model_config = {"extra": "forbid"}

    level: Literal["off", "standard", "strict", "maximum"] = Field(
        default="standard",
        description="Sandbox level preset: 'off', 'standard', 'strict', or 'maximum'.",
    )
    pool_size: int = Field(
        default=2,
        ge=1,
        le=32,
        description="Number of pre-warmed workers in the pool.",
    )
    pool_max: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Maximum workers under load (pool_size ≤ pool_max).",
    )
    namespaces: list[str] = Field(
        default_factory=list,
        description="Linux namespaces to create: 'user', 'pid', 'net', 'mount'.",
    )
    workspace_snapshot: bool = Field(
        default=False,
        description="Enable CoW workspace snapshots per session.",
    )
    audit: bool = Field(
        default=False,
        description="Enable per-session audit trail (security event log).",
    )
    session_timeout: int = Field(
        default=3600,
        ge=60,
        description="Maximum session duration in seconds before auto-termination.",
    )
    idle_timeout: int = Field(
        default=300,
        ge=30,
        description="Idle timeout in seconds before worker recycling.",
    )
    allow_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Additional filesystem paths the sandbox may access, beyond the workspace. "
            "Each entry is 'path' (read-only) or 'path:rw' (read-write). "
            "Supports {{variables}} and ~ for home directory. "
            "Example: ['/data/models', '~/datasets:rw', '/etc/myapp']"
        ),
    )
    resources: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-worker resource limits. "
            "Keys: 'memory' (e.g. '512MB'), 'cpu' (cores), 'processes' (max PIDs)."
        ),
    )


class PayloadFieldConfig(BaseModel):
    """One declared field on a background app's session payload metadata.

    The list of these is what the Flutter dashboard uses to render a
    typed form for the user instead of a generic key/value editor.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(
        ...,
        description="Internal key used in payload.metadata. Must be a valid identifier.",
    )
    label: str = Field(
        default="",
        description="Human-friendly label shown in the form. Defaults to ``name``.",
    )
    type: Literal["string", "number", "integer", "boolean", "select", "text"] = Field(
        default="string",
        description=(
            "Form field type. ``text`` = multiline string. ``select`` requires "
            "``options`` to be set."
        ),
    )
    required: bool = Field(
        default=False,
        description="Whether this metadata field must be set before activation.",
    )
    default: Any = Field(
        default=None,
        description="Default value pre-filled in the form.",
    )
    description: str = Field(
        default="",
        description="Help text shown under the field.",
    )
    placeholder: str = Field(
        default="",
        description="Input placeholder.",
    )
    options: list[str] = Field(
        default_factory=list,
        description="Allowed values for ``type: select``.",
    )
    min: float | None = Field(
        default=None,
        description="Min value for number/integer fields.",
    )
    max: float | None = Field(
        default=None,
        description="Max value for number/integer fields.",
    )


class PayloadFileRuleConfig(BaseModel):
    """Constraint on the files a user can attach to a session payload."""

    model_config = {"extra": "forbid"}

    name: str = Field(
        ...,
        description=(
            "Logical slot name (e.g. ``cv``, ``cover_letter``). Free-form. "
            "When ``required: true``, the user must upload at least one file "
            "matching ``mime`` for this slot."
        ),
    )
    label: str = Field(default="", description="Human-friendly label.")
    description: str = Field(default="", description="Help text shown next to the upload zone.")
    required: bool = Field(default=False, description="Whether at least one matching file is mandatory.")
    mime: list[str] = Field(
        default_factory=list,
        description=(
            "Accepted MIME types (e.g. ``['application/pdf']``). Empty = any. "
            "Wildcards like ``image/*`` are supported."
        ),
    )
    max_size_mb: float = Field(
        default=25.0,
        gt=0,
        description="Per-file size cap in MB (server hard cap is 25 MB).",
    )
    max_count: int = Field(
        default=1,
        ge=1,
        description="Max number of files for this slot.",
    )


class CredentialFieldConfig(BaseModel):
    """One field inside a credential provider (e.g. ``api_key``, ``bot_token``).

    Directly mapped to the form widget the Flutter client renders.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(..., description="Internal field name (identifier).")
    label: str = Field(default="", description="Human label shown in the form.")
    type: Literal[
        "secret", "string", "url", "select", "number", "boolean",
        "connection_string",
    ] = Field(
        default="secret",
        description=(
            "Form widget type. ``secret`` = masked password field, "
            "``url`` = URL input with validation, ``select`` requires "
            "``options``, ``connection_string`` = URL with scheme/host check."
        ),
    )
    required: bool = Field(default=False)
    default: Any = Field(default=None, description="Pre-filled default value.")
    description: str = Field(default="", description="Help text.")
    placeholder: str = Field(default="", description="Input placeholder.")
    validation_regex: str = Field(
        default="",
        description=(
            "Optional regex the value must match. Validated both server-side "
            "(handler) and client-side (form)."
        ),
    )
    options: list[str] = Field(
        default_factory=list,
        description="Allowed values for ``type: select``.",
    )
    help: str = Field(
        default="",
        description="Extra inline help shown below the input.",
    )


class CredentialProviderConfig(BaseModel):
    """One provider entry inside ``credentials_schema.providers``.

    Each provider declares which fields are needed, which handler
    should process them (``type``), and which scope rules apply
    (``per_user`` / ``per_app_shared`` / ``system_wide``).
    """

    model_config = {"extra": "forbid"}

    name: str = Field(
        ...,
        description=(
            "Internal provider id. Used as the path segment in "
            "``/credentials/{app_id}/{provider_name}`` routes."
        ),
    )
    label: str = Field(default="", description="Human label for the UI.")
    type: Literal[
        "api_key", "multi_field", "oauth2", "connection_string",
        "mcp_server", "custom",
    ] = Field(
        default="api_key",
        description=(
            "Handler type. Determines the form widget, validation rules, "
            "and lifecycle behaviour."
        ),
    )
    scope: Literal[
        "per_user", "per_app_shared", "system_wide",
    ] = Field(
        default="per_user",
        description=(
            "Where the credential lives: ``per_user`` means each user has "
            "their own (default), ``per_app_shared`` means one credential "
            "for all users of this app, ``system_wide`` means daemon-level "
            "config (admin only)."
        ),
    )
    required: bool = Field(
        default=True,
        description="Whether the app refuses to run without this provider filled.",
    )
    icon: str = Field(default="", description="Logo URL shown in the form.")
    docs_url: str = Field(
        default="", description="Link to the provider's docs / 'where do I get this?'",
    )
    fields: list[CredentialFieldConfig] = Field(
        default_factory=list,
        description="Fields the user must fill.",
    )

    # ── OAuth specific ──
    oauth_provider: str = Field(
        default="",
        description=(
            "For ``type: oauth2``: the key of the OAuth provider registered "
            "on the daemon (notion, google, github, slack). The daemon's "
            "client_id / client_secret for this provider must be configured "
            "by the admin."
        ),
    )
    oauth_scopes: list[str] = Field(
        default_factory=list,
        description="OAuth scopes to request during the flow.",
    )

    # ── MCP server specific ──
    transport: Literal["stdio", "http", "ws", ""] = Field(
        default="",
        description="For ``type: mcp_server``: stdio / http / ws.",
    )
    command: list[str] = Field(
        default_factory=list,
        description="For stdio MCP servers: command + args to spawn.",
    )
    url: str = Field(
        default="",
        description="For http/ws MCP servers: the server URL.",
    )
    env_template: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "For MCP servers: extra env vars to inject into the spawned "
            "process. Supports ``{{field.X}}`` substitution pulling from "
            "the filled credential fields."
        ),
    )
    health_check: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "For MCP servers: how to check the server is alive. "
            "e.g. ``{method: tools/list, timeout_s: 5}``."
        ),
    )

    # ── Live test (optional for api_key/connection_string) ──
    test: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional live-connection test declaration. For api_key: "
            "``{method, url, auth_header, expected_status}``. For "
            "connection_string: ``{test_query}``."
        ),
    )


class CredentialsSchemaConfig(BaseModel):
    """Declarative credentials schema for a Digitorn app.

    When set, the Flutter client fetches this from
    ``GET /api/apps/{id}/credentials/schema`` and renders a typed
    form for each provider. The daemon's resolver also uses it to
    know what's expected so it can fail with a clean "credential
    missing" error rather than a cryptic compile-time secret miss.

    Example::

        credentials_schema:
          required: true
          providers:
            - name: openai
              label: OpenAI
              type: api_key
              scope: per_user
              fields:
                - name: api_key
                  type: secret
                  required: true
                  validation_regex: "^sk-[A-Za-z0-9_-]{20,}$"
            - name: notion
              type: oauth2
              oauth_provider: notion
              scope: per_user
              oauth_scopes: [read_content, update_content]
            - name: notion_mcp
              type: mcp_server
              transport: stdio
              command: ["npx", "-y", "@modelcontextprotocol/server-notion"]
              env_template:
                NOTION_API_KEY: "{{field.api_key}}"
              fields:
                - name: api_key
                  type: secret
                  required: true
    """

    model_config = {"extra": "forbid"}

    required: bool = Field(
        default=True,
        description=(
            "If true, the daemon blocks activation when any required "
            "provider is not filled for the user."
        ),
    )
    providers: list[CredentialProviderConfig] = Field(
        default_factory=list,
        description="Declared credential providers.",
    )


class PayloadSchemaConfig(BaseModel):
    """Declarative description of the user-pre-filled session payload.

    When set on a background app, the Flutter dashboard renders a typed
    form (instead of the generic key/value editor) and the daemon can
    enforce validation before letting the cron fire on an empty
    session. See ``ExecutionConfig.payload_schema``.

    Example::

        execution:
          mode: background
          payload_schema:
            required: true
            prompt:
              required: true
              label: "What should I look for?"
              placeholder: "Find me remote Python jobs paying 80k+"
              min_length: 20
            metadata:
              - name: location
                type: string
                required: true
                label: "City"
              - name: min_salary
                type: integer
                min: 0
                default: 60000
              - name: remote_only
                type: boolean
                default: true
            files:
              - name: cv
                label: "Your CV"
                required: true
                mime: [application/pdf]
                max_size_mb: 5
              - name: portfolio
                required: false
                mime: [application/pdf, image/*]
                max_count: 5
    """

    model_config = {"extra": "forbid"}

    required: bool = Field(
        default=False,
        description=(
            "If true, the daemon refuses to fire triggers for a session "
            "whose payload doesn't satisfy the schema (missing required "
            "prompt / metadata field / file). The dashboard also blocks "
            "the 'Activate session' button until the user fills it in."
        ),
    )
    prompt: dict[str, Any] = Field(
        default_factory=lambda: {"required": False},
        description=(
            "Prompt field config. Recognised keys: ``required`` (bool), "
            "``label`` (str), ``placeholder`` (str), ``description`` (str), "
            "``default`` (str), ``min_length`` (int), ``max_length`` (int)."
        ),
    )
    metadata: list[PayloadFieldConfig] = Field(
        default_factory=list,
        description="Typed metadata fields the user fills in via a form.",
    )
    files: list[PayloadFileRuleConfig] = Field(
        default_factory=list,
        description="File slots with mime/size/count constraints.",
    )


class ExecutionConfig(BaseModel):
    """Execution mode and runtime parameters.

    Example::

        execution:
          mode: conversation
          entry_agent: coordinator
          max_turns: 50
          timeout: 300
          input:
            type: text
            required: true
          output:
            type: json
    """

    model_config = {"extra": "forbid"}

    mode: Literal["one_shot", "conversation", "background", "pipeline"] = Field(
        default="conversation",
        description="Execution mode: 'conversation' (default, multi-turn chat), 'one_shot' (single input then stop), 'background' (trigger-driven), 'pipeline' (multi-app sequencing).",
    )
    entry_agent: str = Field(
        default="",
        description="Agent to start with. Default: first agent in list.",
    )
    max_turns: int = Field(
        default=50,
        ge=1,
        description="Maximum agent loop iterations (per turn for conversation, per activation for background). Must be >= 1.",
    )
    timeout: float = Field(
        default=300.0,
        gt=0,
        description="Timeout in seconds (per turn for conversation, per activation for background). Must be > 0.",
    )

    input: InputConfig = Field(
        default_factory=InputConfig,
        description="Input contract (one_shot mode).",
    )
    output: OutputConfig = Field(
        default_factory=OutputConfig,
        description="Output contract (one_shot mode).",
    )

    greeting: str = Field(
        default="",
        description="Welcome message displayed at conversation start.",
    )

    triggers: list[TriggerConfig] = Field(
        default_factory=list,
        description="Triggers for background mode.",
    )
    session_mode: Literal["mono", "multi"] = Field(
        default="mono",
        description=(
            "Background session mode: "
            "'mono' (1 session per user, auto-created) or "
            "'multi' (N sessions per user, created via API with custom params)."
        ),
    )
    max_sessions_per_user: int = Field(
        default=10,
        ge=0,
        description=(
            "Max background sessions per user in multi mode (0 = unlimited). "
            "Ignored in mono mode."
        ),
    )
    max_concurrent_activations: int = Field(
        default=20,
        ge=1,
        description=(
            "Max concurrent LLM calls when a broadcast trigger fires. "
            "Prevents rate limit storms when thousands of sessions exist. "
            "Activations beyond this limit are queued and processed in order."
        ),
    )

    credentials_schema: CredentialsSchemaConfig | None = Field(
        default=None,
        description=(
            "Optional declarative credentials schema. Declares every "
            "external service (OpenAI API, Notion OAuth, Slack bot, "
            "Postgres DB, MCP servers, …) the app needs to run. The "
            "daemon exposes this to the Flutter client which renders a "
            "typed form, and blocks activations until all required "
            "providers are filled for the current user."
        ),
    )

    payload_schema: PayloadSchemaConfig | None = Field(
        default=None,
        description=(
            "Optional declarative schema for the per-session user payload "
            "(prompt + typed metadata + file slots). When set, the Flutter "
            "dashboard renders a typed form and the daemon validates the "
            "payload before firing triggers. Only meaningful in "
            "``mode: background``."
        ),
    )

    workspace: str = Field(
        default="",
        description=(
            "Working directory for the app. Defaults to the current directory. "
            "Auto-indexed at startup for faster file search. "
            "Supports {{variables}} and {{env.PWD}}."
        ),
    )

    workspace_mode: Literal["none", "required", "fixed", "auto"] = Field(
        default="auto",
        description=(
            "How workspace is handled: "
            "'none' = no workspace (chatbot, Q&A). "
            "'required' = user must select a workspace before chatting. "
            "'fixed' = use the workspace path from YAML, no override allowed. "
            "'auto' = use YAML workspace if set, allow override per session."
        ),
    )

    sandbox: "SandboxConfig | None" = Field(
        default=None,
        description=(
            "OS-level sandbox configuration for per-session isolation. "
            "When set, workers run in kernel-enforced sandboxes with Landlock, "
            "seccomp, namespaces, and process hardening. "
            "Use 'level' presets for quick configuration, or fine-tune individual settings."
        ),
    )

    project_memory: str = Field(
        default="auto",
        description=(
            "Path to a project memory file loaded into the system prompt at startup. "
            "Set to 'auto' to scan for .digitorn.md, CLAUDE.md, or README.md in the workspace. "
            "Set to a specific path (relative to workspace) to load that file. "
            "Set to '' (empty) to disable."
        ),
    )

    direct_modules: list[str] = Field(
        default_factory=list,
        description=(
            "Module IDs whose actions are always injected as direct tools, "
            "even when the system uses discovery mode for other modules. "
            "Use this for fundamental operations the agent should never need to 'discover'. "
            "Example: ['filesystem', 'git'] ensures read/edit/status are always one call away."
        ),
    )
    tool_injection: Literal["direct", "compact_direct", "discovery"] | None = Field(
        default=None,
        description=(
            "Force a specific tool injection mode: 'direct', 'compact_direct', or 'discovery'. "
            "If not set, the mode is auto-detected based on tool count vs context window. "
            "Use 'discovery' to keep the prompt small with many modules."
        ),
    )

    context: ContextConfig = Field(
        default_factory=ContextConfig,
        description="Context window management configuration.",
    )

    hooks: list[HookConfig] = Field(
        default_factory=list,
        description=(
            "Internal hooks evaluated during the agent loop. "
            "Each hook has a condition and an action. "
            "Works in all execution modes."
        ),
    )

    watchers: bool = Field(
        default=False,
        description=(
            "Enable persistent watcher capabilities. When true, the agent "
            "can start periodic watchers to monitor data sources (APIs, files, "
            "databases, processes) and get notified only when something "
            "interesting happens. Uses smart escalation to minimize token usage."
        ),
    )

    scheduler: bool = Field(
        default=False,
        description=(
            "Enable scheduler capabilities. When true, the agent can schedule "
            "one-shot timers, cron jobs, and reminders. Jobs persist across "
            "daemon restarts. Requires watchers to also be enabled."
        ),
    )

    default_channel: str = Field(
        default="llm_notification",
        description=(
            "Default output channel for scheduled jobs and watchers. "
            "References a channel instance name from the 'channels:' block, "
            "or 'llm_notification' (always available). "
            "Can be overridden per-job via output_channel."
        ),
    )



class UserResolverConfig(BaseModel):
    """Configuration for auto-resolving user-specific delivery targets.

    When a channel delivers a notification, the resolver automatically
    looks up the user's contact info (email, phone, chat_id, etc.) from
    a data source, using the session_id to identify who the user is.

    This works like authentication middleware: the system knows who the
    user is and adapts. One app serves 10,000 users - no per-user
    configuration needed.

    Example::

        user_resolver:
          module: database
          action: fetch_results
          params:
            query: "SELECT phone, email FROM users WHERE session_id = :session_id"
          mapping:
            to_number: phone
            to_address: email
          cache_ttl: 300
    """

    model_config = {"extra": "forbid"}

    module: str = Field(
        ...,
        description=(
            "Module ID to query for user info (e.g. 'database', 'http'). "
            "Must be declared in the app's modules: block."
        ),
    )
    action: str = Field(
        ...,
        description=(
            "Action to call on the module (e.g. 'fetch_results', 'get'). "
            "The action should return user-specific data."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Parameters for the action. Use ':session_id' or '{{session_id}}' "
            "as a placeholder - it will be replaced with the actual session ID "
            "at delivery time."
        ),
    )
    mapping: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps result field names to per-delivery config field names. "
            "e.g. {'to_number': 'phone'} means: take the 'phone' column "
            "from the query result and pass it as the channel's 'to_number'."
        ),
    )
    cache_ttl: float = Field(
        default=300.0,
        ge=0,
        description=(
            "How long to cache resolved results in seconds. "
            "0 = no cache. Default: 300 (5 min)."
        ),
    )


class ChannelInstanceConfig(BaseModel):
    """Configuration for a named output channel instance.

    Each entry in the ``channels:`` block defines a channel instance
    with a user-chosen name, a channel type, and type-specific config.

    Optionally, a ``user_resolver`` auto-resolves per-user delivery targets
    (email, phone, chat_id) from a data source - no manual ``output_config``
    needed.

    Example::

        channels:
          slack_alerts:
            type: webhook
            config:
              url: "{{secret.SLACK_WEBHOOK}}"

          sms_user:
            type: sms
            config:
              account_sid: "{{env.TWILIO_SID}}"
              from_number: "+33600000000"
            user_resolver:
              module: database
              action: fetch_results
              params:
                query: "SELECT phone FROM users WHERE session_id = :session_id"
              mapping:
                to_number: phone
    """

    model_config = {"extra": "forbid"}

    type: str = Field(
        ...,
        description=(
            "Channel type ID. Built-in: 'llm_notification', 'webhook', 'log'. "
            "Plugins: 'slack', 'gmail', 'telegram', 'kafka', 'sms', etc. "
            "(via pip install digitorn-channel-<type>)"
        ),
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Channel-specific configuration. Supports {{variables}} and "
            "{{secret.X}} / {{env.X}} for credentials. "
            "See 'digitorn channel schema <type>' for available fields."
        ),
    )
    user_resolver: UserResolverConfig | None = Field(
        default=None,
        description=(
            "Optional user resolver for auto-targeting notifications. "
            "When set, the channel automatically looks up the user's "
            "delivery address (email, phone, chat_id) from a data source "
            "using the session_id. No manual output_config needed."
        ),
    )


class AgentDefinition(BaseModel):
    """Definition of a single agent in the app YAML.

    Only ``id`` and ``brain`` are required for now.
    Other fields (tools, signals, loop, watch) will be added
    when we implement the full agent runtime.

    Example::

        agents:
          - id: coordinator
            role: coordinator
            brain:
              provider: deepseek
              model: deepseek-chat
              temperature: 0.2
              config:
                api_key: "{{secret.DEEPSEEK_API_KEY}}"
                base_url: "https://api.deepseek.com/v1"
            system_prompt: |
              You are a coordinator agent.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(..., description="Unique agent identifier within this app.")
    role: str = Field(
        default="worker",
        description=(
            "Agent role hint. Functional roles: 'coordinator' (can spawn agents), "
            "'specialist' (pre-configured expert), 'worker' (default). "
            "Descriptive roles like 'assistant', 'analyst', 'reviewer' are also "
            "accepted and used in the system prompt."
        ),
    )
    brain: AgentBrain = Field(..., description="LLM provider configuration for this agent.")
    system_prompt: str = Field(default="", description="System prompt injected at conversation start.")
    plan_first: bool = Field(
        default=True,
        description=(
            "When true, the agent must explain its plan in plain text before "
            "executing any tools on the first turn. Prevents silent tool calls."
        ),
    )
    specialty: str = Field(
        default="",
        description="Short description of this specialist's expertise (shown to coordinator).",
    )
    delegate_to: list[str] = Field(
        default_factory=list,
        description=(
            "Agent IDs this coordinator can delegate to. The compiler verifies "
            "each entry references a declared agent id."
        ),
    )
    skills: str = Field(
        default="",
        description="Path to a .md file with detailed methodology/instructions for this specialist.",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "List of skill names to auto-load from the bundle's "
            "``skills/`` directory. The compiler reads "
            "``skills/<name>.md`` for each entry and appends the "
            "content to this agent's ``system_prompt`` under an "
            "``## Available capabilities`` section. Clean way to "
            "separate the agent's identity (system_prompt) from "
            "its skill definitions (individual markdown files)."
        ),
    )
    modules: list["str | dict[str, list[str]]"] = Field(
        default_factory=list,
        description=(
            "Modules this specialist can access. Empty = same as coordinator.\n"
            "Supports two formats (mix is OK):\n"
            "  - Simple: ['filesystem', 'shell'] - full module access\n"
            "  - Granular: [{filesystem: [read, grep]}, 'shell'] - restrict actions"
        ),
    )

    @field_validator("modules", mode="before")
    @classmethod
    def _validate_modules_shape(cls, v: Any) -> list:
        # Each entry is either a plain string (module id) or a single-key
        # dict mapping module id to a list of action names. Anything else
        # is a malformed entry the user almost certainly wrote by mistake.
        if not isinstance(v, list):
            raise ValueError("agent.modules must be a list, got " + type(v).__name__)
        for i, entry in enumerate(v):
            if isinstance(entry, str):
                continue
            if isinstance(entry, dict):
                if len(entry) != 1:
                    raise ValueError(
                        f"agent.modules[{i}]: granular entries must have exactly "
                        f"one module key. Got {sorted(entry.keys())}. Use multiple "
                        f"list entries for multiple modules."
                    )
                actions = next(iter(entry.values()))
                if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
                    raise ValueError(
                        f"agent.modules[{i}]: granular value must be a list of "
                        f"action-name strings. Got {actions!r}."
                    )
                continue
            raise ValueError(
                f"agent.modules[{i}]: must be a string (module id) or a "
                f"single-key dict {{module_id: [actions]}}, got "
                f"{type(entry).__name__}: {entry!r}"
            )
        return v
    pool: AgentPoolConfig = Field(
        default_factory=AgentPoolConfig,
        description="Agent pool config for coordinators (max_workers, progress, auto_retry).",
    )
    # ── Phase 9 grouped sub-blocks ─────────────────────────────
    # `coordination:` and `instructions:` collect the historical
    # scatter of delegate_to/pool/specialty/skills/capabilities.
    # When set, they are aliased into the legacy fields at compile
    # so downstream code (compiler, runtime, canvas) keeps working.
    coordination: CoordinationBlock | None = Field(
        default=None,
        description=(
            "Grouped orchestration block: delegate_to + pool. "
            "Replaces the historical scatter."
        ),
    )
    instructions: InstructionsBlock | None = Field(
        default=None,
        description=(
            "Grouped prompt-extension block: file + capabilities + "
            "specialty. Replaces the historical scatter."
        ),
    )
    hooks: list[HookConfig] = Field(
        default_factory=list,
        description=(
            "Per-agent hooks - merged with ``execution.hooks`` but only "
            "evaluated when this specific agent is active. Use for "
            "specialist-specific behavior (e.g. a `reviewer` agent that "
            "runs extra lint, a `writer` agent that logs every edit). "
            "App-wide hooks still fire for every agent; these add on top."
        ),
    )


class ModeDef(BaseModel):
    """Per-mode runtime configuration.

    The chat composer surfaces a "mode picker" pill (Ask / Plan / Auto / …)
    backed by `runtime.modes`. Each entry is a *sparse* override:
    only fields the user sets are applied on top of the app's normal
    runtime / agent / tool config when that mode is the active one.
    Empty fields fall back to the app's defaults.

    Conceptually a mode lets one app behave like several:
      - tighten / loosen the agent's autonomy (`max_turns`, `timeout`)
      - swap or amend the system prompt (`system_prompt`)
      - gate which tools the agent can reach (`tool_grants`)
      - flip the behavior engine profile (`behavior_profile`)

    Picker UX: the composer hides the picker entirely when only a
    single mode is declared (no choice = no menu). Apps that want the
    picker must declare at least two entries.
    """

    model_config = {"extra": "forbid"}

    # ── Picker affordance ────────────────────────────────────────
    label: str = Field(
        default="",
        description=(
            "Display name shown in the picker pill + the dropdown row. "
            "Defaults to the mode id capitalised when empty."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "One-line subtitle shown under the label in the picker "
            "dropdown. Keep it short (≤ 30 chars) so the panel stays "
            "narrow."
        ),
    )
    icon: str = Field(
        default="",
        description=(
            "Optional icon hint for the picker. Known: `lightbulb`, "
            "`map`, `sparkles`, `wrench`, `shield`. When empty the "
            "client picks a sensible default per mode id."
        ),
    )
    accent: str = Field(
        default="",
        description=(
            "Optional accent colour for the pill border + dropdown "
            "row tint. Known: `primary`, `secondary`, `cyan`, "
            "`purple`, `red`, `green`, `orange`. Empty falls back to "
            "the theme accent."
        ),
    )

    # ── Runtime overrides (sparse, optional) ─────────────────────
    max_turns: int | None = Field(
        default=None, ge=1,
        description=(
            "Override `runtime.max_turns` while this mode is active. "
            "Use 1 for a strict one-shot mode."
        ),
    )
    timeout: float | None = Field(
        default=None, gt=0,
        description="Override `runtime.timeout` (seconds) for this mode.",
    )
    workspace_mode: str | None = Field(
        default=None,
        description=(
            "Override `ui.workspace.mode` for this mode. e.g. `none` "
            "for an Ask mode that hides the workspace panel."
        ),
    )

    # ── Agent context overrides ──────────────────────────────────
    system_prompt: str = Field(
        default="",
        description=(
            "Suffix appended to the agent's existing system prompt "
            "when this mode is active. Use to nudge the agent towards "
            "the mode's intent (e.g. \"Reply concisely, no tools.\")."
        ),
    )

    # ── Tool gating ──────────────────────────────────────────────
    tool_grants: list[CapabilityGrant] = Field(
        default_factory=list,
        description=(
            "Subset of the app's tools the agent may reach in this "
            "mode. Same shape as `tools.grant`. Empty = inherit "
            "everything from the app's normal grant list."
        ),
    )

    # ── Behavior engine ──────────────────────────────────────────
    behavior_profile: str = Field(
        default="",
        description=(
            "Override the behavior module profile while this mode is "
            "active (e.g. `assistant` for Ask, `coding` for Auto). "
            "Empty inherits the app's normal `security.behavior.profile`."
        ),
    )


class RuntimeBlock(BaseModel):
    """Lifecycle + execution policy: how the app actually runs.

    Holds every field that controls the daemon's per-turn behaviour:
    mode, max_turns, triggers, hooks, sandbox/security gates, context
    window, and the orchestration extensions (middleware, pipeline,
    flow).

    Fields formerly named under ``execution:`` keep their semantics
    here unchanged. Two renames for unambiguity:

    -  ``execution.workspace``      ->  ``runtime.workdir``
    -  ``execution.workspace_mode`` ->  ``runtime.workdir_mode``

    The renames disambiguate from ``ui.workspace`` (the Flutter
    renderer block) which is a completely different concept.
    """

    model_config = {"extra": "forbid"}

    mode: Literal["one_shot", "conversation", "background", "pipeline"] = Field(
        default="conversation",
        description="Execution mode: 'conversation' (default, multi-turn chat), 'one_shot' (single input then stop), 'background' (trigger-driven), 'pipeline' (multi-app sequencing).",
    )
    entry_agent: str = Field(
        default="",
        description="Agent to start with. Default: first agent in list.",
    )
    max_turns: int = Field(
        default=50, ge=1,
        description="Maximum agent loop iterations (per turn for conversation, per activation for background). Must be >= 1.",
    )
    timeout: float = Field(
        default=300.0, gt=0,
        description="Timeout in seconds (per turn for conversation, per activation for background). Must be > 0.",
    )

    modes: dict[str, ModeDef] = Field(
        default_factory=dict,
        description=(
            "Composer mode picker — keyed by mode id (`ask`, `plan`, "
            "`auto`, or anything app-specific). Each value is a "
            "sparse `ModeDef` that overrides the app's runtime / "
            "agent / tools / behavior config when the user picks "
            "that mode in the chat composer. Empty dict = no picker "
            "(client hides the pill). One entry = no picker either "
            "(no choice). Two or more = pill shown with the listed "
            "entries; the chat-store's `selectedMode` indexes into "
            "this dict at dispatch time."
        ),
    )

    input: InputConfig = Field(
        default_factory=InputConfig,
        description="Input contract (one_shot mode).",
    )
    output: OutputConfig = Field(
        default_factory=OutputConfig,
        description="Output contract (one_shot mode).",
    )

    triggers: list[TriggerConfig] = Field(
        default_factory=list,
        description="Triggers for background mode.",
    )
    session_mode: Literal["mono", "multi"] = Field(
        default="mono",
        description=(
            "Background session mode: 'mono' (1 session per user, "
            "auto-created) or 'multi' (N sessions per user, created via API "
            "with custom params)."
        ),
    )
    max_sessions_per_user: int = Field(
        default=10, ge=0,
        description="Max background sessions per user in multi mode (0 = unlimited). Ignored in mono mode.",
    )
    max_concurrent_activations: int = Field(
        default=20, ge=1,
        description="Max concurrent LLM calls when a broadcast trigger fires. Prevents rate limit storms.",
    )

    payload_schema: PayloadSchemaConfig | None = Field(
        default=None,
        description="Optional declarative schema for the per-session user payload. Only meaningful in mode=background.",
    )

    workdir: str = Field(
        default="",
        description=(
            "Working directory (formerly ``execution.workspace``). The "
            "filesystem path the app's modules operate within. Auto-indexed "
            "at startup. Supports {{variables}} and {{env.PWD}}. Renamed "
            "from `workspace` to disambiguate from `ui.workspace` (renderer)."
        ),
    )
    workdir_mode: Literal["none", "required", "fixed", "auto"] = Field(
        default="auto",
        description=(
            "Working-directory mode (formerly ``execution.workspace_mode``):"
            " 'none', 'required', 'fixed', or 'auto'."
        ),
    )

    project_memory: str = Field(
        default="auto",
        description="Path to a project memory file loaded into the system prompt at startup. 'auto' scans for .digitorn.md / CLAUDE.md / README.md.",
    )

    direct_modules: list[str] = Field(
        default_factory=list,
        description="Module IDs whose actions are always injected as direct tools.",
    )
    tool_injection: Literal["direct", "compact_direct", "discovery"] | None = Field(
        default=None,
        description="Force a specific tool injection mode. Auto-detected when None.",
    )

    context: ContextConfig = Field(
        default_factory=ContextConfig,
        description="Context window management configuration.",
    )

    hooks: list[HookConfig] = Field(
        default_factory=list,
        description="Internal hooks evaluated during the agent loop.",
    )

    watchers: bool = Field(
        default=False,
        description="Enable persistent watcher capabilities.",
    )
    scheduler: bool = Field(
        default=False,
        description="Enable scheduler capabilities. Requires watchers=true.",
    )

    default_channel: str = Field(
        default="llm_notification",
        description="Default output channel for scheduled jobs and watchers.",
    )

    middleware: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "App-level middleware pipeline that wraps every LLM call. "
            "Built-in: mask_secrets, prompt_inject, content_filter, "
            "rag_inject, response_filter."
        ),
    )

    pipeline: list["PipelineStep"] = Field(
        default_factory=list,
        description=(
            "Pipeline of apps executed in sequence (mode=pipeline only). "
            "Each step calls a deployed app and pipes its output to the "
            "next step."
        ),
    )

    # Note: ``flow:`` is now a TOP-LEVEL block (8th canonical block).
    # It changes how agents coordinate (explicit scenography vs
    # implicit ``Agent()`` calls), which is a big enough shift that
    # it gets its own block instead of sitting under ``runtime``.
    # The legacy alias ``runtime.flow`` still works via
    # ``schema_aliases`` for backward compat.


class ToolsBlock(BaseModel):
    """What the agent can call: modules, capabilities (grant/deny), output channels.

    Modules expose actions; capabilities filter which actions are
    callable (auto / approve / deny); channels are the typed output
    surfaces (slack, email, webhook, ...) that triggers and the
    scheduler can deliver to.
    """

    model_config = {"extra": "forbid"}

    modules: dict[str, ModuleBlock] = Field(
        default_factory=dict,
        description="Per-module configuration. Keys are module IDs.",
    )
    capabilities: CapabilitiesConfig | None = Field(
        default=None,
        description="Application security capabilities (grant/deny). Absent = dev/test mode (no enforcement).",
    )
    channels: dict[str, ChannelInstanceConfig] = Field(
        default_factory=dict,
        description="Named output channel instances. Keys are instance names (e.g. 'slack_alerts', 'email_reports').",
    )


class SecurityBlock(BaseModel):
    """Runtime security boundaries: behavioral rules, OS sandbox, secret vault.

    `behavior` describes per-tool checks the daemon enforces at
    runtime. `sandbox` is the OS-level isolation layer (Landlock /
    seccomp / namespaces). `credentials_schema` declares every external
    secret the app needs.
    """

    model_config = {"extra": "forbid"}

    behavior: BehaviorConfig | None = Field(
        default=None,
        description=(
            "Behavioral enforcement rules (preset profile + custom). "
            "Actively monitored at runtime."
        ),
    )
    sandbox: "SandboxConfig | None" = Field(
        default=None,
        description=(
            "OS-level sandbox configuration (Landlock + seccomp + "
            "namespaces). Use 'level' presets or fine-tune."
        ),
    )
    credentials_schema: CredentialsSchemaConfig | None = Field(
        default=None,
        description=(
            "Declarative credentials schema. Declares every external "
            "service (OpenAI, Notion OAuth, Slack, MCP servers, ...) the "
            "app needs."
        ),
    )


class UIBlock(BaseModel):
    """How the client renders the app: pure display, daemon never reads.

    Holds the Flutter / web client manifest extensions. Theme, feature
    toggles, declarative widgets, the workspace renderer, slash
    commands, quick prompts, and the welcome greeting.
    """

    model_config = {"extra": "forbid"}

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> "UIBlock":
        # ``ui.preview`` was removed when dev-server lifecycle moved to
        # the LLM-driven ``web_preview`` module. Old YAMLs (user apps
        # deployed before the migration) still ship the block — silently
        # drop it instead of failing the whole compile, since the rest
        # of UIBlock is still valid.
        if isinstance(obj, dict) and "preview" in obj:
            obj = {k: v for k, v in obj.items() if k != "preview"}
        return super().model_validate(obj, *args, **kwargs)

    theme: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Client theme override map. Keys: accent (hex), background "
            "(hex, client-reserved)."
        ),
    )
    features: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "UI feature toggles consumed by the Flutter client. Missing "
            "keys default to true (feature visible)."
        ),
    )
    widgets: "WidgetsConfig | None" = Field(
        default=None,
        description="Declarative UI widgets rendered by the Flutter client.",
    )
    workspace: "WorkspaceBlock | None" = Field(
        default=None,
        description=(
            "Workspace renderer config: render_mode + entry_file + title. "
            "Tells the client THIS app uses a virtual file workspace "
            "streamed via Socket.IO. Distinct from runtime.workdir (the "
            "FS path)."
        ),
    )
    slash_commands: list[SlashCommand] = Field(
        default_factory=list,
        description="Custom /slash palette entries rendered by the client.",
    )
    quick_prompts: list[QuickPrompt] = Field(
        default_factory=list,
        description="Suggested prompts shown to the user when the app loads.",
    )
    greeting: str = Field(
        default="",
        description=(
            "Welcome message displayed by the client at conversation start. "
            "Pure display - the daemon never reads it. Lifted from "
            "execution.greeting."
        ),
    )
    # ── Chat layout / behaviour blocks (added 2026-05-04) ──────────
    #
    # All optional. When omitted the client falls back to its
    # default (current behaviour) so existing app YAMLs keep working
    # untouched. Each new sub-block lets the YAML override one
    # specific aspect of the chat panel without redefining the rest.
    #
    # ``layout`` is the high-level preset: it lets the client pick
    # an opinionated combination of the lower-level flags
    # (Lovable-style, builder, research, …) without the YAML having
    # to set ten flags individually. The fine-grained sub-blocks
    # below ALWAYS win over the preset so an author can derive from
    # ``lovable`` and tweak just the bits they need.
    layout: str = Field(
        default="default",
        description=(
            "High-level chat preset that sets sensible defaults for "
            "every other block: default | code | builder | research | "
            "minimal | lovable. Sub-blocks override individual flags."
        ),
    )
    density: str = Field(
        default="comfortable",
        description="Bubble spacing density: compact | comfortable.",
    )
    thinking: ChatThinkingBlock | None = Field(
        default=None,
        description=(
            "Visibility / collapse defaults for ``<thinking>`` blocks "
            "in assistant messages."
        ),
    )
    tool_calls: ChatToolCallsBlock | None = Field(
        default=None,
        description=(
            "Visibility / collapse defaults for tool-call chips. "
            "``show_silent`` controls plumbing-tool rendering."
        ),
    )
    composer: ChatComposerBlock | None = Field(
        default=None,
        description=(
            "Composer toolbar config (file upload, voice, slash, "
            "quick prompts). Wins over the legacy ``features.X`` "
            "boolean keys for the same concepts."
        ),
    )
    visual: ChatVisualBlock | None = Field(
        default=None,
        description=(
            "Bubble accent / style / alignment overrides. ``accent`` "
            "wins over ``theme.accent`` and ``app.color`` when set."
        ),
    )
    slots: SlotsConfig | None = Field(
        default=None,
        description=(
            "Per-app placement of inline widgets in the chat "
            "surface (header, sidebar_left, sidebar_right, "
            "above_composer, footer). Each slot references an "
            "entry from ``ui.widgets.inline`` by name. The "
            "architectural foundation for Phase 2 message actions "
            "and Phase 3 tool renderers."
        ),
    )
    activity: "ActivityPanelBlock | None" = Field(
        default=None,
        description=(
            "Opt-in Activity pane: live sub-agent fan-out + recent "
            "terminal events + aggregate stats. The block being "
            "PRESENT in YAML is the gate — both clients hide the "
            "Activity workspace mode entry when this field is null. "
            "Simple chat apps that never spawn sub-agents leave it "
            "off. Coordinator / multi-agent / dev-assistant apps "
            "opt in."
        ),
    )
    # Phase 3 - YAML-driven custom rendering for tool calls. Lives in
    # ``tool_renderers.py`` so it can be removed without touching this
    # file beyond this single field. Forward-string type means schema.py
    # still imports cleanly if the module is ever deleted.
    tool_renderers: "ToolRenderersBlock | None" = Field(
        default=None,
        description=(
            "Phase-3 dispatcher mapping tool names (or regex "
            "patterns) to inline-widget refs. When the block is "
            "absent or ``enabled: false``, every tool call uses the "
            "client's legacy chip - opt-in only. The widget tree "
            "receives ``{{tool.name}}``, ``{{tool.params.X}}``, "
            "``{{tool.result.X}}``, ``{{tool.error}}``, "
            "``{{tool.duration_ms}}``, ``{{tool.status}}`` bindings."
        ),
    )
    # Phase 2 - per-message custom action rows. Same isolation
    # pattern as tool_renderers; lives in ``message_actions.py``.
    message_actions: "MessageActionsBlock | None" = Field(
        default=None,
        description=(
            "Phase-2 dispatcher mounting a custom widget tree UNDER "
            "each chat message that matches one of the rules. When "
            "the block is absent or ``enabled: false``, no extra "
            "row renders under messages - opt-in only. The widget "
            "tree receives ``{{message.role}}``, ``{{message.id}}``, "
            "``{{message.text}}``, ``{{message.has_tools}}``, "
            "``{{message.tools}}``, ``{{message.first_tool}}``, "
            "``{{message.tool_status}}`` bindings."
        ),
    )


class DevBlock(BaseModel):
    """Developer affordances: skills, variables, fragmentation directives.

    Things authored at design time that don't fit cleanly into the
    other blocks. Skills are /command markdown files. Variables are
    template substitutions. Include is the fragmentation directive.
    """

    model_config = {"extra": "forbid"}

    skills: list[SkillEntry] = Field(
        default_factory=list,
        description="App-level /command skill files the agent can invoke.",
    )
    variables: dict[str, str] = Field(
        default_factory=dict,
        description="Template variables available as {{name}} in params and constraints.",
    )
    include: IncludeBlock | None = Field(
        default=None,
        description=(
            "Optional fragmentation block. Splits agents, hooks, and "
            "other list-shaped sections into separate files."
        ),
    )


class TemplateBlock(BaseModel):
    """A starter template declared by the app.

    The chat client renders these in a native gallery under the
    composer. The user picks one, previews it in an iframe, attaches
    it to their next message; on send the daemon copies the seed
    files into the workspace and injects ``system_prompt`` as a
    one-turn system addendum so the agent knows the directive.

    Both path fields are **relative to the app's install directory**.
    No path traversal is allowed (no leading ``/``, no ``..``).

    Example YAML — typically lives in a ``templates.yaml`` fragment
    next to ``app.yaml`` to keep the main manifest small::

        templates:
          - id: landing-ai-saas
            name: "AI SaaS landing"
            description: "Mesh-gradient hero, features, pricing, FAQ."
            preview_path: "templates/landing-ai-saas/dist/index.html"
            seed_dir: "templates/landing-ai-saas/files/"
            system_prompt: |
              You are working from the AI SaaS landing template.
              Keep the mesh-gradient hero and premium dark feel.
    """

    model_config = {"extra": "forbid"}

    id: str = Field(
        ...,
        description=(
            "Unique slug for this template within the app. Used as a "
            "URL segment and as the ``template_id`` the chat client "
            "sends with the user message. Lowercase letters, digits, "
            "hyphens."
        ),
    )
    name: str = Field(
        ...,
        description="Display name shown in the gallery card.",
    )
    description: str = Field(
        default="",
        description="One-line summary shown under the card title. Optional.",
    )
    preview_path: str = Field(
        ...,
        description=(
            "Path relative to the app's install_dir pointing at a "
            "browser-loadable file (built ``dist/index.html``, .pdf, "
            ".html, etc.). The daemon serves it via "
            "``GET /api/apps/{id}/template-assets/{path}`` and the "
            "chat client loads that URL in the preview iframe."
        ),
    )
    seed_dir: str = Field(
        ...,
        description=(
            "Directory path relative to the app's install_dir holding "
            "the seed files copied into the user's workspace when the "
            "template is applied. May be an empty dir if no seeds "
            "(prompt-only template)."
        ),
    )
    system_prompt: str = Field(
        ...,
        description=(
            "System addendum injected for the turn that applies this "
            "template. Imposes the template's directive on the agent. "
            "One-turn only; subsequent turns proceed with the app's "
            "default system prompt."
        ),
    )

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", v):
            raise ValueError(
                f"Invalid template id '{v}': must start with a-z/0-9 and "
                f"contain only lowercase letters, digits, hyphens."
            )
        return v

    @field_validator("preview_path", "seed_dir")
    @classmethod
    def _validate_relative_path(cls, v: str) -> str:
        # Reject absolute paths + traversal segments. The daemon will
        # also re-check before serving / copying, but failing here makes
        # bad YAML a deploy-time error instead of a runtime surprise.
        if not v:
            raise ValueError("path must not be empty")
        if v.startswith("/") or v.startswith("\\"):
            raise ValueError(f"path '{v}' must be relative (no leading slash)")
        parts = v.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            raise ValueError(f"path '{v}' must not contain '..' segments")
        return v


class AppDefinition(BaseModel):
    """Root model - direct parse target for an app YAML file.

    The schema groups every field into 7 semantic blocks. There is
    exactly ONE canonical place to declare each field: no top-level
    flat aliases, no duplicates between ``app.X`` / ``ui.X``. Legacy
    YAMLs (flat shape) still work via :mod:`schema_aliases`, which
    reshapes them to this canonical form before Pydantic validates.

    Example YAML::

        app:
          app_id: my-agent
          name: "My Agent"

        runtime:
          mode: conversation
          entry_agent: coordinator

        agents:
          - id: coordinator
            role: coordinator
            brain:
              provider: deepseek
              model: deepseek-chat

        tools:
          modules:
            database:
              setup: [...]
          capabilities:
            default_policy: auto
            grant: [...]

        security:
          behavior:
            profile: coding

        ui:
          theme: { accent: "#6EE7B7" }
          features: { voice: false }

        dev:
          variables:
            workspace: "{{env.PWD}}"
    """

    model_config = {"extra": "forbid"}

    schema_version: int = Field(
        default=2,
        description=(
            "Canonical schema version. v2 = 8 nested top-level blocks "
            "(``app``, ``runtime``, ``agents``, ``tools``, ``security``, "
            "``ui``, ``dev``, ``flow``). v1 = legacy flat shape "
            "(``execution:``, ``modules:`` at top level, ...). The alias "
            "pass auto-detects when this field is absent. Setting it "
            "explicitly future-proofs the file against breaking changes."
        ),
    )
    app: AppMeta = Field(..., description="Application identity (id, name, version, icon, color, ...).")
    runtime: RuntimeBlock = Field(
        default_factory=RuntimeBlock,
        description="Lifecycle + execution: mode, triggers, middleware, pipeline, hooks, context.",
    )
    agents: list[AgentDefinition] = Field(
        default_factory=list,
        description="Agent definitions. Each agent has a brain (LLM config) and role.",
    )
    tools: ToolsBlock = Field(
        default_factory=ToolsBlock,
        description="Modules + capabilities + channels. What the agent can call.",
    )
    security: SecurityBlock = Field(
        default_factory=SecurityBlock,
        description="Runtime boundaries: behavior + sandbox + credentials_schema.",
    )
    ui: UIBlock = Field(
        default_factory=UIBlock,
        description="Pure display layer: theme + features + widgets + workspace + preview + slash_commands + quick_prompts + greeting.",
    )
    dev: DevBlock = Field(
        default_factory=DevBlock,
        description="Developer affordances: skills + variables + include (fragmentation).",
    )
    flow: "FlowConfig | None" = Field(
        default=None,
        description=(
            "Optional declarative orchestration graph for multi-agent "
            "apps. A top-level block (NOT under runtime) because flow "
            "changes how agents coordinate: explicit scenography "
            "instead of agents coordinating themselves via Agent() "
            "tool calls. Nodes are agent / tool / parallel / approval "
            "/ decision / terminal; routes are conditional edges."
        ),
    )
    templates: list[TemplateBlock] = Field(
        default_factory=list,
        description=(
            "Starter templates the chat client renders as a native "
            "gallery below the composer. Typically loaded from a "
            "``templates.yaml`` fragment beside ``app.yaml`` so the "
            "main manifest stays readable. Each entry points at a "
            "pre-built preview asset and a seed directory; on send "
            "the daemon copies seeds into the workspace and injects "
            "the template's ``system_prompt`` for that turn."
        ),
    )


class ActivityPanelBlock(BaseModel):
    """``ui.activity:`` block — opt-in observability pane for sub-agents.

    Surfaces the live sub-agent fan-out, background tasks, and recent
    terminal events as a dedicated workspace mode (web) / shell pane
    (Flutter). Pure display: the daemon never inspects this; both
    clients consume the field through the manifest summary and gate
    the Activity mode entry on its presence.

    **Opt-in contract**: when this block is omitted (default), the
    Activity mode is HIDDEN from the workspace mode menu and the
    panel is not rendered. A simple chat app that never spawns
    sub-agents shouldn't see it. Apps that orchestrate fan-out
    (coordinator, dev assistant, multi-agent research) opt in.

    Example YAML::

        ui:
          activity:
            enabled: true
            position: right
            title: "Activity"
            show_running: true
            show_recent: true
            show_stats: true
            show_bg_tasks: true
            max_recent: 50
            auto_open_on_spawn: false
    """

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch. When this block is present in YAML the "
            "client renders the Activity mode entry. Set ``enabled: "
            "false`` to disable the pane while keeping the rest of "
            "the config (useful for staged rollouts)."
        ),
    )
    position: str = Field(
        default="right",
        description=(
            "Where the activity pane sits relative to the chat: "
            "right | bottom | overlay. Mirror of "
            "``WorkspaceBlock.position`` semantics."
        ),
    )
    title: str | None = Field(
        default=None,
        description=(
            "Panel header label. Defaults to a localised ``Activity`` "
            "string when omitted."
        ),
    )
    show_running: bool = Field(
        default=True,
        description=(
            "Render the live sub-agent strip at the top of the pane. "
            "Set false to suppress and keep only the recent + stats "
            "sections (useful for archival / read-only views)."
        ),
    )
    show_recent: bool = Field(
        default=True,
        description=(
            "Render the recent-terminal-events scrollable list. "
            "Carries the last ``max_recent`` agents that completed / "
            "failed / cancelled, with one-line preview."
        ),
    )
    show_stats: bool = Field(
        default=True,
        description=(
            "Render the aggregate stats footer (total spawned / "
            "completed / failed, average duration, success rate). "
            "Pulls from ``/api/metrics`` digitorn_agent_* counters."
        ),
    )
    show_bg_tasks: bool = Field(
        default=True,
        description=(
            "Interleave background shell tasks alongside sub-agents "
            "in the live + recent strips. Disable for apps that only "
            "spawn sub-agents (no shell.bash run_in_background)."
        ),
    )
    max_recent: int = Field(
        default=50, ge=5, le=500,
        description=(
            "Cap on the number of terminal events kept in the recent "
            "list. Older events are evicted FIFO. Tune up for "
            "long-running orchestration sessions; the panel's "
            "performance degrades past ~200."
        ),
    )
    auto_open_on_spawn: bool = Field(
        default=False,
        description=(
            "When true, the client auto-switches to the Activity pane "
            "the first time the agent spawns a sub-agent. Off by "
            "default — surface only when the user opens it explicitly."
        ),
    )


class WorkspaceBlock(BaseModel):
    """Top-level ``workspace:`` block in app.yaml.

    Tells the client this app uses a virtual file workspace streamed
    via Socket.IO.  The daemon emits ``preview:state_changed`` with
    ``key: "workspace"`` on the first file write, carrying these values
    so the client can pick the correct renderer.

    Example YAML::

        workspace:
          render_mode: react
          entry_file: src/App.tsx
          title: "My App"
          position: right
          width_pct: 60
          auto_open_on_first_tool: true
    """

    model_config = {"extra": "forbid"}

    render_mode: str = Field(
        default="auto",
        description=(
            "How the client should render workspace files. "
            "Values: react, html, markdown, slides, code, latex, builder, auto. "
            "When 'auto', the daemon detects from the first file written."
        ),
    )
    entry_file: str | None = Field(
        default=None,
        description=(
            "Main file the client opens by default in the preview "
            "(e.g. src/App.tsx, index.html, main.tex). If omitted, "
            "a render_mode-specific default is used."
        ),
    )
    title: str | None = Field(
        default=None,
        description="Optional title shown in the workspace toolbar.",
    )
    # ── Layout / behavior hints (added 2026-05-04) ─────────────────
    # Pure display: the daemon ships these to the client and the
    # client decides how to lay out chat ↔ workspace. Defaults
    # preserve current behaviour (right split, opt-in open).
    position: str = Field(
        default="right",
        description=(
            "Where the workspace sits relative to the chat: "
            "right | bottom | hidden | overlay."
        ),
    )
    width_pct: int = Field(
        default=50, ge=10, le=90,
        description=(
            "Workspace pane width as a percentage of the available "
            "split (10..90). Ignored when ``position`` is "
            "``hidden`` / ``overlay``."
        ),
    )
    auto_open_on_first_tool: bool = Field(
        default=True,
        description=(
            "When true (default), the client opens the workspace "
            "pane the first time the agent writes a file or emits "
            "a workbench_* event (Lovable-style). Set to ``false`` "
            "to keep the workspace closed unless the user opens it "
            "manually - useful for chat-only apps that should not "
            "surface a renderer just because a tool wrote one log."
        ),
    )


class ChatThinkingBlock(BaseModel):
    """Per-app visibility / collapse defaults for ``<thinking>`` blocks."""

    model_config = {"extra": "forbid"}

    visible: bool = Field(
        default=True,
        description="When false, thinking blocks are hidden entirely.",
    )
    collapsed_default: bool = Field(
        default=True,
        description=(
            "Initial collapsed state of thinking blocks; the user can "
            "still toggle them when ``visible`` is true."
        ),
    )


class ChatToolCallsBlock(BaseModel):
    """Per-app visibility / collapse defaults for tool-call chips."""

    model_config = {"extra": "forbid"}

    collapsed_default: bool = Field(
        default=True,
        description="Initial collapsed state of tool-call previews.",
    )
    show_silent: bool = Field(
        default=False,
        description=(
            "When true, plumbing tools (memory ops, agent_spawn "
            "internals, discovery meta-tools) are rendered. Default "
            "false hides them."
        ),
    )
    inject_intent: bool = Field(
        default=False,
        description=(
            "When true, the context builder prepends a required "
            "``intent`` string field to every tool's input schema. The "
            "LLM fills it with a short present-continuous verb phrase "
            "(``Analyzing requirements``, ``Reviewing components``, "
            "etc.) AS THE FIRST KEY of the tool call. The runtime "
            "strips it in ``tool_exec`` before dispatching to the "
            "handler, so no tool code changes. The frontend renders "
            "the captured intents as a single live progress line "
            "(Lovable-style) instead of individual tool rows. "
            "Trade-off: ~10-20 extra tokens per tool call; works on "
            "any tool-using model without per-tool changes."
        ),
    )


class ChatComposerBlock(BaseModel):
    """Composer toolbar configuration (file upload, voice, ...).

    Mirrors the existing ``ui.features`` flags for backward
    compatibility - if both are present, ``composer.X`` wins.
    """

    model_config = {"extra": "forbid"}

    file_upload: bool = Field(
        default=True,
        description="Paperclip / drag-drop file attachment.",
    )
    voice: bool = Field(
        default=True,
        description=(
            "Microphone button. Default ``true`` to match the legacy "
            "``features.voice`` default - apps that want voice off "
            "should set ``composer.voice: false`` explicitly."
        ),
    )
    slash_commands: bool = Field(
        default=True,
        description="Slash command palette via ``/``.",
    )
    quick_prompts_visible: bool = Field(
        default=True,
        description=(
            "Show suggested ``ui.quick_prompts`` chips above the composer "
            "when the conversation is empty."
        ),
    )


class ChatVisualBlock(BaseModel):
    """Visual styling for chat bubbles / accent / alignment."""

    model_config = {"extra": "forbid"}

    accent: str = Field(
        default="",
        description=(
            "Hex accent colour, e.g. ``#3b82f6``. Falls back to "
            "``ui.theme.accent`` then ``app.color`` when empty."
        ),
    )
    bubble_style: str = Field(
        default="card",
        description="Bubble visual treatment: card | flat | minimal.",
    )
    user_bubble_alignment: str = Field(
        default="right",
        description="User message alignment: right (default) | left.",
    )


class SlotEntry(BaseModel):
    """One slot placement: which widget kind and what to render.

    Phase 1 (2026-05-04) supports a single ``kind: inline`` that
    references an entry from ``ui.widgets.inline`` by name. Phase 4
    will add ``chart``, ``data_table``, ``iframe`` as native kinds
    so a slot can carry a primitive without going through
    ``inline``. The ``extra: allow`` policy keeps the contract
    forward-compatible - unknown kind-specific fields stay on the
    payload and reach the client untouched.
    """

    model_config = {"extra": "allow"}

    kind: str = Field(
        default="inline",
        description=(
            "Renderer for this slot. Phase 1 supports ``inline`` "
            "(reference to ``ui.widgets.inline.<ref>``)."
        ),
    )
    ref: str = Field(
        default="",
        description=(
            "Name of the inline widget to render (must exist in "
            "``ui.widgets.inline``). Required when ``kind: inline``."
        ),
    )


class SlotsConfig(BaseModel):
    """Per-app placement of inline widgets in the chat surface.

    Generalises the legacy ``ui.widgets.chat_side`` (single
    right-side panel) to five named placements the YAML can fill
    independently:

    - ``header``: floating overlay at the top of the chat panel
      (top-right). Does NOT take vertical layout space.
    - ``sidebar_left`` / ``sidebar_right``: left/right of the
      message list (inside the chat panel - distinct from the
      global workspace splitter).
    - ``footer_left``: REPLACES the workspace-path chip in the
      ``StatusLine`` row below the composer. Renders inline at
      the left edge of that row, no extra vertical space.
    - ``footer_right``: REPLACES the model chip in the same
      ``StatusLine`` row, pinned to the far-right edge.

    The footer pair is the "no-extra-row" override mechanism:
    instead of adding a new line below the composer (which users
    rejected as wasted vertical space), the YAML can hijack the
    two existing chips that already live there - workspace path
    on the left, model name on the right - and substitute its
    own widget. Set neither and the StatusLine is unchanged.

    No ``above_composer`` slot: action rows between the message
    list and the composer were rejected as visually competing
    with both the chat scroll area and the composer itself.
    Apps that want pre-composer affordances should use
    ``header`` (overlay) or the upcoming Phase-2
    ``message_actions`` (per-message buttons) instead.

    Each slot is optional; omitted slots stay empty so existing
    apps without a ``ui.slots`` block keep their historical
    layout. The slot system is the architectural foundation that
    Phase 2 (``message_actions``) and Phase 3 (``tool_renderers``)
    build on.
    """

    model_config = {"extra": "forbid"}

    header: SlotEntry | None = Field(default=None)
    sidebar_left: SlotEntry | None = Field(default=None)
    sidebar_right: SlotEntry | None = Field(default=None)
    footer_left: SlotEntry | None = Field(default=None)
    footer_right: SlotEntry | None = Field(default=None)


# ─────────────────────────────────────────────────────────────────
# Widgets - declarative UI rendered by the Flutter client (v1)
# ─────────────────────────────────────────────────────────────────
#
# Spec: docs/app-language/42-widgets.md
#
# Compile-time guarantees enforced here:
#   - ``version`` must equal 1 (the only one we support today)
#   - ``type`` of every WidgetNode is in WIDGET_PRIMITIVES
#   - Action ``action`` field is in WIDGET_ACTIONS
#   - ``accent`` / ``color`` use a closed semantic palette
#   - Recursive children/arrays are typed as ``WidgetNode``
#   - Free-form ``data:`` blocks accept any dict (validated at deploy
#     time by the data-source resolver, not by pydantic, because the
#     spec lists 5 source types each with their own schema)
#
# The ``ref:`` mechanism (an inline widget referenced from chat_side
# or modals or by widget.render SSE) is validated structurally here
# but cycle-checked in compiler.py during the compile pass.

# Closed sets ---------------------------------------------------------

WIDGET_PRIMITIVES: frozenset[str] = frozenset({
    # Layout
    "column", "row", "card", "section", "tabs", "split", "grid",
    "spacer", "divider",
    # Content
    "markdown", "text", "image", "icon",
    # Data display
    "list", "table", "chart", "stat", "timeline", "tree", "kanban",
    # Input
    "form", "text_input", "textarea", "select", "multi_select",
    "radio", "checkbox", "switch", "slider",
    "date", "time", "datetime", "file_upload", "code_editor",
    # Action
    "button", "icon_button", "link", "confirm",
    # Feedback
    "alert", "badge", "progress", "skeleton", "empty_state",
})

WIDGET_ACTIONS: frozenset[str] = frozenset({
    "chat", "tool", "http", "open_url", "open_workspace",
    "open_modal", "close", "set_state", "refresh",
    "copy", "download", "navigate", "confirm", "sequence",
    "alert",
})

WIDGET_ACCENTS: frozenset[str] = frozenset({
    "blue", "purple", "green", "orange", "red", "cyan",
})

WIDGET_COLORS: frozenset[str] = frozenset({
    "text", "bright", "muted", "dim", "accent",
    "error", "success", "warning", "info",
})

WIDGET_DENSITIES: frozenset[str] = frozenset({"compact", "normal", "roomy"})

class PipelineStep(BaseModel):
    """A single step in a pipeline: call a deployed app with an input."""

    model_config = {"extra": "forbid"}

    app: str = Field(..., description="Deployed app_id to invoke.")
    input: str = Field(
        default="",
        description=(
            "Input for this step. Supports {{variables}} including "
            "{{input}} (original pipeline input) and "
            "{{steps[N].output}} (output of step N)."
        ),
    )
    output_as: str = Field(
        default="",
        description="Optional name to reference this step's output in later steps.",
    )
    optional: bool = Field(
        default=False,
        description="If true, continue pipeline even if this step fails.",
    )


WIDGET_FILTERS: frozenset[str] = frozenset({
    "upper", "lower", "title",
    "truncate", "default", "length",
    "date", "relative_time", "money", "number", "percent",
    "json", "filter", "map", "pluck", "join",
    "first", "last", "sort", "reverse", "slice",
    "replace", "markdown",
    "plus_days", "minus_days",
    # Aliases / safe extensions
    "filter_search", "source_icon", "tree_icon", "kind_color",
    "status_color", "sev_color",
})


class WidgetNode(BaseModel):
    """Recursive widget tree node - every primitive shares this base.

    Pydantic refuses extra fields globally, BUT each primitive needs
    its own keys (``items`` for list, ``rows`` for table, ``children``
    for column/row, etc.). Rather than declare 30 strict subclasses
    we use a permissive shape and validate the per-primitive contract
    in :func:`digitorn.core.app.compiler._validate_widget_tree`.
    """

    type: str = Field(..., description="Primitive name - must be in WIDGET_PRIMITIVES.")

    # Universal node fields (spec §4)
    id: str | None = None
    when: str | None = Field(default=None, description="Conditional render expression.")
    # ``for`` is reserved in Python - accept it via alias
    for_: str | None = Field(default=None, alias="for")
    as_: str | None = Field(default=None, alias="as")
    key: str | None = None
    accent: str | None = None
    density: str | None = None
    hidden: bool | None = None

    # Tree containers (only one is set depending on primitive)
    children: list["WidgetNode"] | None = None
    item: "WidgetNode | None" = None
    first: "WidgetNode | None" = None
    second: "WidgetNode | None" = None
    body: "WidgetNode | None" = None
    render: "WidgetNode | None" = None
    empty: "WidgetNode | None" = None
    loading: "WidgetNode | None" = None
    submit: dict[str, Any] | None = None
    reset: dict[str, Any] | None = None

    # Per-primitive payload - validated post-parse by the compiler.
    # We accept arbitrary keys to keep the schema flexible enough for
    # all 30+ primitives without 30 subclasses.
    model_config = {"extra": "allow", "populate_by_name": True}


WidgetNode.model_rebuild()


class ChatSideWidget(BaseModel):
    """Z2 - companion side panel rendered next to the chat."""

    model_config = {"extra": "forbid"}

    title: str | None = None
    icon: str | None = None
    collapsible: bool = True
    default_open: bool = True
    accent: str | None = None
    density: str | None = None
    width: int = Field(default=300, ge=260, le=420)
    data: dict[str, Any] = Field(default_factory=dict)
    tree: WidgetNode


class WorkspaceTabWidget(BaseModel):
    """Z3 - one tab in the workspace 'Widgets' container."""

    model_config = {"extra": "forbid"}

    id: str
    title: str
    icon: str | None = None
    accent: str | None = None
    density: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    tree: WidgetNode


class ModalWidget(BaseModel):
    """Z4 - modal pushed by ``action: open_modal``."""

    model_config = {"extra": "forbid"}

    title: str | None = None
    width: int | str = Field(
        default=560,
        description=(
            "Modal width preset (one of 420|560|640|720|'full') or px int."
        ),
    )
    dismissible: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    tree: WidgetNode


class InlineWidget(BaseModel):
    """Named inline widget - referenceable by ``ref:`` from agent SSE."""

    model_config = {"extra": "forbid"}

    data: dict[str, Any] = Field(default_factory=dict)
    tree: WidgetNode


class WidgetsConfig(BaseModel):
    """Top-level ``widgets:`` block in app.yaml.

    Structure mirrors the Flutter spec v1: one optional chat_side
    panel, an array of workspace_tabs, a dict of named modals, and a
    dict of named inline widgets that the agent can push via
    ``widget.render`` with a ``ref:``.

    External widget files under ``./widgets/*.yaml`` in the bundle
    dir are loaded by the compiler and merged into the ``inline``
    map (keyed by file stem) - same pattern as skills.
    """

    model_config = {"extra": "forbid"}

    version: int = Field(
        default=1,
        description="Spec version. Daemon refuses unknown versions.",
    )
    chat_side: ChatSideWidget | None = None
    workspace_tabs: list[WorkspaceTabWidget] = Field(default_factory=list)
    modals: dict[str, ModalWidget] = Field(default_factory=dict)
    inline: dict[str, InlineWidget] = Field(default_factory=dict)


# Resolve the FlowConfig forward reference. Imported lazily to keep the
# module surface small - flow.py lives in its own file.
from digitorn.core.app.flow import FlowConfig  # noqa: E402
# Phase-3 tool_renderers block - same lazy import pattern. Wrapped in
# try / except so deleting tool_renderers.py also cleanly drops the
# field (UIBlock then has a permanently-None tool_renderers attribute,
# which is exactly the rollback behaviour we want).
try:
    from digitorn.core.app.tool_renderers import (  # noqa: E402, F401
        ToolRenderersBlock,
    )
except ImportError:
    ToolRenderersBlock = None  # type: ignore[assignment, misc]
# Phase-2 message_actions block - same isolation pattern.
try:
    from digitorn.core.app.message_actions import (  # noqa: E402, F401
        MessageActionsBlock,
    )
except ImportError:
    MessageActionsBlock = None  # type: ignore[assignment, misc]

ContextConfig.model_rebuild()
ExecutionConfig.model_rebuild()
AppDefinition.model_rebuild()
UIBlock.model_rebuild()
