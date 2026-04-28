"""XL compiler stress test - 600+ cases covering behavior, widgets, skills,
credentials, setup, constraints, placeholders, and no-CONFIG_MODEL modules.
"""
from __future__ import annotations

import json
import os as _os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import httpx
import yaml as _y

DAEMON_URL = "http://127.0.0.1:9876"
DEEPSEEK_KEY = "sk-6f07faba787a450cb3234dc78fc7cf21"
RESULT_DIR = Path(__file__).parent / "results"
RESULT_DIR.mkdir(exist_ok=True)

# Shared scratch dir for skills/prompts/behavior/widgets fixtures
FIXTURES = Path(__file__).parent / "fixtures"
FIXTURES.mkdir(exist_ok=True)


def _brain(**over):
    base = {
        "provider": "deepseek", "model": "deepseek-chat",
        "backend": "openai_compat",
        "config": {"api_key": DEEPSEEK_KEY},
        "temperature": 0, "max_tokens": 256,
    }
    base.update(over)
    return base


def _agent(agent_id="main", role="worker", **over):
    base = {
        "id": agent_id, "role": role,
        "brain": _brain(), "system_prompt": "test",
    }
    base.update(over)
    return base


def _app_base(app_id, **blocks):
    base = {
        "app": {"app_id": app_id, "name": app_id},
        "agents": [_agent()],
        "execution": {"mode": "conversation", "max_turns": 3},
        "capabilities": {"default_policy": "auto"},
    }
    base.update(blocks)
    return base


def _yaml(d):
    return _y.dump(d, sort_keys=False, allow_unicode=True)


CASES: list[tuple[str, str, str, str]] = []


def _add(tid, category, content, expected):
    CASES.append((tid, category, content, expected))


# ══════════════════════════════════════════════════════════════════════
# AA - behavior (all condition types, state_tracking, classifier)
# ══════════════════════════════════════════════════════════════════════

for p in ["dev", "coding", "research", "data", "creative", "assistant"]:
    _add(f"AA01_profile_{p}", "behavior",
         _yaml(_app_base(f"stress-aa01-{p}", behavior={"profile": p})), "valid")

for bad in ["coder", "devvv", "default", "PROFILE"]:
    _add(f"AA02_profile_bad_{bad}", "behavior",
         _yaml(_app_base(f"stress-aa02-{bad}", behavior={"profile": bad})), "invalid")

_add("AA03_profile_template", "behavior",
     _yaml(_app_base("stress-aa03", behavior={"profile": "{{behavior.custom}}"})),
     "valid")

for rule in ["read_before_edit", "test_after_changes", "confirm_destructive",
             "no_bash_for_files", "always_lint_check", "plan_before_execute"]:
    _add(f"AA10_rule_{rule}", "behavior", _yaml(_app_base(f"stress-aa10-{rule}",
        behavior={"profile": "coding", "rules": {rule: True}})), "valid")

_add("AA11_rule_int", "behavior", _yaml(_app_base("stress-aa11",
    behavior={"profile": "coding",
              "rules": {"max_sequential_same_tool": 5}})), "valid")

_add("AA12_rule_wrong_type_int_for_bool", "behavior", _yaml(_app_base("stress-aa12",
    behavior={"profile": "coding",
              "rules": {"read_before_edit": 42}})), "invalid")

_add("AA13_rule_wrong_type_bool_for_int", "behavior", _yaml(_app_base("stress-aa13",
    behavior={"profile": "coding",
              "rules": {"max_sequential_same_tool": True}})), "invalid")

_add("AA14_unknown_rule", "behavior", _yaml(_app_base("stress-aa14",
    behavior={"profile": "coding",
              "rules": {"ghost_rule_name": True}})), "invalid")

