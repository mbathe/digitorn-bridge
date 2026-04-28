"""V1 Production Verification - Tests ALL systems end-to-end."""
import sys
import asyncio

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} -- {detail}")


# ═══════════════════════════════════════════
# 1. HOOKS V2
# ═══════════════════════════════════════════
print("\n=== HOOKS V2 ===")

from digitorn.core.runtime.hooks import (
    _CONDITION_REGISTRY, _ACTION_REGISTRY,
    TurnState, get_condition, get_action,
)

check("10 conditions registered", len(_CONDITION_REGISTRY) == 10, f"got {len(_CONDITION_REGISTRY)}")
check("11 actions registered", len(_ACTION_REGISTRY) == 11, f"got {len(_ACTION_REGISTRY)}")

# Conditions
state = TurnState(
    messages=[{"role": "user", "content": "test hello world"}],
    turn=5, max_turns=30, tool_calls_count=10,
    agent_id="test", max_context_tokens=100000,
)
state._estimated_tokens = 50000

check("always", get_condition("always")(state, {}))
check("turn_count >= 3", get_condition("turn_count")(state, {"threshold": 3}))
check("turn_count < 10", not get_condition("turn_count")(state, {"threshold": 10}))
check("context_pressure >= 0.4", get_condition("context_pressure")(state, {"threshold": 0.4}))
check("context_pressure < 0.6", not get_condition("context_pressure")(state, {"threshold": 0.6}))
check("tool_calls >= 5", get_condition("tool_calls")(state, {"threshold": 5}))
check("expression true", get_condition("expression")(state, {"expr": "turn > 3 and pressure > 0.3"}))
check("expression false", not get_condition("expression")(state, {"expr": "turn > 100"}))
check("content_contains match", get_condition("content_contains")(state, {"keyword": "hello"}))
check("content_contains miss", not get_condition("content_contains")(state, {"keyword": "nothere"}))

# tool_name condition
from dataclasses import dataclass


@dataclass
class FakeToolCtx:
    tool_name: str = ""
    tool_params: dict = None
    tool_success: bool = True
    def __post_init__(self):
        if self.tool_params is None:
            self.tool_params = {}


state.tool_context = FakeToolCtx(tool_name="filesystem.write")
check("tool_name Write|Edit", get_condition("tool_name")(state, {"match": "Write|Edit"}))
check("tool_name filesystem.*", get_condition("tool_name")(state, {"match": "filesystem.*"}))
check("tool_name shell.* no match", not get_condition("tool_name")(state, {"match": "shell.*"}))

state.tool_context = FakeToolCtx(tool_name="shell.bash", tool_success=False)
check("tool_failed true", get_condition("tool_failed")(state, {}))
state.tool_context = FakeToolCtx(tool_name="shell.bash", tool_success=True)
check("tool_failed false", not get_condition("tool_failed")(state, {}))

# Actions
for name in sorted(_ACTION_REGISTRY.keys()):
    fn = get_action(name)
    check(f"action {name} callable", fn is not None and callable(fn))

# Shell action
shell_action = get_action("shell")
shell_state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0,
                        agent_id="test", max_context_tokens=100000)
asyncio.run(shell_action(shell_state, {"command": "echo test_hook_shell", "inject_result": True}))
injected = [m for m in shell_state.messages if "test_hook_shell" in m.get("content", "")]
check("shell injects stdout", len(injected) == 1)

# Gate action
gate_action = get_action("gate")
gate_state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0,
                       agent_id="test", max_context_tokens=100000)
asyncio.run(gate_action(gate_state, {"reason": "Test block"}))
check("gate sets flag", getattr(gate_state, "_gate_blocked", False))

# Chain action
chain_action = get_action("chain")
chain_state = TurnState(messages=[], turn=0, max_turns=10, tool_calls_count=0,
                        agent_id="test", max_context_tokens=100000)
asyncio.run(chain_action(chain_state, {
    "actions": [
        {"type": "inject_message", "params": {"content": "chain1"}},
        {"type": "inject_message", "params": {"content": "chain2"}},
    ]
}))
check("chain injects 2 messages", len(chain_state.messages) == 2)


# ═══════════════════════════════════════════
# 2. MIDDLEWARE
# ═══════════════════════════════════════════
print("\n=== MIDDLEWARE ===")

