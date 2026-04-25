"""Debug: Does prompt_inject middleware actually modify the system prompt?"""
import httpx, time, sys, yaml, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
client = httpx.Client(timeout=60)

r = client.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

# Deploy with prompt_inject middleware
config = {
    "app": {"app_id": "test-mw-debug", "name": "MW Debug", "version": "1.0",
            "middleware": [{"prompt_inject": {"system": "IMPORTANT: You MUST start every response with the word INJECTED_OK."}}]},
    "agents": [{"id": "main", "role": "assistant",
                "brain": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                          "config": {"api_key": "claude-code"}, "temperature": 0.0, "max_tokens": 256},
                "system_prompt": "You are a helpful assistant. Follow all instructions exactly."}],
    "execution": {"mode": "conversation", "max_turns": 3, "timeout": 30},
    "capabilities": {"default_policy": "auto"},
}
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
    yaml.dump(config, f)
    tmp = f.name

r = client.post(f"{B}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, headers=h)
print(f"Deploy: {r.json().get('success')}")

for _ in range(15):
    time.sleep(2)
    r = client.get(f"{B}/api/apps/test-mw-debug", headers=h)
    if r.status_code == 200:
        print("App ready")
        break

sid = f"mw-debug-{int(time.time())}"
r = client.post(f"{B}/api/apps/test-mw-debug/sessions/{sid}/messages", json={
    "message": "Say hello."
}, headers=h)
print(f"Message: {r.json().get('success')}")

print("Waiting 15s...")
time.sleep(15)

r = client.get(f"{B}/api/apps/test-mw-debug/sessions/{sid}/history", headers=h)
data = r.json().get("data", {})
messages = data.get("messages", [])

print(f"\nMessages ({len(messages)}):")
for m in messages:
    role = m.get("role", "?")
    content = m.get("content", "")
    if isinstance(content, str):
        short = content[:150]
        print(f"  [{role}] {short}")

# Check if system prompt was modified
system_msgs = [m for m in messages if m.get("role") == "system"]
if system_msgs:
    sys_content = system_msgs[0].get("content", "")
    print(f"\nSystem prompt contains INJECTED_OK instruction: {'INJECTED_OK' in sys_content}")
    print(f"System prompt length: {len(sys_content)}")

# Check assistant response
for m in messages:
    if m.get("role") == "assistant":
        content = m.get("content", "")
        print(f"\nAssistant response: {content[:200]}")
        print(f"Starts with INJECTED_OK: {content.strip().startswith('INJECTED_OK')}")

client.delete(f"{B}/api/apps/test-mw-debug", headers=h)
client.close()
