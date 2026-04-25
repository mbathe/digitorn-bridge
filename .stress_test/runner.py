"""Stress test the compiler — hundreds of apps, good and bad cases.

Each test case is a tuple (name, yaml_content, expected) where expected is:
  - "valid"   → compile should succeed AND app should deploy AND answer a message
  - "invalid" → compile should fail (we record the error message)

We use a fresh DevClient pointing at port 9876 (isolated daemon).
"""
from __future__ import annotations

import json
import time
import uuid as _uuid_pkg
import uuid
from pathlib import Path
from typing import Any

import httpx

DAEMON_URL = "http://127.0.0.1:9876"
DEEPSEEK_KEY = "sk-6f07faba787a450cb3234dc78fc7cf21"
WORKDIR = Path(__file__).parent
RESULT_DIR = WORKDIR / "results"
RESULT_DIR.mkdir(exist_ok=True)


def daemon_post(path: str, body: dict) -> httpx.Response:
    with httpx.Client(timeout=60.0) as c:
        return c.post(f"{DAEMON_URL}{path}", json=body)


def daemon_get(path: str, params: dict | None = None) -> httpx.Response:
    with httpx.Client(timeout=30.0) as c:
        return c.get(f"{DAEMON_URL}{path}", params=params or {})


def compile_yaml(yaml_content: str) -> dict:
    r = daemon_post("/api/discovery/compile", {"yaml": yaml_content})
    try:
        body = r.json()
    except Exception:
        return {"valid": False, "errors": [f"HTTP {r.status_code}: {r.text[:500]}"]}
    if not body.get("success"):
        return {"valid": False, "errors": [body.get("error") or "unknown error"]}
    data = body.get("data") or {}
    return {
        "valid": bool(data.get("valid")),
        "errors": data.get("errors") or [],
        "warnings": data.get("warnings") or [],
        "summary": data.get("summary"),
    }


def _brain_config(model: str = "deepseek-chat") -> dict:
    return {
        "provider": "deepseek",
        "model": model,
        "backend": "openai_compat",
        "config": {"api_key": DEEPSEEK_KEY},
        "temperature": 0,
        "max_tokens": 256,
    }


def _base_agent(agent_id: str = "main", system_prompt: str = "You are a test agent.") -> dict:
    return {
        "id": agent_id,
        "role": "worker",
        "brain": _brain_config(),
        "system_prompt": system_prompt,
    }


def _wrap(app_id: str, **overrides) -> dict:
    """Build a minimal valid YAML dict."""
    base = {
        "app": {"app_id": app_id, "name": app_id},
        "agents": [_base_agent()],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 60},
        "capabilities": {"default_policy": "auto"},
    }
    for k, v in overrides.items():
        base[k] = v
    return base


def _yaml_str(d: dict) -> str:
    import yaml as _y
    return _y.dump(d, sort_keys=False, allow_unicode=True)


# ── CASE GENERATORS ──────────────────────────────────────────────────────

