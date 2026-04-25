"""Exhaustive compiler stress test — aims for 300+ cases covering the full docs.

Runs in two phases:
  1. Compile every case against /api/discovery/compile, classify vs expected.
  2. (Optional --deploy) Deploy every valid case to the stress daemon.

Use --deploy to include deploy phase.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

import httpx
import yaml as _y

DAEMON_URL = "http://127.0.0.1:9876"
DEEPSEEK_KEY = "sk-6f07faba787a450cb3234dc78fc7cf21"
RESULT_DIR = Path(__file__).parent / "results"
RESULT_DIR.mkdir(exist_ok=True)


def _brain(**over):
    base = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "backend": "openai_compat",
        "config": {"api_key": DEEPSEEK_KEY},
        "temperature": 0,
        "max_tokens": 256,
    }
    base.update(over)
    return base


def _agent(agent_id="main", role="worker", **over):
    base = {
        "id": agent_id,
        "role": role,
        "brain": _brain(),
        "system_prompt": "You are a test agent.",
    }
    base.update(over)
    return base


def _app_base(app_id, **blocks):
    base = {
        "app": {"app_id": app_id, "name": app_id},
        "agents": [_agent()],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 60},
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
# A — Top-level structure (AppDefinition strict)
# ══════════════════════════════════════════════════════════════════════

_add("A01_minimal", "structure", _yaml(_app_base("stress-a01")), "valid")
_add("A02_no_app_key", "structure", _yaml({
    "agents": [_agent()], "execution": {"mode": "conversation"},
}), "invalid")
_add("A03_no_agents", "structure", _yaml({
    "app": {"app_id": "stress-a03", "name": "x"},
    "execution": {"mode": "conversation"},
}), "invalid")
_add("A04_empty_agents", "structure", _yaml({
    "app": {"app_id": "stress-a04", "name": "x"},
    "agents": [],
    "execution": {"mode": "conversation"},
}), "invalid")
_add("A05_unknown_root_key", "structure", _yaml({
    "app": {"app_id": "stress-a05", "name": "x"},
    "agents": [_agent()],
    "execution": {"mode": "conversation"},
    "ghost_block": "bad",
}), "invalid")
_add("A06_app_id_present", "structure", _yaml(_app_base("stress-a06")), "valid")
_add("A07_app_missing_app_id", "structure", _yaml({
    "app": {"name": "x"}, "agents": [_agent()], "execution": {"mode": "conversation"},
}), "invalid")
_add("A08_app_extra_key", "structure", _yaml({
    "app": {"app_id": "stress-a08", "name": "x", "ghost_field": "bad"},
    "agents": [_agent()], "execution": {"mode": "conversation"},
}), "invalid")
_add("A09_agent_missing_brain", "structure", _yaml({
    "app": {"app_id": "stress-a09", "name": "x"},
    "agents": [{"id": "x", "role": "worker"}],
    "execution": {"mode": "conversation"},
}), "invalid")
_add("A10_agent_extra_key", "structure", _yaml({
    "app": {"app_id": "stress-a10", "name": "x"},
    "agents": [{**_agent(), "ghost_field": "bad"}],
    "execution": {"mode": "conversation"},
}), "invalid")


# ══════════════════════════════════════════════════════════════════════
# B — execution.mode (Literal)
# ══════════════════════════════════════════════════════════════════════

_trigger = [{"id": "c", "type": "cron", "schedule": "0 * * * *"}]
_add("B01_conversation", "mode", _yaml(_app_base("stress-b01")), "valid")
_add("B02_one_shot", "mode", _yaml(_app_base("stress-b02",
    execution={"mode": "one_shot", "max_turns": 3})), "valid")
_add("B03_background", "mode", _yaml(_app_base("stress-b03",
    execution={"mode": "background", "triggers": _trigger})), "valid")
_add("B04_pipeline", "mode", _yaml(_app_base("stress-b04",
    execution={"mode": "pipeline", "max_turns": 1},
    pipeline=[{"app": "foo", "input": "{{input}}"}])), "valid")
_add("B05_mode_typo", "mode", _yaml(_app_base("stress-b05",
    execution={"mode": "chat"})), "invalid")
_add("B06_mode_uppercase", "mode", _yaml(_app_base("stress-b06",
    execution={"mode": "CONVERSATION"})), "invalid")
_add("B07_mode_empty", "mode", _yaml(_app_base("stress-b07",
    execution={"mode": ""})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# C — execution.workspace_mode
# ══════════════════════════════════════════════════════════════════════

for wm in ["none", "required", "fixed", "auto"]:
    _add(f"C01_{wm}", "workspace_mode", _yaml(_app_base(f"stress-c01-{wm}",
        execution={"mode": "conversation", "workspace_mode": wm})), "valid")
for bad in ["requiered", "REQUIRED", "forced", "automatic", "optional"]:
    _add(f"C02_bad_{bad}", "workspace_mode", _yaml(_app_base(f"stress-c02-{bad}",
        execution={"mode": "conversation", "workspace_mode": bad})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# D — execution.session_mode
# ══════════════════════════════════════════════════════════════════════

for sm in ["mono", "multi"]:
    _add(f"D01_{sm}", "session_mode", _yaml(_app_base(f"stress-d01-{sm}",
        execution={"mode": "background", "session_mode": sm, "triggers": _trigger})), "valid")
for bad in ["single", "multiple", "MONO", "mono_user"]:
    _add(f"D02_bad_{bad}", "session_mode", _yaml(_app_base(f"stress-d02-{bad}",
        execution={"mode": "background", "session_mode": bad, "triggers": _trigger})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# E — execution.tool_injection
# ══════════════════════════════════════════════════════════════════════

for ti in ["direct", "compact_direct", "discovery"]:
    _add(f"E01_{ti}", "tool_injection", _yaml(_app_base(f"stress-e01-{ti}",
        execution={"mode": "conversation", "tool_injection": ti})), "valid")
for bad in ["direc", "compact", "DISCOVERY", "auto_discover"]:
    _add(f"E02_bad_{bad}", "tool_injection", _yaml(_app_base(f"stress-e02-{bad}",
        execution={"mode": "conversation", "tool_injection": bad})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# F — execution.context
# ══════════════════════════════════════════════════════════════════════

for strat in ["truncate", "summarize"]:
    _add(f"F01_{strat}", "context", _yaml(_app_base(f"stress-f01-{strat}",
        execution={"mode": "conversation", "context": {"strategy": strat}})), "valid")
for bad in ["summarise", "truncat", "compress", "SUMMARIZE"]:
    _add(f"F02_bad_{bad}", "context", _yaml(_app_base(f"stress-f02-{bad}",
        execution={"mode": "conversation", "context": {"strategy": bad}})), "invalid")
_add("F03_max_tokens_valid", "context", _yaml(_app_base("stress-f03",
    execution={"mode": "conversation", "context": {"max_tokens": 100000}})), "valid")
_add("F04_keep_recent", "context", _yaml(_app_base("stress-f04",
    execution={"mode": "conversation",
               "context": {"strategy": "summarize", "keep_recent": 10}})), "valid")
_add("F05_context_extra_key", "context", _yaml(_app_base("stress-f05",
    execution={"mode": "conversation",
               "context": {"strategy": "truncate", "ghost": 1}})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# G — hooks: `on` field (YAML 1.1 booleans, 14 events)
# ══════════════════════════════════════════════════════════════════════

_HOOK_EVENTS = [
    "turn_start", "turn_end", "tool_start", "tool_end",
    "pre_tool_use", "post_tool_use", "user_prompt",
    "session_start", "session_end", "pre_compact", "error",
    "approval_request", "agent_spawn", "agent_complete", "activation",
]


def _hook_app(app_id, event, condition="always", action="log", cond_params=None, act_params=None):
    return _app_base(app_id, execution={
        "mode": "conversation",
        "hooks": [{
            "id": f"h_{app_id}",
            "on": event,
            "condition": {"type": condition, **(cond_params or {})},
            "action": {"type": action, **(act_params or {"message": "hi"})},
        }],
    })


for event in _HOOK_EVENTS:
    _add(f"G01_event_{event}", "hooks_event",
         _yaml(_hook_app(f"stress-g01-{event.replace('_', '-')}", event)), "valid")

# unquoted `on:` → parses as True, must be rejected
unquoted_yaml = """app: {app_id: stress-g02-unquoted, name: x}
agents:
  - id: main
    role: worker
    brain: {provider: deepseek, model: deepseek-chat, backend: openai_compat, config: {api_key: sk-x}}
    system_prompt: hi
