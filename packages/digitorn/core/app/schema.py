"""Pydantic models defining the app YAML structure.

These models are the **parse target** — the YAML is validated directly
against ``AppDefinition``.  They also serve as the documentation: every
field has a description that can be rendered in ``digitorn app schema``.

The structure is intentionally generic.  The ``modules`` block maps
module IDs to ``ModuleBlock``s, and each block contains:

- ``setup``: ordered list of action calls (action name + params)
- ``constraints``: runtime restrictions (validated against the module's
  ``ConstraintSpec`` declarations)

No module-specific knowledge is baked in — any module with ``@action``
methods is automatically configurable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AppMeta(BaseModel):
    """Top-level application identity."""

    model_config = {"extra": "forbid"}

    app_id: str = Field(..., description="Unique application identifier.")
    name: str = Field(..., description="Human-readable application name.")
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
    quick_prompts: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Suggested prompts shown as clickable buttons when the user opens the app. "
            "Each entry: {label: 'short text', message: 'full prompt', icon: 'emoji'}. "
            "If empty, the client shows just the input field."
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
    """A fully declarative behavioral rule — works for ANY action.

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
            "  target_not_in_set: <set_name>    — target param NOT in tracked set\n"
            "  target_in_set: <set_name>         — target param IS in tracked set\n"
            "  counter_gte: {name, value}         — counter >= threshold\n"
            "  param_matches: {param, pattern}    — param matches regex\n"
            "  param_contains: {param, value}     — param contains string\n"
            "  flag_is: {name, value}             — flag equals value\n"
            "  no_text_before_tools: true         — agent didn't explain before tools\n"
            "  consecutive_gte: <N>               — same tool called N+ times\n"
            "  all: [conditions...]               — all must match\n"
            "  any: [conditions...]               — at least one matches\n"
            "  not: <condition>                   — negation"
        ),
    )
    message: str = Field(
        default="",
        description=(
            "Message template. Placeholders:\n"
            "  {target}              — file_path or primary target param\n"
            "  {tool}                — current tool name\n"
            "  {param:<name>}        — any param value\n"
            "  {counter:<name>}      — counter value\n"
            "  {set_count:<name>}    — size of a tracked set\n"
            "  {turn}                — current turn number"
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
    """Configure what the session state tracks — fully declarative.

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
            "  'every_turn'    — before every agent turn (classifier can skip via skip_reason)\n"
            "  'first_turn'    — only on the first turn of a session\n"
            "  'every_n_turns' — every N turns (set frequency_n)\n"
            "  'on_new_message'— only when the user sent a new message (skip tool-only turns)"
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

    # ── Output schema — what the classifier produces ──
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
            "Risk levels. Same format as approaches — string or dict:\n\n"
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
            "The prompt receives the output schema dynamically — you don't\n"
            "need to hardcode complexity/approach values in your prompt."
        ),
    )

    # ── Directive format ──
    directive_prefix: str = Field(
        default="[BEHAVIOR DIRECTIVE — {complexity} complexity, {risk} risk]",
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
    """Behavioral enforcement rules — actively monitored at runtime.

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
        description="Fully declarative rules — works for ANY action. See BehaviorRuleDefinition.",
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
            "hooks, and channels — but the agent cannot see or call their tools. "
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

        process.exec     — spawn subprocesses (required for stdio transport)
        process.*        — all process permissions (exec + spawn_daemon)
        net.http         — outbound HTTP (required for SSE/HTTP transport)
        net.socket       — raw socket access
        net.listen       — bind/listen on a port
        net.*            — all network permissions
        fs.read          — read files beyond workspace
        fs.write         — write files beyond workspace
        fs.delete        — delete files beyond workspace
        fs.*             — all filesystem permissions
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

    - ``config``: Static module configuration — pushed via
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


class ContextConfig(BaseModel):
    """Context management configuration for the agent loop.

    Controls how the context window is managed to prevent overflow errors.
    When the context fills up, the runtime can automatically compact it
    using the configured strategy.

    Can be set at two levels:
    - ``execution.context`` — default for all agents
    - ``agent.brain.context`` — per-brain override (multi-agent apps)

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

    1. **Inline** — full provider config embedded in the agent::

        brain:
          provider: deepseek
          model: deepseek-chat
          temperature: 0.2
          config:
            api_key: "{{secret.DEEPSEEK_API_KEY}}"
            base_url: "https://api.deepseek.com/v1"

    2. **Reference** — points to a named provider in ``modules.llm_provider``::

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
            "perplexity, fireworks."
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
    backend: Literal["openai_compat", "anthropic"] = Field(
        default="openai_compat",
        description="Provider backend: 'anthropic' or 'openai_compat'.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific config (api_key, base_url, etc.).",
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
            "Feature flag. When False the hook is loaded but never fires — "
            "lets apps A/B gate new behavior without YAML surgery."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Free-form tags for grouping / querying hooks. Not used by the "
            "runtime — surfaced in /api/apps/{id}/hooks for introspection."
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
          mode: one_shot
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
        default="one_shot",
        description="Execution mode: 'one_shot', 'conversation', 'background', or 'pipeline'.",
    )
    entry_agent: str = Field(
        default="",
        description="Agent to start with. Default: first agent in list.",
    )
    max_turns: int = Field(
        default=50,
        description="Maximum agent loop iterations (per turn for conversation, per activation for background).",
    )
    timeout: float = Field(
        default=300.0,
        description="Timeout in seconds (per turn for conversation, per activation for background).",
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
    user is and adapts. One app serves 10,000 users — no per-user
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
            "as a placeholder — it will be replaced with the actual session ID "
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
    (email, phone, chat_id) from a data source — no manual ``output_config``
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
    modules: list[Any] = Field(
        default_factory=list,
        description=(
            "Modules this specialist can access. Empty = same as coordinator.\n"
            "Supports two formats:\n"
            "  - Simple: ['filesystem', 'shell', 'memory'] — full module access\n"
            "  - Granular: [{'filesystem': ['read', 'grep', 'glob']}, 'shell', 'memory'] — restrict actions per module"
        ),
    )
    pool: dict[str, Any] = Field(
        default_factory=dict,
        description="Agent pool config for coordinators. Keys: max_workers (int).",
    )
    hooks: list[HookConfig] = Field(
        default_factory=list,
        description=(
            "Per-agent hooks — merged with ``execution.hooks`` but only "
            "evaluated when this specific agent is active. Use for "
            "specialist-specific behavior (e.g. a `reviewer` agent that "
            "runs extra lint, a `writer` agent that logs every edit). "
            "App-wide hooks still fire for every agent; these add on top."
        ),
    )


class AppDefinition(BaseModel):
    """Root model — direct parse target for an app YAML file.

    Example YAML::

        app:
          app_id: my-agent
          name: "My Agent"

        variables:
          workspace: "{{env.PWD}}"

        modules:
          database:
            setup:
              - action: connect
                params:
                  connection_id: main
                  driver: sqlite
                  database: "{{workspace}}/data.db"
            constraints:
              allowed_actions: [fetch_results, list_tables]
              blocked_actions: [execute_query]

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
            system_prompt: "You are a coordinator."

        capabilities:
          default_policy: auto
          max_risk_level: medium
          grant:
            - module: database
              actions: [fetch_results]
          deny:
            - module: database
              actions: [execute_query]
              reason: "Read-only mode"
    """

    model_config = {"extra": "forbid"}

    app: AppMeta = Field(..., description="Application identity.")
    variables: dict[str, str] = Field(
        default_factory=dict,
        description="Template variables available as {{name}} in params and constraints.",
    )
    modules: dict[str, ModuleBlock] = Field(
        default_factory=dict,
        description="Per-module configuration. Keys are module IDs.",
    )
    channels: dict[str, ChannelInstanceConfig] = Field(
        default_factory=dict,
        description=(
            "Named output channel instances. Keys are instance names "
            "(e.g. 'slack_alerts', 'email_reports'). Used by scheduler "
            "and watchers to route notifications to external systems."
        ),
    )
    agents: list[AgentDefinition] = Field(
        default_factory=list,
        description="Agent definitions. Each agent has a brain (LLM config) and role.",
    )
    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig,
        description="Execution mode and runtime parameters.",
    )
    middleware: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "App-level middleware pipeline. Runs before/after each LLM call. "
            "Built-in: mask_secrets, prompt_inject, content_filter, rag_inject, response_filter. "
            "Custom: {custom: {path: './my_mw.py', class: 'MyMiddleware'}}"
        ),
    )
    skills: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "App-level skills — reusable command files (.md) the agent can invoke. "
            "Each entry: {command: '/name', description: '...', path: './skills/name.md'}"
        ),
    )
    pipeline: list["PipelineStep"] = Field(
        default_factory=list,
        description=(
            "Pipeline of apps to execute in sequence (one_shot mode only). "
            "Each step calls a deployed app and passes its output to the next step. "
            "Steps: [{app: 'app_id', input: '{{input}}'}, {app: 'other', input: '{{steps[0].output}}'}]"
        ),
    )
    behavior: BehaviorConfig | None = Field(
        default=None,
        description=(
            "Behavioral enforcement rules. Actively monitored at runtime — "
            "violations are detected and signaled to the agent immediately. "
            "Use a preset profile (coding, research, data, creative, assistant) "
            "or define custom rules."
        ),
    )
    capabilities: CapabilitiesConfig | None = Field(
        default=None,
        description="Application security capabilities (grant/deny). When absent, no security enforcement is applied (dev/test mode).",
    )
    preview: "PreviewConfig | None" = Field(
        default=None,
        description=(
            "Optional dev-server preview for apps shipping a web UI "
            "(Vite, Next, etc.). The daemon spawns the command on deploy "
            "and exposes it via /api/apps/{app_id}/preview/dev/*."
        ),
    )
    workspace: "WorkspaceBlock | None" = Field(
        default=None,
        description=(
            "Workspace config — tells the client this app uses a virtual "
            "file workspace streamed via Socket.IO. The agent writes files "
            "with WsWrite/WsEdit/WsDelete and the client renders them "
            "based on render_mode (react, html, markdown, slides, code)."
        ),
    )
    widgets: "WidgetsConfig | None" = Field(
        default=None,
        description=(
            "Declarative UI widgets rendered by the Flutter client. The "
            "compiler validates the tree at deploy time; the agent can "
            "push live widget render/update events to per-session zones "
            "(inline, chat_side, workspace_tabs, modals)."
        ),
    )
    # ── Client manifest extensions ────────────────────────────────
    # These three blocks are read by the Flutter/web client to tailor
    # the UI (hide panels, override colors, expose /commands). The
    # daemon just parses + passes them through to DeployedApp.summary()
    # so `GET /api/apps/{id}` delivers them to the client unchanged.
    features: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "UI feature toggles consumed by the Flutter client. Keys: "
            "voice, attachments, tools_panel, snippets, tasks_panel, "
            "memory_panel, context_ring, markdown, slash_commands, "
            "message_actions, status_pills, token_badges. "
            "Missing keys default to true (feature visible). "
            "Also accepted nested under app.features for client compat."
        ),
    )
    theme: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Client theme override map. Keys: accent (hex like '#6EE7B7' — "
            "overrides app.color), background (hex, client-reserved)."
        ),
    )
    slash_commands: list[dict[str, str]] = Field(
        default_factory=list,
        description=(
            "Custom /slash palette entries rendered by the client. "
            "Each entry: {command: 'deploy', description: '…', "
            "template: 'Deploy to {env}'}. Currently parsed only; the "
            "Flutter client surfaces them in a later release."
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


class PreviewConfig(BaseModel):
    """Dev server spawned on app deploy and proxied through the daemon.

    Example YAML::

        preview:
          enabled: true
          command: [npm, run, dev]
          cwd: ./web
          port: 5173
          install_command: [npm, install]
          health_path: /
          env:
            VITE_API_URL: "http://localhost:8000"
    """

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=True,
        description="Disable to skip starting the preview server without removing the block.",
    )
    command: list[str] = Field(
        ...,
        description="Command + args to run, e.g. ['npm', 'run', 'dev'].",
    )
    cwd: str = Field(
        default=".",
        description=(
            "Working directory for the preview process, relative to the "
            "package bundle dir."
        ),
    )
    port: int = Field(
        ...,
        ge=1024,
        le=65535,
        description="Port the dev server binds to on localhost.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for the preview process.",
    )
    install_command: list[str] | None = Field(
        default=None,
        description=(
            "Optional command to run once when the package is installed "
            "(e.g. ['npm', 'install']). Runs from ``cwd``."
        ),
    )
    health_path: str = Field(
        default="/",
        description="HTTP path polled to detect dev-server readiness.",
    )
    startup_timeout: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="Seconds to wait for the health check before declaring the preview failed.",
    )
    restart_on_crash: bool = Field(
        default=True,
        description="Restart the preview process if it exits unexpectedly (max 3 retries per minute).",
    )


# ─────────────────────────────────────────────────────────────────
# Widgets — declarative UI rendered by the Flutter client (v1)
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
    """Recursive widget tree node — every primitive shares this base.

    Pydantic refuses extra fields globally, BUT each primitive needs
    its own keys (``items`` for list, ``rows`` for table, ``children``
    for column/row, etc.). Rather than declare 30 strict subclasses
    we use a permissive shape and validate the per-primitive contract
    in :func:`digitorn.core.app.compiler._validate_widget_tree`.
    """

    type: str = Field(..., description="Primitive name — must be in WIDGET_PRIMITIVES.")

    # Universal node fields (spec §4)
    id: str | None = None
    when: str | None = Field(default=None, description="Conditional render expression.")
    # ``for`` is reserved in Python — accept it via alias
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

    # Per-primitive payload — validated post-parse by the compiler.
    # We accept arbitrary keys to keep the schema flexible enough for
    # all 30+ primitives without 30 subclasses.
    model_config = {"extra": "forbid", "populate_by_name": True}


WidgetNode.model_rebuild()


class ChatSideWidget(BaseModel):
    """Z2 — companion side panel rendered next to the chat."""

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
    """Z3 — one tab in the workspace 'Widgets' container."""

    model_config = {"extra": "forbid"}

    id: str
    title: str
    icon: str | None = None
    accent: str | None = None
    density: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    tree: WidgetNode


class ModalWidget(BaseModel):
    """Z4 — modal pushed by ``action: open_modal``."""

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
    """Named inline widget — referenceable by ``ref:`` from agent SSE."""

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
    map (keyed by file stem) — same pattern as skills.
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


ContextConfig.model_rebuild()
ExecutionConfig.model_rebuild()
AppDefinition.model_rebuild()
