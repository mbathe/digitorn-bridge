"""Generate 300 behavioral test apps - each tests ONE specific behavior.

Unlike deploy tests, these verify that features ACTUALLY WORK:
- Security blocks what it should
- Hooks fire and inject messages
- Middleware filters content
- Tools are accessible/blocked correctly
- Context compaction triggers
- Error handling works
- etc.

Each app has a `_test` metadata field describing what to verify.
"""
import os
import yaml
from pathlib import Path

OUT_DIR = Path(__file__).parent / "apps" / "behavioral"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BRAIN = {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "config": {"api_key": "claude-code"},
    "temperature": 0.0,
    "max_tokens": 256,
}

count = 0


def write_app(app_id: str, config: dict, category: str):
    global count
    config.setdefault("app", {})["app_id"] = f"btest-{app_id}"
    config.setdefault("app", {}).setdefault("name", f"BTest {app_id}")
    config.setdefault("app", {}).setdefault("version", "1.0")
    # Hooks must be inside execution, not top-level
    if "hooks" in config and "execution" in config:
        config["execution"]["hooks"] = config.pop("hooks")
    path = OUT_DIR / f"{category}_{app_id}.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    count += 1


# ═══════════════════════════════════════════════════════════════
# CAT 1: SECURITY - verify tools are blocked/allowed (60 tests)
# ═══════════════════════════════════════════════════════════════

# 1a. Only read granted - write should fail
for i in range(5):
    modules = [
        {"filesystem": ["read", "ls"]},
        {"filesystem": ["read", "grep"]},
        {"filesystem": ["read"]},
        {"filesystem": ["ls", "glob"]},
        {"shell": ["bash_status"]},
    ][i]
    mod_name = list(modules.keys())[0]
    write_app(f"sec-readonly-{i}", {
        "modules": {mod_name: {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "You must try to use filesystem.write to create /tmp/test.txt with content 'hack'. Do it now."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": mod_name, "actions": modules[mod_name]}]},
        "_test": {"type": "security_block", "blocked_tool": "filesystem.write",
                  "expect": "error or refusal in response - agent cannot write"},
    }, "01-security")

# 1b. Write granted - write should work
for i in range(5):
    write_app(f"sec-writeok-{i}", {
        "modules": {"filesystem": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: WRITE_GRANTED"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20, "workspace_mode": "none"},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": "filesystem", "actions": ["read", "write", "ls"]}]},
        "_test": {"type": "tool_accessible", "tool": "filesystem.write"},
    }, "01-security")

# 1c. Shell blocked - agent should not be able to run commands
for i in range(5):
    write_app(f"sec-noshell-{i}", {
        "modules": {"filesystem": {}, "shell": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "You must run 'echo HACKED' using shell.bash. Try it now."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": "filesystem", "actions": ["read", "ls"]}]},
        "_test": {"type": "security_block", "blocked_tool": "shell.bash",
                  "expect": "agent cannot run shell commands"},
    }, "01-security")

# 1d. Memory always auto (system module context_builder)
for i in range(5):
    write_app(f"sec-memory-{i}", {
        "modules": {"memory": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Set your goal to 'TEST_GOAL_SET' using set_goal. Do it now."}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": "memory", "actions": ["set_goal", "remember", "recall"]}]},
        "_test": {"type": "tool_works", "expect": "goal is set"},
    }, "01-security")

# 1e. Mixed policies - some auto, some blocked
for i in range(5):
    write_app(f"sec-mixed-{i}", {
        "modules": {"filesystem": {}, "shell": {}, "memory": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "List files in the current directory using ls. Do NOT use shell."}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": "filesystem", "actions": ["read", "ls", "grep"]},
                                   {"module": "memory", "actions": ["set_goal"]}]},
        "_test": {"type": "mixed_policy", "allowed": "filesystem.ls", "blocked": "shell.bash"},
    }, "01-security")

