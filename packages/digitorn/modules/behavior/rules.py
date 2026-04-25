"""Built-in behavioral rules — the enforcement logic."""

from __future__ import annotations

import os
import re
from typing import Any

from digitorn.modules.behavior.state import BhvSessionState

# Tool name normalization — handle FQN, short, and double-underscore formats
_EDIT_TOOLS = frozenset({"edit", "filesystem.edit", "filesystem__edit"})
_READ_TOOLS = frozenset({"read", "filesystem.read", "filesystem__read"})
_WRITE_TOOLS = frozenset({"write", "filesystem.write", "filesystem__write"})
_GREP_TOOLS = frozenset({"grep", "filesystem.grep", "filesystem__grep"})
_GLOB_TOOLS = frozenset({"glob", "filesystem.glob", "filesystem__glob"})
_SEARCH_TOOLS = _GREP_TOOLS | _GLOB_TOOLS
_BASH_TOOLS = frozenset({"bash", "shell.bash", "shell__bash"})
_WEB_SEARCH_TOOLS = frozenset({"search", "web.search", "web__search"})
_AGENT_TOOLS = frozenset({"agent", "agent_spawn.agent", "agent_spawn__agent"})
_TEST_COMMANDS = re.compile(
    r"\b(pytest|py\.test|npm\s+test|npx\s+jest|cargo\s+test|go\s+test|make\s+test|"
    r"python\s+-m\s+pytest|node\s+--test|vitest|mocha|rspec|phpunit)\b",
    re.IGNORECASE,
)
_FILE_OP_COMMANDS = re.compile(
    r"\b(cat|head|tail|less|more|bat)\s|"
    r"\bsed\s+-|sed\s+'|\bawk\s|perl\s+-[pi]",
    re.IGNORECASE,
)
_BLIND_EXPLORE_COMMANDS = re.compile(
    r"(find\s+\.|ls\s+-[lRa]|ls\s+\.|tree\b|dir\s+/[sS])",
    re.IGNORECASE,
)
_DESTRUCTIVE_COMMANDS = re.compile(
    r"(rm\s+-rf|git\s+reset\s+--hard|git\s+push\s+--force|git\s+push\s+-f\b|"
    r"drop\s+table|drop\s+database|truncate\s+table|git\s+clean\s+-fd)",
    re.IGNORECASE,
)
# Uncertainty about FACTS (not opinions). We look for uncertainty + technical context.
# "I think we should use React" = opinion (OK)
# "I think the API returns JSON" = uncertainty about a fact (should search)
# Pattern: uncertainty phrase followed by technical terms
_UNCERTAIN_FACT_PHRASES = re.compile(
    r"\b(not sure|I'?m unsure|don'?t know|uncertain|can'?t remember|"
    r"I forget|not certain|no idea)\b"
    r".*\b(how|what|which|where|whether|if it|the API|the function|"
    r"the syntax|the command|the method|the parameter|the flag|the option)\b",
    re.IGNORECASE | re.DOTALL,
)
# Stronger uncertainty: explicit hedging about correctness
_STRONG_UNCERTAINTY = re.compile(
    r"\b(might be wrong|could be incorrect|not 100% sure|"
    r"I may be mistaken|this might not work|not entirely sure)\b",
    re.IGNORECASE,
)


def _resolve_path(params: dict) -> str:
    """Extract file path from tool params."""
    return params.get("file_path") or params.get("path") or params.get("filepath") or ""


def _bare(tool_name: str) -> str:
    """Get bare action name: 'filesystem.edit' → 'edit'."""
    return tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name


# ── Violation result ────────────────────────────────────────────────

class BhvViolation:
    """A detected rule violation or reminder."""

    __slots__ = ("rule_id", "level", "message")

    def __init__(self, rule_id: str, level: str, message: str) -> None:
        self.rule_id = rule_id
        self.level = level      # "block" | "warn" | "remind"
        self.message = message

    def format(self) -> str:
        if self.level == "block":
            return (
                f"[BEHAVIOR BLOCKED] {self.message}\n"
                f"Rule: {self.rule_id}\n"
                f"The tool call was NOT executed. Fix the violation first."
            )
        elif self.level == "warn":
            return (
                f"[BEHAVIOR WARNING] {self.message}\n"
                f"Rule: {self.rule_id}"
            )
        else:
            return (
                f"[BEHAVIOR REMINDER] {self.message}\n"
                f"Rule: {self.rule_id}"
            )


# Backward-compat aliases (linter renames these — export all variants)
Violation = BhvViolation
BehaviorViolation = BhvViolation  # noqa: F841


# ── Built-in rule checks ───────────────────────────────────────────

