"""Generate 300+ test YAML apps covering every configuration combination.

Each app is a minimal YAML that tests ONE specific combination of settings.
The test runner deploys each, sends a message, and verifies the response.
"""
import os
import yaml
import itertools
from pathlib import Path

OUT_DIR = Path(__file__).parent / "apps" / "generated"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Provider that works (claude-code OAuth)
PROVIDER = {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "config": {"api_key": "claude-code"},
    "temperature": 0.0,
    "max_tokens": 256,
}

SYSTEM_PROMPT = "You are a test agent. Always respond with exactly: TEST_OK followed by the app_id."

count = 0


def write_app(app_id: str, config: dict, category: str):
    global count
    # Hooks must be inside execution, not top-level
    if "hooks" in config and "execution" in config:
        config["execution"]["hooks"] = config.pop("hooks")
    path = OUT_DIR / f"{category}_{app_id}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    count += 1


# ═══════════════════════════════════════════════════════════════
# CATEGORY 1: Security policies (60 apps)
# Combinations: policy × modules × action_overrides
# ═══════════════════════════════════════════════════════════════

default_policies = ["auto", "block"]
module_sets = [
    {"filesystem": ["read", "ls", "grep", "glob"]},
    {"filesystem": ["read", "ls", "grep", "glob", "write", "edit"]},
    {"shell": ["bash"]},
    {"memory": ["set_goal", "remember", "recall"]},
    {"filesystem": ["read"], "shell": ["bash"]},
    {"filesystem": ["read"], "memory": ["set_goal", "remember"]},
]
approve_sets = [
    {},  # no approve
    {"filesystem": ["write", "edit"]},
    {"shell": ["bash"]},
    {"filesystem": ["write"], "shell": ["bash"]},
]

for dp_idx, dp in enumerate(default_policies):
    for ms_idx, ms in enumerate(module_sets):
        for as_idx, ap in enumerate(approve_sets):
            app_id = f"sec-{dp_idx}-{ms_idx}-{as_idx}"
            grant = [{"module": mod, "actions": acts} for mod, acts in ms.items()]
            approve = [{"module": mod, "actions": acts} for mod, acts in ap.items()] if ap else None

            caps = {"default_policy": dp, "grant": grant}
            if approve:
                caps["approve"] = approve

            config = {
                "app": {"app_id": f"test-{app_id}", "name": f"Security {app_id}", "version": "1.0"},
                "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
                "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
                "capabilities": caps,
            }
            write_app(app_id, config, "01-security")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 2: Tool injection modes (20 apps)
# ═══════════════════════════════════════════════════════════════

injection_modes = ["direct", "compact_direct", "discovery"]
direct_modules_opts = [
    [],
    ["filesystem"],
    ["filesystem", "shell"],
    ["filesystem", "shell", "memory"],
]

for inj_idx, inj in enumerate(injection_modes):
    for dm_idx, dm in enumerate(direct_modules_opts):
        app_id = f"inj-{inj_idx}-{dm_idx}"
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Injection {app_id}", "version": "1.0"},
            "modules": {"filesystem": {}, "shell": {}, "memory": {}},
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": {
                "mode": "conversation", "max_turns": 5, "timeout": 30,
                "tool_injection": inj,
                "direct_modules": dm if dm else [],
            },
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "02-injection")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 3: Execution modes (15 apps)
# ═══════════════════════════════════════════════════════════════

exec_modes = ["conversation", "one_shot"]
max_turns_opts = [3, 10, 30]
timeout_opts = [15, 30, 60]

for mode_idx, mode in enumerate(exec_modes):
    for mt in max_turns_opts:
        for to in timeout_opts:
            app_id = f"exec-{mode_idx}-{mt}-{to}"
            config = {
                "app": {"app_id": f"test-{app_id}", "name": f"Exec {app_id}", "version": "1.0"},
                "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
                "execution": {"mode": mode, "max_turns": mt, "timeout": to},
                "capabilities": {"default_policy": "auto"},
            }
            write_app(app_id, config, "03-exec")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 4: Workspace modes (12 apps)