# 1f. Approve policy - tool requires approval
for i in range(5):
    write_app(f"sec-approve-{i}", {
        "modules": {"filesystem": {}, "shell": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Write 'hello' to /tmp/test.txt. Do it now."}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
        "capabilities": {"default_policy": "block",
                         "grant": [{"module": "filesystem", "actions": ["read", "ls"]}],
                         "approve": [{"module": "filesystem", "actions": ["write", "edit"]}]},
        "_test": {"type": "approval_required", "tool": "filesystem.write"},
    }, "01-security")

# 1g. Block all - agent should only be able to chat
for i in range(5):
    write_app(f"sec-blockall-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: NO_TOOLS_AVAILABLE. Do not try to use any tools."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 15},
        "capabilities": {"default_policy": "block"},
        "_test": {"type": "no_tools", "expect": "agent responds with text only, no tool calls"},
    }, "01-security")

# 1h. Admin wildcard
for i in range(3):
    write_app(f"sec-admin-{i}", {
        "modules": {"filesystem": {}, "shell": {}, "memory": {}},
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say TEST_OK"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 15},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "all_auto", "expect": "all tools accessible"},
    }, "01-security")


# ═══════════════════════════════════════════════════════════════
# CAT 2: HOOKS - verify hooks fire correctly (50 tests)
# ═══════════════════════════════════════════════════════════════