# rule_definitions (structured rules)
_add("AA20_rule_def_valid", "behavior", _yaml(_app_base("stress-aa20",
    modules={"filesystem": {}},
    capabilities={"default_policy": "auto", "grant": [
        {"module": "filesystem", "actions": ["read", "edit"]},
    ]},
    behavior={"rule_definitions": [{
        "id": "r1",
        "trigger": ["edit", "filesystem.edit"],
        "when": "pre_tool",
        "action": "warn",
        "condition": {"target_not_in_set": "read_files"},
        "message": "{target} not read yet",
    }]})), "valid")

_add("AA21_rule_def_ghost_tool", "behavior", _yaml(_app_base("stress-aa21",
    modules={"filesystem": {}},
    capabilities={"default_policy": "auto", "grant": [
        {"module": "filesystem", "actions": ["read"]},
    ]},
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": ["ghost_tool"],
        "when": "pre_tool", "action": "warn",
        "condition": {"target_not_in_set": "read_files"},
        "message": "x",
    }]})), "invalid")

_add("AA22_rule_def_bad_when", "behavior", _yaml(_app_base("stress-aa22",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "during_tool",
        "action": "warn",
        "condition": {"no_text_before_tools": True},
        "message": "x",
    }]})), "invalid")

_add("AA23_rule_def_bad_action", "behavior", _yaml(_app_base("stress-aa23",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "pre_tool",
        "action": "BLOCK",
        "condition": {"no_text_before_tools": True},
        "message": "x",
    }]})), "invalid")

_add("AA24_rule_def_ghost_condition", "behavior", _yaml(_app_base("stress-aa24",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "pre_tool", "action": "warn",
        "condition": {"ghost_cond": True},
        "message": "x",
    }]})), "invalid")

_add("AA25_rule_def_all_condition", "behavior", _yaml(_app_base("stress-aa25",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "pre_tool", "action": "warn",
        "condition": {"all": [
            {"no_text_before_tools": True},
            {"consecutive_gte": 3},
        ]},
        "message": "x",
    }]})), "valid")

_add("AA26_rule_def_ghost_set_ref", "behavior", _yaml(_app_base("stress-aa26",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "pre_tool", "action": "warn",
        "condition": {"target_not_in_set": "ghost_set"},
        "message": "x",
    }]})), "invalid")

_add("AA27_rule_def_ghost_counter_ref", "behavior", _yaml(_app_base("stress-aa27",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*",
        "when": "pre_tool", "action": "warn",
        "condition": {"counter_gte": {"name": "ghost_counter", "value": 5}},
        "message": "x",
    }]})), "invalid")

_add("AA28_rule_def_duplicate_ids", "behavior", _yaml(_app_base("stress-aa28",
    behavior={"rule_definitions": [
        {"id": "same", "trigger": "*", "when": "pre_tool", "action": "warn",
         "condition": {"no_text_before_tools": True}, "message": "a"},
        {"id": "same", "trigger": "*", "when": "pre_tool", "action": "warn",
         "condition": {"no_text_before_tools": True}, "message": "b"},
    ]})), "invalid")

_add("AA29_rule_def_extra_key", "behavior", _yaml(_app_base("stress-aa29",
    behavior={"rule_definitions": [{
        "id": "r1", "trigger": "*", "when": "pre_tool",
        "action": "warn",
        "condition": {"no_text_before_tools": True},
        "message": "x", "ghost_field": "bad",
    }]})), "invalid")

# state_tracking
_add("AA30_state_tracking_valid", "behavior", _yaml(_app_base("stress-aa30",
    modules={"filesystem": {}, "shell": {}},
    capabilities={"default_policy": "auto", "grant": [
        {"module": "filesystem", "actions": ["read", "write", "edit"]},
        {"module": "shell", "actions": ["bash"]},
    ]},
    behavior={"state_tracking": {
        "sets": {"read_files": {"add_on": ["read"], "target": "file_path"}},
        "counters": {"changes": {"increment_on": ["edit", "write"]}},
        "flags": {"has_tested": {"set_on": ["bash"]}},
    }})), "valid")

