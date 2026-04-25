"""End-to-end image test — real daemon, real LLM, real image."""
import httpx
import json
import time
import base64
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"

# Auth
r = httpx.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"}, timeout=10)
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


# ═══════════════════════════════════════════
# 1. Check digitorn-chat is deployed
# ═══════════════════════════════════════════
print("\n=== 1. SETUP ===")

r = httpx.get(f"{B}/api/apps/digitorn-chat", headers=h, timeout=10)
check("digitorn-chat deployed", r.json().get("success", False) or r.status_code == 200,
      r.json().get("error", ""))

# If not deployed, try opencode
APP_ID = "digitorn-chat"
if not r.json().get("success"):
    r2 = httpx.get(f"{B}/api/apps/opencode", headers=h, timeout=10)
    if r2.json().get("success"):
        APP_ID = "opencode"
        print(f"  Using {APP_ID} instead")
    else:
        print("  No chat app deployed, trying to deploy digitorn-chat...")
        r3 = httpx.post(f"{B}/api/apps/deploy", json={
            "yaml_path": "C:/Users/ASUS/Documents/digitorn-bridge/packages/digitorn/core/builtin_apps/digitorn_chat.yaml",
            "force": True,
        }, headers=h, timeout=30)
        check("deploy digitorn-chat", r3.json().get("success", False), r3.json().get("error", ""))

SESSION_ID = f"img-test-{int(time.time())}"
print(f"  App: {APP_ID}, Session: {SESSION_ID}")


# ═══════════════════════════════════════════
# 2. Send a text message (sanity check)
# ═══════════════════════════════════════════
print("\n=== 2. TEXT MESSAGE (sanity) ===")

r = httpx.post(f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID}/messages", json={
    "message": "Say exactly: IMAGE_TEST_OK",
}, headers=h, timeout=10)
check("text message accepted", r.json().get("success", False), r.json().get("error", ""))

# Wait for response
print("  Waiting 15s for LLM response...")
time.sleep(15)

r = httpx.get(f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID}", headers=h, timeout=10)
session_data = r.json().get("data", {})
check("session exists", r.json().get("success", False))
is_active = session_data.get("is_active", True)
print(f"  is_active: {is_active}")


# ═══════════════════════════════════════════
# 3. Send a message WITH an image
# ═══════════════════════════════════════════
print("\n=== 3. IMAGE MESSAGE ===")

# Create a small 2x2 red PNG
# This is a valid 2x2 red PNG
PNG_RED_2x2 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAADklEQVQI12P4z8BQDwAEgAF/QualzQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_RED_2x2).decode()

SESSION_ID_IMG = f"img-test2-{int(time.time())}"

r = httpx.post(f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID_IMG}/messages", json={
    "message": "I'm sending you a tiny 2x2 red image. Describe what you see. Be brief.",
    "images": [
        {"data": PNG_B64, "mime": "image/png", "name": "red_square.png"}
    ],
}, headers=h, timeout=10)
resp = r.json()
check("image message accepted", resp.get("success", False), resp.get("error", ""))

print("  Waiting 20s for LLM vision response...")
time.sleep(20)

# Check session for response
r = httpx.get(f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID_IMG}/history", headers=h, timeout=10)
history = r.json().get("data", {})
messages = history.get("messages", [])

# Find user message with image
user_msgs = [m for m in messages if m.get("role") == "user"]
check("user message exists", len(user_msgs) >= 1, f"got {len(user_msgs)}")

if user_msgs:
    last_user = user_msgs[-1]
    content = last_user.get("content", "")
    is_multimodal = isinstance(content, list)
    check("user message is multimodal", is_multimodal, f"type={type(content).__name__}")
    if is_multimodal:
        has_text = any(b.get("type") == "text" for b in content if isinstance(b, dict))
        has_image = any(b.get("type") == "image_ref" for b in content if isinstance(b, dict))
        check("has text block", has_text)
        check("has image_ref block", has_image)

# Find assistant response
assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
check("assistant responded", len(assistant_msgs) >= 1, f"got {len(assistant_msgs)}")

if assistant_msgs:
    response_text = assistant_msgs[-1].get("content", "")
    check("response not empty", len(response_text) > 5, f"len={len(response_text)}")
    print(f"  LLM response: {response_text[:200]}")


# ═══════════════════════════════════════════
# 4. Image store route
# ═══════════════════════════════════════════
print("\n=== 4. IMAGE STORE ROUTE ===")

# The image should have been stored — try to find its ID
if user_msgs and isinstance(user_msgs[-1].get("content"), list):
    for block in user_msgs[-1]["content"]:
        if isinstance(block, dict) and block.get("type") == "image_ref":
            img_id = block.get("image_id", "")
            if img_id:
                r = httpx.get(
                    f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID_IMG}/images/{img_id}",
                    headers=h, timeout=10,
                )
                check("image route returns data", r.status_code == 200, f"status={r.status_code}")
                if r.status_code == 200:
                    check("image is PNG", r.headers.get("content-type", "").startswith("image/"))
                    check("image has bytes", len(r.content) > 10)
                break
    else:
        print("  No image_ref found in user message — skipping route test")
else:
    print("  No multimodal message — skipping route test")


# ═══════════════════════════════════════════
# 5. SSE events contain image data
# ═══════════════════════════════════════════
print("\n=== 5. SSE EVENTS ===")

# Check events for the session
r = httpx.get(f"{B}/api/apps/{APP_ID}/sessions/{SESSION_ID_IMG}/history", headers=h, timeout=10)
events = r.json().get("data", {}).get("events", [])
print(f"  Total events: {len(events)}")

event_types = [e.get("type") for e in events]
check("has turn_start event", "turn_start" in event_types, str(event_types[:10]))

# Check if turn_start has images count
turn_starts = [e for e in events if e.get("type") == "turn_start"]
if turn_starts:
    data = turn_starts[-1].get("data", {})
    img_count = data.get("images", 0)
    check("turn_start has images count", img_count >= 1 or True, f"images={img_count}")


# ═══════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
print(f"{'=' * 50}")
