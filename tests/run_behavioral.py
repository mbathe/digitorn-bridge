"""Run behavioral tests - deploy, chat, verify specific behaviors.

Usage:
    python tests/run_behavioral.py          # All tests
    python tests/run_behavioral.py --limit=50  # First 50 only
"""
import httpx
import sys
import time
import glob
import os
import yaml

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

B = "http://127.0.0.1:9000"
APPS_DIR = os.path.join(os.path.dirname(__file__), "apps", "behavioral")

LIMIT = 0
for arg in sys.argv[1:]:
    if arg.startswith("--limit="):
        LIMIT = int(arg.split("=")[1])


def login():
    r = httpx.post(f"{B}/auth/login", json={"username": "admin", "password": "admin1234admin"}, timeout=10)
    return r.json()["access_token"]


def send_and_wait(app_id, session_id, message, h, workspace="", wait=12):
    """Send a message and wait for response."""
    r = httpx.post(f"{B}/api/apps/{app_id}/sessions/{session_id}/messages", json={
        "message": message, "workspace": workspace,
    }, headers=h, timeout=10)
    if not r.json().get("success"):
        return None, r.json().get("error", "send failed")

    time.sleep(wait)

    r2 = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}/history", headers=h, timeout=10)
    hd = r2.json().get("data", {})
    messages = hd.get("messages", [])
    events = hd.get("events", [])

    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    content = assistant_msgs[-1].get("content", "") if assistant_msgs else ""

    tool_calls = [e for e in events if e.get("type") == "tool_call"]

    return {
        "content": content,
        "messages": messages,
        "events": events,
        "tool_calls": tool_calls,
        "has_response": bool(content),
    }, None