def check_pre_tool(
    rules: dict,
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    agent_text: str = "",
) -> list[Violation]:
    """Check all rules BEFORE a tool executes. Returns violations."""
    violations: list[Violation] = []
    tn = tool_name.lower()

    # ── read_before_edit ──
    if rules.get("read_before_edit") and tn in _EDIT_TOOLS:
        fp = _resolve_path(params)
        if fp and fp not in state.read_files:
            violations.append(Violation(
                "read_before_edit", "warn",
                f"You are editing '{fp}' without reading it first. "
                f"Call Read('{fp}') before editing — you need to see the current content.",
            ))

    # ── read_before_write_existing ──
    if rules.get("read_before_write_existing") and tn in _WRITE_TOOLS:
        fp = _resolve_path(params)
        if fp and fp not in state.read_files and os.path.exists(fp):
            violations.append(Violation(
                "read_before_write_existing", "warn",
                f"'{fp}' already exists and you haven't read it. "
                f"Read it first to understand the current content, or use Edit for surgical changes.",
            ))

    # ── search_before_read ──
    max_blind = rules.get("max_blind_reads", 3)
    if rules.get("search_before_read") and tn in _READ_TOOLS:
        fp = _resolve_path(params)
        if fp and fp not in state.read_files and state.reads_since_search >= max_blind:
            violations.append(Violation(
                "search_before_read", "warn",
                f"You have read {state.reads_since_search} files without searching first. "
                f"Use Grep or Glob to find what you need, then Read the specific section.",
            ))

    # ── no_bash_for_files ──
    if rules.get("no_bash_for_files") and tn in _BASH_TOOLS:
        command = params.get("command", "")
        if _FILE_OP_COMMANDS.search(command):
            violations.append(Violation(
                "no_bash_for_files", "warn",
                f"Don't use Bash for file operations. Command: '{command[:80]}'. "
                f"Use Read/Edit/Write/Grep/Glob instead — they are faster and tracked.",
            ))

    # ── no_blind_exploration ──
    if rules.get("no_blind_exploration") and tn in _BASH_TOOLS:
        command = params.get("command", "")
        if _BLIND_EXPLORE_COMMANDS.search(command):
            violations.append(Violation(
                "no_blind_exploration", "warn",
                f"Don't use Bash to explore the filesystem. Command: '{command[:80]}'. "
                f"Use Glob('**/*.py') to see structure or Grep('pattern') to find content.",
            ))

    # ── confirm_destructive ──
    if rules.get("confirm_destructive") and tn in _BASH_TOOLS:
        command = params.get("command", "")
        if _DESTRUCTIVE_COMMANDS.search(command):
            violations.append(Violation(
                "confirm_destructive", "block",
                f"Destructive command detected: '{command[:100]}'. "
                f"Ask the user for confirmation before executing.",
            ))

    # ── confirm_complex_plans ──
    threshold = rules.get("changes_before_test_reminder", 3)
    if rules.get("confirm_complex_plans") and tn in (_EDIT_TOOLS | _WRITE_TOOLS):
        if state.changes_since_test >= threshold * 2 and not state.plan_stated:
            violations.append(Violation(
                "confirm_complex_plans", "warn",
                f"You have made {state.changes_since_test} changes without presenting a plan. "
                f"Explain your approach to the user and get validation before continuing.",
            ))

    # ── plan_before_execute ──
    if rules.get("plan_before_execute") and state.tool_calls_this_turn == 0:
        if not state.plan_stated and not agent_text.strip():
            violations.append(Violation(
                "plan_before_execute", "warn",
                "State your plan in text before calling tools. "
                "The user cannot see tool parameters — explain what you're about to do.",
            ))

    # ── max_sequential_same_tool ──
    max_seq = rules.get("max_sequential_same_tool", 8)
    # +1 because post_tool hasn't incremented yet for this call
    upcoming_count = (state.consecutive_same_tool + 1) if tool_name.lower() == state.last_tool_name.lower() else 1
    if max_seq and upcoming_count >= max_seq:
        bare = _bare(tool_name)
        violations.append(Violation(
            "max_sequential_same_tool", "warn",
            f"You have called '{bare}' {state.consecutive_same_tool} times in a row. "
            f"Step back — parallelize calls or try a different approach.",
        ))

    # ── Custom rules (pre_tool) ──
    for custom in rules.get("custom", []):
        if custom.get("enforce") != "pre_tool":
            continue
        trigger = custom.get("trigger", "")
        if trigger and _bare(tool_name) != trigger and tool_name != trigger:
            continue
        if _check_custom_condition(custom.get("condition", {}), params, state):
            action = custom.get("action", "warn")
            violations.append(Violation(
                custom.get("id", "custom"),
                action,
                custom.get("message", custom.get("rule", "")),
            ))

    return violations