execution:
  mode: conversation
  hooks:
    - id: h
      on: tool_end
      condition: {type: always}
      action: {type: log, message: hi}
capabilities: {default_policy: auto}
"""
_add("G02_on_unquoted_yaml11", "hooks_event", unquoted_yaml, "invalid")

for bad in ["tool_endZ", "TURN_END", "turnend", "unknown_event", "beforetools"]:
    _add(f"G03_event_bad_{bad}", "hooks_event",
         _yaml(_hook_app(f"stress-g03-{bad}", bad)), "invalid")


# ══════════════════════════════════════════════════════════════════════
# H — hook conditions (14 registered)
# ══════════════════════════════════════════════════════════════════════

_COND_CASES = [
    ("always", {}, "valid"),
    ("never", {}, "valid"),
    ("tool_name", {"match": "Write"}, "valid"),
    ("tool_name", {"match": ["Write", "Edit"]}, "valid"),
    ("tool_name", {"value": "Write"}, "invalid"),
    ("tool_name", {"matches": "Write"}, "invalid"),
    ("tool_name", {}, "invalid"),
    ("tool_failed", {}, "valid"),
    ("context_pressure", {"threshold": 0.8}, "valid"),
    ("context_pressure", {"tresh_hold": 0.8}, "invalid"),
    ("turn_count", {"threshold": 5}, "valid"),
    ("turn_count", {"threshold": 5, "every": 2}, "valid"),
    ("turn_count", {}, "invalid"),
    ("tool_calls", {"threshold": 10}, "valid"),
    ("tool_calls", {}, "invalid"),
    ("message_count", {"threshold": 20}, "valid"),
    ("message_count", {}, "invalid"),
    ("content_contains", {"keyword": "error"}, "valid"),
    ("content_contains", {"text": "error"}, "invalid"),
    ("error_type", {"match": "RuntimeError"}, "valid"),
    ("error_type", {}, "invalid"),
    ("expression", {"expr": "state.turn > 3"}, "valid"),
    ("expression", {"expression": "state.turn > 3"}, "invalid"),
    ("all_of", {"conditions": [{"type": "always"}, {"type": "tool_failed"}]}, "valid"),
    ("all_of", {}, "invalid"),
    ("any_of", {"conditions": [{"type": "always"}]}, "valid"),
    ("not", {"condition": {"type": "tool_failed"}}, "valid"),
    ("not", {}, "invalid"),
    ("unknown_condition", {}, "invalid"),
    ("typo_condiiton", {}, "invalid"),
]

for i, (cname, params, exp) in enumerate(_COND_CASES):
    _add(f"H{i:02d}_cond_{cname}_{exp}", "hooks_cond",
         _yaml(_hook_app(f"stress-h{i:02d}-{cname.replace('_', '-')}-{exp}",
                          "turn_end", condition=cname, cond_params=params)), exp)


# ══════════════════════════════════════════════════════════════════════
# I — hook actions (13 registered)
# ══════════════════════════════════════════════════════════════════════

_ACTION_CASES = [
    ("log", {"message": "hi"}, "valid"),
    ("log", {"message": "hi", "level": "warn"}, "valid"),
    ("log", {"msg": "hi"}, "invalid"),
    ("log", {}, "invalid"),
    ("inject_message", {"content": "hi"}, "valid"),
    ("inject_message", {"content": "hi", "role": "system"}, "valid"),
    ("inject_message", {}, "invalid"),
    ("inject_message", {"text": "hi"}, "invalid"),
    ("compact_context", {}, "valid"),
    ("compact_context", {"strategy": "summarize"}, "valid"),
    ("compact_context", {"strategy": "summarize", "keep_last": 10}, "valid"),
    ("module_action", {"module": "memory", "action": "remember"}, "valid"),
    ("module_action", {"module": "memory"}, "invalid"),
    ("module_action", {"action": "remember"}, "invalid"),
    ("module_action_inject", {"module": "memory", "action": "remember"}, "valid"),
    ("shell", {"command": "echo hi"}, "valid"),
    ("shell", {"command": "echo hi", "timeout": 5}, "valid"),
    ("shell", {}, "invalid"),
    ("gate", {"reason": "halt"}, "valid"),
    ("transform_params", {"transformation": "foo"}, "valid"),
    ("transform_params", {}, "invalid"),
    ("transform_result", {"transformation": "foo"}, "valid"),
    ("chain", {"actions": [{"type": "log", "message": "a"}]}, "valid"),
    ("chain", {}, "invalid"),
    ("notify", {"title": "t", "message": "m"}, "valid"),
    ("pipe", {"to": "other.action", "map": {"x": "y"}}, "valid"),
    ("pipe", {}, "invalid"),
    ("lsp_diagnose", {"inject_result": True}, "valid"),
    ("lsp_diagnose", {"inject_result_typo": True}, "invalid"),
    ("unknown_action", {}, "invalid"),
    ("notfiy", {"title": "x", "message": "y"}, "invalid"),
]

for i, (aname, params, exp) in enumerate(_ACTION_CASES):
    modules = {"memory": {}} if "memory" in str(params) else {}
    app_cfg = _app_base(f"stress-i{i:02d}-{aname.replace('_', '-')}-{exp}",
        execution={"mode": "conversation",
                   "hooks": [{"id": "h", "on": "turn_end",
                              "condition": {"type": "always"},
                              "action": {"type": aname, **params}}]})
    if modules:
        app_cfg["modules"] = modules
    _add(f"I{i:02d}_act_{aname}_{exp}", "hooks_action", _yaml(app_cfg), exp)


# ══════════════════════════════════════════════════════════════════════
# J — capabilities.grant (cross-refs)
# ══════════════════════════════════════════════════════════════════════

_add("J01_grant_valid", "capabilities", _yaml(_app_base("stress-j01",
    modules={"memory": {}, "filesystem": {}},
    capabilities={"default_policy": "auto",
                  "grant": [{"module": "memory", "actions": ["remember"]},
                            {"module": "filesystem", "actions": ["read"]}]})), "valid")
_add("J02_grant_ghost_module", "capabilities", _yaml(_app_base("stress-j02",
    modules={"memory": {}},
    capabilities={"default_policy": "auto",
                  "grant": [{"module": "ghost", "actions": ["x"]}]})), "invalid")
_add("J03_grant_typo", "capabilities", _yaml(_app_base("stress-j03",
    modules={"filesystem": {}},
    capabilities={"default_policy": "auto",
                  "grant": [{"module": "filesytem", "actions": ["read"]}]})), "invalid")
_add("J04_grant_bad_action", "capabilities", _yaml(_app_base("stress-j04",
    modules={"filesystem": {}},
    capabilities={"default_policy": "auto",
                  "grant": [{"module": "filesystem", "actions": ["ghost_action"]}]})), "invalid")
_add("J05_grant_system", "capabilities", _yaml(_app_base("stress-j05",
    capabilities={"default_policy": "auto",
                  "grant": [{"module": "context_builder", "actions": ["ask_user"]}]})), "valid")
_add("J06_policy_valid", "capabilities", _yaml(_app_base("stress-j06",
    capabilities={"default_policy": "approve"})), "valid")
for bad in ["yes", "accept", "allow"]:
    _add(f"J07_policy_bad_{bad}", "capabilities", _yaml(_app_base(f"stress-j07-{bad}",
        capabilities={"default_policy": bad})), "invalid")
for risk in ["low", "medium", "high"]:
    _add(f"J08_risk_{risk}", "capabilities", _yaml(_app_base(f"stress-j08-{risk}",
        capabilities={"default_policy": "auto", "max_risk_level": risk})), "valid")
_add("J09_risk_bad", "capabilities", _yaml(_app_base("stress-j09",
    capabilities={"default_policy": "auto", "max_risk_level": "critical"})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# K — agents (multi-agent, delegate_to, unique ids)
# ══════════════════════════════════════════════════════════════════════

_add("K01_delegate_valid", "agents", _yaml({
    "app": {"app_id": "stress-k01", "name": "x"},
    "agents": [
        _agent("coord", role="coordinator", delegate_to=["w1", "w2"]),
        _agent("w1", role="specialist"),
        _agent("w2", role="specialist"),
    ],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("K02_delegate_ghost", "agents", _yaml({
    "app": {"app_id": "stress-k02", "name": "x"},
    "agents": [_agent("coord", role="coordinator", delegate_to=["ghost"])],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("K03_duplicate_agent_id", "agents", _yaml({
    "app": {"app_id": "stress-k03", "name": "x"},
    "agents": [_agent("same"), _agent("same")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("K04_specialist_modules_valid", "agents", _yaml({
    "app": {"app_id": "stress-k04", "name": "x"},
    "agents": [_agent("sp", role="specialist",
                       modules=[{"filesystem": ["read", "write"]}])],
    "modules": {"filesystem": {}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("K05_specialist_modules_ghost_action", "agents", _yaml({
    "app": {"app_id": "stress-k05", "name": "x"},
    "agents": [_agent("sp", role="specialist",
                       modules=[{"filesystem": ["ghost_action"]}])],
    "modules": {"filesystem": {}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("K06_specialist_ghost_module", "agents", _yaml({
    "app": {"app_id": "stress-k06", "name": "x"},
    "agents": [_agent("sp", role="specialist", modules=["ghost_mod"])],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("K07_pool_valid", "agents", _yaml({
    "app": {"app_id": "stress-k07", "name": "x"},
    "agents": [_agent("sp", role="specialist", pool={"max_workers": 3, "progress": True})],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")


# ══════════════════════════════════════════════════════════════════════
# L — brain provider / backend / model
# ══════════════════════════════════════════════════════════════════════

_KNOWN_PROVIDERS = ["openai", "deepseek", "groq", "mistral", "together",
                    "anthropic", "gemini", "grok", "cerebras", "ollama"]
for p in _KNOWN_PROVIDERS:
    _add(f"L01_provider_{p}", "brain", _yaml({
        "app": {"app_id": f"stress-l01-{p}", "name": "x"},
        "agents": [_agent(brain=_brain(provider=p, model="x-model"))],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["deepsek", "claud", "gpt4", "openaii", "anthropic-api"]:
    _add(f"L02_provider_bad_{bad}", "brain", _yaml({
        "app": {"app_id": f"stress-l02-{bad.replace('-', '_')}", "name": "x"},
        "agents": [_agent(brain=_brain(provider=bad, model="x"))],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

for back in ["openai_compat", "anthropic"]:
    _add(f"L03_backend_{back}", "brain", _yaml({
        "app": {"app_id": f"stress-l03-{back}", "name": "x"},
        "agents": [_agent(brain=_brain(backend=back))],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["openai_compatible", "claude_native", "raw"]:
    _add(f"L04_backend_bad_{bad}", "brain", _yaml({
        "app": {"app_id": f"stress-l04-{bad}", "name": "x"},
        "agents": [_agent(brain=_brain(backend=bad))],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

_add("L05_missing_brain", "brain", _yaml({
    "app": {"app_id": "stress-l05", "name": "x"},
    "agents": [{"id": "main", "role": "worker"}],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")


# ══════════════════════════════════════════════════════════════════════
# M — placeholders {{var}}, {{env.X}}, {{secret.X}}, {{credential.P.F}}
# ══════════════════════════════════════════════════════════════════════

_add("M01_var_declared", "placeholders", _yaml({
    "app": {"app_id": "stress-m01", "name": "x"},
    "variables": {"greeting": "hi"},
    "agents": [_agent(system_prompt="Say {{greeting}}")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("M02_var_undeclared", "placeholders", _yaml({
    "app": {"app_id": "stress-m02", "name": "x"},
    "agents": [_agent(system_prompt="Say {{ghost}}")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("M03_env_ns", "placeholders", _yaml({
    "app": {"app_id": "stress-m03", "name": "x"},
    "agents": [_agent(system_prompt="User: {{env.USER}}")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("M04_secret_ns", "placeholders", _yaml({
    "app": {"app_id": "stress-m04", "name": "x"},
    "agents": [_agent(system_prompt="Key: {{secret.MY_KEY}}")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("M05_sys_ns", "placeholders", _yaml({
    "app": {"app_id": "stress-m05", "name": "x"},
    "agents": [_agent(system_prompt="Platform: {{sys.platform}}")],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("M06_cred_valid", "placeholders", _yaml({
    "app": {"app_id": "stress-m06", "name": "x"},
    "agents": [_agent(brain=_brain(config={"api_key": "{{credential.deepseek.api_key}}"}))],
    "execution": {"mode": "conversation",
                  "credentials_schema": {"providers": [{"name": "deepseek", "type": "api_key",
                                                         "fields": [{"name": "api_key"}]}]}},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("M07_cred_ghost_provider", "placeholders", _yaml({
    "app": {"app_id": "stress-m07", "name": "x"},
    "agents": [_agent(brain=_brain(config={"api_key": "{{credential.openai.api_key}}"}))],
    "execution": {"mode": "conversation",
                  "credentials_schema": {"providers": [{"name": "deepseek", "type": "api_key",
                                                         "fields": [{"name": "api_key"}]}]}},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("M08_cred_bad_field", "placeholders", _yaml({
    "app": {"app_id": "stress-m08", "name": "x"},
    "agents": [_agent(brain=_brain(config={"api_key": "{{credential.deepseek.apikey}}"}))],
    "execution": {"mode": "conversation",
                  "credentials_schema": {"providers": [{"name": "deepseek", "type": "api_key",
                                                         "fields": [{"name": "api_key"}]}]}},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("M09_cred_missing_schema", "placeholders", _yaml({
    "app": {"app_id": "stress-m09", "name": "x"},
    "agents": [_agent(brain=_brain(config={"api_key": "{{credential.deepseek.api_key}}"}))],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")


# ══════════════════════════════════════════════════════════════════════
# N — filters in placeholders {{x | f}}
# ══════════════════════════════════════════════════════════════════════

_VALID_FILTERS = ["upper", "lower", "title", "length", "default", "json",
                  "date", "trim", "replace", "b64encode", "urlencode"]
for f in _VALID_FILTERS:
    _add(f"N01_filter_{f}", "filters", _yaml({
        "app": {"app_id": f"stress-n01-{f}", "name": "x"},
        "variables": {"v": "hi"},
        "agents": [_agent(system_prompt=f"{{{{v | {f}}}}}")],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["uppper", "trimm", "b64enc", "capitilize", "UPPER"]:
    _add(f"N02_filter_bad_{bad}", "filters", _yaml({
        "app": {"app_id": f"stress-n02-{bad}", "name": "x"},
        "variables": {"v": "hi"},
        "agents": [_agent(system_prompt=f"{{{{v | {bad}}}}}")],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")


# ══════════════════════════════════════════════════════════════════════
# O — channels
# ══════════════════════════════════════════════════════════════════════

_CHANNEL_TYPES = ["webhook", "log", "gmail", "telegram", "sms", "slack", "email", "llm_notification", "hook"]
for ct in _CHANNEL_TYPES:
    _add(f"O01_type_{ct}", "channels", _yaml({
        "app": {"app_id": f"stress-o01-{ct}", "name": "x"},
        "agents": [_agent()],
        "channels": {"my": {"type": ct, "config": {"url": "x"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["webhok", "tgram", "smsgateway", "email_smtp", "SLACK"]:
    _add(f"O02_type_bad_{bad}", "channels", _yaml({
        "app": {"app_id": f"stress-o02-{bad}", "name": "x"},
        "agents": [_agent()],
        "channels": {"my": {"type": bad, "config": {"url": "x"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

_add("O03_default_channel_ok", "channels", _yaml({
    "app": {"app_id": "stress-o03", "name": "x"},
    "agents": [_agent()],
    "channels": {"alerts": {"type": "webhook", "config": {"url": "x"}}},
    "execution": {"mode": "conversation", "default_channel": "alerts"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_add("O04_default_channel_ghost", "channels", _yaml({
    "app": {"app_id": "stress-o04", "name": "x"},
    "agents": [_agent()],
    "execution": {"mode": "conversation", "default_channel": "ghost_chan"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

_add("O05_default_channel_builtin", "channels", _yaml({
    "app": {"app_id": "stress-o05", "name": "x"},
    "agents": [_agent()],
    "execution": {"mode": "conversation", "default_channel": "llm_notification"},
    "capabilities": {"default_policy": "auto"},
}), "valid")


# ══════════════════════════════════════════════════════════════════════
# P — middleware names
# ══════════════════════════════════════════════════════════════════════

_APP_MW = ["mask_secrets", "prompt_inject", "content_filter", "rag_inject", "response_filter"]
for mw in _APP_MW:
    _add(f"P01_mw_{mw}", "middleware", _yaml({
        "app": {"app_id": f"stress-p01-{mw}", "name": "x"},
        "agents": [_agent()],
        "middleware": [{"name": mw}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["mask_secret", "promptinject", "content_filters", "rag_injector"]:
    _add(f"P02_mw_bad_{bad}", "middleware", _yaml({
        "app": {"app_id": f"stress-p02-{bad}", "name": "x"},
        "agents": [_agent()],
        "middleware": [{"name": bad}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

_add("P03_mw_custom", "middleware", _yaml({
    "app": {"app_id": "stress-p03", "name": "x"},
    "agents": [_agent()],
    "middleware": [{"name": "custom:my.pkg.Middleware"}],
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")

_MOD_MW = ["audit", "retry", "timeout"]
for mw in _MOD_MW:
    _add(f"P04_modmw_{mw}", "middleware", _yaml({
        "app": {"app_id": f"stress-p04-{mw}", "name": "x"},
        "agents": [_agent()],
        "modules": {"memory": {"middleware": [{"name": mw}]}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["audits", "reetry", "timeouts"]:
    _add(f"P05_modmw_bad_{bad}", "middleware", _yaml({
        "app": {"app_id": f"stress-p05-{bad}", "name": "x"},
        "agents": [_agent()],
        "modules": {"memory": {"middleware": [{"name": bad}]}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")


# ══════════════════════════════════════════════════════════════════════
# Q — modules CONFIG_MODEL strictness (7 modules have CONFIG_MODEL)
# ══════════════════════════════════════════════════════════════════════

# web
_add("Q01_web_ok", "modules_config", _yaml({
    "app": {"app_id": "stress-q01", "name": "x"},
    "agents": [_agent()],
    "modules": {"web": {"config": {"search_backend": "duckduckgo", "cache_ttl": 600}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")
for bad in ["search_backen", "searchbackend", "serach_backend"]:
    _add(f"Q02_web_bad_{bad}", "modules_config", _yaml({
        "app": {"app_id": f"stress-q02-{bad}", "name": "x"},
        "agents": [_agent()],
        "modules": {"web": {"config": {bad: "duckduckgo"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

# rag
_add("Q03_rag_ok", "modules_config", _yaml({
    "app": {"app_id": "stress-q03", "name": "x"},
    "agents": [_agent()],
    "modules": {"rag": {"config": {"embedding_model": "minilm-l12"}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")
_add("Q04_rag_wrong_level", "modules_config", _yaml({
    "app": {"app_id": "stress-q04", "name": "x"},
    "agents": [_agent()],
    "modules": {"rag": {"backend": {"type": "qdrant"}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

# shell
_add("Q05_shell_ok", "modules_config", _yaml({
    "app": {"app_id": "stress-q05", "name": "x"},
    "agents": [_agent()],
    "modules": {"shell": {"config": {}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")
_add("Q06_shell_bad", "modules_config", _yaml({
    "app": {"app_id": "stress-q06", "name": "x"},
    "agents": [_agent()],
    "modules": {"shell": {"config": {"ghost_key": True}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")

# http
_add("Q07_http_ok", "modules_config", _yaml({
    "app": {"app_id": "stress-q07", "name": "x"},
    "agents": [_agent()],
    "modules": {"http": {"config": {}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "valid")
_add("Q08_http_bad", "modules_config", _yaml({
    "app": {"app_id": "stress-q08", "name": "x"},
    "agents": [_agent()],
    "modules": {"http": {"config": {"unknown_field": 1}}},
    "execution": {"mode": "conversation"},
    "capabilities": {"default_policy": "auto"},
}), "invalid")


# ══════════════════════════════════════════════════════════════════════
# R — triggers (type + method)
# ══════════════════════════════════════════════════════════════════════

_TRIGGER_TYPES_VALID = [
    ("cron", {"schedule": "0 * * * *"}),
    ("http", {"path": "/hook"}),
    ("watch", {"paths": ["/tmp/x"]}),
]
for tname, extra in _TRIGGER_TYPES_VALID:
    _add(f"R01_trigger_{tname}", "triggers", _yaml({
        "app": {"app_id": f"stress-r01-{tname.replace('_', '-')}", "name": "x"},
        "agents": [_agent()],
        "execution": {"mode": "background",
                      "triggers": [{"id": "t", "type": tname, **extra}]},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for m in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
    _add(f"R02_http_method_{m}", "triggers", _yaml({
        "app": {"app_id": f"stress-r02-{m.lower()}", "name": "x"},
        "agents": [_agent()],
        "execution": {"mode": "background",
                      "triggers": [{"id": "t", "type": "http",
                                     "path": "/x", "method": m}]},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

for bad in ["post", "POSR", "GRAB", "PSOT"]:
    _add(f"R03_http_method_bad_{bad}", "triggers", _yaml({
        "app": {"app_id": f"stress-r03-{bad}", "name": "x"},
        "agents": [_agent()],
        "execution": {"mode": "background",
                      "triggers": [{"id": "t", "type": "http",
                                     "path": "/x", "method": bad}]},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")


# ══════════════════════════════════════════════════════════════════════
# S — pipeline (typed steps)
# ══════════════════════════════════════════════════════════════════════

_add("S01_pipeline_ok", "pipeline", _yaml(_app_base("stress-s01",
    execution={"mode": "pipeline"},
    pipeline=[{"app": "app1", "input": "{{input}}"},
              {"app": "app2", "input": "{{steps[0].output}}"}])), "valid")

_add("S02_pipeline_missing_app", "pipeline", _yaml(_app_base("stress-s02",
    execution={"mode": "pipeline"},
    pipeline=[{"input": "{{input}}"}])), "invalid")

_add("S03_pipeline_extra_key", "pipeline", _yaml(_app_base("stress-s03",
    execution={"mode": "pipeline"},
    pipeline=[{"app": "a", "input": "i", "ghost": "bad"}])), "invalid")


# ══════════════════════════════════════════════════════════════════════
# T — sandbox config
# ══════════════════════════════════════════════════════════════════════

for lvl in ["off", "standard", "strict", "maximum"]:
    _add(f"T01_sandbox_{lvl}", "sandbox", _yaml(_app_base(f"stress-t01-{lvl}",
        execution={"mode": "conversation",
                   "sandbox": {"level": lvl}})), "valid")

for bad in ["ligth", "MAX", "strictest"]:
    _add(f"T02_sandbox_bad_{bad}", "sandbox", _yaml(_app_base(f"stress-t02-{bad}",
        execution={"mode": "conversation",
                   "sandbox": {"level": bad}})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# U — hooks module_action cross-ref
# ══════════════════════════════════════════════════════════════════════

_add("U01_hook_module_action_ok", "hooks_cross", _yaml(_app_base("stress-u01",
    modules={"memory": {}},
    execution={"mode": "conversation",
               "hooks": [{"id": "h", "on": "turn_end",
                          "condition": {"type": "always"},
                          "action": {"type": "module_action",
                                     "module": "memory", "action": "remember"}}]})), "valid")

_add("U02_hook_module_action_ghost", "hooks_cross", _yaml(_app_base("stress-u02",
    execution={"mode": "conversation",
               "hooks": [{"id": "h", "on": "turn_end",
                          "condition": {"type": "always"},
                          "action": {"type": "module_action",
                                     "module": "ghost_mod", "action": "do"}}]})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# V — duplicate hook ids
# ══════════════════════════════════════════════════════════════════════

_add("V01_duplicate_hook_id", "hooks_misc", _yaml(_app_base("stress-v01",
    execution={"mode": "conversation",
               "hooks": [
                   {"id": "same", "on": "turn_end", "condition": {"type": "always"},
                    "action": {"type": "log", "message": "a"}},
                   {"id": "same", "on": "turn_end", "condition": {"type": "always"},
                    "action": {"type": "log", "message": "b"}},
               ]})), "invalid")

_add("V02_hook_extra_key", "hooks_misc", _yaml(_app_base("stress-v02",
    execution={"mode": "conversation",
               "hooks": [{"id": "h", "on": "turn_end",
                          "condition": {"type": "always"},
                          "action": {"type": "log", "message": "hi"},
                          "ghost_field": "bad"}]})), "invalid")


# ══════════════════════════════════════════════════════════════════════
# W — YAML parsing errors (file:line:col reporting)
# ══════════════════════════════════════════════════════════════════════

_add("W01_bad_yaml_indent", "yaml_errors", """app:
  app_id: stress-w01
 bad_indent: foo