_add("AA31_state_tracking_set_extra_key", "behavior", _yaml(_app_base("stress-aa31",
    behavior={"state_tracking": {"sets": {
        "x": {"add_on": ["read"], "target": "file_path", "ghost": 1},
    }}})), "invalid")

_add("AA32_state_tracking_counter_missing_inc", "behavior", _yaml(_app_base("stress-aa32",
    behavior={"state_tracking": {"counters": {"x": {"reset_on": ["bash"]}}}})),
    "invalid")

_add("AA33_state_tracking_flag_missing_set", "behavior", _yaml(_app_base("stress-aa33",
    behavior={"state_tracking": {"flags": {"x": {}}}})), "invalid")

# classifier
_add("AA40_classifier_valid", "behavior", _yaml(_app_base("stress-aa40",
    behavior={
        "classify_turns": True,
        "classifier": {
            "frequency": "every_turn",
            "complexity_levels": ["simple", "moderate", "complex"],
            "approaches": ["direct", "plan_and_confirm"],
            "risk_levels": ["none", "low", "medium", "high"],
        },
    })), "valid")

_add("AA41_classifier_bad_frequency", "behavior", _yaml(_app_base("stress-aa41",
    behavior={"classifier": {"frequency": "everyturn"}})), "invalid")

_add("AA42_classifier_risk_mismatch", "behavior", _yaml(_app_base("stress-aa42",
    behavior={"classifier": {
        "risk_levels": ["none", "low", "high"],
        "high_risk_threshold": "medium",
    }})), "invalid")

_add("AA43_classifier_context_extra_key", "behavior", _yaml(_app_base("stress-aa43",
    behavior={"classifier": {"context": {"tool_inventory": True, "ghost": True}}})),
    "invalid")

_add("AA44_classifier_custom_brain", "behavior", _yaml(_app_base("stress-aa44",
    behavior={"classify_turns": True,
              "classifier": {"frequency": "every_turn"},
              "brain": _brain()})), "valid")

_add("AA45_classifier_brain_bad_provider", "behavior", _yaml(_app_base("stress-aa45",
    behavior={"classify_turns": True,
              "brain": _brain(provider="deepsek")})), "invalid")