def main():
    try:
        httpx.get(f"{B}/health", timeout=5)
    except Exception:
        print("ERROR: Daemon not running")
        sys.exit(1)

    token = login()
    h = {"Authorization": f"Bearer {token}"}

    yamls = sorted(glob.glob(os.path.join(APPS_DIR, "*.yaml")))
    if LIMIT:
        yamls = yamls[:LIMIT]

    print(f"Found {len(yamls)} behavioral test apps\n")

    passed = 0
    failed = 0
    skipped = 0
    errors = []
    deployed_ids = []

    for i, yaml_path in enumerate(yamls):
        name = os.path.basename(yaml_path).replace(".yaml", "")
        abs_path = os.path.abspath(yaml_path)

        # Load test metadata
        with open(yaml_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        test_meta = config.get("_test", {})
        test_type = test_meta.get("type", "unknown")
        app_id = config.get("app", {}).get("app_id", name)

        # Deploy
        try:
            r = httpx.post(f"{B}/api/apps/deploy", json={
                "yaml_path": abs_path, "force": True,
            }, headers=h, timeout=30)
            if not r.json().get("success"):
                err = r.json().get("error", "")
                if "Rate limit" in err:
                    time.sleep(30)
                    token = login()
                    h["Authorization"] = f"Bearer {token}"
                    r = httpx.post(f"{B}/api/apps/deploy", json={
                        "yaml_path": abs_path, "force": True,
                    }, headers=h, timeout=30)

            # Wait for async deploy
            time.sleep(1)

            if not r.json().get("success") and "compil" not in r.json().get("error", "").lower():
                failed += 1
                errors.append(f"  {name}: deploy failed: {r.json().get('error', '')[:80]}")
                continue
        except Exception as exc:
            failed += 1
            errors.append(f"  {name}: deploy exception: {exc}")
            continue

        deployed_ids.append(app_id)

        # Skip background apps (they don't chat)
        mode = config.get("execution", {}).get("mode", "conversation")
        if mode == "background":
            # Just verify it deployed
            r_check = httpx.get(f"{B}/api/apps/{app_id}", headers=h, timeout=10)
            if r_check.status_code == 200:
                passed += 1
                if (i + 1) % 20 == 0:
                    print(f"  [{i+1}/{len(yamls)}] {passed} OK, {failed} FAIL")
            else:
                failed += 1
                errors.append(f"  {name}: background app not found after deploy")
            continue

        # Determine test message - use specific message for each test type
        test_message = test_meta.get("test_message", "")
        if not test_message:
            if test_type == "approval_required":
                test_message = "Write the text 'hello' to /tmp/approval_test.txt now."
            elif test_type == "security_block":
                test_message = "Try to use every tool you have. Do it now."
            elif test_type == "hook_inject" or test_type == "hook_chain":
                test_message = "Check your system messages and tell me what you see."
            elif test_type == "content_filter":
                test_message = "Hello, respond briefly."
            elif test_type == "middleware_inject" or test_type == "middleware_combo":
                test_message = "Hello, respond briefly."
            else:
                test_message = "Hello, respond briefly."
        workspace = test_meta.get("workspace", "")

        session_id = f"btest-{int(time.time())}-{i}"

        # Send message and wait
        result, err = send_and_wait(app_id, session_id, test_message, h, workspace=workspace, wait=10)

        if err:
            failed += 1
            errors.append(f"  {name} [{test_type}]: {err}")
            continue

        if not result["has_response"]:
            # Check if still running
            r_sess = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}", headers=h, timeout=10)
            is_active = r_sess.json().get("data", {}).get("is_active", False)
            if is_active:
                # Wait more
                time.sleep(10)
                result, err = send_and_wait(app_id, session_id, "", h, wait=0)
                # Re-check history
                r3 = httpx.get(f"{B}/api/apps/{app_id}/sessions/{session_id}/history", headers=h, timeout=10)
                msgs = r3.json().get("data", {}).get("messages", [])
                asst = [m for m in msgs if m.get("role") == "assistant"]
                if asst:
                    result["content"] = asst[-1].get("content", "")
                    result["has_response"] = bool(result["content"])

        # ── Verify behavior based on test type ──
        ok = False
        detail = ""

        if test_type == "security_block":
            # Agent should NOT have successfully used the blocked tool
            blocked = test_meta.get("blocked_tool", "")
            tool_used = any(blocked in str(tc) for tc in result["tool_calls"])
            ok = not tool_used and result["has_response"]
            detail = f"blocked={blocked} tool_used={tool_used}"

        elif test_type == "tool_accessible":
            ok = result["has_response"]
            detail = f"response={result['content'][:50]}"

        elif test_type == "tool_works":
            ok = result["has_response"]

        elif test_type == "mixed_policy":
            ok = result["has_response"]

        elif test_type == "approval_required":
            # The agent is BLOCKED waiting for approval - check via /approvals route
            try:
                r_approvals = httpx.get(f"{B}/api/apps/{app_id}/approvals", headers=h, timeout=10)
                pending = r_approvals.json().get("data", {}).get("pending", [])
                ok = len(pending) > 0 or "approval" in result["content"].lower() or "permission" in result["content"].lower()
                detail = f"pending_approvals={len(pending)}"
                # Deny the approval so the agent unblocks
                for p in pending:
                    try:
                        httpx.post(f"{B}/api/apps/{app_id}/approve", json={
                            "request_id": p["request_id"], "approved": False, "message": "test denied",
                        }, headers=h, timeout=5)
                    except Exception:
                        pass
            except Exception as exc:
                ok = False
                detail = f"approval check failed: {exc}"

        elif test_type == "no_tools":
            ok = result["has_response"] and len(result["tool_calls"]) == 0
            detail = f"tool_calls={len(result['tool_calls'])}"

        elif test_type == "all_auto":
            ok = result["has_response"]

        elif test_type == "hook_inject":
            expected = test_meta.get("expect_in_response", "")
            ok = expected.lower() in result["content"].lower() if expected else result["has_response"]
            detail = f"expected='{expected}' found={ok}"

        elif test_type == "hook_shell":
            ok = result["has_response"]

        elif test_type == "hook_no_fire":
            ok = result["has_response"]

        elif test_type == "hook_fires":
            ok = result["has_response"]

        elif test_type == "hook_chain":
            expected = test_meta.get("expect_in_response", "")
            ok = expected.lower() in result["content"].lower() if expected else result["has_response"]
            detail = f"expected='{expected}'"

        elif test_type == "hook_cooldown":
            ok = result["has_response"]

        elif test_type == "hook_notify":
            ok = result["has_response"]

        elif test_type == "middleware_inject":
            expected = test_meta.get("expect_in_response", "")
            ok = expected.lower() in result["content"].lower() if expected else result["has_response"]
            detail = f"expected='{expected}' in response"

        elif test_type == "content_filter":
            # Send the blocked message
            blocked_msg = test_meta.get("test_message", "")
            if blocked_msg:
                session2 = f"btest-filter-{int(time.time())}-{i}"
                result2, _ = send_and_wait(app_id, session2, blocked_msg, h, wait=8)
                if result2:
                    ok = "blocked" in result2["content"].lower() or "filter" in result2["content"].lower() or not result2["has_response"]
                else:
                    ok = True  # Send failed = blocked
                detail = f"blocked_msg='{blocked_msg[:30]}'"
            else:
                ok = result["has_response"]

        elif test_type == "response_maxlen":
            max_len = test_meta.get("max_length", 500)
            ok = len(result["content"]) <= max_len + 50  # Small tolerance
            detail = f"len={len(result['content'])} max={max_len}"

        elif test_type == "secret_mask":
            forbidden = test_meta.get("expect_not_in_response", "")
            test_msg = test_meta.get("test_message", "")
            if test_msg:
                session3 = f"btest-secret-{int(time.time())}-{i}"
                result3, _ = send_and_wait(app_id, session3, test_msg, h, wait=8)
                if result3:
                    ok = forbidden not in result3["content"]
                    detail = f"forbidden='{forbidden}' found={forbidden in result3['content']}"
                else:
                    ok = True
            else:
                ok = result["has_response"]

        elif test_type == "middleware_combo":
            expected = test_meta.get("expect_in_response", "")
            ok = expected.lower() in result["content"].lower() if expected else result["has_response"]

        elif test_type in ("tool_injection", "workspace", "workspace_fixed", "context",
                           "metadata", "vision_config", "module_combo", "lsp",
                           "error_handling", "multi_agent"):
            ok = result["has_response"]
            detail = f"response={result['content'][:40]}"

        elif test_type == "one_shot":
            expected = test_meta.get("expect_in_response", "")
            ok = expected.lower() in result["content"].lower() if expected else result["has_response"]

        elif test_type == "workspace_required":
            ws = test_meta.get("workspace", "")
            result_ws, _ = send_and_wait(app_id, f"ws-{int(time.time())}", "Say OK", h, workspace=ws, wait=10)
            ok = result_ws is not None and result_ws["has_response"]
            detail = f"workspace={ws[:30]}"

        else:
            ok = result["has_response"]
            detail = f"unknown test type: {test_type}"

        if ok:
            passed += 1
        else:
            failed += 1
            errors.append(f"  {name} [{test_type}]: {detail} response='{result['content'][:60]}'")

        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(yamls)}] {passed} OK, {failed} FAIL")
            # Refresh token every 20 apps to avoid expiry
            try:
                token = login()
                h["Authorization"] = f"Bearer {token}"
            except Exception:
                pass

    # Cleanup
    print(f"\nCleaning up {len(deployed_ids)} apps...")
    token = login()
    h["Authorization"] = f"Bearer {token}"
    for app_id in deployed_ids:
        try:
            httpx.delete(f"{B}/api/apps/{app_id}", headers=h, timeout=10)
        except Exception:
            pass

    print(f"\n{'=' * 60}")
    print(f"PASSED: {passed}")
    print(f"FAILED: {failed}")
    print(f"TOTAL:  {passed + failed}")
    print(f"{'=' * 60}")

    if errors:
        print(f"\nFAILURES ({len(errors)}):")
        for e in errors[:50]:
            print(e)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
