#!/usr/bin/env python3
"""CLI feature validation suite - tests all digitorn-cli features."""

import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import time, json, uuid, threading
from collections import Counter
import httpx

DAEMON = "http://127.0.0.1:8000"
OK = "\033[32m OK \033[0m"
FAIL = "\033[31mFAIL\033[0m"
total = 0
passed = 0

def check(name, condition, detail=""):
    global total, passed
    total += 1
    if condition:
        passed += 1
        print(f"  {OK} {name}  {detail}")
    else:
        print(f"  {FAIL} {name}  {detail}")


# Auth
r = httpx.post(f"{DAEMON}/auth/register", json={
    "username": f"test_{uuid.uuid4().hex[:6]}",
    "password": "TestCLI_Secure_12345!",
    "email": f"t{uuid.uuid4().hex[:4]}@t.l",
}, timeout=30)
token = r.json()["access_token"]
h = {"Authorization": f"Bearer {token}"}
import os
creds = {"access_token": token, "refresh_token": r.json().get("refresh_token",""),
         "expires_at": time.time()+3600, "daemon_url": DAEMON}
with open(os.path.expanduser("~/.digitorn/credentials.json"), "w") as f:
    json.dump(creds, f)

print("=" * 60)
print("CLI FEATURE VALIDATION SUITE")
print("=" * 60)

# ── 1. Package imports ─────────────────────────────────
print("\n1. Package imports")
try:
    from digitorn_cli import __version__
    check("digitorn_cli package", True, f"v{__version__}")
except Exception as e:
    check("digitorn_cli package", False, str(e))

try:
    from digitorn_cli.tui.app import DigitornTUI, PromptInput
    check("TUI App + PromptInput", True)
except Exception as e:
    check("TUI App + PromptInput", False, str(e))

try:
    from digitorn_cli.tui.widgets.tab_bar import TabBar
    from digitorn_cli.tui.widgets.code_preview import CodePreview
    from digitorn_cli.tui.widgets.spinner_bar import SpinnerBar
    from digitorn_cli.tui.widgets.status_footer import StatusFooter
    from digitorn_cli.tui.widgets.sidebar import Sidebar
    from digitorn_cli.tui.widgets.chat_log import ChatLog
    check("All widgets", True, "6 widgets")
except Exception as e:
    check("All widgets", False, str(e))

try:
    from digitorn_cli.tui.views.sessions import SessionsScreen
    from digitorn_cli.tui.views.tools import ToolsScreen
    from digitorn_cli.tui.views.context import ContextScreen
    from digitorn_cli.tui.views.search import SearchBar
    check("All views", True, "4 views")
except Exception as e:
    check("All views", False, str(e))

try:
    from digitorn_cli.tui.messages import (
        TokenReceived, StreamDone, OutTokenCount, InTokenCount,
        ToolStarted, ToolCompleted, ThinkingStarted, ThinkingDelta, ThinkingReceived,
        HookFired, TurnComplete, BackendReady, BackendError,
        AgentEvent, MemoryUpdate, ApprovalRequested,
        StatusUpdate, TerminalOutput,
        Notification, NotificationResult, WorkbenchEvent,
    )
    check("All 22 messages", True)
except Exception as e:
    check("All 22 messages", False, str(e))

try:
    from digitorn_cli.keybindings import load_keybindings
    kb = load_keybindings()
    check("Keybindings", len(kb) >= 10, f"{len(kb)} bindings")
except Exception as e:
    check("Keybindings", False, str(e))

# ── 2. Auth ────────────────────────────────────────────
print("\n2. Auth")
try:
    from digitorn_cli.auth import get_auth_headers, daemon_request
    headers = get_auth_headers(DAEMON)
    check("get_auth_headers", "Authorization" in headers)
except Exception as e:
    check("get_auth_headers", False, str(e))

try:
    resp = daemon_request("get", f"{DAEMON}/api/apps", daemon=DAEMON)
    check("daemon_request", resp.status_code == 200)
except Exception as e:
    check("daemon_request", False, str(e))

# ── 3. App commands ────────────────────────────────────
print("\n3. App commands (HTTP)")
try:
    resp = daemon_request("post", f"{DAEMON}/api/apps/validate", daemon=DAEMON,
                          json={"yaml_path": "examples/test-full.yaml"})
    data = resp.json()
    check("app validate", data.get("success"), data.get("data",{}).get("app_id",""))
except Exception as e:
    check("app validate", False, str(e))

try:
    resp = daemon_request("get", f"{DAEMON}/api/apps", daemon=DAEMON)
    apps = resp.json().get("data", [])
    check("app list", len(apps) >= 1, f"{len(apps)} apps")