_add("AA46_behavior_extra_key", "behavior", _yaml(_app_base("stress-aa46",
    behavior={"profile": "coding", "ghost_key": True})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# BB - widgets (primitives, actions, refs, cycles)
# ══════════════════════════════════════════════════════════════════════

_W_PRIM = ["column", "row", "card", "tabs",
           "list", "table", "stat", "form",
           "button", "icon_button", "alert", "badge", "progress",
           "section", "spacer", "divider"]
for prim in _W_PRIM:
    _add(f"BB01_prim_{prim}", "widgets", _yaml(_app_base(f"stress-bb01-{prim}",
        widgets={"version": 1, "chat_side": {"tree": {"type": prim}}})), "valid")

for bad in ["Column", "textbox", "btn", "paragraph", "header"]:
    _add(f"BB02_prim_bad_{bad}", "widgets", _yaml(_app_base(f"stress-bb02-{bad}",
        widgets={"version": 1, "chat_side": {"tree": {"type": bad}}})), "invalid")

# version (must be explicit per schema)
_add("BB30_wrong_version", "widgets", _yaml(_app_base("stress-bb30",
    widgets={"version": 2, "chat_side": {"tree": {"type": "card"}}})), "invalid")

_add("BB31_no_version", "widgets", _yaml(_app_base("stress-bb31",
    widgets={"chat_side": {"tree": {"type": "card"}}})), "valid")

# Invalid primitive type rejected
_add("BB35_primitive_typo", "widgets", _yaml(_app_base("stress-bb35",
    widgets={"version": 1, "chat_side": {"tree": {"type": "btn"}}})), "invalid")

# top-level widgets block extras
_add("BB36_extra_key", "widgets", _yaml(_app_base("stress-bb36",
    widgets={"version": 1, "chat_side": {"tree": {"type": "card"}}, "ghost": 1})),
    "invalid")


# ══════════════════════════════════════════════════════════════════════
# CC - skills / prompts multi-file
# ══════════════════════════════════════════════════════════════════════

def _skill_fixture(name: str, body: str = "# Skill body\n\nDo something.") -> Path:
    """Create a skills/X.md and prompts/Y.md fixture bundle."""
    d = FIXTURES / name
    d.mkdir(parents=True, exist_ok=True)
    skills_dir = d / "skills"
    skills_dir.mkdir(exist_ok=True)
    (skills_dir / "commit.md").write_text(body, encoding="utf-8")
    (skills_dir / "review.md").write_text(body, encoding="utf-8")
    prompts_dir = d / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    (prompts_dir / "main.md").write_text("# Main prompt\nHelp the user.", encoding="utf-8")
    return d


def _skill_app(name, skills_list, agents=None, variables=None):
    """Write app.yaml in fixture dir; returns Path to the yaml."""
    d = _skill_fixture(name)
    app = {
        "app": {"app_id": name, "name": name},
        "agents": agents or [_agent()],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }
    if skills_list is not None:
        app["skills"] = skills_list
    if variables:
        app["variables"] = variables
    (d / "app.yaml").write_text(_yaml(app), encoding="utf-8")
    return d / "app.yaml"


# Skill shape validation (purely structural, no file I/O required)
_add("CC01_skill_missing_cmd", "skills", _yaml(_app_base("stress-cc01",
    skills=[{"description": "x", "path": "./skills/x.md"}])), "invalid")

_add("CC02_skill_missing_path", "skills", _yaml(_app_base("stress-cc02",
    skills=[{"command": "/x", "description": "x"}])), "invalid")

_add("CC03_skill_valid_shape", "skills", _yaml(_app_base("stress-cc03",
    skills=[{"command": "/x", "description": "x", "path": "./a.md"}])),
    "invalid")  # file not found - compiler rejects

_add("CC04_multiple_skills_valid_shape", "skills", _yaml(_app_base("stress-cc04",
    skills=[
        {"command": "/commit", "description": "x", "path": "./a.md"},
        {"command": "/pr", "description": "y", "path": "./b.md"},
    ])), "invalid")  # files don't exist, compile will fail

_add("CC05_skill_capability_ghost", "skills", _yaml(_app_base("stress-cc05",
    agents=[_agent(capabilities=["ghost_skill"])])), "invalid")


# ══════════════════════════════════════════════════════════════════════
# DD - credentials_schema (all 6 types, all 7 field types)
# ══════════════════════════════════════════════════════════════════════

_CRED_TYPES = ["api_key", "multi_field", "oauth2", "connection_string", "mcp_server", "custom"]
for ct in _CRED_TYPES:
    providers = [{"name": "p", "type": ct, "fields": [{"name": "api_key", "type": "secret"}]}]
    if ct == "oauth2":
        providers[0]["oauth_provider"] = "google"
        providers[0]["fields"] = []
    if ct == "mcp_server":
        providers[0]["transport"] = "stdio"
        providers[0]["command"] = ["npx", "-y", "x"]
    _add(f"DD01_cred_type_{ct}", "credentials", _yaml(_app_base(f"stress-dd01-{ct}",
        execution={"mode": "conversation",
                   "credentials_schema": {"providers": providers}})), "valid")

for bad in ["api-key", "oauth", "oauth3", "API_KEY", "conn_string"]:
    _add(f"DD02_cred_type_bad_{bad.replace('-', '_')}", "credentials",
         _yaml(_app_base(f"stress-dd02-{bad.replace('-', '_')}",
            execution={"mode": "conversation",
                       "credentials_schema": {"providers": [{
                           "name": "x", "type": bad,
                           "fields": [{"name": "y", "type": "secret"}],
                       }]}})), "invalid")

# field types
_FIELD_TYPES = ["secret", "string", "url", "select", "number", "boolean", "connection_string"]
for ft in _FIELD_TYPES:
    f_cfg = {"name": "f", "type": ft}
    if ft == "select":
        f_cfg["options"] = ["a", "b"]
    _add(f"DD10_field_type_{ft}", "credentials",
         _yaml(_app_base(f"stress-dd10-{ft.replace('_', '-')}",
             execution={"mode": "conversation",
                        "credentials_schema": {"providers": [{
                            "name": "p", "type": "api_key",
                            "fields": [f_cfg],
                        }]}})), "valid")

for bad in ["password", "integer", "str", "numer"]:
    _add(f"DD11_field_type_bad_{bad}", "credentials",
         _yaml(_app_base(f"stress-dd11-{bad}",
             execution={"mode": "conversation",
                        "credentials_schema": {"providers": [{
                            "name": "p", "type": "api_key",
                            "fields": [{"name": "f", "type": bad}],
                        }]}})), "invalid")

# scope
for sc in ["per_user", "per_app_shared", "system_wide"]:
    _add(f"DD20_scope_{sc}", "credentials",
         _yaml(_app_base(f"stress-dd20-{sc.replace('_', '-')}",
             execution={"mode": "conversation",
                        "credentials_schema": {"providers": [{
                            "name": "p", "type": "api_key", "scope": sc,
                            "fields": [{"name": "k", "type": "secret"}],
                        }]}})), "valid")

for bad in ["per_session", "GLOBAL", "user"]:
    _add(f"DD21_scope_bad_{bad}", "credentials",
         _yaml(_app_base(f"stress-dd21-{bad}",
             execution={"mode": "conversation",
                        "credentials_schema": {"providers": [{
                            "name": "p", "type": "api_key", "scope": bad,
                            "fields": [{"name": "k", "type": "secret"}],
                        }]}})), "invalid")

# provider name format validation
_add("DD30_provider_name_bad_format", "credentials",
     _yaml(_app_base("stress-dd30",
         execution={"mode": "conversation",
                    "credentials_schema": {"providers": [{
                        "name": "Bad Name",
                        "type": "api_key",
                        "fields": [{"name": "k", "type": "secret"}],
                    }]}})), "invalid")

_add("DD31_field_name_bad_format", "credentials",
     _yaml(_app_base("stress-dd31",
         execution={"mode": "conversation",
                    "credentials_schema": {"providers": [{
                        "name": "p", "type": "api_key",
                        "fields": [{"name": "Bad Name", "type": "secret"}],
                    }]}})), "invalid")

# cred schema extra key
_add("DD40_schema_extra_key", "credentials",
     _yaml(_app_base("stress-dd40",
         execution={"mode": "conversation",
                    "credentials_schema": {"providers": [], "ghost": True}})),
     "invalid")

# provider extra key
_add("DD41_provider_extra_key", "credentials",
     _yaml(_app_base("stress-dd41",
         execution={"mode": "conversation",
                    "credentials_schema": {"providers": [{
                        "name": "p", "type": "api_key",
                        "fields": [{"name": "k", "type": "secret"}],
                        "ghost_field": "bad",
                    }]}})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# EE - setup + constraints per module
# ══════════════════════════════════════════════════════════════════════

# setup valid (memory)
_add("EE01_setup_valid_memory", "setup_constraints", _yaml(_app_base("stress-ee01",
    modules={"memory": {"setup": [
        {"action": "set_goal", "params": {"goal": "test"}},
    ]}})), "valid")

_add("EE02_setup_unknown_action", "setup_constraints", _yaml(_app_base("stress-ee02",
    modules={"memory": {"setup": [
        {"action": "ghost_action", "params": {}},
    ]}})), "invalid")

_add("EE03_setup_bad_params", "setup_constraints", _yaml(_app_base("stress-ee03",
    modules={"memory": {"setup": [
        {"action": "set_goal", "params": {"ghost_param": "x"}},
    ]}})), "invalid")

_add("EE04_setup_missing_required_param", "setup_constraints", _yaml(_app_base("stress-ee04",
    modules={"memory": {"setup": [
        {"action": "set_goal", "params": {}},
    ]}})), "invalid")

# constraints
_add("EE10_constraints_valid", "setup_constraints", _yaml(_app_base("stress-ee10",
    modules={"filesystem": {"constraints": {
        "allowed_actions": ["read", "glob", "grep"],
    }}})), "valid")

_add("EE11_constraints_blocked_actions", "setup_constraints", _yaml(_app_base("stress-ee11",
    modules={"filesystem": {"constraints": {
        "blocked_actions": ["write", "edit"],
    }}})), "valid")

_add("EE12_constraints_ghost_action", "setup_constraints", _yaml(_app_base("stress-ee12",
    modules={"filesystem": {"constraints": {
        "allowed_actions": ["read", "ghost_action"],
    }}})), "invalid")

_add("EE13_constraints_bad_key", "setup_constraints", _yaml(_app_base("stress-ee13",
    modules={"filesystem": {"constraints": {
        "ghost_constraint_key": ["read"],
    }}})), "invalid")

_add("EE14_constraints_wrong_type", "setup_constraints", _yaml(_app_base("stress-ee14",
    modules={"filesystem": {"constraints": {
        "allowed_actions": "read",  # should be list
    }}})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# FF - placeholders asset/behavior/skill/prompt + edge cases
# ══════════════════════════════════════════════════════════════════════

for ns in ["prompt", "skill", "asset", "behavior"]:
    _add(f"FF01_ns_{ns}", "placeholders_ns", _yaml(_app_base(f"stress-ff01-{ns}",
        variables={"x": f"{{{{{ns}.something}}}}"})), "valid")

# input, steps, output, caller, request (runtime namespaces)
for ns in ["input", "steps", "output", "caller", "request"]:
    _add(f"FF02_runtime_ns_{ns}", "placeholders_ns",
         _yaml(_app_base(f"stress-ff02-{ns}",
             variables={"x": f"{{{{{ns}.payload}}}}"})), "valid")

# tool, event, state, agent, workspace (runtime)
for ns in ["tool", "event", "state", "agent", "workspace", "runtime_context", "field", "config_data", "client"]:
    _add(f"FF03_runtime2_{ns}", "placeholders_ns",
         _yaml(_app_base(f"stress-ff03-{ns}",
             variables={"x": f"{{{{{ns}.x}}}}"})), "valid")

# typos on namespace
for bad in ["promt", "skll", "ast", "behaviour", "eenv"]:
    _add(f"FF10_ns_typo_{bad}", "placeholders_ns",
         _yaml(_app_base(f"stress-ff10-{bad}",
             agents=[_agent(system_prompt=f"x {{{{{bad}.x}}}}")])), "invalid")

# nested placeholders
_add("FF20_nested_var", "placeholders_ns", _yaml(_app_base("stress-ff20",
    variables={"inner": "world", "outer": "{{inner}}"})), "valid")

# multiple filters
_add("FF30_double_filter", "placeholders_ns", _yaml(_app_base("stress-ff30",
    variables={"x": "hi"},
    agents=[_agent(system_prompt="{{x | upper | lower}}")])), "valid")

_add("FF31_double_filter_bad", "placeholders_ns", _yaml(_app_base("stress-ff31",
    variables={"x": "hi"},
    agents=[_agent(system_prompt="{{x | upper | bogus}}")])), "invalid")

# array index
_add("FF40_array_idx", "placeholders_ns", _yaml(_app_base("stress-ff40",
    execution={"mode": "pipeline"},
    pipeline=[{"app": "a1"}, {"app": "a2", "input": "{{steps[0].output}}"}])),
    "valid")


# ══════════════════════════════════════════════════════════════════════
# GG - CONFIG_MODEL coverage for all 12 modules
#   - memory / lsp / llm_provider are intentionally permissive
#     (extra="allow") because their runtime consumes arbitrary keys
#     (auto_remember, per-language LSP shorthand, default_provider, ...).
#   - The other 9 are strict (extra="forbid") and reject typos.
# ══════════════════════════════════════════════════════════════════════

_PERMISSIVE_CFG_MODS = ["memory", "lsp", "llm_provider"]
_STRICT_CFG_MODS = ["channels", "context_builder", "agent_spawn", "preview",
                    "widget", "cron_native", "mcp", "index", "database"]

for m in _PERMISSIVE_CFG_MODS:
    _add(f"GG01_no_cfg_{m}", "no_config_model",
         _yaml(_app_base(f"stress-gg01-{m.replace('_', '-')}",
             modules={m: {"config": {"ghost_typo_key": "accepted"}}})),
         "valid")  # permissive by design

for m in _STRICT_CFG_MODS:
    _add(f"GG01_no_cfg_{m}", "no_config_model",
         _yaml(_app_base(f"stress-gg01-{m.replace('_', '-')}",
             modules={m: {"config": {"ghost_typo_key": "accepted"}}})),
         "invalid")  # strict CONFIG_MODEL rejects unknown keys


# ══════════════════════════════════════════════════════════════════════
# HH - payload_schema for background apps
# ══════════════════════════════════════════════════════════════════════

_add("HH01_payload_valid", "payload_schema", _yaml(_app_base("stress-hh01",
    execution={"mode": "background",
               "triggers": [{"id": "c", "type": "cron", "schedule": "0 * * * *"}],
               "payload_schema": {
                   "metadata": [
                       {"name": "company", "label": "Company", "type": "string",
                        "required": True},
                       {"name": "role", "type": "select", "options": ["dev", "pm"]},
                   ],
                   "files": [
                       {"name": "cv", "required": True,
                        "mime": ["application/pdf"]},
                   ],
               }})), "valid")

_add("HH02_payload_select_no_options", "payload_schema", _yaml(_app_base("stress-hh02",
    execution={"mode": "background",
               "triggers": [{"id": "c", "type": "cron", "schedule": "*/1 * * * *"}],
               "payload_schema": {
                   "metadata": [{"name": "role", "type": "select"}],
               }})), "invalid")

_add("HH03_payload_bad_type", "payload_schema", _yaml(_app_base("stress-hh03",
    execution={"mode": "background",
               "triggers": [{"id": "c", "type": "cron", "schedule": "*/1 * * * *"}],
               "payload_schema": {
                   "metadata": [{"name": "x", "type": "str"}],
               }})), "invalid")

_add("HH04_payload_duplicate_names", "payload_schema", _yaml(_app_base("stress-hh04",
    execution={"mode": "background",
               "triggers": [{"id": "c", "type": "cron", "schedule": "*/1 * * * *"}],
               "payload_schema": {
                   "metadata": [
                       {"name": "x", "type": "string"},
                       {"name": "x", "type": "string"},
                   ],
               }})), "invalid")

_add("HH05_payload_only_background", "payload_schema", _yaml(_app_base("stress-hh05",
    execution={"mode": "conversation",
               "payload_schema": {"metadata": [{"name": "x", "type": "string"}]},
    })), "invalid")


# ══════════════════════════════════════════════════════════════════════
# II - sandbox details
# ══════════════════════════════════════════════════════════════════════

_add("II01_sandbox_full", "sandbox_full", _yaml(_app_base("stress-ii01",
    execution={"mode": "conversation",
               "sandbox": {
                   "level": "strict",
                   "pool_size": 4,
                   "pool_max": 10,
                   "session_timeout": 600,
                   "idle_timeout": 300,
                   "allow_paths": ["/tmp"],
               }})), "valid")

_add("II02_sandbox_extra_key", "sandbox_full", _yaml(_app_base("stress-ii02",
    execution={"mode": "conversation",
               "sandbox": {"level": "off", "ghost_field": 1}})), "invalid")

_add("II03_sandbox_pool_negative", "sandbox_full", _yaml(_app_base("stress-ii03",
    execution={"mode": "conversation",
               "sandbox": {"level": "standard", "pool_size": -1}})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════

def compile_yaml(yaml_content: str, source_path: str | None = None) -> dict:
    try:
        with httpx.Client(timeout=60.0) as c:
            payload = {"yaml": yaml_content}
            if source_path:
                payload["source"] = source_path
            r = c.post(f"{DAEMON_URL}/api/discovery/compile", json=payload)
    except Exception as exc:
        return {"valid": False, "errors": [f"network: {exc}"]}
    try:
        body = r.json()
    except Exception:
        return {"valid": False, "errors": [f"HTTP {r.status_code}: {r.text[:300]}"]}
    if not body.get("success"):
        return {"valid": False, "errors": [body.get("error") or "unknown"]}
    data = body.get("data") or {}
    return {"valid": bool(data.get("valid")),
            "errors": data.get("errors") or [],
            "warnings": data.get("warnings") or []}


def deploy_yaml(yaml_content: str) -> dict:
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="stress-")
    try:
        _os.write(fd, yaml_content.encode("utf-8"))
        _os.close(fd)
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{DAEMON_URL}/api/apps/deploy",
                       json={"yaml_path": tmp, "force": True})
        try:
            return r.json()
        except Exception:
            return {"success": False, "error": f"HTTP {r.status_code}"}
    finally:
        try:
            _os.remove(tmp)
        except Exception:
            pass


def run(do_deploy: bool = False):
    print(f"total cases: {len(CASES)}  deploy={do_deploy}")
    results = []
    by_cat = defaultdict(list)
    stats = defaultdict(int)

    for i, (tid, cat, yml, exp) in enumerate(CASES):
        r = compile_yaml(yml)
        valid = r["valid"] and not r["errors"]
        if exp == "valid":
            status = "PASS" if valid else "FN"
        else:
            status = "PASS" if not valid else "FP"
        stats[status] += 1

        deploy_status = ""
        if do_deploy and exp == "valid" and valid:
            dr = deploy_yaml(yml)
            if dr.get("success"):
                deploy_status = "deploy_ok"
            else:
                deploy_status = f"DEPLOY_FAIL: {str(dr.get('error', dr))[:150]}"
        results.append({
            "test_id": tid, "category": cat, "expected": exp,
            "valid": valid, "status": status,
            "errors": r["errors"][:2] if r["errors"] else [],
            "deploy": deploy_status,
        })
        by_cat[cat].append(results[-1])
        print(f"[{i+1:3d}/{len(CASES)}] {status:4s} {tid}")

    total = len(CASES)
    ok = stats["PASS"]
    print(f"\n{'='*60}\n=== SUMMARY ({total} cases) ===\n{'='*60}")
    print(f"PASS: {ok}/{total}  ({100*ok/total:.1f}%)")
    print(f"FN (valid rejected):    {stats['FN']}")
    print(f"FP (invalid accepted):  {stats['FP']}  <- DANGER")

    print(f"\n--- by category ---")
    for cat, items in sorted(by_cat.items()):
        ok_cat = sum(1 for it in items if it["status"] == "PASS")
        print(f"  {cat:25s} {ok_cat:3d}/{len(items):3d}")

    fps = [r for r in results if r["status"] == "FP"]
    fns = [r for r in results if r["status"] == "FN"]
    if fps:
        print(f"\n--- FALSE POSITIVES ({len(fps)}) ---")
        for fp in fps:
            print(f"  {fp['test_id']} [{fp['category']}]")
    if fns:
        print(f"\n--- FALSE NEGATIVES ({len(fns)}) - valid cases rejected ---")
        for fn in fns[:40]:
            print(f"  {fn['test_id']} [{fn['category']}]: {str(fn['errors'])[:250]}")

    out = RESULT_DIR / "results_xl.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    do_deploy = "--deploy" in sys.argv
    run(do_deploy=do_deploy)
