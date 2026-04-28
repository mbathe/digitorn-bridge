"""Verify image support - end-to-end tests."""
import sys
import asyncio
import base64
import json
import tempfile
import os

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
# 1. IMAGE STORE
# ═══════════════════════════════════════════
print("\n=== IMAGE STORE ===")

from digitorn.core.image_store import ImageStore, is_image_path, mime_for_path

check("is_image .png", is_image_path("test.png"))
check("is_image .jpg", is_image_path("photo.jpg"))
check("is_image .webp", is_image_path("img.webp"))
check("not image .py", not is_image_path("code.py"))
check("not image .txt", not is_image_path("readme.txt"))

check("mime png", mime_for_path("test.png") == "image/png")
check("mime jpg", mime_for_path("test.jpg") == "image/jpeg")
check("mime webp", mime_for_path("test.webp") == "image/webp")

# Store and retrieve
tmpdir = tempfile.mkdtemp()
store = ImageStore(base_dir=tmpdir)

# Create a tiny 1x1 PNG
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

ref = asyncio.run(store.store(PNG_1x1, "image/png", "test-session", alt_text="tiny pixel"))
check("store returns ref", ref is not None)
check("ref has image_id", len(ref.image_id) == 12)
check("ref has path", os.path.exists(ref.path))
check("ref has mime", ref.mime == "image/png")
check("ref has size", ref.size == len(PNG_1x1))
check("ref has alt", ref.alt_text == "tiny pixel")

# Retrieve
data = asyncio.run(store.get(ref.image_id, "test-session"))
check("get returns data", data == PNG_1x1)

b64 = asyncio.run(store.get_base64(ref.image_id, "test-session"))
check("get_base64 works", b64 is not None and len(b64) > 10)

# Store from base64
b64_input = base64.b64encode(PNG_1x1).decode()
ref2 = asyncio.run(store.store_base64(b64_input, "image/png", "test-session"))
check("store_base64 works", ref2 is not None)

# List refs
refs = store.list_refs("test-session")
check("list_refs count", len(refs) == 2)

# to_dict
d = ref.to_dict()
check("to_dict has image_id", d["image_id"] == ref.image_id)
check("to_dict has mime", d["mime"] == "image/png")

# Cleanup
count = store.cleanup_session("test-session")
check("cleanup returns count", count == 2)
check("cleanup removes files", not os.path.exists(ref.path))


# ═══════════════════════════════════════════
# 2. MULTIMODAL MESSAGES
# ═══════════════════════════════════════════
print("\n=== MULTIMODAL MESSAGES ===")

from digitorn.core.runtime.multimodal import (
    build_user_message_with_images,
    has_images,
    inject_tool_image,
)

# Build message without images
msg1 = build_user_message_with_images("hello", [])
check("no images = string content", isinstance(msg1["content"], str))
check("no images = hello", msg1["content"] == "hello")

# Build message with images
msg2 = build_user_message_with_images("analyze this", [
    {"image_id": "abc", "mime": "image/png", "alt_text": "screenshot"},
])
check("with images = list content", isinstance(msg2["content"], list))
check("with images = 2 blocks", len(msg2["content"]) == 2)
check("first block text", msg2["content"][0]["type"] == "text")
check("second block image_ref", msg2["content"][1]["type"] == "image_ref")

# has_images
check("has_images true", has_images(msg2))
check("has_images false", not has_images(msg1))
check("has_images plain", not has_images({"role": "user", "content": "plain text"}))

# inject_tool_image
msgs = []
inject_tool_image(msgs, "base64data", "image/png", "browser.screenshot", "page capture")
check("inject adds message", len(msgs) == 1)
check("inject role user", msgs[0]["role"] == "user")
check("inject has blocks", isinstance(msgs[0]["content"], list))
check("inject has image", any(b.get("type") == "image" for b in msgs[0]["content"]))


# ═══════════════════════════════════════════
# 3. TO_CHAT_MESSAGES (multimodal)
# ═══════════════════════════════════════════
print("\n=== TO_CHAT_MESSAGES ===")

from digitorn.core.runtime.messages import to_chat_messages

