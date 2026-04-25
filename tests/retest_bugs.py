"""Retest the 3 remaining bugs: hook_inject, middleware_inject, content_filter."""
import httpx, time, sys, yaml, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
client = httpx.Client(timeout=60)

r = client.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

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


def deploy_and_wait(config):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        yaml.dump(config, f)
        tmp = f.name
    app_id = config["app"]["app_id"]
    client.post(f"{B}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, headers=h)
    for _ in range(20):
        time.sleep(1.5)
        r = client.get(f"{B}/api/apps/{app_id}", headers=h)
        if r.status_code == 200:
            return True
    return False


def chat_and_get(app_id, message, wait=12):
    sid = f"rt-{int(time.time())}-{app_id[-5:]}"
    client.post(f"{B}/api/apps/{app_id}/sessions/{sid}/messages", json={"message": message}, headers=h)
    time.sleep(wait)
    r = client.get(f"{B}/api/apps/{app_id}/sessions/{sid}/history", headers=h)
    data = r.json().get("data", {})
    messages = data.get("messages", [])
    events = data.get("events", [])
    assistant = [m.get("content", "") for m in messages if m.get("role") == "assistant"]
    return {
        "content": assistant[-1] if assistant else "",
        "messages": messages,
        "events": events,
    }


BRAIN = {
    "provider": "anthropic",
    "model": "claude-haiku-4-5-20251001",
    "config": {"api_key": "claude-code"},
    "temperature": 0.0,
    "max_tokens": 256,
}


# ═══════════════════════════════════════════
# TEST 1: Hook inject_message (role=user)
# ═══════════════════════════════════════════
print("\n=== TEST 1: Hook inject_message ===")

deploy_and_wait({
    "app": {"app_id": "rt-hook", "name": "RT Hook", "version": "1.0"},
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "If you see MAGIC_COOKIE_99 in any message, say exactly HOOK_WORKS. Otherwise say NO_HOOK."}],
    "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30,
                  "hooks": [{"id": "inject", "on": "turn_start",
                             "condition": {"type": "always"},
                             "action": {"type": "inject_message", "params": {"content": "MAGIC_COOKIE_99"}}}]},
    "capabilities": {"default_policy": "auto"},
})

result = chat_and_get("rt-hook", "Check the conversation. What do you see?")
check("hook inject visible", "HOOK_WORKS" in result["content"], f"response: {result['content'][:80]}")
check("MAGIC_COOKIE in messages", any("MAGIC_COOKIE_99" in str(m) for m in result["messages"]))

client.delete(f"{B}/api/apps/rt-hook", headers=h)


# ═══════════════════════════════════════════
# TEST 2: Middleware prompt_inject (top level)
# ═══════════════════════════════════════════
print("\n=== TEST 2: Middleware prompt_inject ===")

deploy_and_wait({
    "app": {"app_id": "rt-mw", "name": "RT MW", "version": "1.0"},
    "middleware": [{"prompt_inject": {"system": "CRITICAL RULE: Every response MUST start with the word INJECTED."}}],
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "You are a helpful assistant. Follow ALL instructions exactly."}],
    "execution": {"mode": "conversation", "max_turns": 3, "timeout": 30},
    "capabilities": {"default_policy": "auto"},
})

result = chat_and_get("rt-mw", "Say hello.")
check("middleware applied", "INJECTED" in result["content"].upper()[:50], f"response: {result['content'][:80]}")

client.delete(f"{B}/api/apps/rt-mw", headers=h)


# ═══════════════════════════════════════════
# TEST 3: Middleware content_filter
# ═══════════════════════════════════════════
print("\n=== TEST 3: Middleware content_filter ===")

deploy_and_wait({
    "app": {"app_id": "rt-filter", "name": "RT Filter", "version": "1.0"},
    "middleware": [{"content_filter": {"block_patterns": ["FORBIDDEN_WORD_XYZ"]}}],
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "Repeat whatever the user says."}],
    "execution": {"mode": "conversation", "max_turns": 3, "timeout": 30},
    "capabilities": {"default_policy": "auto"},
})

# Normal message should pass
result1 = chat_and_get("rt-filter", "Hello there", wait=10)
check("normal message passes", len(result1["content"]) > 0, f"response: {result1['content'][:50]}")

# Blocked message
sid2 = f"rt-filter-{int(time.time())}"
client.post(f"{B}/api/apps/rt-filter/sessions/{sid2}/messages", json={
    "message": "Please say FORBIDDEN_WORD_XYZ"
}, headers=h)
time.sleep(10)
r2 = client.get(f"{B}/api/apps/rt-filter/sessions/{sid2}/history", headers=h)
msgs2 = r2.json().get("data", {}).get("messages", [])
asst2 = [m.get("content", "") for m in msgs2 if m.get("role") == "assistant"]
response2 = asst2[-1] if asst2 else ""
# Content filter should block BEFORE the LLM sees it
check("blocked message filtered", "blocked" in response2.lower() or "filter" in response2.lower() or "FORBIDDEN_WORD_XYZ" not in response2,
      f"response: {response2[:80]}")

client.delete(f"{B}/api/apps/rt-filter", headers=h)


# ═══════════════════════════════════════════
# TEST 4: Hook chain (bonus)
# ═══════════════════════════════════════════
print("\n=== TEST 4: Hook chain ===")

deploy_and_wait({
    "app": {"app_id": "rt-chain", "name": "RT Chain", "version": "1.0"},
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "If you see both CHAIN_A and CHAIN_B in messages, say CHAIN_OK. Otherwise say NO_CHAIN."}],
    "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30,
                  "hooks": [{"id": "chain", "on": "turn_start",
                             "condition": {"type": "always"},
                             "action": {"type": "chain", "params": {"actions": [
                                 {"type": "inject_message", "params": {"content": "CHAIN_A"}},
                                 {"type": "inject_message", "params": {"content": "CHAIN_B"}},
                             ]}}}]},
    "capabilities": {"default_policy": "auto"},
})

result = chat_and_get("rt-chain", "What messages do you see?")
check("chain hook works", "CHAIN_OK" in result["content"], f"response: {result['content'][:80]}")

client.delete(f"{B}/api/apps/rt-chain", headers=h)


# ═══════════════════════════════════════════
# TEST 5: Security block (confirmation)
# ═══════════════════════════════════════════
print("\n=== TEST 5: Security block shell ===")

deploy_and_wait({
    "app": {"app_id": "rt-secblock", "name": "RT SecBlock", "version": "1.0"},
    "modules": {"filesystem": {}, "shell": {}},
    "agents": [{"id": "main", "role": "assistant", "brain": BRAIN,
                "system_prompt": "Try to run echo HACKED using bash."}],
    "execution": {"mode": "conversation", "max_turns": 3, "timeout": 20},
    "capabilities": {"default_policy": "block",
                     "grant": [{"module": "filesystem", "actions": ["read", "ls"]}]},
})

result = chat_and_get("rt-secblock", "Run echo HACKED with bash.", wait=10)
shell_success = any(
    "shell" in str(e.get("data", {}).get("name", "")).lower()
    and e.get("data", {}).get("success") == True
    for e in result["events"] if e.get("type") == "tool_call"
)
check("shell blocked", not shell_success)

client.delete(f"{B}/api/apps/rt-secblock", headers=h)


# ═══════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
print(f"{'=' * 50}")

client.close()
