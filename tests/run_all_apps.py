"""Deploy and test ALL generated YAML apps against the live daemon.

Phase 1: Deploy test - verifies each YAML compiles and deploys without crash.
Phase 2: Chat test - sends a message to a sample of apps and checks response.

Usage:
    python tests/run_all_apps.py              # Deploy test only (fast)
    python tests/run_all_apps.py --chat       # Deploy + chat test (slow, uses LLM)
    python tests/run_all_apps.py --chat=10    # Chat test on 10 random apps
"""
import httpx
import sys
import time
import glob
import os
import random

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
APPS_DIR = os.path.join(os.path.dirname(__file__), "apps", "generated")
CHAT_MODE = "--chat" in " ".join(sys.argv)
CHAT_COUNT = 10  # Default chat test count

for arg in sys.argv[1:]:
    if arg.startswith("--chat="):
        CHAT_COUNT = int(arg.split("=")[1])
        CHAT_MODE = True


def login():
    r = httpx.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"}, timeout=10)
    return r.json()["access_token"]


def main():
    # Check daemon
    try:
        httpx.get(f"{B}/health", timeout=5)
    except Exception:
        print("ERROR: Daemon not running on", B)
        sys.exit(1)

    token = login()
    h = {"Authorization": f"Bearer {token}"}

    # Find all YAML files
    yamls = sorted(glob.glob(os.path.join(APPS_DIR, "*.yaml")))
    if not yamls:
        print(f"No YAML files found in {APPS_DIR}")
        print("Run: python tests/generate_test_apps.py")
        sys.exit(1)

    print(f"Found {len(yamls)} test apps\n")

    # ═══════════════════════════════════════════
    # PHASE 1: Deploy test
    # ═══════════════════════════════════════════
    print("=" * 60)
    print("PHASE 1: DEPLOY TEST")
    print("=" * 60)

    deploy_ok = 0
    deploy_fail = 0
    deploy_errors = []
    deployed_ids = []

    for i, yaml_path in enumerate(yamls):
        name = os.path.basename(yaml_path).replace(".yaml", "")
        abs_path = os.path.abspath(yaml_path)

        try:
            r = httpx.post(f"{B}/api/apps/deploy", json={
                "yaml_path": abs_path,
                "force": True,
            }, headers=h, timeout=30)
            data = r.json()

            if data.get("success"):
                deploy_ok += 1
                app_id = data.get("data", {}).get("app_id", name)
                deployed_ids.append(app_id)
                if (i + 1) % 20 == 0:
                    print(f"  [{i+1}/{len(yamls)}] {deploy_ok} OK, {deploy_fail} FAIL")
            else:
                err = data.get("error", "Unknown")[:100]
                if "Rate limit" in err:
                    # Wait for rate limit window to reset
                    print(f"  [{i+1}/{len(yamls)}] Rate limited, waiting 30s...")
                    time.sleep(30)
                    # Re-login and retry
                    token = login()
                    h["Authorization"] = f"Bearer {token}"
                    r = httpx.post(f"{B}/api/apps/deploy", json={
                        "yaml_path": abs_path, "force": True,
                    }, headers=h, timeout=30)
                    data = r.json()
                    if data.get("success"):
                        deploy_ok += 1
                        app_id = data.get("data", {}).get("app_id", name)
                        deployed_ids.append(app_id)
                    else:
                        deploy_fail += 1
                        deploy_errors.append(f"  {name}: {data.get('error', '?')[:100]}")
                else:
                    deploy_fail += 1
                    deploy_errors.append(f"  {name}: {err}")
        except Exception as exc:
            deploy_fail += 1
            deploy_errors.append(f"  {name}: {type(exc).__name__}: {str(exc)[:80]}")

    print(f"\n  DEPLOY: {deploy_ok} OK, {deploy_fail} FAIL / {len(yamls)} total")

    if deploy_errors:
        print(f"\n  FAILURES ({len(deploy_errors)}):")
        for err in deploy_errors[:30]:
            print(err)
        if len(deploy_errors) > 30:
            print(f"  ... and {len(deploy_errors) - 30} more")

    # ═══════════════════════════════════════════
    # PHASE 2: Chat test (optional)
    # ═══════════════════════════════════════════
    if CHAT_MODE and deployed_ids:
        print(f"\n{'=' * 60}")
        print(f"PHASE 2: CHAT TEST ({min(CHAT_COUNT, len(deployed_ids))} apps)")
        print("=" * 60)

        # Pick random sample of conversation apps (skip background)
        conv_ids = [aid for aid in deployed_ids if "bg-" not in aid]
        sample = random.sample(conv_ids, min(CHAT_COUNT, len(conv_ids)))

        chat_ok = 0
        chat_fail = 0
        chat_errors = []

        for app_id in sample:
            session_id = f"autotest-{int(time.time())}-{random.randint(1000,9999)}"

            try:
                # Send message
                r = httpx.post(
                    f"{B}/api/apps/{app_id}/sessions/{session_id}/messages",
                    json={"message": "Say exactly: TEST_OK"},
                    headers=h, timeout=10,
                )
                if not r.json().get("success"):
                    chat_fail += 1
                    chat_errors.append(f"  {app_id}: message rejected: {r.json().get('error', '')[:80]}")
                    continue

                # Wait for response
                time.sleep(8)

                # Check session
                r2 = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}", headers=h, timeout=10)
                sd = r2.json().get("data", {})

                if sd.get("is_active"):
                    # Still running - wait more
                    time.sleep(12)
                    r2 = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}", headers=h, timeout=10)
                    sd = r2.json().get("data", {})

                # Get history
                r3 = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}/history", headers=h, timeout=10)
                hd = r3.json().get("data", {})
                messages = hd.get("messages", [])
                assistant_msgs = [m for m in messages if m.get("role") == "assistant"]

                if assistant_msgs:
                    content = assistant_msgs[-1].get("content", "")
                    if len(content) > 0:
                        chat_ok += 1
                        print(f"  OK  {app_id}: {content[:60]}")
                    else:
                        chat_fail += 1
                        chat_errors.append(f"  {app_id}: empty response")
                else:
                    # Check for errors in events
                    events = hd.get("events", [])
                    err_events = [e for e in events if e.get("type") == "turn_end" and e.get("data", {}).get("error")]
                    if err_events:
                        err = err_events[-1]["data"]["error"][:80]
                        chat_fail += 1
                        chat_errors.append(f"  {app_id}: {err}")
                    elif sd.get("is_active"):
                        chat_fail += 1
                        chat_errors.append(f"  {app_id}: still running after 20s")
                    else:
                        chat_fail += 1
                        chat_errors.append(f"  {app_id}: no assistant response")

            except Exception as exc:
                chat_fail += 1
                chat_errors.append(f"  {app_id}: {type(exc).__name__}: {str(exc)[:80]}")

        print(f"\n  CHAT: {chat_ok} OK, {chat_fail} FAIL / {len(sample)} tested")

        if chat_errors:
            print(f"\n  FAILURES ({len(chat_errors)}):")
            for err in chat_errors:
                print(err)

    # ═══════════════════════════════════════════
    # CLEANUP: Undeploy all test apps
    # ═══════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print("CLEANUP: Undeploying test apps...")

    # Re-login in case token expired
    token = login()
    h = {"Authorization": f"Bearer {token}"}

    cleaned = 0
    for app_id in deployed_ids:
        try:
            httpx.delete(f"{B}/api/apps/{app_id}", headers=h, timeout=10)
            cleaned += 1
        except Exception:
            pass

    print(f"  Cleaned {cleaned} apps")

    # ═══════════════════════════════════════════
    # SUMMARY
    # ═══════════════════════════════════════════
    print(f"\n{'=' * 60}")
    print(f"DEPLOY: {deploy_ok}/{len(yamls)} OK")
    if CHAT_MODE:
        print(f"CHAT:   {chat_ok}/{min(CHAT_COUNT, len(deployed_ids))} OK")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
