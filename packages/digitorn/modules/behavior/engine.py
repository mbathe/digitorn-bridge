"""BehaviorEngine - the runtime enforcement core.

Fully YAML-driven: rule definitions, state tracking, and conditions
all come from the config. No hardcoded tool names or logic.

Maintains per-session state and evaluates rules on every tool call.
"""

from __future__ import annotations

import logging
from typing import Any

from digitorn.modules.behavior.generic_rules import (
    DEFAULT_RULE_DEFINITIONS,
    DEFAULT_STATE_TRACKING,
    Violation,
    build_prompt_section,
    check_rules,
    update_state,
)
from digitorn.modules.behavior.profiles import resolve_profile
from digitorn.modules.behavior.state import BhvSessionState

logger = logging.getLogger(__name__)

# Map of boolean rule flags → rule IDs in DEFAULT_RULE_DEFINITIONS
_BOOL_TO_RULE_ID = {
    "read_before_edit": "read_before_edit",
    "read_before_write_existing": "read_before_write_existing",
    "search_before_read": "search_before_read",
    "no_bash_for_files": "no_bash_for_files",
    "no_blind_exploration": "no_blind_exploration",
    "confirm_destructive": "confirm_destructive",
    "plan_before_execute": "plan_before_execute",
    "verify_after_edit": "verify_after_edit",
    "test_after_changes": "test_after_changes",
    "delegate_complex": "delegate_complex",
    "delegate_large_reads": "delegate_large_reads",
    "web_search_when_unknown": "web_search_when_unknown",
    "always_lint_check": "always_lint_check",
    "max_sequential_same_tool": "max_sequential_same_tool",
}