def cases() -> list[tuple[str, str, str, str]]:
    """Return list of (test_id, category, yaml_str, expected)."""
    out: list[tuple[str, str, str, str]] = []

    def add(tid: str, cat: str, content: str, expected: str):
        out.append((tid, cat, content, expected))

    # ─────────────── CATEGORY: basic app ───────────────
    add("01_minimal_valid", "basic",
        _yaml_str(_wrap("stress-minimal-valid")), "valid")

    add("01_missing_app_id", "basic", _yaml_str({
        "app": {"name": "X"},
        "agents": [_base_agent()],
        "execution": {"mode": "conversation"},
    }), "invalid")

    add("01_missing_agents", "basic", _yaml_str({
        "app": {"app_id": "stress-no-agents", "name": "X"},
        "execution": {"mode": "conversation"},
    }), "invalid")

    add("01_unknown_root_key", "basic", _yaml_str({
        "app": {"app_id": "stress-unknown-root", "name": "X"},
        "agents": [_base_agent()],
        "execution": {"mode": "conversation"},
        "ghost": "bad",
    }), "invalid")

    # ─────────────── CATEGORY: execution.mode (Literal) ───────────────
    for mode in ["conversation", "one_shot"]:
        add(f"02_mode_{mode}", "execution", _yaml_str(_wrap(
            f"stress-mode-{mode}",
            execution={"mode": mode, "max_turns": 3, "timeout": 60},
        )), "valid")
    # background needs a trigger
    add("02_mode_background", "execution", _yaml_str(_wrap(
        "stress-mode-background",
        execution={"mode": "background", "max_turns": 3,
                   "triggers": [{"id": "c", "type": "cron", "schedule": "0 * * * *"}]},
    )), "valid")
    add("02_mode_pipeline", "execution", _yaml_str(_wrap(
        "stress-mode-pipeline",
        execution={"mode": "pipeline", "max_turns": 3},
        pipeline=[{"app": "some-other", "input": "{{input}}"}],
    )), "valid")
    add("02_mode_invalid", "execution", _yaml_str(_wrap(
        "stress-mode-bad",
        execution={"mode": "chat", "max_turns": 3},
    )), "invalid")

    # ─────────────── CATEGORY: workspace_mode (new Literal) ───────────────
    for wm in ["none", "required", "fixed", "auto"]:
        add(f"03_wsmode_{wm}", "execution", _yaml_str(_wrap(
            f"stress-wsmode-{wm}",
            execution={"mode": "conversation", "workspace_mode": wm, "max_turns": 3},
        )), "valid")
    add("03_wsmode_typo", "execution", _yaml_str(_wrap(
        "stress-wsmode-typo",
        execution={"mode": "conversation", "workspace_mode": "requiered"},
    )), "invalid")

    # ─────────────── CATEGORY: session_mode (new Literal) ───────────────
    _trig = [{"id": "c", "type": "cron", "schedule": "0 * * * *"}]
    add("04_sessmode_mono", "execution", _yaml_str(_wrap(
        "stress-sessmode-mono",
        execution={"mode": "background", "session_mode": "mono", "triggers": _trig},
    )), "valid")
    add("04_sessmode_multi", "execution", _yaml_str(_wrap(
        "stress-sessmode-multi",
        execution={"mode": "background", "session_mode": "multi", "triggers": _trig},
    )), "valid")
    add("04_sessmode_bad", "execution", _yaml_str(_wrap(
        "stress-sessmode-bad",
        execution={"mode": "background", "session_mode": "mnoo", "triggers": _trig},
    )), "invalid")

    # ─────────────── CATEGORY: tool_injection ───────────────
    for ti in ["direct", "compact_direct", "discovery"]:
        add(f"05_tinj_{ti}", "execution", _yaml_str(_wrap(
            f"stress-tinj-{ti}",
            execution={"mode": "conversation", "tool_injection": ti},
        )), "valid")
    add("05_tinj_bad", "execution", _yaml_str(_wrap(
        "stress-tinj-bad",
        execution={"mode": "conversation", "tool_injection": "direckt"},
    )), "invalid")

    # ─────────────── CATEGORY: context.strategy ───────────────
    for strat in ["truncate", "summarize"]:
        add(f"06_strat_{strat}", "context",
            _yaml_str(_wrap(
                f"stress-strat-{strat}",
                execution={"mode": "conversation",
                           "context": {"strategy": strat, "max_tokens": 100000}},
            )), "valid")
    add("06_strat_bad", "context", _yaml_str(_wrap(
        "stress-strat-bad",
        execution={"mode": "conversation",
                   "context": {"strategy": "summarise"}},
    )), "invalid")

    # ─────────────── CATEGORY: hooks — on field (YAML boolean trap) ───────────────
    add("10_hook_on_unquoted", "hooks", """app: {app_id: stress-hook-unquoted, name: X}
agents:
  - id: main
    role: worker
    brain: {provider: deepseek, model: deepseek-chat, backend: openai_compat, config: {api_key: X}}
    system_prompt: hi
execution:
  mode: conversation
  hooks:
    - id: h
      on: tool_end
      condition: {type: always}
      action: {type: log, message: hi}
capabilities: {default_policy: auto}
""", "invalid")
    add("10_hook_on_quoted", "hooks", """app: {app_id: stress-hook-quoted, name: X}
agents:
  - id: main
    role: worker
    brain: {provider: deepseek, model: deepseek-chat, backend: openai_compat, config: {api_key: X}}
    system_prompt: hi
execution:
  mode: conversation
  hooks:
    - id: h
      "on": tool_end
      condition: {type: always}
      action: {type: log, message: hi}
capabilities: {default_policy: auto}
""", "valid")

    # ─────────────── CATEGORY: hooks — event typo ───────────────
    add("11_hook_event_typo", "hooks", """app: {app_id: stress-hook-evt-typo, name: X}
agents: [{id: main, role: worker, brain: {provider: deepseek, model: deepseek-chat, backend: openai_compat, config: {api_key: X}}, system_prompt: hi}]
execution:
  mode: conversation
  hooks:
    - id: h
      "on": tool_endZ
      condition: {type: always}
      action: {type: log, message: hi}
capabilities: {default_policy: auto}
""", "invalid")

    # ─────────────── CATEGORY: hooks — conditions ───────────────
    for cname, params, exp in [
        ("always", {}, "valid"),
        ("never", {}, "valid"),
        ("tool_name", {"match": "Write"}, "valid"),
        ("tool_name", {"value": "Write"}, "invalid"),
        ("context_pressure", {"threshold": 0.8}, "valid"),
        ("context_pressure", {"tresh_hold": 0.8}, "invalid"),
        ("turn_count", {"threshold": 5}, "valid"),
        ("turn_count", {}, "invalid"),
        ("unknown_condition", {}, "invalid"),
    ]:
        tid = f"12_cond_{cname}_{exp}"
        hook_cfg = {"id": "h", "on": "turn_end",
                    "condition": {"type": cname, **params},
                    "action": {"type": "log", "message": "hi"}}
        add(tid, "hooks", _yaml_str({
            "app": {"app_id": f"stress-cond-{cname.replace('_', '-')}-{exp}", "name": "X"},
            "agents": [_base_agent()],
            "execution": {"mode": "conversation", "max_turns": 3, "hooks": [hook_cfg]},
            "capabilities": {"default_policy": "auto"},
        }), exp)

    for aname, params, exp in [
        ("log", {"message": "hi"}, "valid"),
        ("log", {"msg": "hi"}, "invalid"),
        ("inject_message", {"content": "hi"}, "valid"),
        ("inject_message", {}, "invalid"),
        ("module_action", {"module": "memory", "action": "remember"}, "valid"),
        ("module_action", {"module": "memory"}, "invalid"),
        ("compact_context", {"strategy": "summarize"}, "valid"),
        ("unknown_action", {}, "invalid"),
    ]:
        tid = f"13_action_{aname}_{exp}"
        hook_cfg = {"id": "h", "on": "turn_end",
                    "condition": {"type": "always"},
                    "action": {"type": aname, **params}}
        add(tid, "hooks", _yaml_str({
            "app": {"app_id": f"stress-act-{aname.replace('_', '-')}-{exp}", "name": "X"},
            "agents": [_base_agent()],
            "modules": {"memory": {}},
            "execution": {"mode": "conversation", "max_turns": 3, "hooks": [hook_cfg]},
            "capabilities": {"default_policy": "auto"},
        }), exp)

    # ─────────────── CATEGORY: capabilities.grant module cross-ref ───────────────
    add("20_grant_valid", "capabilities", _yaml_str(_wrap(
        "stress-grant-ok",
        modules={"memory": {}, "filesystem": {}},
        capabilities={"default_policy": "auto", "grant": [
            {"module": "memory", "actions": ["remember"]},
            {"module": "filesystem", "actions": ["read"]},
        ]},
    )), "valid")

    add("20_grant_undeclared_module", "capabilities", _yaml_str(_wrap(
        "stress-grant-ghost",
        modules={"memory": {}},
        capabilities={"default_policy": "auto", "grant": [
            {"module": "ghost_mod", "actions": ["remember"]},
        ]},
    )), "invalid")

    add("20_grant_typo_module", "capabilities", _yaml_str(_wrap(
        "stress-grant-typo",
        modules={"filesystem": {}},
        capabilities={"default_policy": "auto", "grant": [
            {"module": "filesytem", "actions": ["read"]},
        ]},
    )), "invalid")

    add("20_grant_system_module_ok", "capabilities", _yaml_str(_wrap(
        "stress-grant-sys",
        capabilities={"default_policy": "auto", "grant": [
            {"module": "context_builder", "actions": ["ask_user"]},
        ]},
    )), "valid")

    # ─────────────── CATEGORY: agents.delegate_to ───────────────
    add("30_delegate_ok", "agents", _yaml_str({
        "app": {"app_id": "stress-deleg-ok", "name": "X"},
        "agents": [
            {"id": "coord", "role": "coordinator", "brain": _brain_config(),
             "delegate_to": ["worker1", "worker2"], "system_prompt": "coord"},
            {"id": "worker1", "role": "specialist", "brain": _brain_config(),
             "system_prompt": "w1"},
            {"id": "worker2", "role": "specialist", "brain": _brain_config(),
             "system_prompt": "w2"},
        ],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("30_delegate_ghost", "agents", _yaml_str({
        "app": {"app_id": "stress-deleg-ghost", "name": "X"},
        "agents": [
            {"id": "coord", "role": "coordinator", "brain": _brain_config(),
             "delegate_to": ["ghost_agent"], "system_prompt": "x"},
        ],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("30_duplicate_agent_id", "agents", _yaml_str({
        "app": {"app_id": "stress-dup-id", "name": "X"},
        "agents": [
            {"id": "same", "role": "worker", "brain": _brain_config(), "system_prompt": "a"},
            {"id": "same", "role": "worker", "brain": _brain_config(), "system_prompt": "b"},
        ],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    # ─────────────── CATEGORY: brain provider ───────────────
    add("40_provider_ok", "brain", _yaml_str(_wrap("stress-prov-ok")), "valid")

    add("40_provider_typo", "brain", _yaml_str({
        "app": {"app_id": "stress-prov-typo", "name": "X"},
        "agents": [{"id": "main", "role": "worker",
                    "brain": {"provider": "deepsek", "model": "deepseek-chat",
                              "backend": "openai_compat",
                              "config": {"api_key": DEEPSEEK_KEY}},
                    "system_prompt": "hi"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("40_backend_typo", "brain", _yaml_str({
        "app": {"app_id": "stress-back-typo", "name": "X"},
        "agents": [{"id": "main", "role": "worker",
                    "brain": {"provider": "deepseek", "model": "deepseek-chat",
                              "backend": "openai_compatible",
                              "config": {"api_key": DEEPSEEK_KEY}},
                    "system_prompt": "hi"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    # ─────────────── CATEGORY: placeholders ───────────────
    add("50_placeholder_var_ok", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-var-ok", "name": "X"},
        "variables": {"greeting": "hello"},
        "agents": [{"id": "main", "role": "worker", "brain": _brain_config(),
                    "system_prompt": "Say {{greeting}}"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("50_placeholder_var_undeclared", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-var-bad", "name": "X"},
        "agents": [{"id": "main", "role": "worker", "brain": _brain_config(),
                    "system_prompt": "Say {{ghost_var}}"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("50_placeholder_credential_ok", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-cred-ok", "name": "X"},
        "agents": [{"id": "main", "role": "worker",
                    "brain": {"provider": "deepseek", "model": "deepseek-chat",
                              "backend": "openai_compat",
                              "config": {"api_key": "{{credential.deepseek.api_key}}"}},
                    "system_prompt": "hi"}],
        "execution": {
            "mode": "conversation",
            "credentials_schema": {
                "providers": [{"name": "deepseek", "type": "api_key",
                               "fields": [{"name": "api_key"}]}],
            },
        },
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("50_placeholder_credential_undeclared_provider", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-cred-ghost", "name": "X"},
        "agents": [{"id": "main", "role": "worker",
                    "brain": {"provider": "openai", "model": "gpt-4",
                              "backend": "openai_compat",
                              "config": {"api_key": "{{credential.openai.api_key}}"}},
                    "system_prompt": "hi"}],
        "execution": {
            "mode": "conversation",
            "credentials_schema": {
                "providers": [{"name": "deepseek", "type": "api_key",
                               "fields": [{"name": "api_key"}]}],
            },
        },
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("50_placeholder_credential_bad_field", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-cred-bad-field", "name": "X"},
        "agents": [{"id": "main", "role": "worker",
                    "brain": {"provider": "deepseek", "model": "deepseek-chat",
                              "backend": "openai_compat",
                              "config": {"api_key": "{{credential.deepseek.apikey}}"}},
                    "system_prompt": "hi"}],
        "execution": {
            "mode": "conversation",
            "credentials_schema": {
                "providers": [{"name": "deepseek", "type": "api_key",
                               "fields": [{"name": "api_key"}]}],
            },
        },
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("50_placeholder_reserved_ns_ok", "placeholders", _yaml_str({
        "app": {"app_id": "stress-ph-reserved", "name": "X"},
        "agents": [{"id": "main", "role": "worker", "brain": _brain_config(),
                    "system_prompt": "User {{env.USER}} on {{sys.platform}}"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    # ─────────────── CATEGORY: filters in placeholders ───────────────
    add("51_filter_ok", "placeholders", _yaml_str({
        "app": {"app_id": "stress-filter-ok", "name": "X"},
        "variables": {"name": "world"},
        "agents": [{"id": "main", "role": "worker", "brain": _brain_config(),
                    "system_prompt": "Say hi to {{name | upper}}"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("51_filter_typo", "placeholders", _yaml_str({
        "app": {"app_id": "stress-filter-typo", "name": "X"},
        "variables": {"name": "world"},
        "agents": [{"id": "main", "role": "worker", "brain": _brain_config(),
                    "system_prompt": "{{name | uppper}}"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    # ─────────────── CATEGORY: channels ───────────────
    add("60_channel_type_ok", "channels", _yaml_str({
        "app": {"app_id": "stress-ch-ok", "name": "X"},
        "agents": [_base_agent()],
        "channels": {
            "mywebhook": {"type": "webhook", "config": {"url": "https://example.com"}},
        },
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("60_channel_type_typo", "channels", _yaml_str({
        "app": {"app_id": "stress-ch-typo", "name": "X"},
        "agents": [_base_agent()],
        "channels": {
            "mywebhook": {"type": "webhok", "config": {"url": "https://example.com"}},
        },
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    # ─────────────── CATEGORY: middleware ───────────────
    add("70_middleware_ok", "middleware", _yaml_str({
        "app": {"app_id": "stress-mw-ok", "name": "X"},
        "agents": [_base_agent()],
        "middleware": [{"name": "mask_secrets"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("70_middleware_typo", "middleware", _yaml_str({
        "app": {"app_id": "stress-mw-typo", "name": "X"},
        "agents": [_base_agent()],
        "middleware": [{"name": "mask_secret"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("70_middleware_custom_ok", "middleware", _yaml_str({
        "app": {"app_id": "stress-mw-custom", "name": "X"},
        "agents": [_base_agent()],
        "middleware": [{"name": "custom:my.module.MyMiddleware"}],
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    # ─────────────── CATEGORY: modules.<id>.config with CONFIG_MODEL ───────────────
    add("80_web_config_ok", "modules", _yaml_str({
        "app": {"app_id": "stress-web-ok", "name": "X"},
        "agents": [_base_agent()],
        "modules": {"web": {"config": {"search_backend": "duckduckgo"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("80_web_config_typo", "modules", _yaml_str({
        "app": {"app_id": "stress-web-typo", "name": "X"},
        "agents": [_base_agent()],
        "modules": {"web": {"config": {"search_backen": "duckduckgo"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    add("80_rag_config_ok", "modules", _yaml_str({
        "app": {"app_id": "stress-rag-ok", "name": "X"},
        "agents": [_base_agent()],
        "modules": {"rag": {"config": {"embedding_model": "minilm-l12"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("80_rag_config_bad_level", "modules", _yaml_str({
        "app": {"app_id": "stress-rag-bad", "name": "X"},
        "agents": [_base_agent()],
        "modules": {"rag": {"backend": {"type": "qdrant"}}},
        "execution": {"mode": "conversation"},
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    # ─────────────── CATEGORY: trigger method Literal ───────────────
    add("90_trigger_method_ok", "triggers", _yaml_str({
        "app": {"app_id": "stress-trig-ok", "name": "X"},
        "agents": [_base_agent()],
        "execution": {
            "mode": "background",
            "triggers": [{"id": "w", "type": "http", "method": "POST", "path": "/x"}],
        },
        "capabilities": {"default_policy": "auto"},
    }), "valid")

    add("90_trigger_method_bad", "triggers", _yaml_str({
        "app": {"app_id": "stress-trig-bad", "name": "X"},
        "agents": [_base_agent()],
        "execution": {
            "mode": "background",
            "triggers": [{"id": "w", "type": "http", "method": "POSR", "path": "/x"}],
        },
        "capabilities": {"default_policy": "auto"},
    }), "invalid")

    return out


# ── RUNNER ──────────────────────────────────────────────────────────────

def deploy_yaml(yaml_content: str) -> dict:
    """Deploy via inline YAML through the builder drafts mechanism."""
    import tempfile, os as _os
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


def send_smoke_message(app_id: str) -> dict:
    """Send a tiny message and wait for response."""
    with httpx.Client(timeout=60.0) as c:
        r = c.post(
            f"{DAEMON_URL}/api/apps/{app_id}/sessions/smoke-{uuid.uuid4().hex[:6]}/messages",
            json={"message": "Say just 'OK' and nothing else."},
        )
    try:
        return r.json()
    except Exception:
        return {"success": False, "error": f"HTTP {r.status_code}"}


def run(do_deploy: bool = False, do_chat: bool = False):
    all_cases = cases()
    print(f"total cases: {len(all_cases)}  deploy={do_deploy}  chat={do_chat}")

    results = []
    compile_failures_expected = 0
    compile_failures_unexpected = 0
    compile_ok_expected = 0
    compile_ok_unexpected = 0
    deploy_ok = 0
    deploy_fail = 0

    for idx, (tid, cat, yaml_content, expected) in enumerate(all_cases):
        r = compile_yaml(yaml_content)
        valid = r.get("valid", False) and not r.get("errors", [])
        compile_msg = r.get("errors", []) or r.get("warnings", []) or r.get("error", "")

        if expected == "valid":
            if valid:
                compile_ok_expected += 1
                status = "PASS"
            else:
                compile_ok_unexpected += 1
                status = "FAIL (expected valid, got errors)"
        else:  # expected invalid
            if not valid:
                compile_failures_expected += 1
                status = "PASS (correctly rejected)"
            else:
                compile_failures_unexpected += 1
                status = "FAIL (expected rejection, compiled OK)"

        deploy_status = ""
        chat_status = ""
        if do_deploy and expected == "valid" and valid:
            dresult = deploy_yaml(yaml_content)
            if dresult.get("success"):
                app_id = (dresult.get("data") or {}).get("app_id", "")
                deploy_ok += 1
                deploy_status = f"deployed={app_id}"
                if do_chat and "mode: conversation" in yaml_content:
                    time.sleep(1.5)
                    cr = send_smoke_message(app_id)
                    if cr.get("success"):
                        chat_status = "chat_ok"
                    else:
                        chat_status = f"CHAT_FAIL: {cr.get('error', str(cr))[:150]}"
            else:
                deploy_fail += 1
                deploy_status = f"DEPLOY_FAIL: {dresult.get('error', str(dresult))[:200]}"

        results.append({
            "test_id": tid,
            "category": cat,
            "expected": expected,
            "valid": valid,
            "status": status,
            "compile_msg": str(compile_msg)[:400] if compile_msg else "",
            "deploy": deploy_status,
        })
        results[-1]["chat"] = chat_status
        suffix = f" | {deploy_status}" if deploy_status else ""
        if chat_status:
            suffix += f" | {chat_status}"
        print(f"[{idx+1:3d}/{len(all_cases)}] {status:50s} {tid}{suffix}")

    total = len(all_cases)
    passes = compile_ok_expected + compile_failures_expected
    print(f"\n=== SUMMARY ===")
    print(f"Total:                    {total}")
    print(f"Compile pass:             {compile_ok_expected + compile_ok_unexpected}")
    print(f"Compile fail:             {compile_failures_expected + compile_failures_unexpected}")
    print(f"Correct (expected match): {passes}/{total}  ({100*passes/total:.1f}%)")
    print(f"  - valid → valid:        {compile_ok_expected}")
    print(f"  - invalid → rejected:   {compile_failures_expected}")
    print(f"Incorrect:                {total - passes}")
    print(f"  - valid → rejected:     {compile_ok_unexpected}  (false negatives)")
    print(f"  - invalid → accepted:   {compile_failures_unexpected}  (false positives — DANGER)")

    (RESULT_DIR / "results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    print(f"\nResults written to {RESULT_DIR/'results.json'}")

    if do_deploy:
        print(f"\n=== DEPLOY ===")
        print(f"Deploy OK:   {deploy_ok}")
        print(f"Deploy FAIL: {deploy_fail}")

    false_positives = [r for r in results if "FAIL (expected rejection" in r["status"]]
    false_negatives = [r for r in results if "FAIL (expected valid" in r["status"]]
    if false_positives:
        print(f"\n--- FALSE POSITIVES ({len(false_positives)}) — compiler let bad YAML through ---")
        for fp in false_positives:
            print(f"  {fp['test_id']} ({fp['category']})")
    if false_negatives:
        print(f"\n--- FALSE NEGATIVES ({len(false_negatives)}) — compiler rejected valid YAML ---")
        for fn in false_negatives:
            print(f"  {fn['test_id']} ({fn['category']}): {fn['compile_msg'][:200]}")


if __name__ == "__main__":
    import sys
    do_deploy = "--deploy" in sys.argv
    do_chat = "--chat" in sys.argv
    run(do_deploy=do_deploy, do_chat=do_chat)
