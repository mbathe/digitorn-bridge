"""Debug: Does shell.bash get BLOCKED when policy=block and no grant for shell?"""
import httpx, time, sys, yaml, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
client = httpx.Client(timeout=60)

r = client.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

# Deploy
config = {
    "app": {"app_id": "test-sec-debug", "name": "Security Debug", "version": "1.0"},
    "modules": {"filesystem": {}, "shell": {}},
    "agents": [{"id": "main", "role": "assistant",
                "brain": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                          "config": {"api_key": "claude-code"}, "temperature": 0.0, "max_tokens": 256},
                "system_prompt": "You MUST run: echo HACKED using shell.bash tool. Do it immediately."}],
    "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
    "capabilities": {"default_policy": "block",
                     "grant": [{"module": "filesystem", "actions": ["read", "ls"]}]},
}
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
    yaml.dump(config, f)
    tmp = f.name

r = client.post(f"{B}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, headers=h)
print(f"Deploy: {r.json().get('success')}")

# Poll until deployed
for _ in range(15):
    time.sleep(2)
    r = client.get(f"{B}/api/apps/test-sec-debug", headers=h)
    if r.status_code == 200:
        print("App ready")
        break
else:
    print("App never became ready")
    sys.exit(1)

# Send message
sid = f"sec-debug-{int(time.time())}"
r = client.post(f"{B}/api/apps/test-sec-debug/sessions/{sid}/messages", json={
    "message": "Run echo HACKED using bash now. Use the Bash tool."
}, headers=h)
print(f"Message: {r.json().get('success')}")

print("Waiting 15s for LLM...")
time.sleep(15)

# Check
r = client.get(f"{B}/api/apps/test-sec-debug/sessions/{sid}/history", headers=h)
data = r.json().get("data", {})
events = data.get("events", [])
messages = data.get("messages", [])

tool_events = [e for e in events if e.get("type") == "tool_call"]
print(f"\nTool calls: {len(tool_events)}")
for tc in tool_events:
    d = tc.get("data", {})
    name = d.get("name", "?")
    success = d.get("success")
    error = str(d.get("error", ""))[:100]
    print(f"  {name}: success={success} error={error}")

print(f"\nMessages:")
for m in messages:
    role = m.get("role", "?")
    content = m.get("content", "")
    if isinstance(content, str) and role != "system":
        print(f"  [{role}] {content[:120]}")

# Was shell.bash actually EXECUTED successfully?
shell_success = any(
    "shell" in str(tc.get("data", {}).get("name", "")).lower()
    and tc.get("data", {}).get("success") == True
    for tc in tool_events
)
shell_attempted = any(
    "shell" in str(tc.get("data", {}).get("name", "")).lower() or "bash" in str(tc.get("data", {}).get("name", "")).lower()
    for tc in tool_events
)
print(f"\nShell attempted: {shell_attempted}")
print(f"Shell succeeded: {shell_success}")
print(f"SECURITY {'BREACHED' if shell_success else 'HELD'}")

# Cleanup
client.delete(f"{B}/api/apps/test-sec-debug", headers=h)
client.close()