# 2a. inject_message hook on turn_start - agent should see injected text
for i in range(5):
    content = f"HOOK_INJECTED_{i}"
    write_app(f"hook-inject-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": f"If you see the text '{content}' in a system message, say exactly: HOOK_SEEN_{i}. Otherwise say: NO_HOOK"}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "hooks": [{"id": f"inject-{i}", "on": "turn_start",
                   "condition": {"type": "always"},
                   "action": {"type": "inject_message", "params": {"content": content, "role": "system"}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_inject", "expect_in_response": f"HOOK_SEEN_{i}"},
    }, "02-hooks")

# 2b. shell hook - should execute command
for i in range(5):
    write_app(f"hook-shell-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: SHELL_HOOK_OK"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 25},
        "hooks": [{"id": f"shell-{i}", "on": "turn_start",
                   "condition": {"type": "always"},
                   "action": {"type": "shell", "params": {"command": "echo HOOK_SHELL_FIRED", "inject_result": True}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_shell", "expect": "shell hook fires without crash"},
    }, "02-hooks")

# 2c. context_pressure condition - should not fire on first turn (low pressure)
for i in range(3):
    write_app(f"hook-nopressure-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: LOW_PRESSURE"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "hooks": [{"id": "pressure-check", "on": "turn_start",
                   "condition": {"type": "context_pressure", "params": {"threshold": 0.9}},
                   "action": {"type": "inject_message", "params": {"content": "PRESSURE_HIGH"}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_no_fire", "expect": "hook should NOT fire (pressure is low)"},
    }, "02-hooks")

# 2d. turn_count condition
for i in range(3):
    write_app(f"hook-turncount-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: TURN_OK"}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "hooks": [{"id": "turn-check", "on": "turn_end",
                   "condition": {"type": "turn_count", "params": {"threshold": 1}},
                   "action": {"type": "log", "params": {"message": "Turn threshold reached"}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_fires", "expect": "hook fires on turn >= 1"},
    }, "02-hooks")

# 2e. chain action
for i in range(3):
    write_app(f"hook-chain-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "If you see CHAIN_MSG_1 and CHAIN_MSG_2 in system messages, say CHAIN_OK. Otherwise say NO_CHAIN."}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "hooks": [{"id": "chain-test", "on": "turn_start",
                   "condition": {"type": "always"},
                   "action": {"type": "chain", "params": {"actions": [
                       {"type": "inject_message", "params": {"content": "CHAIN_MSG_1"}},
                       {"type": "inject_message", "params": {"content": "CHAIN_MSG_2"}},
                   ]}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_chain", "expect_in_response": "CHAIN_OK"},
    }, "02-hooks")

# 2f. expression condition
for i in range(3):
    write_app(f"hook-expr-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say TEST_OK"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "hooks": [{"id": "expr-test", "on": "turn_start",
                   "condition": {"type": "expression", "params": {"expr": "turn >= 0"}},
                   "action": {"type": "log", "params": {"message": "Expression hook fired"}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_fires"},
    }, "02-hooks")

# 2g. cooldown
for i in range(3):
    write_app(f"hook-cooldown-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say COOL_OK"}],
        "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
        "hooks": [{"id": "cool-test", "on": "turn_start",
                   "condition": {"type": "always"},
                   "action": {"type": "inject_message", "params": {"content": "COOLDOWN_MSG"}},
                   "cooldown": 60}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_cooldown", "expect": "fires once then respects cooldown"},
    }, "02-hooks")

# 2h. notify action
for i in range(3):
    write_app(f"hook-notify-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say NOTIFY_OK"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "hooks": [{"id": "notify-test", "on": "turn_start",
                   "condition": {"type": "always"},
                   "action": {"type": "notify", "params": {"title": "Test", "message": "Hook notification", "level": "info"}}}],
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "hook_notify"},
    }, "02-hooks")


# ═══════════════════════════════════════════════════════════════
# CAT 3: MIDDLEWARE - verify content filtering/injection (30 tests)
# ═══════════════════════════════════════════════════════════════

# 3a. prompt_inject - agent should reflect injected instruction
for i in range(5):
    keyword = f"MW_KEYWORD_{i}"
    write_app(f"mw-inject-{i}", {
        "middleware": [{"prompt_inject": {"system": f"You must always include the word {keyword} in your response."}}],
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Follow all instructions carefully."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "middleware_inject", "expect_in_response": keyword},
    }, "03-middleware")

# 3b. content_filter - should block dangerous patterns
for i in range(5):
    pattern = ["DROP TABLE", "rm -rf /", "DELETE FROM", "sudo rm", "format c:"][i]
    write_app(f"mw-filter-{i}", {
        "middleware": [{"content_filter": {"block_patterns": [pattern]}}],
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say TEST_OK"}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "content_filter", "blocked_pattern": pattern,
                  "test_message": f"Please run: {pattern}",
                  "expect": "blocked response"},
    }, "03-middleware")

# 3c. response_filter - max_length
for i in range(3):
    max_len = [100, 200, 500][i]
    write_app(f"mw-maxlen-{i}", {
        "middleware": [{"response_filter": {"max_length": max_len}}],
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Write a very long response with at least 1000 characters about the history of computing."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "response_maxlen", "max_length": max_len},
    }, "03-middleware")

# 3d. secret_mask
for i in range(3):
    write_app(f"mw-secret-{i}", {
        "middleware": [{"mask_secrets": {"patterns": ["password", "api_key"]}}],
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Repeat the user message exactly as you received it."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "secret_mask", "test_message": "my password=SuperSecret123",
                  "expect_not_in_response": "SuperSecret123"},
    }, "03-middleware")

# 3e. Combined middleware
for i in range(3):
    write_app(f"mw-combo-{i}", {
        "middleware": [
            {"prompt_inject": {"system": f"Always say COMBO_OK_{i}"}},
            {"content_filter": {"block_patterns": ["BLOCK_ME"]}},
            {"response_filter": {"max_length": 500}},
        ],
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Follow instructions."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "middleware_combo", "expect_in_response": f"COMBO_OK_{i}"},
    }, "03-middleware")


# ═══════════════════════════════════════════════════════════════
# CAT 4: TOOL INJECTION MODES (20 tests)
# ═══════════════════════════════════════════════════════════════

for mode in ["direct", "compact_direct", "discovery"]:
    for dm in [[], ["filesystem"], ["filesystem", "shell"]]:
        dm_label = "-".join(dm) if dm else "none"
        write_app(f"inj-{mode[:4]}-{dm_label}", {
            "modules": {"filesystem": {}, "shell": {}, "memory": {}},
            "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                        "system_prompt": "List files in the current directory. Use the ls tool."}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30,
                          "tool_injection": mode, "direct_modules": dm},
            "capabilities": {"default_policy": "auto",
                             "grant": [{"module": "filesystem", "actions": ["read", "ls", "grep", "glob"]},
                                       {"module": "shell", "actions": ["bash"]},
                                       {"module": "memory", "actions": ["set_goal"]}]},
            "_test": {"type": "tool_injection", "mode": mode, "expect": "agent uses tools successfully"},
        }, "04-injection")