def check_post_tool(
    rules: dict,
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    result: Any,
) -> list[Violation]:
    """Check rules AFTER a tool executes. Returns reminders."""
    reminders: list[Violation] = []
    tn = tool_name.lower()
    fp = _resolve_path(params)

    # ── Update state ──
    state.on_tool_call(tool_name)

    if tn in _READ_TOOLS and fp:
        state.on_read(fp)
    elif tn in _EDIT_TOOLS and fp:
        state.on_edit(fp)
    elif tn in _WRITE_TOOLS and fp:
        state.on_write(fp)
    elif tn in _SEARCH_TOOLS:
        pattern = params.get("pattern", "")
        state.on_search(pattern)
    elif tn in _WEB_SEARCH_TOOLS:
        state.on_web_search()
    elif tn in _BASH_TOOLS:
        command = params.get("command", "")
        if _TEST_COMMANDS.search(command):
            state.on_test_run()

    # ── verify_after_edit ──
    if rules.get("verify_after_edit") and tn in _EDIT_TOOLS and fp:
        reminders.append(Violation(
            "verify_after_edit", "remind",
            f"You just edited '{fp}'. Read the modified section to verify your changes are correct.",
        ))

    # ── test_after_changes ──
    threshold = rules.get("changes_before_test_reminder", 3)
    if rules.get("test_after_changes") and tn in (_EDIT_TOOLS | _WRITE_TOOLS):
        if state.changes_since_test >= threshold:
            reminders.append(Violation(
                "test_after_changes", "remind",
                f"You have made {state.changes_since_test} changes since the last test run. "
                f"Run tests now to catch regressions early.",
            ))

    # ── always_lint_check ──
    if rules.get("always_lint_check") and tn in (_EDIT_TOOLS | _WRITE_TOOLS):
        lint = None
        if isinstance(result, dict):
            lint = result.get("lint") or result.get("data", {}).get("lint")
        elif hasattr(result, "data") and isinstance(result.data, dict):
            lint = result.data.get("lint")
        if lint and isinstance(lint, list):
            errors = [d for d in lint if isinstance(d, dict) and d.get("severity") == "error"]
            if errors:
                reminders.append(Violation(
                    "always_lint_check", "warn",
                    f"Lint found {len(errors)} error(s) in your last change. Fix them before moving on.",
                ))

    # ── delegate_complex ──
    if rules.get("delegate_complex") and state.tool_calls_this_turn >= 8:
        # Only remind once per turn (check if we already reminded)
        if state.tool_calls_this_turn == 8:
            reminders.append(Violation(
                "delegate_complex", "remind",
                f"You have made {state.tool_calls_this_turn} tool calls in this turn. "
                f"Consider delegating remaining sub-tasks to sub-agents — "
                f"they run in parallel and don't consume your context.",
            ))

    # ── delegate_large_reads ──
    if rules.get("delegate_large_reads") and tn in _READ_TOOLS:
        if state.reads_since_search >= 5:
            reminders.append(Violation(
                "delegate_large_reads", "remind",
                "You're reading many files sequentially. "
                "Consider delegating bulk exploration to a sub-agent to protect your context.",
            ))

    # ── Custom rules (post_tool) ──
    for custom in rules.get("custom", []):
        if custom.get("enforce") != "post_tool":
            continue
        trigger = custom.get("trigger", "")
        if trigger and _bare(tool_name) != trigger and tool_name != trigger:
            continue
        if _check_custom_condition(custom.get("condition", {}), params, state):
            reminders.append(Violation(
                custom.get("id", "custom"),
                custom.get("action", "remind"),
                custom.get("message", custom.get("rule", "")),
            ))

    return reminders


def check_agent_text(
    rules: dict,
    state: BhvSessionState,
    text: str,
) -> list[Violation]:
    """Check rules on the agent's text output (before tool calls)."""
    violations: list[Violation] = []

    if text and text.strip():
        state.plan_stated = True

    # ── web_search_when_unknown ──
    if rules.get("web_search_when_unknown") and text:
        is_uncertain = (
            _UNCERTAIN_FACT_PHRASES.search(text)
            or _STRONG_UNCERTAINTY.search(text)
        )
        if is_uncertain and not state.has_web_searched:
            violations.append(Violation(
                "web_search_when_unknown", "warn",
                "You expressed uncertainty. Search the web instead of guessing — "
                "use WebSearch to find the accurate answer.",
            ))

    return violations


# ── Custom condition evaluator ──────────────────────────────────────

def _check_custom_condition(
    condition: dict,
    params: dict[str, Any],
    state: BhvSessionState,
) -> bool:
    """Evaluate a custom rule condition."""
    if not condition:
        return True  # No condition = always matches

    param_name = condition.get("param", "")
    param_value = str(params.get(param_name, "")) if param_name else ""

    # contains check
    if "contains" in condition:
        return condition["contains"].lower() in param_value.lower()

    # matches check (regex)
    if "matches" in condition:
        try:
            return bool(re.search(condition["matches"], param_value))
        except re.error:
            return False

    # not_in state check
    if "not_in" in condition:
        state_field = condition["not_in"]
        state_set = getattr(state, state_field, set())
        return param_value not in state_set

    return True
