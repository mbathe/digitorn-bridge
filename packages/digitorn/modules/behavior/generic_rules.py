"""Generic YAML-driven rule evaluator + state tracker."""

from __future__ import annotations

import re
import logging
from typing import Any

from digitorn.modules.behavior.state import BhvSessionState

logger = logging.getLogger(__name__)

DEFAULT_STATE_TRACKING: dict[str, Any] = {
    "sets": {
        "read_files": {
            "add_on": ["read", "filesystem.read", "filesystem__read"],
            "target": "file_path",
            "aliases": ["path", "filepath"],
        },
        "edited_files": {
            "add_on": ["edit", "filesystem.edit", "filesystem__edit"],
            "target": "file_path",
            "aliases": ["path", "filepath"],
        },
        "written_files": {
            "add_on": ["write", "filesystem.write", "filesystem__write"],
            "target": "file_path",
            "aliases": ["path", "filepath"],
        },
        "searched_patterns": {
            "add_on": ["grep", "glob", "filesystem.grep", "filesystem.glob",
                        "filesystem__grep", "filesystem__glob"],
            "target": "pattern",
        },
    },
    "counters": {
        "reads_since_search": {
            "increment_on": ["read", "filesystem.read", "filesystem__read"],
            "reset_on": ["grep", "glob", "filesystem.grep", "filesystem.glob",
                         "filesystem__grep", "filesystem__glob"],
        },
        "changes_since_test": {
            "increment_on": ["edit", "write", "filesystem.edit", "filesystem.write",
                             "filesystem__edit", "filesystem__write"],
            "reset_on": [],
            "reset_when": {
                "tool": "bash,shell.bash,shell__bash",
                "param": "command",
                "matches": r"\b(pytest|py\.test|npm\s+test|npx\s+jest|cargo\s+test|go\s+test|"
                           r"make\s+test|python\s+-m\s+pytest|vitest|mocha|rspec|phpunit)\b",
            },
        },
    },
    "flags": {
        "has_web_searched": {
            "set_on": ["search", "web.search", "web__search"],
        },
    },
}

