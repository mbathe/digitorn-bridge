"""Complete test of ALL background mode features."""
import httpx, json, time, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
YAML_CRON = "C:/Users/ASUS/Documents/digitorn-bridge/tests/functional/apps/background_cron.yaml"
YAML_MULTI = "C:/Users/ASUS/Documents/digitorn-bridge/tests/functional/apps/background_multi.yaml"
SECRET = {"DEEPSEEK_API_KEY": "***REMOVED***"}

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


# ═══════════════════════════════════════
# MONO MODE
# ═══════════════════════════════════════
print("\n=== MONO MODE ===")

r = httpx.post(f"{B}/api/apps/deploy", json={"yaml_path": YAML_CRON, "force": True, "secrets": SECRET}, headers=h, timeout=30)
check("deploy mono", r.json().get("success"))

# Create (auto get_or_create)
r1 = httpx.post(f"{B}/api/apps/test-bg-cron/background-sessions", json={"name": "First"}, headers=h)
sid1 = r1.json().get("data", {}).get("id", "")
check("create mono", r1.json().get("success"))

# Same session returned on second create
r2 = httpx.post(f"{B}/api/apps/test-bg-cron/background-sessions", json={"name": "Second"}, headers=h)
sid2 = r2.json().get("data", {}).get("id", "")
check("mono same session", sid1 == sid2, f"{sid1[:8]} vs {sid2[:8]}")

# Detail
r = httpx.get(f"{B}/api/apps/test-bg-cron/background-sessions/{sid1}", headers=h)
check("detail", r.json().get("success"))

# Pause + Resume
r = httpx.post(f"{B}/api/apps/test-bg-cron/background-sessions/{sid1}/pause", headers=h)
check("pause", r.json().get("data", {}).get("status") == "paused")
r = httpx.post(f"{B}/api/apps/test-bg-cron/background-sessions/{sid1}/resume", headers=h)
check("resume", r.json().get("data", {}).get("status") == "active")

# Fire
r = httpx.post(f"{B}/api/apps/test-bg-cron/triggers/minutely-check/fire", headers=h)
check("fire", r.json().get("success"))
print("  ... waiting 15s for activation")
time.sleep(15)

# Activations
r = httpx.get(f"{B}/api/apps/test-bg-cron/activations", headers=h)
acts = r.json().get("data", {}).get("activations", [])
check("activation exists", len(acts) >= 1, f"count={len(acts)}")

if acts:
    aid = acts[0]["id"]
    r = httpx.get(f"{B}/api/apps/test-bg-cron/activations/{aid}", headers=h)
    ad = r.json().get("data", {})
    check("activation detail", r.json().get("success"))
    check("activation completed", ad.get("status") == "completed", ad.get("status"))
    check("activation response", len(ad.get("response", "")) > 0)
    check("activation duration", ad.get("duration_ms", 0) > 0)

# Stats
r = httpx.get(f"{B}/api/apps/test-bg-cron/activations/stats", headers=h)
stats = r.json().get("data", {})
check("stats total >= 1", stats.get("total", 0) >= 1)
check("stats success_rate > 0", stats.get("success_rate", 0) > 0)

# Test trigger sync
r = httpx.post(f"{B}/api/apps/test-bg-cron/triggers/minutely-check/test", json={}, headers=h, timeout=60)
check("test sync", r.json().get("success"))
check("test response", len(r.json().get("data", {}).get("response", "")) > 0)

# Errors
r = httpx.get(f"{B}/api/apps/test-bg-cron/errors", headers=h)
check("errors endpoint", r.json().get("success"))

# Undeploy
r = httpx.delete(f"{B}/api/apps/test-bg-cron", headers=h)
check("undeploy mono", r.json().get("success"))


# ═══════════════════════════════════════
# MULTI MODE
# ═══════════════════════════════════════
print("\n=== MULTI MODE ===")

r = httpx.post(f"{B}/api/apps/deploy", json={"yaml_path": YAML_MULTI, "force": True, "secrets": SECRET}, headers=h, timeout=30)
check("deploy multi", r.json().get("success"))

# Clean up old sessions from previous test runs
r = httpx.get(f"{B}/api/apps/test-bg-multi/background-sessions", headers=h)
for s in r.json().get("data", {}).get("sessions", []):
    httpx.delete(f"{B}/api/apps/test-bg-multi/background-sessions/{s['id']}", headers=h)

# Create 3 distinct sessions
sids = []
for name, params, rk in [
    ("Alice DS", {"cv": "Data scientist"}, {"telegram": "alice_ds"}),
    ("Alice DevOps", {"cv": "DevOps Docker"}, {"telegram": "alice_devops"}),
    ("Bob Frontend", {"cv": "React TS"}, {"telegram": "bob_front"}),
]:
    r = httpx.post(f"{B}/api/apps/test-bg-multi/background-sessions", json={
        "name": name, "params": params, "routing_keys": rk,
    }, headers=h)
    d = r.json().get("data") or {}
    sids.append(d.get("id", ""))
    check(f"create {name}", r.json().get("success") and d.get("name") == name,
          r.json().get("error", ""))