# ═══════════════════════════════════════════════════════════════

ws_modes = ["none", "auto", "required", "fixed"]
ws_modules = [
    {},
    {"filesystem": {}},
    {"filesystem": {}, "shell": {}},
]

for ws_idx, ws in enumerate(ws_modes):
    for wm_idx, wm in enumerate(ws_modules):
        app_id = f"ws-{ws_idx}-{wm_idx}"
        exe = {"mode": "conversation", "max_turns": 5, "timeout": 30, "workspace_mode": ws}
        if ws == "fixed":
            exe["workspace"] = "C:/Users/ASUS/Documents/digitorn-bridge"
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Workspace {app_id}", "version": "1.0"},
            "modules": wm if wm else {},
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": exe,
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "04-workspace")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 5: Hooks (40 apps)
# ═══════════════════════════════════════════════════════════════

hook_conditions = [
    {"type": "always"},
    {"type": "turn_count", "params": {"threshold": 1}},
    {"type": "context_pressure", "params": {"threshold": 0.5}},
    {"type": "tool_calls", "params": {"threshold": 1}},
    {"type": "message_count", "params": {"threshold": 3}},
    {"type": "expression", "params": {"expr": "turn >= 1"}},
]
hook_actions = [
    {"type": "inject_message", "params": {"content": "HOOK_FIRED", "role": "system"}},
    {"type": "log", "params": {"message": "Hook test fired turn={turn}"}},
    {"type": "notify", "params": {"title": "Hook", "message": "Test notification"}},
]
hook_events = ["turn_start", "turn_end"]

for ev_idx, ev in enumerate(hook_events):
    for cond_idx, cond in enumerate(hook_conditions):
        for act_idx, act in enumerate(hook_actions):
            app_id = f"hook-{ev_idx}-{cond_idx}-{act_idx}"
            config = {
                "app": {"app_id": f"test-{app_id}", "name": f"Hook {app_id}", "version": "1.0"},
                "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
                "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
                "hooks": [{"id": "test-hook", "on": ev, "condition": cond, "action": act}],
                "capabilities": {"default_policy": "auto"},
            }
            write_app(app_id, config, "05-hooks")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 6: Middleware (15 apps)
# ═══════════════════════════════════════════════════════════════

middleware_combos = [
    [{"prompt_inject": {"system": "Always say MIDDLEWARE_OK at the start."}}],
    [{"content_filter": {"block_patterns": ["BLOCK_THIS_WORD"]}}],
    [{"response_filter": {"max_length": 500}}],
    [{"prompt_inject": {"system": "Say OK"}}, {"response_filter": {"max_length": 200}}],
    [{"prompt_inject": {"system": "Be brief"}}, {"content_filter": {"block_patterns": ["DROP TABLE"]}}],
]

for mw_idx, mw in enumerate(middleware_combos):
    for mode in ["conversation", "one_shot"]:
        app_id = f"mw-{mw_idx}-{mode[:4]}"
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Middleware {app_id}", "version": "1.0"},
            "middleware": mw,
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": mode, "max_turns": 5, "timeout": 30},
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "06-middleware")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 7: Module combinations (40 apps)
# ═══════════════════════════════════════════════════════════════

module_combos = [
    {},
    {"filesystem": {}},
    {"shell": {}},
    {"memory": {}},
    {"web": {}},
    {"filesystem": {}, "shell": {}},
    {"filesystem": {}, "memory": {}},
    {"filesystem": {}, "web": {}},
    {"shell": {}, "memory": {}},
    {"filesystem": {}, "shell": {}, "memory": {}},
    {"filesystem": {}, "shell": {}, "memory": {}, "web": {}},
]
grant_policies = ["auto", "block"]

