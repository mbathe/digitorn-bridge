"""Semantic classifier — generic pre-turn behavioral analysis engine.

Fully data-driven: every aspect is configurable via YAML. The app author
defines complexity levels, approaches, risk levels, system prompt, directive
format, when to run, and what context to include.

The classifier receives a ``ClassifierConfig`` dict and builds its prompts
dynamically. No hardcoded enum values — they come from the config.

Integration:
  - Called by module.py ``classify_turn()`` before the main LLM call
  - Directives injected as a system message the agent sees
  - Frequency, skip logic, timeout all controlled by config
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Default system prompt — used when classifier.system_prompt is null.
# The app author can replace this entirely via YAML or {{prompt.X}}.
# ────────────────────────────────────────────────────────────────────

_DEFAULT_SYSTEM_PROMPT = """\
You are a behavioral director for an AI agent. You analyze what the user wants, \
what the agent has done so far, and what tools it has — then you produce precise \
directives that push the agent toward optimal behavior.

You are NOT a simple task categorizer. You are a coach who understands the domain \
and directs the agent's behavior in real-time.

## What you receive

1. **User message** — what the user just asked
2. **Agent tools** — the exact tools available with short descriptions
3. **Session state** — what the agent has already done: files read, edits, tests, violations, turn number
4. **Workspace context** — project type, languages, scale, recent activity
5. **Active rules** — behavioral rules being enforced at runtime
6. **Recent history** — last few messages with tool calls and results
7. **Profile context** — custom behavioral instructions from the app author

## Your output

{output_schema}

Return `{{"skip_reason": "..."}}` with empty directives when classification is unnecessary \
(simple follow-up, agent already on track, trivial acknowledgment).

## Behavioral model

### Phase 1: UNDERSTAND (before any action)
- What is the user actually trying to achieve? (intent, not literal words)
- How big is this? (1 step? 5 steps? cross-cutting?)
- What could go wrong? (data loss, breaking changes, security)
- Does the agent know enough to start?

### Phase 2: SCOPE & PLAN
{complexity_guide}