DEFAULT_RULE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "read_before_edit",
        "description": "ALWAYS Read a file before Edit - edit on unread files is flagged",
        "trigger": ["edit", "filesystem.edit", "filesystem__edit"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"target_not_in_set": "read_files"},
        "message": "You are editing '{target}' without reading it first. Read it before editing.",
    },
    {
        "id": "read_before_write_existing",
        "description": "Read existing files before Write - avoids losing content",
        "trigger": ["write", "filesystem.write", "filesystem__write"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"all": [
            {"target_not_in_set": "read_files"},
            {"target_exists_on_disk": True},
        ]},
        "message": "'{target}' already exists. Read it first or use Edit for changes.",
    },
    {
        "id": "search_before_read",
        "description": "Grep/Glob before Read - don't read files blindly",
        "trigger": ["read", "filesystem.read", "filesystem__read"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"all": [
            {"target_not_in_set": "read_files"},
            {"counter_gte": {"name": "reads_since_search", "value": 3}},
        ]},
        "message": "You have read {counter:reads_since_search} files without searching. Use Grep or Glob first.",
    },
    {
        "id": "no_bash_for_files",
        "description": "NEVER Bash for file ops (cat/sed/find) - use Read/Edit/Grep/Glob",
        "trigger": ["bash", "shell.bash", "shell__bash"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"param_matches": {
            "param": "command",
            "pattern": r"\b(cat|head|tail|less|more|bat)\s|"
                       r"\bsed\s+-|sed\s+'|\bawk\s|perl\s+-[pi]",
        }},
        "message": "Don't use Bash for file operations: '{param:command}'. Use Read/Edit instead.",
    },
    {
        "id": "no_blind_exploration",
        "description": "NEVER Bash to explore (find/ls -la/tree) - use Glob",
        "trigger": ["bash", "shell.bash", "shell__bash"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"param_matches": {
            "param": "command",
            "pattern": r"(find\s+\.|ls\s+-[lRa]|ls\s+\.|tree\b|dir\s+/[sS])",
        }},
        "message": "Don't use Bash to explore. Use Glob for structure, Grep for content.",
    },
    {
        "id": "confirm_destructive",
        "description": "Destructive commands are BLOCKED - ask user first",
        "trigger": ["bash", "shell.bash", "shell__bash"],
        "when": "pre_tool",
        "action": "block",
        "condition": {"param_matches": {
            "param": "command",
            "pattern": r"(rm\s+-rf|git\s+reset\s+--hard|git\s+push\s+--force|git\s+push\s+-f\b|"
                       r"drop\s+table|drop\s+database|truncate\s+table|git\s+clean\s+-fd)",
        }},
        "message": "Destructive command detected: '{param:command}'. Ask user confirmation first.",
    },
    {
        "id": "plan_before_execute",
        "description": "State your plan in text before tools",
        "trigger": "*",
        "when": "pre_tool",
        "action": "warn",
        "condition": {"all": [
            {"no_text_before_tools": True},
            {"first_tool_this_turn": True},
        ]},
        "message": "State your plan before calling tools. The user cannot see tool parameters.",
    },
    {
        "id": "verify_after_edit",
        "description": "Re-read the modified section after Edit",
        "trigger": ["edit", "filesystem.edit", "filesystem__edit"],
        "when": "post_tool",
        "action": "remind",
        "condition": {},
        "message": "You just edited '{target}'. Read back the modified section to verify.",
    },
    {
        "id": "test_after_changes",
        "description": "Run tests after N changes",
        "trigger": ["edit", "write", "filesystem.edit", "filesystem.write",
                     "filesystem__edit", "filesystem__write"],
        "when": "post_tool",
        "action": "remind",
        "condition": {"counter_gte": {"name": "changes_since_test", "value": 3}},
        "message": "You have made {counter:changes_since_test} changes since last test. Run tests now.",
    },
    {
        "id": "delegate_complex",
        "description": "Delegate to sub-agents when too many tool calls in one turn",
        "trigger": "*",
        "when": "post_tool",
        "action": "remind",
        "condition": {"tool_calls_this_turn_eq": 8},
        "message": "You have made {tool_calls_this_turn} tool calls this turn. Delegate to sub-agents.",
    },
    {
        "id": "delegate_large_reads",
        "description": "Delegate bulk reading to sub-agents",
        "trigger": ["read", "filesystem.read", "filesystem__read"],
        "when": "post_tool",
        "action": "remind",
        "condition": {"counter_gte": {"name": "reads_since_search", "value": 5}},
        "message": "You're reading many files sequentially. Delegate bulk exploration to a sub-agent.",
    },
    {
        "id": "web_search_when_unknown",
        "description": "Search the web when expressing uncertainty",
        "trigger": "*",
        "when": "on_text",
        "action": "warn",
        "condition": {"all": [
            {"text_matches": r"\b(not sure|I'm unsure|don't know|uncertain|can't remember)\b"},
            {"flag_is": {"name": "has_web_searched", "value": False}},
        ]},
        "message": "You expressed uncertainty. Search the web instead of guessing.",
    },
    {
        "id": "always_lint_check",
        "description": "Check lint after Edit/Write",
        "trigger": ["edit", "write", "filesystem.edit", "filesystem.write",
                     "filesystem__edit", "filesystem__write"],
        "when": "post_tool",
        "action": "warn",
        "condition": {"result_has_lint_errors": True},
        "message": "Lint found errors in your last change. Fix them before moving on.",
    },
    {
        "id": "max_sequential_same_tool",
        "description": "Don't repeat the same tool too many times",
        "trigger": "*",
        "when": "pre_tool",
        "action": "warn",
        "condition": {"consecutive_gte": 8},
        "message": "You have called '{tool}' {consecutive_same_tool} times in a row. Try a different approach.",
    },
]

def _bare(tool_name: str) -> str:
    return tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name

def _tool_matches(tool_name: str, triggers: list[str] | str) -> bool:
    if triggers == "*" or triggers == ["*"]:
        return True
    if isinstance(triggers, str):
        triggers = [triggers]
    tn = tool_name.lower()
    bare = _bare(tn)
    for t in triggers:
        tl = t.lower()
        if tn == tl or bare == tl or bare == _bare(tl):
            return True
    return False

def _extract_target(params: dict[str, Any], tracking_cfg: dict | None = None) -> str:
    # If tracking config specifies which param, use that
    if tracking_cfg:
        target_param = tracking_cfg.get("target", "file_path")
        aliases = tracking_cfg.get("aliases", [])
        val = params.get(target_param)
        if val:
            return str(val)
        for alias in aliases:
            val = params.get(alias)
            if val:
                return str(val)
    # Default: try common param names
    for key in ("file_path", "path", "filepath", "url", "query", "pattern", "target"):
        val = params.get(key)
        if val:
            return str(val)
    return ""