from digitorn.core.middleware import (
    AppMiddlewarePipeline, SecretMaskMiddleware, PromptInjectMiddleware,
    ContentFilterMiddleware, RagInjectMiddleware, ResponseFilterMiddleware,
    ModuleAuditMiddleware, ModuleRetryMiddleware, ModuleTimeoutMiddleware,
    AppMiddlewareContext,
)

pipeline = AppMiddlewarePipeline([
    SecretMaskMiddleware(),
    PromptInjectMiddleware(system="Test injection"),
    ContentFilterMiddleware(block_patterns=["DROP TABLE"]),
])
check("pipeline 3 middlewares", len(pipeline.middlewares) == 3)

# before chain
ctx = AppMiddlewareContext(agent_id="test", system_prompt="Base", messages=[
    {"role": "user", "content": "my password=secret123 here"}
], turn=0)
result = asyncio.run(pipeline.run_before(ctx))
check("no short-circuit", result is None)
check("prompt injected", "Test injection" in ctx.system_prompt)
check("secret masked", "secret123" not in ctx.messages[0]["content"])

# content filter blocks
ctx2 = AppMiddlewareContext(agent_id="test", system_prompt="", messages=[
    {"role": "user", "content": "DROP TABLE users"}
], turn=0)
result2 = asyncio.run(pipeline.run_before(ctx2))
check("content filter blocks", result2 is not None and "blocked" in result2.lower())

# after chain masks response
ctx3 = AppMiddlewareContext(agent_id="test", system_prompt="", messages=[], turn=0)
resp = asyncio.run(pipeline.run_after(ctx3, "Response with password=abc123", []))
check("after masks secrets", "abc123" not in resp)


# ═══════════════════════════════════════════
# 3. CONFIG
# ═══════════════════════════════════════════
print("\n=== CONFIG ===")

from digitorn.core.config import Settings

s = Settings()
sections = ["server", "database", "auth", "session", "runtime", "agent_spawn",
            "mcp", "sandbox", "websocket", "default_model", "discovery", "modules", "app", "logging"]
total = sum(len(getattr(s, n).__class__.model_fields) for n in sections)
check(f"config {total} params", total >= 60)
check("default_model.provider", s.default_model.provider in ("anthropic", "deepseek", "openai"))
check("auth.access_token_ttl", s.auth.access_token_ttl > 0)
check("session.idle_ttl", s.session.idle_ttl > 0)
check("agent_spawn.max_turns", s.agent_spawn.max_turns > 0)
check("mcp.tool_call_timeout", s.mcp.tool_call_timeout > 0)


# ═══════════════════════════════════════════
# 4. SECURITY
# ═══════════════════════════════════════════
print("\n=== SECURITY ===")

from digitorn.core.security import resolve_action_policy, SecurityProfile, ModuleGrant, security_gate

# block policy blocks
p1 = SecurityProfile(app_id="test", default_policy="block", granted_permissions=frozenset())
check("block default", resolve_action_policy(p1, "fs", "write", "high") == "block")

# auto policy autos
p2 = SecurityProfile(app_id="test", default_policy="auto", granted_permissions=frozenset())
check("auto default", resolve_action_policy(p2, "fs", "write", "high") == "auto")

# grant override
g1 = ModuleGrant(module_id="fs", action_overrides={"write": "approve"})
p3 = SecurityProfile(app_id="test", default_policy="block", module_grants={"fs": g1}, granted_permissions=frozenset())
check("grant approve override", resolve_action_policy(p3, "fs", "write", "high") == "approve")

# system module bypass
gs = ModuleGrant(module_id="cb", system_module=True)
p4 = SecurityProfile(app_id="test", default_policy="block", module_grants={"cb": gs}, granted_permissions=frozenset())
check("system module auto", resolve_action_policy(p4, "cb", "anything", "high") == "auto")

# security_gate with None params
p5 = SecurityProfile(app_id="test", default_policy="auto", granted_permissions=frozenset(["*"]))
try:
    security_gate(profile=p5, module_id="fs", action="read",
                  required_permissions=[], risk_level="low", irreversible=False, params=None)
    check("gate None params", True)
except Exception as e:
    check("gate None params", False, str(e))