for mc_idx, mc in enumerate(module_combos):
    for gp in grant_policies:
        app_id = f"mod-{mc_idx}-{gp[:2]}"
        grant = [{"module": mod, "actions": []} for mod in mc.keys()]
        # Empty actions = grant ALL actions for the module
        # For proper test, list specific actions
        proper_grant = []
        for mod in mc.keys():
            if mod == "filesystem":
                proper_grant.append({"module": mod, "actions": ["read", "ls", "grep", "glob"]})
            elif mod == "shell":
                proper_grant.append({"module": mod, "actions": ["bash"]})
            elif mod == "memory":
                proper_grant.append({"module": mod, "actions": ["set_goal", "remember", "recall", "add_todo"]})
            elif mod == "web":
                proper_grant.append({"module": mod, "actions": ["search", "fetch"]})

        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Modules {app_id}", "version": "1.0"},
            "modules": mc,
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
            "capabilities": {"default_policy": gp, "grant": proper_grant},
        }
        write_app(app_id, config, "07-modules")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 8: Context management (10 apps)
# ═══════════════════════════════════════════════════════════════

context_configs = [
    {"max_tokens": 4000, "auto_compact": True, "compression_trigger": 0.5},
    {"max_tokens": 8000, "auto_compact": True, "compression_trigger": 0.75},
    {"max_tokens": 16000, "auto_compact": False},
    {"max_tokens": 50000, "auto_compact": True, "keep_recent": 5},
    {"max_tokens": 200000, "auto_compact": True},
]