# Text-only - flatten
msgs_text = to_chat_messages([
    {"role": "user", "content": [{"type": "text", "text": "hello"}]},
])
check("text-only flattened", isinstance(msgs_text[0].content, str))
check("text-only value", msgs_text[0].content == "hello")

# Multimodal - preserve
msgs_multi = to_chat_messages([
    {"role": "user", "content": [
        {"type": "text", "text": "look at this"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
    ]},
])
check("multimodal preserved", isinstance(msgs_multi[0].content, list))
check("multimodal 2 blocks", len(msgs_multi[0].content) == 2)


# ═══════════════════════════════════════════
# 4. CONFIG
# ═══════════════════════════════════════════
print("\n=== CONFIG ===")

from digitorn.core.config import Settings

s = Settings()
check("images section exists", hasattr(s, "images"))
check("max_per_message", s.images.max_per_message == 10)
check("max_size_bytes", s.images.max_size_bytes == 10_485_760)
check("low_res_size", s.images.low_res_size == 512)
check("aging_full_turns", s.images.aging_full_turns >= 0)


# ═══════════════════════════════════════════
# 5. SCHEMA (YAML brain config)
# ═══════════════════════════════════════════
print("\n=== YAML SCHEMA ===")

from digitorn.core.app.schema import AgentBrain

# Auto-detect vision
brain1 = AgentBrain(model="claude-sonnet-4-20250514")
check("claude sonnet = vision", brain1.supports_vision)

brain2 = AgentBrain(model="gpt-4o")
check("gpt-4o = vision", brain2.supports_vision)

brain3 = AgentBrain(model="deepseek-chat")
check("deepseek-chat = no vision", not brain3.supports_vision)

brain4 = AgentBrain(model="llava-1.5")
check("llava = vision", brain4.supports_vision)

# Manual override
brain5 = AgentBrain(model="deepseek-chat", vision=True)
check("manual vision=true", brain5.supports_vision)

brain6 = AgentBrain(model="claude-sonnet-4-20250514", vision=False)
check("manual vision=false", not brain6.supports_vision)

# image_generation
brain7 = AgentBrain(model="dall-e-3", image_generation=True)
check("image_generation", brain7.image_generation)


# ═══════════════════════════════════════════
# 6. FILESYSTEM READ IMAGE
# ═══════════════════════════════════════════
print("\n=== FILESYSTEM READ IMAGE ===")

from digitorn.core.image_store import is_image_path
check("detect .png", is_image_path("/path/to/screenshot.png"))
check("detect .jpg", is_image_path("/photos/vacation.JPG"))
check("detect .svg", is_image_path("icon.svg"))
check("skip .py", not is_image_path("main.py"))


# ═══════════════════════════════════════════
# 7. PREVIOUS verify_v1 still passes
# ═══════════════════════════════════════════
print("\n=== REGRESSION: verify_v1 subset ===")

from digitorn.core.runtime.hooks import _CONDITION_REGISTRY, _ACTION_REGISTRY
check("10 conditions", len(_CONDITION_REGISTRY) == 10)
check("11 actions", len(_ACTION_REGISTRY) == 11)

from digitorn.core.security import resolve_action_policy, SecurityProfile, ModuleGrant
p = SecurityProfile(app_id="t", default_policy="block", granted_permissions=frozenset())
check("block blocks", resolve_action_policy(p, "fs", "w", "h") == "block")

gs = ModuleGrant(module_id="cb", system_module=True)
ps = SecurityProfile(app_id="t", default_policy="block", module_grants={"cb": gs}, granted_permissions=frozenset())
check("system auto", resolve_action_policy(ps, "cb", "x", "h") == "auto")

from digitorn.core.runtime.tool_names import to_fqn
check("Bash resolve", to_fqn("Bash") == "shell.bash")
check("Write resolve", to_fqn("Write") == "filesystem.write")

from digitorn.core.runtime.messages import serialize_result
from digitorn.modules.base import ActionResult
r = ActionResult(success=False, error="err", data={"k": "v"})
s = serialize_result(r)
check("serialize has data", "k" in s and "err" in s)


# ═══════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════
print(f"\n{'=' * 50}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
print(f"{'=' * 50}")

sys.exit(0 if failed == 0 else 1)
