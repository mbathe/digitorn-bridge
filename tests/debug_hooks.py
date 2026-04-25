"""Debug: Do hooks inject messages that the LLM can see?"""
import httpx, time, sys, yaml, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
client = httpx.Client(timeout=60)

r = client.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"})
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}

# Deploy app with inject_message hook
config = {
    "app": {"app_id": "test-hook-debug", "name": "Hook Debug", "version": "1.0"},
    "agents": [{"id": "main", "role": "assistant",
                "brain": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
                          "config": {"api_key": "claude-code"}, "temperature": 0.0, "max_tokens": 256},
                "system_prompt": "If you see MAGIC_WORD_42 anywhere in the conversation, say exactly: HOOK_WORKS. If you don't see it, say: NO_HOOK_FOUND"}],
    "execution": {"mode": "conversation", "max_turns": 5, "timeout": 30},
    "hooks": [{"id": "test-inject", "on": "turn_start",
               "condition": {"type": "always"},
               "action": {"type": "inject_message",
                          "params": {"content": "MAGIC_WORD_42", "role": "system"}}}],
    "capabilities": {"default_policy": "auto"},
}
with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as f:
    yaml.dump(config, f)
    tmp = f.name

r = client.post(f"{B}/api/apps/deploy", json={"yaml_path": tmp, "force": True}, headers=h)
print(f"Deploy: {r.json().get('success')}")

for _ in range(15):
    time.sleep(2)
    r = client.get(f"{B}/api/apps/test-hook-debug", headers=h)
    if r.status_code == 200:
        print("App ready")
        break

# Send message
sid = f"hook-debug-{int(time.time())}"
r = client.post(f"{B}/api/apps/test-hook-debug/sessions/{sid}/messages", json={
    "message": "Check all messages in our conversation. Do you see MAGIC_WORD_42?"
}, headers=h)
print(f"Message: {r.json().get('success')}")

print("Waiting 15s...")
time.sleep(15)

# Check
r = client.get(f"{B}/api/apps/test-hook-debug/sessions/{sid}/history", headers=h)
data = r.json().get("data", {})
messages = data.get("messages", [])
events = data.get("events", [])

print(f"\nAll messages ({len(messages)}):")
for m in messages:
    role = m.get("role", "?")
    content = m.get("content", "")
    if isinstance(content, str):
        print(f"  [{role}] {content[:100]}")
    else:
        print(f"  [{role}] (multiblock, {len(content)} blocks)")

# Check if MAGIC_WORD_42 is in any message
magic_found = any("MAGIC_WORD_42" in str(m.get("content", "")) for m in messages)
print(f"\nMAGIC_WORD_42 in messages: {magic_found}")

# Check assistant response
for m in messages:
    if m.get("role") == "assistant":
        content = m.get("content", "")
        print(f"Assistant: {content[:200]}")
        if "HOOK_WORKS" in content:
            print("HOOK WORKS!")
        else:
            print("HOOK NOT DETECTED BY LLM")

# Cleanup
client.delete(f"{B}/api/apps/test-hook-debug", headers=h)
client.close()