agents: []
""", "invalid")

_add("W02_bad_yaml_tab", "yaml_errors", """app:
	app_id: stress-w02
agents: []
""", "invalid")

_add("W03_bad_yaml_colon", "yaml_errors", """app:
  app_id stress-w03
agents: []
""", "invalid")


# ══════════════════════════════════════════════════════════════════════
# RUNNER
# ══════════════════════════════════════════════════════════════════════

def compile_yaml(yaml_content: str) -> dict:
    try:
        with httpx.Client(timeout=60.0) as c:
            r = c.post(f"{DAEMON_URL}/api/discovery/compile", json={"yaml": yaml_content})
    except Exception as exc:
        return {"valid": False, "errors": [f"network: {exc}"]}
    try:
        body = r.json()
    except Exception:
        return {"valid": False, "errors": [f"HTTP {r.status_code}: {r.text[:300]}"]}
    if not body.get("success"):
        return {"valid": False, "errors": [body.get("error") or "unknown"]}
    data = body.get("data") or {}
    return {"valid": bool(data.get("valid")), "errors": data.get("errors") or [],
            "warnings": data.get("warnings") or []}


def deploy_yaml(yaml_content: str) -> dict:
    import os as _os
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
    by_cat: dict[str, list] = defaultdict(list)

    compile_ok_expected = 0
    compile_ok_unexpected = 0
    compile_fail_expected = 0
    compile_fail_unexpected = 0
    deploy_ok = 0
    deploy_fail = 0

    for i, (tid, cat, yml, exp) in enumerate(CASES):
        r = compile_yaml(yml)
        valid = r["valid"] and not r["errors"]
        status = "PASS"
        deploy_status = ""

        if exp == "valid":
            if valid:
                compile_ok_expected += 1
            else:
                compile_ok_unexpected += 1
                status = f"FN (valid rejected): {str(r['errors'])[:150]}"
        else:
            if valid:
                compile_fail_unexpected += 1
                status = "FP (invalid accepted)"
            else:
                compile_fail_expected += 1

        if do_deploy and exp == "valid" and valid:
            dr = deploy_yaml(yml)
            if dr.get("success"):
                deploy_ok += 1
                deploy_status = "deploy_ok"
            else:
                deploy_fail += 1
                deploy_status = f"DEPLOY_FAIL: {str(dr.get('error', dr))[:100]}"
                status = f"{status} | {deploy_status}"

        results.append({
            "test_id": tid, "category": cat, "expected": exp,
            "valid": valid, "status": status,
            "errors": r["errors"][:2] if r["errors"] else [],
            "deploy": deploy_status,
        })
        by_cat[cat].append(results[-1])
        short = status.split(":")[0]
        print(f"[{i+1:3d}/{len(CASES)}] {short:35s} {tid}")

    total = len(CASES)
    correct = compile_ok_expected + compile_fail_expected
    print(f"\n{'='*60}\n=== SUMMARY ({total} cases) ===\n{'='*60}")
    print(f"Compile correct:    {correct}/{total}  ({100*correct/total:.1f}%)")
    print(f"  valid→valid:      {compile_ok_expected}")
    print(f"  invalid→rejected: {compile_fail_expected}")
    print(f"False negatives (valid rejected): {compile_ok_unexpected}")
    print(f"False positives (invalid accepted): {compile_fail_unexpected}  ← DANGER")
    if do_deploy:
        print(f"\nDeploy OK:   {deploy_ok}")
        print(f"Deploy FAIL: {deploy_fail}")

    print(f"\n--- by category ---")
    for cat, items in sorted(by_cat.items()):
        ok = sum(1 for it in items if it["status"] == "PASS" or it["status"].startswith("PASS"))
        print(f"  {cat:20s} {ok:3d}/{len(items):3d}")

    fps = [r for r in results if "FP" in r["status"]]
    fns = [r for r in results if "FN" in r["status"]]
    if fps:
        print(f"\n--- FALSE POSITIVES ({len(fps)}) ---")
        for fp in fps:
            print(f"  {fp['test_id']} [{fp['category']}]")
    if fns:
        print(f"\n--- FALSE NEGATIVES ({len(fns)}) ---")
        for fn in fns[:20]:
            print(f"  {fn['test_id']} [{fn['category']}]: {str(fn['errors'])[:200]}")

    (RESULT_DIR / "results_full.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"\nSaved to {RESULT_DIR/'results_full.json'}")


if __name__ == "__main__":
    do_deploy = "--deploy" in sys.argv
    run(do_deploy=do_deploy)