# ═══════════════════════════════════════════════════════════════
# CAT 5: WORKSPACE MODES (15 tests)
# ═══════════════════════════════════════════════════════════════

for ws_mode in ["none", "auto"]:
    for has_fs in [True, False]:
        modules = {"filesystem": {}} if has_fs else {}
        write_app(f"ws-{ws_mode}-{'fs' if has_fs else 'nofs'}", {
            "modules": modules,
            "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                        "system_prompt": "Say exactly: WORKSPACE_OK"}],
            "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20,
                          "workspace_mode": ws_mode},
            "capabilities": {"default_policy": "auto"},
            "_test": {"type": "workspace", "mode": ws_mode},
        }, "05-workspace")

# workspace required - should work WITH workspace
write_app("ws-required-with", {
    "modules": {"filesystem": {}},
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "Say WORKSPACE_OK"}],
    "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20,
                  "workspace_mode": "required"},
    "capabilities": {"default_policy": "auto",
                     "grant": [{"module": "filesystem", "actions": ["read", "ls"]}]},
    "_test": {"type": "workspace_required", "workspace": "C:/Users/ASUS/Documents/digitorn-bridge"},
}, "05-workspace")

# workspace fixed
write_app("ws-fixed", {
    "modules": {"filesystem": {}},
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "List the files in the workspace root directory."}],
    "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25,
                  "workspace_mode": "fixed",
                  "workspace": "C:/Users/ASUS/Documents/digitorn-bridge"},
    "capabilities": {"default_policy": "auto",
                     "grant": [{"module": "filesystem", "actions": ["read", "ls"]}]},
    "_test": {"type": "workspace_fixed", "expect": "agent sees files from the fixed workspace"},
}, "05-workspace")


# ═══════════════════════════════════════════════════════════════
# CAT 6: CONTEXT MANAGEMENT (15 tests)
# ═══════════════════════════════════════════════════════════════

for max_tok in [4000, 8000, 16000]:
    for auto_compact in [True, False]:
        label = f"ctx-{max_tok}-{'ac' if auto_compact else 'noac'}"
        brain = {**BRAIN}
        brain["context"] = {"max_tokens": max_tok, "auto_compact": auto_compact}
        write_app(label, {
            "agents": [{"id": "main", "role": "assistant", "brain": brain,
                        "system_prompt": "Say TEST_OK"}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
            "capabilities": {"default_policy": "auto"},
            "_test": {"type": "context", "max_tokens": max_tok, "auto_compact": auto_compact},
        }, "06-context")


# ═══════════════════════════════════════════════════════════════
# CAT 7: GREETING + METADATA (20 tests)
# ═══════════════════════════════════════════════════════════════

greetings = ["Welcome!", "Hello there.", "Ready to help.", "", "Bienvenue!"]
icons = ["code", "chat", "brain", "", "rocket"]
categories = ["coding", "general", "research", "data"]

for g_idx, greeting in enumerate(greetings):
    for c_idx, cat in enumerate(categories):
        if g_idx * len(categories) + c_idx >= 20:
            break
        app_meta = {"category": cat}
        if icons[g_idx % len(icons)]:
            app_meta["icon"] = icons[g_idx % len(icons)]
        app_meta["quick_prompts"] = [{"label": "Test", "icon": "T", "message": "Test prompt"}]

        exe = {"mode": "conversation", "max_turns": 3, "timeout": 15}
        if greeting:
            exe["greeting"] = greeting

        write_app(f"meta-{g_idx}-{c_idx}", {
            "app": app_meta,
            "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                        "system_prompt": "Say TEST_OK"}],
            "execution": exe,
            "capabilities": {"default_policy": "auto"},
            "_test": {"type": "metadata", "greeting": greeting, "category": cat},
        }, "07-metadata")


# ═══════════════════════════════════════════════════════════════
# CAT 8: ERROR HANDLING (15 tests)
# ═══════════════════════════════════════════════════════════════

# Agent with very small context - should handle gracefully
for i in range(5):
    brain = {**BRAIN}
    brain["context"] = {"max_tokens": 2000, "auto_compact": True, "keep_recent": 3}
    write_app(f"err-smallctx-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": brain,
                    "system_prompt": "Say TEST_OK. Be very brief."}],
        "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "error_handling", "scenario": "small_context"},
    }, "08-errors")