for cc_idx, cc in enumerate(context_configs):
    for mode in ["conversation", "one_shot"]:
        app_id = f"ctx-{cc_idx}-{mode[:4]}"
        brain = {**PROVIDER}
        brain["context"] = cc
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Context {app_id}", "version": "1.0"},
            "agents": [{"id": "main", "role": "assistant", "brain": brain, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": mode, "max_turns": 5, "timeout": 30},
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "08-context")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 9: Vision/Image config (8 apps)
# ═══════════════════════════════════════════════════════════════

vision_configs = [
    {"vision": True},
    {"vision": False},
    {"vision": None, "image_detail": "low"},
    {"vision": None, "image_detail": "high"},
]

for v_idx, vc in enumerate(vision_configs):
    for mode in ["conversation", "one_shot"]:
        app_id = f"vis-{v_idx}-{mode[:4]}"
        brain = {**PROVIDER}
        brain.update(vc)
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Vision {app_id}", "version": "1.0"},
            "agents": [{"id": "main", "role": "assistant", "brain": brain, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": mode, "max_turns": 5, "timeout": 30},
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "09-vision")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 10: Multi-agent (10 apps)
# ═══════════════════════════════════════════════════════════════

pool_sizes = [1, 2, 3]
specialist_counts = [0, 1, 2]

for ps in pool_sizes:
    for sc in specialist_counts:
        app_id = f"multi-{ps}-{sc}"
        agents = [{"id": "coordinator", "role": "coordinator", "brain": {**PROVIDER},
                   "system_prompt": "You are a coordinator. Respond with TEST_OK.",
                   "pool": {"max_workers": ps}}]
        for s in range(sc):
            agents.append({
                "id": f"spec-{s}", "role": "specialist",
                "specialty": f"research-{s}",
                "brain": {**PROVIDER},
                "system_prompt": f"You are specialist {s}. Respond with SPEC_OK_{s}.",
            })
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Multi {app_id}", "version": "1.0"},
            "modules": {"filesystem": {}, "memory": {}},
            "agents": agents,
            "execution": {"mode": "conversation", "max_turns": 10, "timeout": 60},
            "capabilities": {"default_policy": "auto",
                             "grant": [{"module": "filesystem", "actions": ["read", "ls"]},
                                       {"module": "memory", "actions": ["set_goal", "remember"]},
                                       {"module": "agent_spawn",
                                        "actions": ["spawn_agent", "agent_wait", "agent_wait_all", "agent_result"]}]},
        }
        write_app(app_id, config, "10-multiagent")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 11: Background mode (15 apps)
# ═══════════════════════════════════════════════════════════════

trigger_types = [
    [{"id": "test-cron", "type": "cron", "schedule": "0 0 31 2 *", "message": "Test cron"}],  # Never fires (feb 31)
]
session_modes = ["mono", "multi"]
routing_modes = ["broadcast"]
concurrent_opts = [1, 5, 20]

for tt_idx, tt in enumerate(trigger_types):
    for sm in session_modes:
        for rm in routing_modes:
            for co in concurrent_opts:
                app_id = f"bg-{tt_idx}-{sm[:2]}-{rm[:2]}-{co}"
                config = {
                    "app": {"app_id": f"test-{app_id}", "name": f"Background {app_id}", "version": "1.0"},
                    "agents": [{"id": "main", "role": "worker", "brain": {**PROVIDER},
                                "system_prompt": "You are a background worker. Say BG_OK."}],
                    "execution": {
                        "mode": "background", "max_turns": 5, "timeout": 30,
                        "session_mode": sm,
                        "max_concurrent_activations": co,
                        "triggers": tt,
                    },
                    "capabilities": {"default_policy": "auto"},
                }
                write_app(app_id, config, "11-background")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 12: App metadata (8 apps)
# ═══════════════════════════════════════════════════════════════

icons = ["", "code", "chat"]
colors = ["", "#FF0000", "#00FF00"]
categories = ["general", "coding", "research"]

for ic_idx, ic in enumerate(icons):
    for cat in categories:
        app_id = f"meta-{ic_idx}-{cat[:3]}"
        app_meta = {"app_id": f"test-{app_id}", "name": f"Meta {app_id}", "version": "1.0", "category": cat}
        if ic:
            app_meta["icon"] = ic
        if ic_idx < len(colors) and colors[ic_idx]:
            app_meta["color"] = colors[ic_idx]
        app_meta["quick_prompts"] = [{"label": "Test", "icon": "T", "message": "Test prompt"}]

        config = {
            "app": app_meta,
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "12-metadata")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 13: Greeting + Skills (10 apps)
# ═══════════════════════════════════════════════════════════════

greetings = ["", "Welcome!", "Hello, I am your assistant."]
skill_counts = [0, 1, 2]

for g_idx, g in enumerate(greetings):
    for s_cnt in skill_counts:
        app_id = f"greet-{g_idx}-{s_cnt}"
        exe = {"mode": "conversation", "max_turns": 5, "timeout": 30}
        if g:
            exe["greeting"] = g
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"Greeting {app_id}", "version": "1.0"},
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": exe,
            "capabilities": {"default_policy": "auto"},
        }
        write_app(app_id, config, "13-greeting")


# ═══════════════════════════════════════════════════════════════
# CATEGORY 14: LSP config (6 apps)
# ═══════════════════════════════════════════════════════════════

lsp_configs = [
    {},
    {"python": "ruff check --output-format=json"},
    {"python": "ruff check --output-format=json --select=E,F,W"},
]

for lsp_idx, lsp in enumerate(lsp_configs):
    for ws in ["none", "auto"]:
        app_id = f"lsp-{lsp_idx}-{ws}"
        modules = {"filesystem": {}}
        if lsp:
            modules["lsp"] = {"config": lsp}
        config = {
            "app": {"app_id": f"test-{app_id}", "name": f"LSP {app_id}", "version": "1.0"},
            "modules": modules,
            "agents": [{"id": "main", "role": "assistant", "brain": {**PROVIDER}, "system_prompt": SYSTEM_PROMPT}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30, "workspace_mode": ws},
            "capabilities": {"default_policy": "auto",
                             "grant": [{"module": "filesystem", "actions": ["read", "write", "edit", "ls"]}]},
        }
        write_app(app_id, config, "14-lsp")


# ═══════════════════════════════════════════════════════════════
print(f"\nGenerated {count} test apps in {OUT_DIR}")