def update_state(
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    result: Any,
    tracking: dict[str, Any],
) -> None:
    """Update session state based on tracking config after a tool executes."""
    tn = tool_name.lower()
    bare = _bare(tn)

    state.on_tool_call(tool_name, _extract_target(params))

    # Sets
    for set_name, cfg in tracking.get("sets", {}).items():
        add_on = [t.lower() for t in cfg.get("add_on", [])]
        if tn in add_on or bare in add_on or any(bare == _bare(t) for t in add_on):
            target = _extract_target(params, cfg)
            if target:
                state.add_to_set(set_name, target)

    # Counters
    for counter_name, cfg in tracking.get("counters", {}).items():
        inc_on = [t.lower() for t in cfg.get("increment_on", [])]
        reset_on = [t.lower() for t in cfg.get("reset_on", [])]

        if tn in inc_on or bare in inc_on or any(bare == _bare(t) for t in inc_on):
            state.increment_counter(counter_name)

        if tn in reset_on or bare in reset_on or any(bare == _bare(t) for t in reset_on):
            state.reset_counter(counter_name)

        # reset_when: conditional reset based on param match
        rw = cfg.get("reset_when", {})
        if rw:
            rw_tools = [t.strip().lower() for t in rw.get("tool", "").split(",")]
            if tn in rw_tools or bare in rw_tools or any(bare == _bare(t) for t in rw_tools):
                param_val = str(params.get(rw.get("param", ""), ""))
                pattern = rw.get("matches", "")
                if pattern and re.search(pattern, param_val, re.IGNORECASE):
                    state.reset_counter(counter_name)

    # Flags
    for flag_name, cfg in tracking.get("flags", {}).items():
        set_on = [t.lower() for t in cfg.get("set_on", [])]
        unset_on = [t.lower() for t in cfg.get("unset_on", [])]

        if tn in set_on or bare in set_on or any(bare == _bare(t) for t in set_on):
            state.set_flag(flag_name, True)
        if tn in unset_on or bare in unset_on or any(bare == _bare(t) for t in unset_on):
            state.set_flag(flag_name, False)

def evaluate_condition(
    condition: dict[str, Any],
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    result: Any = None,
    agent_text: str = "",
    tracking: dict[str, Any] | None = None,
) -> bool:
    """Evaluate a rule condition. Returns True if the rule should fire."""
    if not condition:
        return True  # No condition = always fires

    if "target_not_in_set" in condition:
        set_name = condition["target_not_in_set"]
        set_cfg = (tracking or {}).get("sets", {}).get(set_name, {})
        target = _extract_target(params, set_cfg)
        return bool(target) and target not in state.get_set(set_name)

    if "target_in_set" in condition:
        set_name = condition["target_in_set"]
        set_cfg = (tracking or {}).get("sets", {}).get(set_name, {})
        target = _extract_target(params, set_cfg)
        return bool(target) and target in state.get_set(set_name)

    if "counter_gte" in condition:
        cfg = condition["counter_gte"]
        name = cfg.get("name", "")
        value = cfg.get("value", 0)
        return state.get_counter(name) >= value

    if "param_matches" in condition:
        cfg = condition["param_matches"]
        param_name = cfg.get("param", "")
        pattern = cfg.get("pattern", "")
        param_val = str(params.get(param_name, ""))
        if not pattern or not param_val:
            return False
        try:
            return bool(re.search(pattern, param_val, re.IGNORECASE))
        except re.error:
            return False

    if "param_contains" in condition:
        cfg = condition["param_contains"]
        param_name = cfg.get("param", "")
        value = cfg.get("value", "")
        param_val = str(params.get(param_name, ""))
        return value.lower() in param_val.lower()

    if "flag_is" in condition:
        cfg = condition["flag_is"]
        name = cfg.get("name", "")
        value = cfg.get("value", True)
        return state.get_flag(name) == value

    if condition.get("no_text_before_tools"):
        return not state.plan_stated

    if condition.get("first_tool_this_turn"):
        return state.tool_calls_this_turn == 0

    if "consecutive_gte" in condition:
        threshold = condition["consecutive_gte"]
        upcoming = (state.consecutive_same_tool + 1) if tool_name.lower() == state.last_tool_name.lower() else 1
        return upcoming >= threshold

    if "tool_calls_this_turn_eq" in condition:
        return state.tool_calls_this_turn == condition["tool_calls_this_turn_eq"]

    if condition.get("target_exists_on_disk"):
        import os
        target = _extract_target(params, None)
        return bool(target) and os.path.exists(target)

    if "text_matches" in condition:
        pattern = condition["text_matches"]
        try:
            return bool(re.search(pattern, agent_text, re.IGNORECASE))
        except re.error:
            return False

    if condition.get("result_has_lint_errors"):
        lint = None
        if isinstance(result, dict):
            lint = result.get("lint") or result.get("data", {}).get("lint")
        elif hasattr(result, "data") and isinstance(getattr(result, "data", None), dict):
            lint = result.data.get("lint")
        if lint and isinstance(lint, list):
            return any(isinstance(d, dict) and d.get("severity") == "error" for d in lint)
        return False

    if "all" in condition:
        return all(
            evaluate_condition(c, state, tool_name, params, result, agent_text, tracking)
            for c in condition["all"]
        )
    if "any" in condition:
        return any(
            evaluate_condition(c, state, tool_name, params, result, agent_text, tracking)
            for c in condition["any"]
        )
    if "not" in condition:
        return not evaluate_condition(
            condition["not"], state, tool_name, params, result, agent_text, tracking,
        )

    # Unknown condition - don't fire
    logger.warning("behavior: unknown condition type: %s", list(condition.keys()))
    return False