### Phase 3: EXECUTE with discipline
- Search BEFORE reading (don't read blindly — find the right target first)
- Read BEFORE editing (understand what you're changing)
- Plan BEFORE implementing (state your approach)
- Verify AFTER changing (re-read, check results, run tests)
- Delegate WHEN appropriate (parallel work, bulk operations)
- Ask WHEN uncertain (ambiguous requirements, destructive operations)

### Phase 4: VERIFY
- After every change: did it come out right? Any errors?
- After a feature: do tests pass? Does it work?
- Before answering: did the agent check the actual current state?

## How to write directives

Directives are BEHAVIORAL COMMANDS, not task instructions. The agent already knows WHAT \
to do — you tell it HOW to approach it.

**BAD** (too vague): "Help the user", "Be careful", "Use the right tools"
**BAD** (micromanaging): "Call Read on file.py", "Run Bash('npm test')"
**GOOD** (behavioral guidance with reasoning):
- "This touches critical logic — explore the module structure before making changes."
- "You've edited files without testing. Run the test suite NOW before more changes."
- "The user asks about unfamiliar tech. Search for current docs before answering."
- "This is cross-cutting. Present a plan with file paths and wait for approval."
- "You have sub-agents. Launch parallel explore agents instead of reading files one by one."

Adapt to session state:
- Turn 0: focus on approach — how should the agent start?
- Agent exploring: is it efficient? should it delegate?
- Files edited but no tests: push hard for testing
- Violations detected: reference them explicitly

{risk_guide}

## When to skip (return skip_reason)

- "yes", "ok", "continue", "go ahead" → agent already on track
- Simple question needing 1 action → trivial
- Agent clearly following previous directives → don't re-classify
- No task content → skip
"""


def build_classifier_config_defaults() -> dict[str, Any]:
    """Return the default classifier config as a plain dict.

    Used when no ClassifierConfig is provided — matches the Pydantic
    model defaults so behavior is identical.
    """
    return {
        "frequency": "every_turn",
        "frequency_n": 3,
        "skip_followups": True,
        "timeout": 15,
        "complexity_levels": [
            {"name": "trivial", "when": "1 action, obvious answer", "behavior": "Just do it"},
            {"name": "simple", "when": "2-4 actions, clear path", "behavior": "Search, read, act, verify"},
            {"name": "moderate", "when": "5-15 actions, multi-step", "behavior": "Explore structure, plan, confirm with user, implement, verify"},
            {"name": "complex", "when": "15+ actions, multi-file or cross-cutting", "behavior": "Explore via sub-agents, detailed plan, user approval, phased implementation, test each phase"},
            {"name": "critical", "when": "Destructive, migrations, security, production", "behavior": "ALWAYS confirm with user first, explain risks, get explicit approval"},
        ],
        "approaches": [
            {"name": "direct", "label": "Execute directly", "when": "Task is trivial or simple with a clear path", "behavior": "Go straight to tool calls, minimal planning text"},
            {"name": "explore_first", "label": "Explore first, then act", "when": "Agent needs to understand the codebase/context before acting", "behavior": "Search and read before making any changes, summarize findings"},
            {"name": "plan_and_confirm", "label": "Plan and get user approval", "when": "Task touches 3+ files or has multiple valid approaches", "behavior": "Write a numbered plan with file paths, present to user, wait for approval"},
            {"name": "delegate", "label": "Delegate to sub-agents", "when": "Task has independent sub-tasks or requires bulk exploration", "behavior": "Launch sub-agents in parallel, collect results, then synthesize"},
            {"name": "research_first", "label": "Research before implementing", "when": "Unknown APIs, unfamiliar tech, ambiguous requirements", "behavior": "Search the web or docs first, verify assumptions, then implement"},
        ],
        "risk_levels": [
            {"name": "none", "when": "Read-only, questions, exploration"},
            {"name": "low", "when": "Small edits, new files, safe commands"},
            {"name": "medium", "when": "Multiple file edits, dependency changes, config modifications"},
            {"name": "high", "when": "Deletions, migrations, security-related, production configs, irreversible operations", "behavior": "MUST confirm with user, explain what could go wrong"},
        ],
        "max_directives": 5,
        "context": {
            "tool_inventory": True,
            "session_state": True,
            "workspace_info": True,
            "recent_history": True,
            "history_depth": 8,
        },
        "system_prompt": None,
        "directive_prefix": "[BEHAVIOR DIRECTIVE — {complexity} complexity, {risk} risk]",
        "high_risk_warning": (
            "Risk level: {risk}. "
            "Confirm destructive or irreversible actions with the user before proceeding."
        ),
        "high_risk_threshold": "medium",
        "directive_footer": (
            "Follow these directives. They are based on your behavioral rules "
            "and the current session state. Violations are detected in real-time."
        ),
    }


def _get_cfg(classifier_config: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """Safe access to classifier config with defaults fallback."""
    if classifier_config and key in classifier_config:
        return classifier_config[key]
    defaults = build_classifier_config_defaults()
    return defaults.get(key, default)


def _get_context_cfg(classifier_config: dict[str, Any] | None, key: str) -> bool:
    """Safe access to classifier context config."""
    ctx = {}
    if classifier_config:
        ctx = classifier_config.get("context", {})
        if hasattr(ctx, "model_dump"):
            ctx = ctx.model_dump()
        elif not isinstance(ctx, dict):
            ctx = {}
    defaults = build_classifier_config_defaults()["context"]
    return ctx.get(key, defaults.get(key, True))


# ────────────────────────────────────────────────────────────────────
# System prompt builder — dynamic from config
# ────────────────────────────────────────────────────────────────────

def build_system_prompt(classifier_config: dict[str, Any] | None = None) -> str:
    """Build the classifier's system prompt from config.

    If ``classifier_config.system_prompt`` is set, uses it directly.
    Otherwise builds from the default template with dynamic sections
    generated from the config's complexity levels, approaches, etc.
    """
    custom_prompt = _get_cfg(classifier_config, "system_prompt")
    if custom_prompt:
        return custom_prompt

    complexity_levels = _get_cfg(classifier_config, "complexity_levels")
    approaches = _get_cfg(classifier_config, "approaches")
    risk_levels = _get_cfg(classifier_config, "risk_levels")
    max_directives = _get_cfg(classifier_config, "max_directives")

    # Normalize all entries to dicts
    c_items = [_normalize_entry(c) for c in complexity_levels]
    a_items = [_normalize_entry(a) for a in approaches]
    r_items = [_normalize_entry(r) for r in risk_levels]

    output_schema = _build_output_schema(c_items, a_items, r_items, max_directives)
    complexity_guide = _build_guide("Complexity levels", c_items)
    approach_guide = _build_guide("Approaches", a_items)
    risk_guide = _build_guide("Risk assessment", r_items)

    return _DEFAULT_SYSTEM_PROMPT.format(
        output_schema=output_schema,
        complexity_guide=complexity_guide + "\n\n" + approach_guide,
        risk_guide=risk_guide,
    )


# ── Entry normalization ──

def _normalize_entry(entry: str | dict[str, str]) -> dict[str, str]:
    """Normalize a string or dict entry to a full dict.

    Supports:
      - ``"direct"`` → ``{"name": "direct"}``
      - ``{"name": "direct", "label": "...", "when": "...", "behavior": "..."}``
    """
    if isinstance(entry, str):
        return {"name": entry}
    if isinstance(entry, dict):
        return entry
    return {"name": str(entry)}


def _entry_name(entry: str | dict[str, str]) -> str:
    """Get the name from a normalized or raw entry."""
    if isinstance(entry, str):
        return entry
    return entry.get("name", str(entry))


def _entry_label(entry: dict[str, str]) -> str:
    """Get the label (for display to agent) from an entry."""
    return entry.get("label", entry.get("name", ""))


# ── Builders ──

def _build_output_schema(
    complexity: list[dict], approaches: list[dict],
    risk: list[dict], max_directives: int,
) -> str:
    c_enum = " | ".join(f'"{e["name"]}"' for e in complexity)
    a_enum = " | ".join(f'"{e["name"]}"' for e in approaches)
    r_enum = " | ".join(f'"{e["name"]}"' for e in risk)
    return (
        "A JSON object:\n"
        "```json\n"
        "{\n"
        f'  "complexity": {c_enum},\n'
        f'  "approach": {a_enum},\n'
        f'  "risk_level": {r_enum},\n'
        f'  "directives": ["directive 1", ...] // max {max_directives}\n'
        "}\n"
        "```"
    )


def _build_guide(title: str, items: list[dict[str, str]]) -> str:
    """Build a guide section from structured entries.

    Each entry can have: name, label, when, behavior.
    Produces a rich guide that tells the classifier exactly when
    to pick each value and how the agent should behave.
    """
    lines = [f"## {title}"]
    for item in items:
        name = item.get("name", "?")
        label = item.get("label", "")
        when = item.get("when", "")
        behavior = item.get("behavior", "")

        parts = [f"**{name}**"]
        if label:
            parts.append(f"({label})")
        line = " ".join(parts)

        if when:
            line += f"\n  When: {when}"
        if behavior:
            line += f"\n  Agent behavior: {behavior}"

        lines.append(f"- {line}")
    return "\n".join(lines)


def get_approach_label(
    approach_name: str,
    classifier_config: dict[str, Any] | None,
) -> str:
    """Get the display label for an approach from config."""
    approaches = _get_cfg(classifier_config, "approaches") or []
    for entry in approaches:
        item = _normalize_entry(entry)
        if item["name"] == approach_name:
            return _entry_label(item)
    return approach_name


def get_entry_names(entries: list) -> list[str]:
    """Extract names from a list of string-or-dict entries."""
    return [_entry_name(e) for e in entries]


# ────────────────────────────────────────────────────────────────────
# Skip logic
# ────────────────────────────────────────────────────────────────────

_FOLLOWUP_PATTERNS = re.compile(
    r"^(yes|yeah|yep|ok|okay|oui|d'accord|go|go ahead|continue|do it|proceed|sure|lgtm|ship it|"
    r"valide|validé|c'est bon|parfait|nice|great|cool|thanks|merci|thx|ty|k|👍|✅)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def should_skip_followup(user_message: str) -> bool:
    """Return True if the message is a simple follow-up that doesn't
    need classification."""
    text = user_message.strip()
    if len(text) > 50:
        return False
    return bool(_FOLLOWUP_PATTERNS.match(text))


def should_run_this_turn(
    turn: int,
    classifier_config: dict[str, Any] | None,
    user_message: str,
) -> bool:
    """Decide whether to run the classifier on this turn based on config."""
    frequency = _get_cfg(classifier_config, "frequency")
    skip_followups = _get_cfg(classifier_config, "skip_followups")

    # Skip simple follow-ups before anything else
    if skip_followups and should_skip_followup(user_message):
        return False

    if frequency == "first_turn":
        return turn == 0
    elif frequency == "every_n_turns":
        n = _get_cfg(classifier_config, "frequency_n") or 3
        return turn % n == 0
    elif frequency == "on_new_message":
        # Always run when called (agent_loop only calls when there's a user msg)
        return True
    else:
        # "every_turn" (default) — always run
        return True


# ────────────────────────────────────────────────────────────────────
# Message builder
# ────────────────────────────────────────────────────────────────────

def build_classify_messages(
    user_message: str,
    capabilities: list[str],
    active_rules: list[str],
    recent_context: list[dict[str, Any]] | None = None,
    *,
    profile_context: dict[str, Any] | None = None,
    tool_inventory: list[dict[str, str]] | None = None,
    session_state: dict[str, Any] | None = None,
    workspace_context: dict[str, Any] | None = None,
    classifier_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build messages for the classifier LLM call.

    What gets included is controlled by ``classifier_config.context``.
    """
    parts: list[str] = []

    # ── 1. User message (always) ──
    parts.append(f"## User message\n{user_message}")

    # ── 2. Tool inventory ──
    if _get_context_cfg(classifier_config, "tool_inventory") and tool_inventory:
        tool_lines = [f"- **{t.get('name', '')}**: {t.get('description', '')}" for t in tool_inventory]
        parts.append("## Agent tools\n" + "\n".join(tool_lines))
    elif capabilities:
        parts.append(f"## Agent capabilities (modules)\n{', '.join(capabilities)}")

    # ── 3. Session state ──
    if _get_context_cfg(classifier_config, "session_state") and session_state:
        parts.append("## Session state\n" + _format_session_state(session_state))
    elif not session_state:
        parts.append("## Session state\nTurn: 0 (fresh session)")

    # ── 4. Workspace context ──
    if _get_context_cfg(classifier_config, "workspace_info") and workspace_context:
        ws_text = _format_workspace(workspace_context)
        if ws_text:
            parts.append("## Workspace context\n" + ws_text)

    # ── 5. Active rules ──
    if active_rules:
        parts.append(f"## Active behavioral rules\n{', '.join(active_rules)}")

    # ── 6. Custom profile context ──
    if profile_context:
        profile_text = _format_profile(profile_context)
        if profile_text:
            parts.append("## Behavior profile\n" + profile_text)

    # ── 7. Recent history ──
    if _get_context_cfg(classifier_config, "recent_history") and recent_context:
        depth = 8
        ctx_cfg = classifier_config.get("context", {}) if classifier_config else {}
        if hasattr(ctx_cfg, "history_depth"):
            depth = ctx_cfg.history_depth
        elif isinstance(ctx_cfg, dict):
            depth = ctx_cfg.get("history_depth", 8)
        history_lines = _format_rich_history(recent_context, max_messages=depth)
        if history_lines:
            parts.append("## Recent conversation\n" + "\n".join(history_lines))

    # Build system prompt from config
    system_prompt = build_system_prompt(classifier_config)

    # Append a strict role + JSON-only reminder. Critical for smaller LLMs
    # (DeepSeek-chat, Qwen) which often impersonate the agent when they see
    # the user's task in their input — they answer as if they had to do the
    # task, instead of coaching another agent about how to approach it.
    parts.append(
        "## CRITICAL — your role\n"
        "You are the COACH. You are NOT the agent. The user message above is "
        "what the user just asked the AGENT (not you). Your job is to analyze "
        "the agent's upcoming turn and produce directives that tell the agent HOW "
        "to approach it. You do NOT execute the task. You do NOT explore the codebase. "
        "You produce a JSON object with directives for the agent.\n\n"
        "## Output — RESPOND WITH JSON ONLY\n"
        "Output a single JSON object: "
        '{\"complexity\": \"...\", \"approach\": \"...\", \"risk_level\": \"...\", '
        '\"directives\": [\"...\", \"...\"]}. '
        "No preamble, no markdown fences, no prose.\n"
        "Use `skip_reason` ONLY for trivial follow-ups like 'yes', 'ok', 'continue'. "
        "Never use skip_reason to say you need more info — always produce directives "
        "telling the agent what to explore/clarify."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ────────────────────────────────────────────────────────────────────
# Formatters
# ────────────────────────────────────────────────────────────────────

def _format_session_state(state: dict[str, Any]) -> str:
    """Format session state generically — works for any app type.

    The state dict comes from ``BhvSessionState.snapshot()`` and contains:
    - Universal fields: turn, total_tool_calls, violations_count, etc.
    - Named sets (lists): read_files, edited_files, fetched_urls, etc.
    - Named counters: ``counter:changes_since_test``, ``counter:queries_since_verify``
    - Named flags: ``flag:has_web_searched``, ``flag:plan_approved``
    - Recent tools: list of tool call summaries

    All formatted dynamically — no hardcoded field names.
    """
    lines: list[str] = []

    # Universal scalars first
    turn = state.get("turn", 0)
    lines.append(f"Turn: {turn}")

    total_tc = state.get("total_tool_calls", 0)
    if total_tc:
        lines.append(f"Total tool calls: {total_tc}")

    tc_turn = state.get("tool_calls_this_turn", 0)
    if tc_turn:
        lines.append(f"Tool calls this turn: {tc_turn}")

    consec = state.get("consecutive_same_tool", 0)
    last_tool = state.get("last_tool_name", "")
    if consec > 2:
        lines.append(f"Repeating: {last_tool} x{consec}")

    violations = state.get("violations_count", 0)
    if violations:
        lines.append(f"Violations: {violations} (last: {state.get('last_violation', '')})")

    # Named sets (dynamic: any list value that's not a known scalar)
    _SCALAR_KEYS = {
        "turn", "total_tool_calls", "tool_calls_this_turn",
        "consecutive_same_tool", "last_tool_name",
        "violations_count", "last_violation", "recent_tools",
    }
    for key, value in state.items():
        if key in _SCALAR_KEYS or key.startswith("counter:") or key.startswith("flag:"):
            continue
        if isinstance(value, list) and key != "recent_tools":
            count = len(value)
            # Show count + last few items
            sample = value[-5:] if count > 5 else value
            label = key.replace("_", " ").title()
            lines.append(f"{label}: {count} ({', '.join(str(s) for s in sample)})")

    # Named counters
    for key, value in state.items():
        if key.startswith("counter:") and value:
            label = key[8:].replace("_", " ").title()
            lines.append(f"{label}: {value}")

    # Named flags
    flag_names = [key[5:] for key, value in state.items() if key.startswith("flag:") and value]
    if flag_names:
        lines.append(f"Active flags: {', '.join(flag_names)}")

    # Recent tool history
    recent = state.get("recent_tools", [])
    if recent:
        lines.append(f"Recent actions: {' -> '.join(recent[-6:])}")

    return "\n".join(lines)


def _format_workspace(ws: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("project_type", "languages", "file_count", "framework", "has_tests", "git_status", "workspace_path"):
        val = ws.get(key)
        if val is not None:
            if isinstance(val, list):
                val = ", ".join(str(v) for v in val)
            label = key.replace("_", " ").title()
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


def _format_profile(ctx: dict[str, Any]) -> str:
    lines: list[str] = []
    pname = ctx.get("profile_name", "")
    pdesc = ctx.get("profile_description", "")
    if pname or pdesc:
        lines.append(f"Profile: **{pname}**" + (f" — {pdesc}" if pdesc else ""))
    cprompt = ctx.get("custom_prompt", "")
    if cprompt:
        lines.append(f"Profile instructions:\n{cprompt.strip()}")
    crules = ctx.get("custom_rules", [])
    if crules:
        rule_lines = []
        for cr in crules:
            rule_text = cr.get("rule", "")
            action = cr.get("action", "warn")
            trigger = cr.get("trigger", "all tools")
            if rule_text:
                rule_lines.append(f"- {rule_text} (enforcement: {action}, trigger: {trigger})")
        if rule_lines:
            lines.append("Custom enforcement rules:\n" + "\n".join(rule_lines))
    return "\n".join(lines)


def _format_rich_history(
    messages: list[dict[str, Any]],
    max_messages: int = 8,
) -> list[str]:
    """Format recent messages including tool calls and results."""
    lines: list[str] = []
    for msg in messages[-max_messages:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")

        if role == "user":
            text = _truncate(str(content), 300)
            if text.strip():
                lines.append(f"[user] {text}")

        elif role == "assistant":
            text = _truncate(str(content or ""), 200)
            if text.strip():
                lines.append(f"[assistant] {text}")
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls[:5]:
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args = fn.get("arguments", "")
                if isinstance(args, str):
                    args_preview = _truncate(args, 120)
                else:
                    args_preview = _truncate(json.dumps(args), 120)
                lines.append(f"  -> {name}({args_preview})")
            if len(tool_calls) > 5:
                lines.append(f"  -> ... +{len(tool_calls) - 5} more")

        elif role == "tool":
            text = _truncate(str(content or ""), 150)
            if text.strip():
                lines.append(f"  <- {text}")

        elif role == "system":
            text = _truncate(str(content or ""), 150)
            if text.strip() and "[BEHAVIOR" in text:
                lines.append(f"[system] {text}")

    return lines


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# ────────────────────────────────────────────────────────────────────
# Response parsing
# ────────────────────────────────────────────────────────────────────

def parse_classification(raw_text: str) -> dict[str, Any] | None:
    """Parse the classifier's JSON output. Fault-tolerant."""
    text = raw_text.strip()

    parsed = _try_parse_json(text)
    if parsed is not None:
        if parsed.get("skip_reason") and not parsed.get("directives"):
            logger.debug("behavior_classify: skipped — %s", parsed["skip_reason"])
            return None
        return parsed

    logger.warning("behavior_classify: failed to parse: %s", text[:200])
    return None


def _try_parse_json(text: str) -> dict[str, Any] | None:
    """Try multiple strategies to extract JSON from text."""
    # Direct parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(1))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # First { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None


# ────────────────────────────────────────────────────────────────────
# Directive formatting (data-driven)
# ────────────────────────────────────────────────────────────────────

def format_directive_message(
    classification: dict[str, Any],
    classifier_config: dict[str, Any] | None = None,
) -> str:
    """Format classification into a system message, using config for templates."""
    directives = classification.get("directives", [])
    if not directives:
        return ""

    complexity = classification.get("complexity", "moderate")
    approach = classification.get("approach", "direct")
    risk = classification.get("risk_level", "low")

    # Get approach label from structured config
    approach_label = get_approach_label(approach, classifier_config)

    # Build header from config template
    prefix_template = _get_cfg(classifier_config, "directive_prefix")
    try:
        prefix = prefix_template.format(
            complexity=complexity, approach=approach,
            risk=risk, approach_label=approach_label,
        )
    except (KeyError, IndexError):
        prefix = f"[BEHAVIOR DIRECTIVE — {complexity} complexity, {risk} risk]"

    lines = [prefix, f"Approach: {approach_label}", ""]

    # Cap directives
    max_d = _get_cfg(classifier_config, "max_directives") or 5
    for i, d in enumerate(directives[:max_d], 1):
        lines.append(f"{i}. {d}")

    # High risk warning — use entry names from structured list
    raw_risk_levels = _get_cfg(classifier_config, "risk_levels") or []
    risk_names = get_entry_names(raw_risk_levels)
    threshold = _get_cfg(classifier_config, "high_risk_threshold") or "medium"
    if threshold in risk_names and risk in risk_names:
        threshold_idx = risk_names.index(threshold)
        risk_idx = risk_names.index(risk)
        if risk_idx >= threshold_idx:
            warning_template = _get_cfg(classifier_config, "high_risk_warning") or ""
            if warning_template:
                lines.append("")
                try:
                    lines.append(warning_template.format(risk=risk))
                except (KeyError, IndexError):
                    lines.append(f"Risk level: {risk}. Confirm with user.")

    # Footer
    footer = _get_cfg(classifier_config, "directive_footer") or ""
    if footer:
        lines.append("")
        lines.append(footer)

    return "\n".join(lines)
