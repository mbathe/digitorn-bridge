"""Deep static validator for behavior config.

Goal: if the compiler accepts a YAML, the behavior engine is guaranteed
to run without runtime errors. Every reference is verified, every type
is checked, every regex compiles, every enum is valid.

Errors include structured (path, message) so the compiler can map them
to file:line locations using a SourceMap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ── Valid enum values ──
_VALID_WHEN = {"pre_tool", "post_tool", "on_text"}
_VALID_ACTION = {"block", "warn", "remind"}
_VALID_FREQUENCY = {"every_turn", "first_turn", "every_n_turns", "on_new_message"}
_VALID_PROFILES = {"dev", "coding", "research", "data", "creative", "assistant"}

# Boolean rule names accepted in the legacy `rules:` block
_LEGACY_BOOLEAN_RULES = {
    "read_before_edit", "read_before_write_existing", "search_before_read",
    "test_after_changes", "verify_after_edit", "plan_before_execute",
    "confirm_complex_plans", "confirm_destructive", "delegate_complex",
    "delegate_large_reads", "web_search_when_unknown", "always_lint_check",
    "no_bash_for_files", "no_blind_exploration",
}
_LEGACY_INT_RULES = {
    "max_blind_reads", "changes_before_test_reminder", "max_sequential_same_tool",
}
_LEGACY_STR_RULES = {"verbosity", "autonomy"}

# Condition keys with expected value shape
_CONDITION_KEYS = {
    "target_not_in_set": ("str", "set name"),
    "target_in_set": ("str", "set name"),
    "counter_gte": ("dict", "{name: str, value: int}"),
    "param_matches": ("dict", "{param: str, pattern: str}"),
    "param_contains": ("dict", "{param: str, value: str}"),
    "flag_is": ("dict", "{name: str, value: bool}"),
    "consecutive_gte": ("int", "positive int"),
    "tool_calls_this_turn_eq": ("int", "int"),
    "no_text_before_tools": ("bool", "true/false"),
    "first_tool_this_turn": ("bool", "true/false"),
    "target_exists_on_disk": ("bool", "true/false"),
    "result_has_lint_errors": ("bool", "true/false"),
    "text_matches": ("str", "regex pattern"),
    "all": ("list", "list of conditions"),
    "any": ("list", "list of conditions"),
    "not": ("dict", "a single condition"),
}

_UNIVERSAL_PLACEHOLDERS = {
    "target", "tool", "turn",
    "tool_calls_this_turn", "consecutive_same_tool",
}
_DIRECTIVE_PREFIX_VARS = {"complexity", "approach", "risk", "approach_label"}
_HIGH_RISK_WARNING_VARS = {"risk", "complexity"}

_TYPED_PLACEHOLDER_RE = re.compile(r"\{(param|counter|set_count|flag):(\w+)\}")
_SIMPLE_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass
class ValidationError:
    """Structured error with path for file:line lookup."""
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


def validate_behavior_config(
    config: dict[str, Any],
    *,
    known_tools: set[str] | None = None,
) -> list[str]:
    """Validate a behavior config. Returns list of error strings.

    For structured errors with path info, use validate_behavior_config_structured.
    """
    return [str(e) for e in validate_behavior_config_structured(config, known_tools=known_tools)]


def validate_behavior_config_structured(
    config: dict[str, Any],
    *,
    known_tools: set[str] | None = None,
) -> list[ValidationError]:
    """Validate a behavior config. Returns structured errors.

    Args:
        config: The ``behavior:`` block (raw dict or Pydantic-dumped).
        known_tools: Optional set of tool names the app has access to.
    """
    errors: list[ValidationError] = []

    if hasattr(config, "model_dump"):
        config = config.model_dump()

    tracking = _normalize_tracking(config.get("state_tracking"))
    known_sets = set(tracking.get("sets", {}).keys())
    known_counters = set(tracking.get("counters", {}).keys())
    known_flags = set(tracking.get("flags", {}).keys())

    # Merge built-in defaults when state_tracking is empty
    if not (known_sets | known_counters | known_flags):
        try:
            from digitorn.modules.behavior.generic_rules import DEFAULT_STATE_TRACKING
            known_sets |= set(DEFAULT_STATE_TRACKING["sets"].keys())
            known_counters |= set(DEFAULT_STATE_TRACKING["counters"].keys())
            known_flags |= set(DEFAULT_STATE_TRACKING["flags"].keys())
        except ImportError:
            pass

    tool_short: set[str] = set()
    tool_fqn: set[str] = set()
    if known_tools:
        for t in known_tools:
            tool_fqn.add(t.lower())
            short = t.rsplit(".", 1)[-1].lower() if "." in t else t.lower()
            tool_short.add(short)
    check_tools = known_tools is not None

    # ── 0. Profile name (top-level behavior.profile) ──
    profile = config.get("profile")
    if profile is not None:
        if not isinstance(profile, str):
            errors.append(_E("profile", f"expected str or null, got {type(profile).__name__}"))
        elif profile and not profile.startswith("{"):
            # Not a resolved {{behavior.X}} (which becomes JSON) - must be a builtin name
            if profile not in _VALID_PROFILES:
                errors.append(_E(
                    "profile",
                    f"unknown profile name {profile!r}. "
                    f"Built-in: {sorted(_VALID_PROFILES)}. "
                    f"For custom profiles, use '{{{{behavior.X}}}}' with a file in ./behavior/"
                ))

    # ── 0b. Legacy boolean rules ──
    rules_block = config.get("rules") or {}
    if rules_block and not isinstance(rules_block, dict):
        errors.append(_E("rules", f"expected dict, got {type(rules_block).__name__}"))
    else:
        for k, v in rules_block.items():
            if k in _LEGACY_BOOLEAN_RULES:
                if not isinstance(v, bool):
                    errors.append(_E(f"rules.{k}", f"expected bool, got {type(v).__name__}"))
            elif k in _LEGACY_INT_RULES:
                if not isinstance(v, int) or isinstance(v, bool):
                    errors.append(_E(f"rules.{k}", f"expected int, got {type(v).__name__}"))
                elif v < 0:
                    errors.append(_E(f"rules.{k}", f"expected >= 0, got {v}"))
            elif k in _LEGACY_STR_RULES:
                if not isinstance(v, str):
                    errors.append(_E(f"rules.{k}", f"expected str, got {type(v).__name__}"))
            else:
                errors.append(_E(
                    f"rules.{k}",
                    f"unknown rule key {k!r}. Did you mean: "
                    f"{_suggest(k, _LEGACY_BOOLEAN_RULES | _LEGACY_INT_RULES | _LEGACY_STR_RULES)}?"
                ))

    # ── 1. classify_turns flag ──
    ct = config.get("classify_turns")
    if ct is not None and not isinstance(ct, bool):
        errors.append(_E("classify_turns", f"expected bool, got {type(ct).__name__}"))

    # ── 2. Rule definitions ──
    errors.extend(_validate_rule_definitions(
        config.get("rule_definitions") or [],
        known_sets=known_sets,
        known_counters=known_counters,
        known_flags=known_flags,
        tool_short=tool_short,
        tool_fqn=tool_fqn,
        check_tools=check_tools,
    ))

    # ── 3. State tracking ──
    errors.extend(_validate_state_tracking(
        tracking,
        tool_short=tool_short,
        tool_fqn=tool_fqn,
        check_tools=check_tools,
    ))

    # ── 4. Classifier ──
    classifier = config.get("classifier") or {}
    if hasattr(classifier, "model_dump"):
        classifier = classifier.model_dump()
    errors.extend(_validate_classifier(classifier))

    # ── 5. Brain (classifier LLM) ──
    brain = config.get("brain")
    if brain is not None:
        if hasattr(brain, "model_dump"):
            brain = brain.model_dump()
        if not isinstance(brain, dict):
            errors.append(_E("brain", f"expected dict, got {type(brain).__name__}"))
        else:
            prov = brain.get("provider")
            model = brain.get("model")
            if not isinstance(prov, str) or not prov:
                errors.append(_E("brain.provider", "expected non-empty string (e.g. 'deepseek', 'anthropic')"))
            if not isinstance(model, str) or not model:
                errors.append(_E("brain.model", "expected non-empty string (e.g. 'deepseek-chat', 'claude-haiku-4-5')"))

    # ── 6. Custom (legacy rules list) ──
    for i, custom in enumerate(config.get("custom") or []):
        if hasattr(custom, "model_dump"):
            custom = custom.model_dump()
        base = f"custom[{i}] (id={custom.get('id', '?')!r})"
        action = custom.get("action", "warn")
        if action not in _VALID_ACTION:
            errors.append(_E(base, f"action={action!r} invalid. Must be one of {sorted(_VALID_ACTION)}"))
        enforce = custom.get("enforce", "pre_tool")
        if enforce not in _VALID_WHEN:
            errors.append(_E(base, f"enforce={enforce!r} invalid. Must be one of {sorted(_VALID_WHEN)}"))
        trig = custom.get("trigger", "")
        if check_tools and trig:
            t_low = trig.lower()
            t_short = t_low.rsplit(".", 1)[-1] if "." in t_low else t_low
            if t_low not in tool_fqn and t_short not in tool_short:
                errors.append(_E(
                    f"{base}.trigger",
                    f"tool {trig!r} not in granted capabilities. "
                    f"Known short names: {sorted(tool_short)[:10]}..."
                ))

    return errors


# ── Helpers ──

def _E(path: str, message: str) -> ValidationError:
    return ValidationError(path, message)


def _suggest(bad: str, candidates: set[str], n: int = 3) -> str:
    """Return the top N closest candidates for a misspelled key."""
    from difflib import get_close_matches
    matches = get_close_matches(bad, candidates, n=n, cutoff=0.6)
    return ", ".join(matches) if matches else "(no close match)"


def _normalize_tracking(tracking: Any) -> dict[str, Any]:
    if tracking is None:
        return {}
    if hasattr(tracking, "model_dump"):
        return tracking.model_dump()
    if not isinstance(tracking, dict):
        return {}
    out: dict[str, Any] = {"sets": {}, "counters": {}, "flags": {}}
    for section in ("sets", "counters", "flags"):
        raw = tracking.get(section) or {}
        if not isinstance(raw, dict):
            continue
        for name, cfg in raw.items():
            if hasattr(cfg, "model_dump"):
                out[section][name] = cfg.model_dump()
            elif isinstance(cfg, dict):
                out[section][name] = cfg
    return out


def _normalize_trigger(trigger: Any) -> list[str] | None:
    if isinstance(trigger, str):
        return [trigger]
    if isinstance(trigger, list):
        if all(isinstance(t, str) for t in trigger):
            return trigger
    return None


# ── Rule definitions ──

def _validate_rule_definitions(
    rules: list,
    *,
    known_sets: set[str],
    known_counters: set[str],
    known_flags: set[str],
    tool_short: set[str],
    tool_fqn: set[str],
    check_tools: bool,
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    seen_ids: dict[str, int] = {}

    for i, rule in enumerate(rules):
        if hasattr(rule, "model_dump"):
            rule = rule.model_dump()
        base = f"rule_definitions[{i}]"
        if not isinstance(rule, dict):
            errors.append(_E(base, f"expected dict, got {type(rule).__name__}"))
            continue

        rid = rule.get("id")
        # Annotation goes in the MESSAGE, not the path (path must stay clean
        # for source_map lookup).
        ann = f"[{rid!r}] " if rid else ""

        if not isinstance(rid, str) or not rid:
            errors.append(_E(f"{base}.id", "missing or empty 'id' (must be non-empty string)"))
        else:
            if rid in seen_ids:
                errors.append(_E(
                    f"{base}.id",
                    f"duplicate rule id {rid!r} (first at rule_definitions[{seen_ids[rid]}])"
                ))
            else:
                seen_ids[rid] = i

        desc = rule.get("description", "")
        if desc and not isinstance(desc, str):
            errors.append(_E(f"{base}.description", f"{ann}expected str, got {type(desc).__name__}"))

        trigger = rule.get("trigger", "*")
        trig_list = _normalize_trigger(trigger)
        if trig_list is None:
            errors.append(_E(
                f"{base}.trigger",
                f"{ann}expected string or list of strings, got {type(trigger).__name__}"
            ))
        else:
            for t in trig_list:
                if not isinstance(t, str):
                    errors.append(_E(f"{base}.trigger", f"{ann}entry {t!r} not a string"))
                elif check_tools and t != "*":
                    t_low = t.lower()
                    t_short = t_low.rsplit(".", 1)[-1] if "." in t_low else t_low
                    if t_low not in tool_fqn and t_short not in tool_short:
                        errors.append(_E(
                            f"{base}.trigger",
                            f"{ann}tool {t!r} not in granted capabilities. "
                            f"Known: {sorted(tool_short)[:10]}..."
                        ))

        when = rule.get("when", "pre_tool")
        if when not in _VALID_WHEN:
            errors.append(_E(
                f"{base}.when",
                f"{ann}value {when!r} invalid. Must be one of {sorted(_VALID_WHEN)}"
            ))

        action = rule.get("action", "warn")
        if action not in _VALID_ACTION:
            errors.append(_E(
                f"{base}.action",
                f"{ann}value {action!r} invalid. Must be one of {sorted(_VALID_ACTION)}"
            ))

        cond = rule.get("condition") or {}
        if not isinstance(cond, dict):
            errors.append(_E(
                f"{base}.condition",
                f"{ann}expected dict, got {type(cond).__name__}"
            ))
        else:
            errors.extend(_validate_condition(
                cond,
                prefix=f"{base}.condition",
                known_sets=known_sets,
                known_counters=known_counters,
                known_flags=known_flags,
                annotation=ann,
            ))

        msg = rule.get("message", "")
        if msg:
            if not isinstance(msg, str):
                errors.append(_E(f"{base}.message", f"{ann}expected str, got {type(msg).__name__}"))
            else:
                errors.extend(_validate_message_placeholders(
                    msg,
                    f"{base}.message",
                    known_counters=known_counters,
                    known_sets=known_sets,
                    known_flags=known_flags,
                    annotation=ann,
                ))

    return errors


def _validate_condition(
    cond: dict,
    *,
    prefix: str,
    known_sets: set[str],
    known_counters: set[str],
    known_flags: set[str],
    annotation: str = "",
) -> list[ValidationError]:
    ann = annotation
    errors: list[ValidationError] = []
    if not cond:
        return errors

    for key, value in cond.items():
        spec = _CONDITION_KEYS.get(key)
        if spec is None:
            errors.append(_E(
                prefix,
                f"{ann}unknown condition key {key!r}. "
                f"Did you mean: {_suggest(key, set(_CONDITION_KEYS.keys()))}? "
                f"Valid: {sorted(_CONDITION_KEYS.keys())}"
            ))
            continue

        expected_type, desc = spec

        if expected_type == "str" and not isinstance(value, str):
            errors.append(_E(f"{prefix}.{key}", f"{ann}expected {desc}, got {type(value).__name__}"))
            continue
        elif expected_type == "dict" and not isinstance(value, dict):
            errors.append(_E(f"{prefix}.{key}", f"{ann}expected {desc}, got {type(value).__name__}"))
            continue
        elif expected_type == "list" and not isinstance(value, list):
            errors.append(_E(f"{prefix}.{key}", f"{ann}expected {desc}, got {type(value).__name__}"))
            continue
        elif expected_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(_E(f"{prefix}.{key}", f"{ann}expected {desc}, got {type(value).__name__}"))
                continue
        elif expected_type == "bool" and not isinstance(value, bool):
            errors.append(_E(f"{prefix}.{key}", f"{ann}expected {desc}, got {type(value).__name__}"))
            continue

        if key in ("target_not_in_set", "target_in_set"):
            if value not in known_sets:
                errors.append(_E(
                    f"{prefix}.{key}",
                    f"{ann}set {value!r} not declared in state_tracking.sets. "
                    f"Declared: {sorted(known_sets) or '[]'}"
                ))

        elif key == "counter_gte":
            name = value.get("name")
            val = value.get("value")
            if not isinstance(name, str) or not name:
                errors.append(_E(f"{prefix}.counter_gte.name", "expected non-empty string"))
            elif name not in known_counters:
                errors.append(_E(
                    f"{prefix}.counter_gte.name",
                    f"{ann}counter {name!r} not declared in state_tracking.counters. "
                    f"Declared: {sorted(known_counters) or '[]'}"
                ))
            if isinstance(val, bool) or not isinstance(val, int):
                errors.append(_E(f"{prefix}.counter_gte.value", f"expected int, got {type(val).__name__}"))
            elif val < 0:
                errors.append(_E(f"{prefix}.counter_gte.value", f"{ann}expected >= 0, got {val}"))

        elif key == "param_matches":
            param = value.get("param")
            pattern = value.get("pattern")
            if not isinstance(param, str) or not param:
                errors.append(_E(f"{prefix}.param_matches.param", "expected non-empty string"))
            if not isinstance(pattern, str) or not pattern:
                errors.append(_E(f"{prefix}.param_matches.pattern", "expected non-empty string"))
            else:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(_E(
                        f"{prefix}.param_matches.pattern",
                        f"{ann}invalid regex {pattern!r} ({exc})"
                    ))

        elif key == "param_contains":
            if not isinstance(value.get("param"), str):
                errors.append(_E(f"{prefix}.param_contains.param", "expected string"))
            if not isinstance(value.get("value"), str):
                errors.append(_E(f"{prefix}.param_contains.value", "expected string"))

        elif key == "flag_is":
            name = value.get("name")
            val = value.get("value")
            if not isinstance(name, str) or not name:
                errors.append(_E(f"{prefix}.flag_is.name", "expected non-empty string"))
            elif name not in known_flags:
                errors.append(_E(
                    f"{prefix}.flag_is.name",
                    f"{ann}flag {name!r} not declared in state_tracking.flags. "
                    f"Declared: {sorted(known_flags) or '[]'}"
                ))
            if not isinstance(val, bool):
                errors.append(_E(f"{prefix}.flag_is.value", f"expected bool, got {type(val).__name__}"))

        elif key == "text_matches":
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(_E(f"{prefix}.text_matches", f"{ann}invalid regex {value!r} ({exc})"))

        elif key == "consecutive_gte" and value < 1:
            errors.append(_E(f"{prefix}.consecutive_gte", f"{ann}expected >= 1, got {value}"))

        elif key in ("all", "any"):
            if len(value) == 0:
                errors.append(_E(f"{prefix}.{key}", f"{ann}empty list - {key} requires at least 1 sub-condition"))
            for j, sub in enumerate(value):
                if not isinstance(sub, dict):
                    errors.append(_E(f"{prefix}.{key}[{j}]", f"expected dict, got {type(sub).__name__}"))
                else:
                    errors.extend(_validate_condition(
                        sub,
                        prefix=f"{prefix}.{key}[{j}]",
                        known_sets=known_sets,
                        known_counters=known_counters,
                        known_flags=known_flags,
                    ))

        elif key == "not":
            errors.extend(_validate_condition(
                value,
                prefix=f"{prefix}.not",
                known_sets=known_sets,
                known_counters=known_counters,
                known_flags=known_flags,
            ))

    return errors


# ── State tracking ──

def _validate_state_tracking(
    tracking: dict,
    *,
    tool_short: set[str],
    tool_fqn: set[str],
    check_tools: bool,
) -> list[ValidationError]:
    errors: list[ValidationError] = []

    def _check_tool_list(tools: Any, path: str) -> None:
        if not isinstance(tools, list):
            errors.append(_E(path, f"expected list of tool names, got {type(tools).__name__}"))
            return
        for t in tools:
            if not isinstance(t, str):
                errors.append(_E(path, f"entry {t!r} not a string"))
            elif check_tools and t != "*":
                t_low = t.lower()
                t_short = t_low.rsplit(".", 1)[-1] if "." in t_low else t_low
                if t_low not in tool_fqn and t_short not in tool_short:
                    errors.append(_E(
                        path,
                        f"tool {t!r} not in granted capabilities. "
                        f"Known: {sorted(tool_short)[:10]}..."
                    ))

    for name, cfg in tracking.get("sets", {}).items():
        base = f"state_tracking.sets.{name}"
        if not isinstance(cfg, dict):
            errors.append(_E(base, f"expected dict, got {type(cfg).__name__}"))
            continue
        if "add_on" not in cfg:
            errors.append(_E(base, "missing 'add_on' field (list of tool names)"))
        else:
            _check_tool_list(cfg["add_on"], f"{base}.add_on")
        target = cfg.get("target", "file_path")
        if not isinstance(target, str):
            errors.append(_E(f"{base}.target", f"expected str, got {type(target).__name__}"))
        aliases = cfg.get("aliases", [])
        if aliases and not isinstance(aliases, list):
            errors.append(_E(f"{base}.aliases", f"expected list, got {type(aliases).__name__}"))

    for name, cfg in tracking.get("counters", {}).items():
        base = f"state_tracking.counters.{name}"
        if not isinstance(cfg, dict):
            errors.append(_E(base, f"expected dict, got {type(cfg).__name__}"))
            continue
        inc = cfg.get("increment_on", [])
        reset = cfg.get("reset_on", [])
        _check_tool_list(inc, f"{base}.increment_on")
        _check_tool_list(reset, f"{base}.reset_on")
        if not inc and not reset and not cfg.get("reset_when"):
            errors.append(_E(
                base,
                "counter has no increment_on, reset_on, or reset_when - it will never update. "
                "Declare at least one trigger."
            ))

        rw = cfg.get("reset_when")
        if rw:
            if not isinstance(rw, dict):
                errors.append(_E(f"{base}.reset_when", f"expected dict, got {type(rw).__name__}"))
            else:
                if "tool" not in rw:
                    errors.append(_E(f"{base}.reset_when.tool", "missing field (comma-separated tool names)"))
                if "param" not in rw:
                    errors.append(_E(f"{base}.reset_when.param", "missing field"))
                pat = rw.get("matches", "")
                if pat:
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        errors.append(_E(f"{base}.reset_when.matches", f"invalid regex {pat!r} ({exc})"))

    for name, cfg in tracking.get("flags", {}).items():
        base = f"state_tracking.flags.{name}"
        if not isinstance(cfg, dict):
            errors.append(_E(base, f"expected dict, got {type(cfg).__name__}"))
            continue
        _check_tool_list(cfg.get("set_on", []), f"{base}.set_on")
        _check_tool_list(cfg.get("unset_on", []), f"{base}.unset_on")

    return errors


# ── Classifier ──

def _validate_classifier(classifier: dict) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not classifier:
        return errors

    freq = classifier.get("frequency", "every_turn")
    if freq not in _VALID_FREQUENCY:
        errors.append(_E(
            "classifier.frequency",
            f"value {freq!r} invalid. Must be one of {sorted(_VALID_FREQUENCY)}"
        ))

    # Dependency: every_n_turns requires frequency_n
    if freq == "every_n_turns":
        n = classifier.get("frequency_n")
        if n is None:
            errors.append(_E(
                "classifier.frequency_n",
                "required when frequency='every_n_turns' (how many turns between runs)"
            ))

    for fld, minval in [("frequency_n", 1), ("timeout", 1), ("max_directives", 1)]:
        v = classifier.get(fld)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append(_E(f"classifier.{fld}", f"expected int, got {type(v).__name__}"))
            elif v < minval:
                errors.append(_E(f"classifier.{fld}", f"expected >= {minval}, got {v}"))

    for fld in ("skip_followups",):
        v = classifier.get(fld)
        if v is not None and not isinstance(v, bool):
            errors.append(_E(f"classifier.{fld}", f"expected bool, got {type(v).__name__}"))

    def _check_entries(key: str) -> list[str]:
        raw = classifier.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list):
            errors.append(_E(f"classifier.{key}", f"expected list, got {type(raw).__name__}"))
            return []
        names = []
        for i, item in enumerate(raw):
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                n = item.get("name")
                if not isinstance(n, str) or not n:
                    errors.append(_E(f"classifier.{key}[{i}]", "missing or invalid 'name' field (required)"))
                else:
                    names.append(n)
                for sub in ("label", "when", "behavior", "description"):
                    if sub in item and not isinstance(item[sub], str):
                        errors.append(_E(
                            f"classifier.{key}[{i}].{sub}",
                            f"expected str, got {type(item[sub]).__name__}"
                        ))
            else:
                errors.append(_E(f"classifier.{key}[{i}]", f"expected str or dict, got {type(item).__name__}"))
        seen: dict[str, int] = {}
        for idx, n in enumerate(names):
            if n in seen:
                errors.append(_E(f"classifier.{key}[{idx}]", f"duplicate name {n!r} (first at index {seen[n]})"))
            seen[n] = idx
        return names

    complexity_names = _check_entries("complexity_levels")
    approach_names = _check_entries("approaches")
    risk_names = _check_entries("risk_levels")

    ctx = classifier.get("context")
    if ctx is not None:
        if not isinstance(ctx, dict):
            errors.append(_E("classifier.context", f"expected dict, got {type(ctx).__name__}"))
        else:
            for k in ("tool_inventory", "session_state", "workspace_info", "recent_history"):
                v = ctx.get(k)
                if v is not None and not isinstance(v, bool):
                    errors.append(_E(f"classifier.context.{k}", f"expected bool, got {type(v).__name__}"))
            hd = ctx.get("history_depth")
            if hd is not None:
                if isinstance(hd, bool) or not isinstance(hd, int):
                    errors.append(_E("classifier.context.history_depth", f"expected positive int, got {type(hd).__name__}"))
                elif hd < 1:
                    errors.append(_E("classifier.context.history_depth", f"expected >= 1, got {hd}"))

    sp = classifier.get("system_prompt")
    if sp is not None and not isinstance(sp, str):
        errors.append(_E("classifier.system_prompt", f"expected str or null, got {type(sp).__name__}"))

    threshold = classifier.get("high_risk_threshold")
    if threshold is not None:
        if not isinstance(threshold, str):
            errors.append(_E("classifier.high_risk_threshold", f"expected str, got {type(threshold).__name__}"))
        elif risk_names and threshold not in risk_names:
            errors.append(_E(
                "classifier.high_risk_threshold",
                f"value {threshold!r} not in risk_levels={risk_names}. "
                f"Did you mean: {_suggest(threshold, set(risk_names))}?"
            ))

    for fld, allowed in [
        ("directive_prefix", _DIRECTIVE_PREFIX_VARS),
        ("high_risk_warning", _HIGH_RISK_WARNING_VARS),
        ("directive_footer", set()),
    ]:
        tmpl = classifier.get(fld)
        if tmpl is None:
            continue
        if not isinstance(tmpl, str):
            errors.append(_E(f"classifier.{fld}", f"expected str, got {type(tmpl).__name__}"))
            continue
        for m in _SIMPLE_PLACEHOLDER_RE.finditer(tmpl):
            name = m.group(1)
            if name not in allowed and name not in _UNIVERSAL_PLACEHOLDERS:
                errors.append(_E(
                    f"classifier.{fld}",
                    f"unknown placeholder {{{name}}}. Valid: {sorted(allowed) or '[]'}"
                ))

    return errors


def _validate_message_placeholders(
    msg: str,
    prefix: str,
    *,
    known_counters: set[str],
    known_sets: set[str],
    known_flags: set[str],
    annotation: str = "",
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not msg:
        return errors
    ann = annotation

    for match in _TYPED_PLACEHOLDER_RE.finditer(msg):
        kind, name = match.group(1), match.group(2)
        if kind == "counter" and name not in known_counters:
            errors.append(_E(
                prefix,
                f"{ann}placeholder {{counter:{name}}} - counter {name!r} not declared. "
                f"Known: {sorted(known_counters) or '[]'}"
            ))
        elif kind == "set_count" and name not in known_sets:
            errors.append(_E(
                prefix,
                f"{ann}placeholder {{set_count:{name}}} - set {name!r} not declared. "
                f"Known: {sorted(known_sets) or '[]'}"
            ))
        elif kind == "flag" and name not in known_flags:
            errors.append(_E(
                prefix,
                f"{ann}placeholder {{flag:{name}}} - flag {name!r} not declared. "
                f"Known: {sorted(known_flags) or '[]'}"
            ))

    for match in _SIMPLE_PLACEHOLDER_RE.finditer(msg):
        name = match.group(1)
        if ":" in name:
            continue
        if name not in _UNIVERSAL_PLACEHOLDERS:
            errors.append(_E(
                prefix,
                f"{ann}unknown placeholder {{{name}}}. "
                f"Valid: {sorted(_UNIVERSAL_PLACEHOLDERS)} or typed forms "
                f"{{param:X}}, {{counter:X}}, {{set_count:X}}, {{flag:X}}"
            ))

    return errors