check("3 unique IDs", len(set(sids)) == 3, f"unique={len(set(sids))}")

# List
r = httpx.get(f"{B}/api/apps/test-bg-multi/background-sessions", headers=h)
sessions = r.json().get("data", {}).get("sessions", [])
new_names = {s["name"] for s in sessions}
check("list has Alice DS", "Alice DS" in new_names)
check("list has Alice DevOps", "Alice DevOps" in new_names)
check("list has Bob Frontend", "Bob Frontend" in new_names)

# Detail with params + routing
r = httpx.get(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[0]}", headers=h)
d = r.json().get("data", {})
check("detail params", d.get("params", {}).get("cv") == "Data scientist")
check("detail routing", d.get("routing_keys", {}).get("telegram") == "alice_ds")

# Pause
r = httpx.post(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[0]}/pause", headers=h)
check("pause multi", r.json().get("data", {}).get("status") == "paused")

# List shows paused
r = httpx.get(f"{B}/api/apps/test-bg-multi/background-sessions", headers=h)
paused = [s for s in r.json().get("data", {}).get("sessions", []) if s["id"] == sids[0]]
check("paused visible", len(paused) == 1 and paused[0]["status"] == "paused")

# Resume
r = httpx.post(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[0]}/resume", headers=h)
check("resume multi", r.json().get("data", {}).get("status") == "active")

# Pause one session before fire (to test paused sessions are skipped)
r = httpx.post(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[2]}/pause", headers=h)
check("pause Bob before fire", r.json().get("data", {}).get("status") == "paused")

# Record activation count before fire
r = httpx.get(f"{B}/api/apps/test-bg-multi/activations/stats", headers=h)
before_total = r.json().get("data", {}).get("total", 0)

# Fire trigger - broadcast should activate ALL active sessions (2 out of 3)
r = httpx.post(f"{B}/api/apps/test-bg-multi/triggers/hourly/fire", headers=h)
check("fire multi", r.json().get("success"))
print("  ... waiting 20s for broadcast activations")
time.sleep(20)

# Activations - broadcast should have created 2 new activations (Alice DS + Alice DevOps, Bob paused)
r = httpx.get(f"{B}/api/apps/test-bg-multi/activations", headers=h)
acts = r.json().get("data", {}).get("activations", [])
r2 = httpx.get(f"{B}/api/apps/test-bg-multi/activations/stats", headers=h)
after_total = r2.json().get("data", {}).get("total", 0)
new_acts = after_total - before_total
check("broadcast created 2 activations", new_acts == 2, f"new={new_acts} (before={before_total}, after={after_total})")

# Check activations have session context injected
recent = [a for a in acts if a.get("trigger_id") == "hourly"]
if len(recent) >= 2:
    # Read full details of the two most recent
    a1 = httpx.get(f"{B}/api/apps/test-bg-multi/activations/{recent[0]['id']}", headers=h).json().get("data", {})
    a2 = httpx.get(f"{B}/api/apps/test-bg-multi/activations/{recent[1]['id']}", headers=h).json().get("data", {})
    msgs = [a1.get("message", ""), a2.get("message", "")]
    has_ds = any("Data scientist" in m for m in msgs)
    has_devops = any("DevOps Docker" in m for m in msgs)
    check("session params injected (DS)", has_ds, f"msgs={[m[:80] for m in msgs]}")
    check("session params injected (DevOps)", has_devops)
else:
    check("session params injected (DS)", False, f"only {len(recent)} hourly activations")
    check("session params injected (DevOps)", False)

# Resume Bob
r = httpx.post(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[2]}/resume", headers=h)

# Stats
r = httpx.get(f"{B}/api/apps/test-bg-multi/activations/stats", headers=h)
check("multi stats", r.json().get("success"))

# Delete one session
r = httpx.delete(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[2]}", headers=h)
check("delete Bob", r.json().get("success"))

# Verify deleted
r = httpx.get(f"{B}/api/apps/test-bg-multi/background-sessions/{sids[2]}", headers=h)
check("Bob gone", r.status_code == 404)

# Triggers
r = httpx.get(f"{B}/api/apps/test-bg-multi/triggers", headers=h)
triggers = r.json().get("data", {}).get("triggers", [])
check("triggers count", len(triggers) == 2, f"count={len(triggers)}")

# Test trigger sync
r = httpx.post(f"{B}/api/apps/test-bg-multi/triggers/hourly/test", json={}, headers=h, timeout=60)
check("test multi sync", r.json().get("success"))

# Undeploy
r = httpx.delete(f"{B}/api/apps/test-bg-multi", headers=h)
check("undeploy multi", r.json().get("success"))

# ═══════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════
print(f"\n{'=' * 40}")
print(f"PASSED: {passed}")
print(f"FAILED: {failed}")
print(f"TOTAL:  {passed + failed}")
print(f"{'=' * 40}")