class BehaviorEngine:
    """Stateful behavioral enforcement engine.

    One engine per app. Maintains per-session state via ``_sessions``.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        profile = config.get("profile")
        rule_overrides = config.get("rules", {})

        from pathlib import Path as _P
        _trace_path = _P.home() / ".digitorn" / "logs" / "engine_trace.log"
        try:
            with open(_trace_path, "a", encoding="utf-8") as _f:
                _f.write(f"\n=== engine_init ===\n")
                _f.write(f"profile type: {type(profile).__name__}\n")
                _f.write(f"profile preview: {str(profile)[:300]}\n")
                _f.write(f"rule_overrides: {rule_overrides}\n")
                _f.write(f"custom in config: {len(config.get('custom') or [])}\n")
                _f.write(f"rule_definitions in config: {len(config.get('rule_definitions') or [])}\n")
        except Exception:
            pass

        # Merge profile defaults with explicit overrides (backward compat)
        self._rules = resolve_profile(profile, rule_overrides)

        try:
            with open(_trace_path, "a", encoding="utf-8") as _f:
                _f.write(f"merged rules keys: {list(self._rules.keys())[:25]}\n")
                _f.write(f"merged rules.custom count: {len(self._rules.get('custom') or [])}\n")
        except Exception:
            pass

        # Build the active rule definitions
        self._rule_defs = self._build_rule_definitions(config)

        # Build state tracking config
        self._tracking = self._build_tracking_config(config)

        # Per-session state
        self._sessions: dict[str, BhvSessionState] = {}

        logger.info(
            "behavior_engine_init profile=%s rule_defs=%d tracking_sets=%d counters=%d flags=%d",
            profile or "none",
            len(self._rule_defs),
            len(self._tracking.get("sets", {})),
            len(self._tracking.get("counters", {})),
            len(self._tracking.get("flags", {})),
        )

    def _build_rule_definitions(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the final list of rule definitions from all sources."""
        defs: list[dict[str, Any]] = []

        # 1. Explicit rule_definitions from YAML (highest priority)
        explicit = config.get("rule_definitions", [])
        if explicit:
            for rd in explicit:
                if hasattr(rd, "model_dump"):
                    defs.append(rd.model_dump())
                elif isinstance(rd, dict):
                    defs.append(rd)

        # 2. Boolean flags from profile + overrides → select from defaults
        explicit_ids = {d["id"] for d in defs}
        merged_rules = self._rules

        for bool_key, rule_id in _BOOL_TO_RULE_ID.items():
            if rule_id in explicit_ids:
                continue  # explicit definition takes priority
            if merged_rules.get(bool_key):
                # Find the matching default rule definition
                for default_rule in DEFAULT_RULE_DEFINITIONS:
                    if default_rule["id"] == rule_id:
                        rule_copy = dict(default_rule)
                        # Apply threshold overrides
                        self._apply_thresholds(rule_copy, merged_rules)
                        defs.append(rule_copy)
                        break

        # 3. Legacy custom rules (old format → convert)
        for custom in merged_rules.get("custom", []):
            if isinstance(custom, dict):
                cid = custom.get("id", "custom")
                if cid not in explicit_ids and cid not in {d["id"] for d in defs}:
                    defs.append(self._convert_legacy_custom(custom))

        return defs

    def _apply_thresholds(self, rule: dict, merged_rules: dict) -> None:
        """Apply threshold overrides from the merged rules to a default rule."""
        condition = rule.get("condition", {})

        # search_before_read uses max_blind_reads
        if rule["id"] == "search_before_read":
            threshold = merged_rules.get("max_blind_reads", 3)
            if "all" in condition:
                for c in condition["all"]:
                    if "counter_gte" in c:
                        c["counter_gte"]["value"] = threshold

        # test_after_changes uses changes_before_test_reminder
        if rule["id"] == "test_after_changes":
            threshold = merged_rules.get("changes_before_test_reminder", 3)
            if "counter_gte" in condition:
                condition["counter_gte"]["value"] = threshold

        # max_sequential_same_tool
        if rule["id"] == "max_sequential_same_tool":
            threshold = merged_rules.get("max_sequential_same_tool", 8)
            if "consecutive_gte" in condition:
                condition["consecutive_gte"] = threshold

    def _convert_legacy_custom(self, custom: dict) -> dict[str, Any]:
        """Convert a legacy BehaviorCustomRule dict to the new format."""
        trigger = custom.get("trigger", "*")
        condition: dict[str, Any] = {}

        old_cond = custom.get("condition", {})
        if old_cond:
            param_name = old_cond.get("param", "")
            if "contains" in old_cond:
                condition = {"param_contains": {"param": param_name, "value": old_cond["contains"]}}
            elif "matches" in old_cond:
                condition = {"param_matches": {"param": param_name, "pattern": old_cond["matches"]}}
            elif "not_in" in old_cond:
                condition = {"target_not_in_set": old_cond["not_in"]}

        return {
            "id": custom.get("id", "custom"),
            "description": custom.get("rule", ""),
            "trigger": [trigger] if trigger else "*",
            "when": custom.get("enforce", "pre_tool"),
            "action": custom.get("action", "warn"),
            "condition": condition,
            "message": custom.get("message", custom.get("rule", "")),
        }

    def _build_tracking_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Build the state tracking config from YAML or defaults."""
        explicit = config.get("state_tracking")
        if explicit:
            if hasattr(explicit, "model_dump"):
                raw = explicit.model_dump()
            elif isinstance(explicit, dict):
                raw = explicit
            else:
                raw = {}
            # Convert Pydantic sub-models to dicts
            result: dict[str, Any] = {"sets": {}, "counters": {}, "flags": {}}
            for section in ("sets", "counters", "flags"):
                for name, cfg in raw.get(section, {}).items():
                    if hasattr(cfg, "model_dump"):
                        result[section][name] = cfg.model_dump()
                    elif isinstance(cfg, dict):
                        result[section][name] = cfg
            return result
        return dict(DEFAULT_STATE_TRACKING)

    @property
    def rules(self) -> dict:
        return self._rules

    @property
    def rule_definitions(self) -> list[dict[str, Any]]:
        return self._rule_defs

    @property
    def tracking(self) -> dict[str, Any]:
        return self._tracking

    def get_session(self, session_id: str) -> BhvSessionState:
        if session_id not in self._sessions:
            self._sessions[session_id] = BhvSessionState()
        return self._sessions[session_id]

    def cleanup_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def on_turn_start(self, session_id: str) -> None:
        state = self.get_session(session_id)
        state.on_new_turn()

    def on_agent_text(self, session_id: str, text: str) -> list[Violation]:
        state = self.get_session(session_id)
        if text and text.strip():
            state.plan_stated = True
        return check_rules(
            self._rule_defs, state, "*", {},
            when="on_text", agent_text=text, tracking=self._tracking,
        )

    def pre_tool(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        agent_text: str = "",
    ) -> list[Violation]:
        state = self.get_session(session_id)
        violations = check_rules(
            self._rule_defs, state, tool_name, params,
            when="pre_tool", agent_text=agent_text, tracking=self._tracking,
        )
        for v in violations:
            state.violations_count += 1
            state.last_violation = v.rule_id
            logger.info(
                "behavior_violation session=%s rule=%s level=%s tool=%s",
                session_id[:12], v.rule_id, v.level, tool_name,
            )
        return violations

    def post_tool(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        result: Any,
    ) -> list[Violation]:
        state = self.get_session(session_id)

        # Update state FIRST (tracking)
        update_state(state, tool_name, params, result, self._tracking)

        # Then check post-tool rules
        reminders = check_rules(
            self._rule_defs, state, tool_name, params,
            when="post_tool", result=result, tracking=self._tracking,
        )
        for v in reminders:
            if v.level in ("warn", "block"):
                state.violations_count += 1
            logger.debug(
                "behavior_reminder session=%s rule=%s tool=%s",
                session_id[:12], v.rule_id, tool_name,
            )
        return reminders

    def build_prompt_section(self) -> str:
        if not self._rule_defs:
            return ""
        return build_prompt_section(self._rule_defs)


# Backward-compat alias
BhvEngine = BehaviorEngine