except Exception as e:
    check("app list", False, str(e))

try:
    resp = daemon_request("get", f"{DAEMON}/api/modules", daemon=DAEMON)
    mods = resp.json().get("modules", [])
    check("modules list", len(mods) >= 10, f"{len(mods)} modules")
except Exception as e:
    check("modules list", False, str(e))

# ── 4. SSE Event Bus ──────────────────────────────────
print("\n4. SSE Event Bus")
sid = str(uuid.uuid4())
events = []
sse_ready = threading.Event()

def listen():
    try:
        with httpx.stream("GET", f"{DAEMON}/api/apps/opencode/sessions/{sid}/events",
                          headers=h, timeout=None) as resp:
            buf = ""
            for chunk in resp.iter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if "connected" in line and line.startswith("event:"):
                        sse_ready.set()
                    if line.startswith("event: "):
                        events.append(line[7:])
    except:
        pass

t = threading.Thread(target=listen, daemon=True)
t.start()
sse_ready.wait(timeout=10)
time.sleep(1)

r = httpx.post(f"{DAEMON}/api/apps/opencode/sessions/{sid}/messages", headers=h,
               json={"message": "Read pyproject.toml and tell me the version. Be brief.",
                     "workspace": "C:/Users/ASUS/Documents/digitorn-bridge"},
               timeout=10)
check("POST /messages", r.status_code == 200)

print("  Waiting for SSE events (30s)...")
time.sleep(30)
counts = Counter(events)

check("SSE connected", "connected" in counts)
check("SSE token", counts.get("token", 0) > 0, f"{counts.get('token', 0)}")
check("SSE status", counts.get("status", 0) > 0, f"{counts.get('status', 0)}")
check("SSE result", counts.get("result", 0) > 0)
check("SSE tool_start", counts.get("tool_start", 0) + counts.get("tool_call", 0) > 0)
check("SSE hook", counts.get("hook", 0) > 0)
check("SSE stream_done", counts.get("stream_done", 0) > 0)
check("SSE out_token", counts.get("out_token", 0) > 0)
check("SSE in_token", counts.get("in_token", 0) > 0)

# ── 5. Session metrics ────────────────────────────────
print("\n5. Session metrics")
r = httpx.get(f"{DAEMON}/api/apps/opencode/sessions/{sid}", headers=h, timeout=10)
data = r.json().get("data", {})
check("is_active", "is_active" in data)
check("tokens.prompt", data.get("tokens", {}).get("prompt", 0) > 0,
      str(data.get("tokens", {}).get("prompt", 0)))
check("tools", "tools" in data)
check("model", bool(data.get("model")), data.get("model", ""))

# ── 6. Labels standalone ─────────────────────────────
print("\n6. Labels (zero daemon imports)")
from digitorn_cli.labels import tool_label, result_status
v, d = tool_label("filesystem__read", {"path": "test.py"})
check("tool_label", v == "Reading", f"{v}({d})")
ok, err = result_status({"success": True, "data": {}})
check("result_status", ok is True)

# ── 7. No daemon imports ─────────────────────────────
print("\n7. Import isolation")
import importlib
cli_mod = importlib.import_module("digitorn_cli")
cli_src = cli_mod.__file__
check("Package location", "digitorn_cli" in cli_src, cli_src)

# Check no digitorn.core imports in the cli package
import subprocess
result = subprocess.run(
    ["grep", "-rn", "from digitorn.core", "packages/digitorn_cli/"],
    capture_output=True, text=True, cwd="C:/Users/ASUS/Documents/digitorn-bridge",
)
# All daemon imports must be inside try/except ImportError blocks
# Grep with context to verify each import is protected
result2 = subprocess.run(
    ["grep", "-B2", "-n", "from digitorn.core", "packages/digitorn_cli/"],
    capture_output=True, text=True, cwd="C:/Users/ASUS/Documents/digitorn-bridge",
)
unprotected = []
lines = result2.stdout.splitlines()
for i, line in enumerate(lines):
    if "from digitorn.core" in line and "#" not in line:
        context = "\n".join(lines[max(0,i-2):i+1])
        if "try" not in context and "except" not in context:
            unprotected.append(line.strip())
check("Zero hard daemon imports", len(unprotected) == 0,
      f"{len(unprotected)} unprotected" if unprotected else "all guarded")

# ── Summary ───────────────────────────────────────────
print()
print("=" * 60)
print(f"RESULTS: {passed}/{total} passed")
if passed == total:
    print("\033[32mALL TESTS PASSED\033[0m")
else:
    print(f"\033[31m{total - passed} FAILURES\033[0m")
print("=" * 60)
sys.exit(0 if passed == total else 1)