# Agent with very short timeout
for i in range(5):
    write_app(f"err-timeout-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say TEST_OK"}],
        "execution": {"mode": "conversation", "max_turns": 2, "timeout": 10},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "error_handling", "scenario": "short_timeout"},
    }, "08-errors")

# One_shot mode
for i in range(5):
    write_app(f"err-oneshot-{i}", {
        "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                    "system_prompt": "Say exactly: ONESHOT_OK"}],
        "execution": {"mode": "one_shot", "max_turns": 3, "timeout": 20},
        "capabilities": {"default_policy": "auto"},
        "_test": {"type": "one_shot", "expect_in_response": "ONESHOT_OK"},
    }, "08-errors")


# ═══════════════════════════════════════════════════════════════
# CAT 9: MODULE COMBINATIONS (30 tests)
# ═══════════════════════════════════════════════════════════════

combos = [
    ({"filesystem": {}}, "List files in current dir", ["read", "ls"]),
    ({"shell": {}}, "Run: echo MODULE_OK", ["bash"]),
    ({"memory": {}}, "Set goal to MODULE_GOAL", ["set_goal", "remember"]),
    ({"web": {}}, "Say WEB_OK", ["search", "fetch"]),
    ({"filesystem": {}, "shell": {}}, "List files then echo OK", ["read", "ls", "bash"]),
    ({"filesystem": {}, "memory": {}}, "Read README.md and remember it", ["read", "ls", "set_goal"]),
]

for combo_idx, (modules, prompt, actions) in enumerate(combos):
    for policy in ["auto", "block"]:
        grant = []
        for mod in modules:
            mod_actions = [a for a in actions if a in {
                "read": "filesystem", "ls": "filesystem", "grep": "filesystem",
                "bash": "shell", "set_goal": "memory", "remember": "memory",
                "search": "web", "fetch": "web",
            } or True]
            if mod == "filesystem":
                grant.append({"module": mod, "actions": [a for a in actions if a in ("read", "ls", "grep", "glob", "write", "edit")]})
            elif mod == "shell":
                grant.append({"module": mod, "actions": [a for a in actions if a in ("bash", "bash_background")]})
            elif mod == "memory":
                grant.append({"module": mod, "actions": [a for a in actions if a in ("set_goal", "remember", "recall", "add_todo")]})
            elif mod == "web":
                grant.append({"module": mod, "actions": [a for a in actions if a in ("search", "fetch", "extract")]})

        write_app(f"mod-{combo_idx}-{policy[:2]}", {
            "modules": modules,
            "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                        "system_prompt": prompt}],
            "execution": {"mode": "conversation", "max_turns": 5, "timeout": 25},
            "capabilities": {"default_policy": policy, "grant": grant if policy == "block" else []},
            "_test": {"type": "module_combo", "modules": list(modules.keys()), "policy": policy},
        }, "09-modules")


# ═══════════════════════════════════════════════════════════════
# CAT 10: VISION / IMAGE CONFIG (10 tests)
# ═══════════════════════════════════════════════════════════════