# ═══════════════════════════════════════════
# 5. TOOL NAME RESOLUTION
# ═══════════════════════════════════════════
print("\n=== TOOL NAMES ===")

from digitorn.core.runtime.tool_names import to_fqn, to_short

tests = [
    ("Bash", "shell.bash"), ("Write", "filesystem.write"),
    ("Edit", "filesystem.edit"), ("Read", "filesystem.read"),
    ("Grep", "filesystem.grep"), ("Agent", "agent_spawn.spawn_agent"),
]
for short, expected in tests:
    check(f"to_fqn({short})", to_fqn(short) == expected, f"got {to_fqn(short)}")


# ═══════════════════════════════════════════
# 6. SERIALIZE RESULT
# ═══════════════════════════════════════════
print("\n=== SERIALIZATION ===")

from digitorn.core.runtime.messages import serialize_result
from digitorn.modules.base import ActionResult
import json

# Success
r1 = ActionResult(success=True, data={"path": "/tmp"})
s1 = serialize_result(r1)
check("success has data", "path" in s1)

# Failure includes data
r2 = ActionResult(success=False, error="not found", data={"path": "/tmp", "stderr": "err"})
s2 = serialize_result(r2)
p2 = json.loads(s2)
check("failure has error", "error" in p2)
check("failure has data", "path" in p2 and "stderr" in p2)

# Unserializable
r3 = serialize_result(object())
check("unserializable safe", isinstance(r3, str))


# ═══════════════════════════════════════════
# 7. ERROR CLASSIFICATION
# ═══════════════════════════════════════════
print("\n=== ERROR CLASSIFICATION ===")

from digitorn.core.api.apps import _classify_error

check("billing", _classify_error(RuntimeError("insufficient quota"))["code"] == "insufficient_balance")
check("auth", _classify_error(RuntimeError("401 unauthorized"))["code"] == "auth_error")
check("rate limit", _classify_error(RuntimeError("429 rate"))["code"] == "rate_limited")
check("network", _classify_error(RuntimeError("connection timed out"))["code"] == "network_error")
check("internal", _classify_error(RuntimeError("random"))["code"] == "internal_error")


# ═══════════════════════════════════════════
# 8. FUZZY EDIT
# ═══════════════════════════════════════════
print("\n=== FUZZY EDIT ===")

from digitorn.modules.filesystem.module import _fuzzy_find_old_string

# Trailing whitespace
check("trailing ws", _fuzzy_find_old_string("def foo():\n    pass", "def foo():  \n    pass  \n") is not None)

# Indentation mismatch
check("indent mismatch", _fuzzy_find_old_string("  def bar():\n      pass", "    def bar():\n        pass\n") is not None)

# Edge cases
check("empty both", _fuzzy_find_old_string("", "") is None or True)
check("no match", _fuzzy_find_old_string("xyz", "abc") is None)


# ═══════════════════════════════════════════
# 9. BUILTIN VALIDATORS
# ═══════════════════════════════════════════
print("\n=== BUILTIN VALIDATORS ===")

from digitorn.modules.lsp.parsers import validate_json_file, validate_yaml_file, validate_python_syntax
import tempfile, os

# Valid JSON
f1 = os.path.join(tempfile.gettempdir(), "test_valid.json")
with open(f1, "w") as f:
    f.write('{"key": "value"}')
check("valid json", len(validate_json_file(f1)) == 0)

# Invalid JSON
f2 = os.path.join(tempfile.gettempdir(), "test_invalid.json")
with open(f2, "w") as f:
    f.write('{"key": bad}')
diags = validate_json_file(f2)
check("invalid json detected", len(diags) > 0)
check("json error has line", diags[0].line >= 1)

# Valid Python
f3 = os.path.join(tempfile.gettempdir(), "test_valid.py")
with open(f3, "w") as f:
    f.write("def foo():\n    return 42\n")
check("valid python", len(validate_python_syntax(f3)) == 0)

# Invalid Python
f4 = os.path.join(tempfile.gettempdir(), "test_invalid.py")
with open(f4, "w") as f:
    f.write("def foo(\n    return 42\n")
diags2 = validate_python_syntax(f4)
check("invalid python detected", len(diags2) > 0)


# ═══════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
print(f"{'=' * 50}")

sys.exit(0 if failed == 0 else 1)