def render_message(
    template: str,
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    tracking: dict[str, Any] | None = None,
) -> str:
    """Render a message template with placeholders."""
    if not template:
        return ""

    target = _extract_target(params, None)
    msg = template.replace("{target}", target)
    msg = msg.replace("{tool}", _bare(tool_name))
    msg = msg.replace("{turn}", str(state.turn))
    msg = msg.replace("{tool_calls_this_turn}", str(state.tool_calls_this_turn))
    msg = msg.replace("{consecutive_same_tool}", str(state.consecutive_same_tool))

    # {param:X} - any param value
    for match in re.finditer(r"\{param:(\w+)\}", msg):
        pname = match.group(1)
        pval = str(params.get(pname, ""))
        if len(pval) > 100:
            pval = pval[:97] + "..."
        msg = msg.replace(match.group(0), pval)

    # {counter:X} - counter value
    for match in re.finditer(r"\{counter:(\w+)\}", msg):
        cname = match.group(1)
        msg = msg.replace(match.group(0), str(state.get_counter(cname)))

    # {set_count:X} - set size
    for match in re.finditer(r"\{set_count:(\w+)\}", msg):
        sname = match.group(1)
        msg = msg.replace(match.group(0), str(len(state.get_set(sname))))

    # {flag:X} - flag value
    for match in re.finditer(r"\{flag:(\w+)\}", msg):
        fname = match.group(1)
        msg = msg.replace(match.group(0), str(state.get_flag(fname)))

    return msg

class Violation:
    """A detected rule violation."""
    __slots__ = ("rule_id", "level", "message")

    def __init__(self, rule_id: str, level: str, message: str) -> None:
        self.rule_id = rule_id
        self.level = level
        self.message = message

    def format(self) -> str:
        if self.level == "block":
            return (
                f"[BEHAVIOR BLOCKED] {self.message}\n"
                f"Rule: {self.rule_id}\n"
                f"The tool call was NOT executed. Fix the violation first."
            )
        elif self.level == "warn":
            return f"[BEHAVIOR WARNING] {self.message}\nRule: {self.rule_id}"
        else:
            return f"[BEHAVIOR REMINDER] {self.message}\nRule: {self.rule_id}"

def check_rules(
    rule_defs: list[dict[str, Any]],
    state: BhvSessionState,
    tool_name: str,
    params: dict[str, Any],
    *,
    when: str,
    result: Any = None,
    agent_text: str = "",
    tracking: dict[str, Any] | None = None,
) -> list[Violation]:
    """Evaluate all rules for a given phase (pre_tool/post_tool/on_text)."""
    violations: list[Violation] = []

    for rule in rule_defs:
        if rule.get("when", "pre_tool") != when:
            continue

        trigger = rule.get("trigger", "*")
        if not _tool_matches(tool_name, trigger):
            continue

        condition = rule.get("condition", {})
        if evaluate_condition(condition, state, tool_name, params, result, agent_text, tracking):
            msg = render_message(
                rule.get("message", rule.get("description", "")),
                state, tool_name, params, tracking,
            )
            violations.append(Violation(
                rule_id=rule.get("id", "custom"),
                level=rule.get("action", "warn"),
                message=msg,
            ))

    return violations

def build_prompt_section(rule_defs: list[dict[str, Any]]) -> str:
    """Build a compact prompt section from rule definitions."""
    lines = [
        "The following rules are ACTIVELY ENFORCED at runtime. "
        "Violations are detected and signaled immediately.",
        "",
    ]

    idx = 1
    for rule in rule_defs:
        desc = rule.get("description", "")
        if not desc:
            desc = rule.get("message", rule.get("id", ""))
        action = rule.get("action", "warn")
        tag = "BLOCKED" if action == "block" else "enforced"
        lines.append(f"{idx}. {desc} ({tag})")
        idx += 1

    return "\n".join(lines)