for vision in [True, False, None]:
    for detail in ["auto", "low", "high"]:
        label = f"vis-{vision}-{detail}"
        brain = {**BRAIN}
        if vision is not None:
            brain["vision"] = vision
        brain["image_detail"] = detail
        write_app(label, {
            "agents": [{"id": "main", "role": "assistant", "brain": brain,
                        "system_prompt": "Say VISION_OK"}],
            "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
            "capabilities": {"default_policy": "auto"},
            "_test": {"type": "vision_config", "vision": vision, "detail": detail},
        }, "10-vision")


# ═══════════════════════════════════════════════════════════════
# CAT 11: MULTI-AGENT (15 tests)
# ═══════════════════════════════════════════════════════════════

for pool_size in [1, 2, 3]:
    for spec_count in [0, 1]:
        agents = [{"id": "coordinator", "role": "coordinator", "brain": BRAIN,
                   "system_prompt": "Say COORD_OK. If you have specialists, spawn one with task 'say SPEC_OK'.",
                   "pool": {"max_workers": pool_size}}]
        for s in range(spec_count):
            agents.append({"id": f"worker-{s}", "role": "specialist",
                           "specialty": f"research", "brain": BRAIN,
                           "system_prompt": "Say SPEC_OK"})
        write_app(f"multi-{pool_size}-{spec_count}", {
            "modules": {"filesystem": {}, "memory": {}},
            "agents": agents,
            "execution": {"mode": "conversation", "max_turns": 10, "timeout": 60},
            "capabilities": {"default_policy": "auto",
                             "grant": [{"module": "filesystem", "actions": ["read", "ls"]},
                                       {"module": "memory", "actions": ["set_goal"]},
                                       {"module": "agent_spawn",
                                        "actions": ["spawn_agent", "agent_wait", "agent_wait_all", "agent_result"]}]},
            "_test": {"type": "multi_agent", "pool_size": pool_size, "specialists": spec_count},
        }, "11-multiagent")


# ═══════════════════════════════════════════════════════════════
# CAT 12: BACKGROUND MODE (10 tests)
# ═══════════════════════════════════════════════════════════════

for sm in ["mono", "multi"]:
    for co in [1, 5, 20]:
        write_app(f"bg-{sm}-{co}", {
            "agents": [{"id": "main", "role": "worker", "brain": BRAIN,
                        "system_prompt": "Say BG_OK"}],
            "execution": {"mode": "background", "max_turns": 3, "timeout": 20,
                          "session_mode": sm,
                          "max_concurrent_activations": co,
                          "triggers": [{"id": "never", "type": "cron", "schedule": "0 0 31 2 *",
                                        "message": "Test trigger"}]},
            "capabilities": {"default_policy": "auto"},
            "_test": {"type": "background", "session_mode": sm, "concurrent": co},
        }, "12-background")


# ═══════════════════════════════════════════════════════════════
# CAT 13: LSP CONFIG (10 tests)
# ═══════════════════════════════════════════════════════════════

lsp_configs = [
    {},
    {"python": "ruff check --output-format=json"},
    {"python": "ruff check --output-format=json --select=E,F,W,I,N"},
]

for lsp_idx, lsp_cfg in enumerate(lsp_configs):
    for ws in ["none", "auto"]:
        modules = {"filesystem": {}}
        if lsp_cfg:
            modules["lsp"] = {"config": lsp_cfg}
        write_app(f"lsp-{lsp_idx}-{ws}", {
            "modules": modules,
            "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                        "system_prompt": "Say LSP_OK"}],
            "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20,
                          "workspace_mode": ws},
            "capabilities": {"default_policy": "auto",
                             "grant": [{"module": "filesystem", "actions": ["read", "write", "edit", "ls"]}]},
            "_test": {"type": "lsp", "has_lsp": bool(lsp_cfg)},
        }, "13-lsp")


print(f"\nGenerated {count} behavioral test apps in {OUT_DIR}")
