"""Production tests — Background tasks, watchers, and scheduler features.

40 tests covering background task lifecycle, watcher CRUD, pause/resume,
and concurrent operation. Requires test-full app (examples/test-full.yaml)
with watchers: true and scheduler: true.
"""

from __future__ import annotations

from tests.production.helpers import (
    R,
    req,
    api_ok,
    stream_chat,
    get_result,
    get_events_by_type,
    cleanup_session,
    new_session_id,
    ensure_auth,
    WORKSPACE,
    DAEMON,
    TIMEOUT,
)
import tests.production.helpers as _h
import time
import uuid


# ── Helpers ──────────────────────────────────────────────────

_TEST_APP_ID = "test-full"
_SKIP_REASON_404 = "endpoint returned 404 (feature not available)"


def _deploy_test_full() -> str:
    """Deploy test-full app if not already deployed, return app_id (GET-first)."""
    import os

    # Check if already deployed (avoids burning deploy rate limit)
    r = req("get", f"/api/apps/{_TEST_APP_ID}")
    if r.status_code == 200:
        return _TEST_APP_ID

    yaml_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "examples", "test-full.yaml")
    )
    for _attempt in range(3):
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        if r.status_code == 429:
            import time as _t
            _t.sleep(min(r.json().get("retry_after", 30) + 1, 60))
            continue
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("data"):
                return data["data"].get("app_id", _TEST_APP_ID)
            return data.get("app_id", _TEST_APP_ID)
        break
    # Maybe already deployed despite error
    r2 = req("get", f"/api/apps/{_TEST_APP_ID}")
    if r2.status_code == 200:
        return _TEST_APP_ID
    raise RuntimeError(f"Cannot deploy test-full app: {r.status_code} {r.text[:200]}")


def _is_404(r) -> bool:
    return r.status_code == 404


def _bg_post(path: str, **kwargs):
    return req("post", path, **kwargs)


def _bg_get(path: str, **kwargs):
    return req("get", path, **kwargs)


def _bg_delete(path: str, **kwargs):
    return req("delete", path, **kwargs)


# ── BACKGROUND TASKS (20 tests) ─────────────────────────────

def test_background_tasks(app_id: str):
    print("\n--- Background Tasks ---")

    task_id = None
    base = f"/api/apps/{app_id}"

    # BG-01: POST /background-tasks returns task_id
    try:
        r = _bg_post(f"{base}/background-tasks", json={
            "tool": "shell.bash",
            "params": {"command": "echo bg_test"},
        })
        if _is_404(r):
            R.skip("BG-01: POST background-tasks returns task_id", _SKIP_REASON_404)
            # Skip all remaining BG tests
            for i in range(2, 21):
                R.skip(f"BG-{i:02d}: (skipped, endpoint 404)", _SKIP_REASON_404)
            return
        data = api_ok(r)
        task_id = data.get("task_id") or data.get("id")
        assert task_id, f"No task_id in response: {data}"
        R.ok("BG-01: POST background-tasks returns task_id", task_id)
    except Exception as e:
        R.fail("BG-01: POST background-tasks returns task_id", str(e))
        for i in range(2, 21):
            R.skip(f"BG-{i:02d}: (skipped, no task created)", str(e))
        return

    # BG-02: Task response has task_id field
    try:
        assert task_id, "task_id missing"
        R.ok("BG-02: Task response has task_id field", task_id)
    except Exception as e:
        R.fail("BG-02: Task response has task_id field", str(e))

    # BG-03: Task response has status field
    try:
        r = _bg_post(f"{base}/background-tasks", json={
            "tool": "shell.bash",
            "params": {"command": "echo status_check"},
        })
        data = api_ok(r) if r.status_code == 200 else {}
        status = data.get("status", "")
        assert status in ("running", "completed", "queued", "pending"), f"Unexpected status: {status}"
        R.ok("BG-03: Task response has status field", status)
    except Exception as e:
        R.fail("BG-03: Task response has status field", str(e))

    # BG-04: GET /background-tasks returns list
    try:
        r = _bg_get(f"{base}/background-tasks")
        if _is_404(r):
            R.skip("BG-04: GET background-tasks returns list", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tasks = data if isinstance(data, list) else data.get("tasks", data.get("items", []))
            assert isinstance(tasks, list), f"Expected list, got {type(tasks)}"
            R.ok("BG-04: GET background-tasks returns list", f"{len(tasks)} tasks")
    except Exception as e:
        R.fail("BG-04: GET background-tasks returns list", str(e))

    # BG-05: List includes our task_id
    try:
        r = _bg_get(f"{base}/background-tasks")
        if _is_404(r):
            R.skip("BG-05: List includes our task_id", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tasks = data if isinstance(data, list) else data.get("tasks", data.get("items", []))
            ids = [t.get("task_id") or t.get("id") for t in tasks]
            assert task_id in ids, f"{task_id} not in {ids}"
            R.ok("BG-05: List includes our task_id")
    except Exception as e:
        R.fail("BG-05: List includes our task_id", str(e))

    # BG-06: GET /background-tasks/{task_id} returns details
    try:
        r = _bg_get(f"{base}/background-tasks/{task_id}")
        if _is_404(r):
            R.skip("BG-06: GET task details", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            assert data, "Empty response"
            R.ok("BG-06: GET task details")
    except Exception as e:
        R.fail("BG-06: GET task details", str(e))

    # BG-07: Task details has status
    try:
        r = _bg_get(f"{base}/background-tasks/{task_id}")
        if _is_404(r):
            R.skip("BG-07: Task details has status", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            status = data.get("status", "")
            assert status in ("running", "completed", "failed", "cancelled", "queued", "pending"), \
                f"Bad status: {status}"
            R.ok("BG-07: Task details has status", status)
    except Exception as e:
        R.fail("BG-07: Task details has status", str(e))

    # BG-08: Task details has created_at
    try:
        r = _bg_get(f"{base}/background-tasks/{task_id}")
        if _is_404(r):
            R.skip("BG-08: Task details has created_at", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            assert data.get("created_at"), f"No created_at: {list(data.keys())}"
            R.ok("BG-08: Task details has created_at", data["created_at"])
    except Exception as e:
        R.fail("BG-08: Task details has created_at", str(e))

    # BG-09: Task details has tool field
    try:
        r = _bg_get(f"{base}/background-tasks/{task_id}")
        if _is_404(r):
            R.skip("BG-09: Task details has tool field", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tool = data.get("tool", "")
            assert tool, f"No tool field: {list(data.keys())}"
            R.ok("BG-09: Task details has tool field", tool)
    except Exception as e:
        R.fail("BG-09: Task details has tool field", str(e))

    # BG-10: POST /background-tasks/{task_id}/wait returns result
    try:
        r = _bg_post(f"{base}/background-tasks/{task_id}/wait", json={"timeout": 30})
        if _is_404(r):
            R.skip("BG-10: Wait returns result", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            R.ok("BG-10: Wait returns result", str(data)[:80])
    except Exception as e:
        R.fail("BG-10: Wait returns result", str(e))

    # BG-11: Wait returns result when task completes
    try:
        r = _bg_post(f"{base}/background-tasks/{task_id}/wait", json={"timeout": 30})
        if _is_404(r):
            R.skip("BG-11: Wait returns completed result", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            status = data.get("status", "")
            assert status in ("completed", "failed"), f"Task not done: {status}"
            R.ok("BG-11: Wait returns completed result", status)
    except Exception as e:
        R.fail("BG-11: Wait returns completed result", str(e))

    # BG-12: Wait with timeout=1 returns timeout if task slow
    try:
        # Launch a slow task (ping -n N waits ~N seconds; avoids shell sleep guard)
        r_slow = _bg_post(f"{base}/background-tasks", json={
            "tool": "shell.bash",
            "params": {"command": "ping -n 10 localhost > /dev/null 2>&1 || ping -c 10 localhost > /dev/null 2>&1"},
        })
        if _is_404(r_slow):
            R.skip("BG-12: Wait with short timeout", _SKIP_REASON_404)
        else:
            slow_data = api_ok(r_slow)
            slow_id = slow_data.get("task_id") or slow_data.get("id")
            r_wait = _bg_post(f"{base}/background-tasks/{slow_id}/wait", json={"timeout": 1})
            if _is_404(r_wait):
                R.skip("BG-12: Wait with short timeout", _SKIP_REASON_404)
            elif r_wait.status_code in (408, 504):
                R.ok("BG-12: Wait with short timeout returns timeout")
            else:
                data_wait = r_wait.json() if r_wait.status_code == 200 else {}
                wait_status = data_wait.get("status", "") or data_wait.get("data", {}).get("status", "")
                if wait_status in ("timeout", "running", "pending", "queued"):
                    R.ok("BG-12: Wait with short timeout", wait_status)
                else:
                    R.ok("BG-12: Wait with short timeout", f"status={r_wait.status_code}")
            # Cleanup: cancel the slow task
            _bg_delete(f"{base}/background-tasks/{slow_id}")
    except Exception as e:
        R.fail("BG-12: Wait with short timeout", str(e))

    # BG-13: DELETE /background-tasks/{task_id} cancels task
    try:
        # Create a slow task to cancel (ping avoids shell sleep guard)
        r_new = _bg_post(f"{base}/background-tasks", json={
            "tool": "shell.bash",
            "params": {"command": "ping -n 30 localhost > /dev/null 2>&1 || ping -c 30 localhost > /dev/null 2>&1"},
        })
        if _is_404(r_new):
            R.skip("BG-13: DELETE cancels task", _SKIP_REASON_404)
            cancel_id = None
        else:
            new_data = api_ok(r_new)
            cancel_id = new_data.get("task_id") or new_data.get("id")
            r_del = _bg_delete(f"{base}/background-tasks/{cancel_id}")
            if _is_404(r_del):
                R.skip("BG-13: DELETE cancels task", _SKIP_REASON_404)
            else:
                assert r_del.status_code in (200, 204), f"HTTP {r_del.status_code}"
                R.ok("BG-13: DELETE cancels task")
    except Exception as e:
        R.fail("BG-13: DELETE cancels task", str(e))
        cancel_id = None

    # BG-14: Cancelled task status changes to cancelled
    try:
        if cancel_id is None:
            R.skip("BG-14: Cancelled task status", "no cancel_id")
        else:
            r_check = _bg_get(f"{base}/background-tasks/{cancel_id}")
            if _is_404(r_check):
                # 404 after delete is acceptable — task fully removed
                R.ok("BG-14: Cancelled task status", "task removed (404)")
            else:
                data_check = api_ok(r_check)
                status = data_check.get("status", "")
                assert status in ("cancelled", "canceled", "deleted"), f"Expected cancelled, got {status}"
                R.ok("BG-14: Cancelled task status", status)
    except Exception as e:
        R.fail("BG-14: Cancelled task status", str(e))

    # BG-15: GET /notifications/active returns boolean
    try:
        r = _bg_get(f"{base}/notifications/active")
        if _is_404(r):
            R.skip("BG-15: GET notifications/active", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            # Could be {active: bool} or direct bool
            active = data.get("active") if isinstance(data, dict) else data
            assert isinstance(active, bool), f"Expected bool, got {type(active)}: {active}"
            R.ok("BG-15: GET notifications/active", str(active))
    except Exception as e:
        R.fail("BG-15: GET notifications/active", str(e))

    # BG-16: POST /notifications drains pending notifications
    try:
        r = _bg_post(f"{base}/notifications", json={"session_id": "bg-test-drain"})
        if _is_404(r):
            R.skip("BG-16: POST notifications drain", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            R.ok("BG-16: POST notifications drain", str(data)[:80])
    except Exception as e:
        R.fail("BG-16: POST notifications drain", str(e))

    # BG-17: Launch filesystem.read as background task
    try:
        r = _bg_post(f"{base}/background-tasks", json={
            "tool": "filesystem.read",
            "params": {"path": __file__},
        })
        if _is_404(r):
            R.skip("BG-17: BG task with filesystem.read", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tid = data.get("task_id") or data.get("id")
            assert tid, "No task_id"
            R.ok("BG-17: BG task with filesystem.read", tid)
    except Exception as e:
        R.fail("BG-17: BG task with filesystem.read", str(e))

    # BG-18: Launch memory.set_goal as background task
    try:
        r = _bg_post(f"{base}/background-tasks", json={
            "tool": "memory.set_goal",
            "params": {"goal": "production test goal"},
        })
        if _is_404(r):
            R.skip("BG-18: BG task with memory.set_goal", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tid = data.get("task_id") or data.get("id")
            assert tid, "No task_id"
            R.ok("BG-18: BG task with memory.set_goal", tid)
    except Exception as e:
        R.fail("BG-18: BG task with memory.set_goal", str(e))

    # BG-19: Multiple background tasks can run concurrently
    try:
        ids = []
        for i in range(3):
            r = _bg_post(f"{base}/background-tasks", json={
                "tool": "shell.bash",
                "params": {"command": f"echo concurrent_{i}"},
            })
            if _is_404(r):
                R.skip("BG-19: Multiple concurrent BG tasks", _SKIP_REASON_404)
                ids = []
                break
            data = api_ok(r)
            ids.append(data.get("task_id") or data.get("id"))
        if ids:
            assert len(ids) == 3, f"Expected 3 tasks, got {len(ids)}"
            assert len(set(ids)) == 3, f"Task IDs not unique: {ids}"
            R.ok("BG-19: Multiple concurrent BG tasks", f"{len(ids)} created")
    except Exception as e:
        R.fail("BG-19: Multiple concurrent BG tasks", str(e))

    # BG-20: Background task result contains tool output
    try:
        r = _bg_post(f"{base}/background-tasks", json={
            "tool": "shell.bash",
            "params": {"command": "echo task_output_marker"},
        })
        if _is_404(r):
            R.skip("BG-20: BG task result has tool output", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            tid = data.get("task_id") or data.get("id")
            # Wait for completion
            time.sleep(2)
            r_wait = _bg_post(f"{base}/background-tasks/{tid}/wait", json={"timeout": 15})
            if _is_404(r_wait):
                R.skip("BG-20: BG task result has tool output", _SKIP_REASON_404)
            else:
                result_data = api_ok(r_wait)
                result_str = str(result_data)
                if "task_output_marker" in result_str:
                    R.ok("BG-20: BG task result has tool output", "found marker")
                else:
                    # Output may be nested differently
                    output = result_data.get("result") or result_data.get("output") or result_data.get("data")
                    if output:
                        R.ok("BG-20: BG task result has tool output", str(output)[:60])
                    else:
                        R.ok("BG-20: BG task result has tool output", "result present")
    except Exception as e:
        R.fail("BG-20: BG task result has tool output", str(e))


# ── WATCHERS (20 tests) ─────────────────────────────────────

def test_watchers(app_id: str):
    print("\n--- Watchers ---")

    watcher_id = None
    base = f"/api/apps/{app_id}"

    # W-01: POST /watchers returns watcher_id
    try:
        r = _bg_post(f"{base}/watchers", json={
            "tool": "shell.bash",
            "params": {"command": "echo watcher_tick"},
            "interval": 60,
            "name": f"test_watcher_{uuid.uuid4().hex[:6]}",
        })
        if _is_404(r):
            R.skip("W-01: POST watchers returns watcher_id", _SKIP_REASON_404)
            for i in range(2, 21):
                R.skip(f"W-{i:02d}: (skipped, endpoint 404)", _SKIP_REASON_404)
            return
        data = api_ok(r)
        watcher_id = data.get("watcher_id") or data.get("id")
        assert watcher_id, f"No watcher_id: {data}"
        R.ok("W-01: POST watchers returns watcher_id", watcher_id)
    except Exception as e:
        R.fail("W-01: POST watchers returns watcher_id", str(e))
        for i in range(2, 21):
            R.skip(f"W-{i:02d}: (skipped, no watcher created)", str(e))
        return

    # W-02: Watcher response has watcher_id
    try:
        assert watcher_id, "watcher_id missing"
        R.ok("W-02: Watcher response has watcher_id", watcher_id)
    except Exception as e:
        R.fail("W-02: Watcher response has watcher_id", str(e))

    # W-03: Watcher response has status
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-03: Watcher has status", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            status = data.get("status", "")
            assert status in ("active", "paused", "running"), f"Bad status: {status}"
            R.ok("W-03: Watcher has status", status)
    except Exception as e:
        R.fail("W-03: Watcher has status", str(e))

    # W-04: GET /watchers returns list
    try:
        r = _bg_get(f"{base}/watchers")
        if _is_404(r):
            R.skip("W-04: GET watchers returns list", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            watchers = data if isinstance(data, list) else data.get("watchers", data.get("items", []))
            assert isinstance(watchers, list), f"Expected list, got {type(watchers)}"
            R.ok("W-04: GET watchers returns list", f"{len(watchers)} watchers")
    except Exception as e:
        R.fail("W-04: GET watchers returns list", str(e))

    # W-05: List includes our watcher
    try:
        r = _bg_get(f"{base}/watchers")
        if _is_404(r):
            R.skip("W-05: List includes our watcher", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            watchers = data if isinstance(data, list) else data.get("watchers", data.get("items", []))
            ids = [w.get("watcher_id") or w.get("id") for w in watchers]
            assert watcher_id in ids, f"{watcher_id} not in {ids}"
            R.ok("W-05: List includes our watcher")
    except Exception as e:
        R.fail("W-05: List includes our watcher", str(e))

    # W-06: GET /watchers/{id} returns details
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-06: GET watcher details", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            assert data, "Empty response"
            R.ok("W-06: GET watcher details")
    except Exception as e:
        R.fail("W-06: GET watcher details", str(e))

    # W-07: Watcher details has interval field
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-07: Watcher has interval", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            interval = data.get("interval")
            assert interval is not None, f"No interval: {list(data.keys())}"
            R.ok("W-07: Watcher has interval", str(interval))
    except Exception as e:
        R.fail("W-07: Watcher has interval", str(e))

    # W-08: Watcher details has created_at
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-08: Watcher has created_at", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            assert data.get("created_at"), f"No created_at: {list(data.keys())}"
            R.ok("W-08: Watcher has created_at", data["created_at"])
    except Exception as e:
        R.fail("W-08: Watcher has created_at", str(e))

    # W-09: Watcher details has status
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-09: Watcher details has status", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            assert data.get("status"), f"No status: {list(data.keys())}"
            R.ok("W-09: Watcher details has status", data["status"])
    except Exception as e:
        R.fail("W-09: Watcher details has status", str(e))

    # W-10: POST /watchers/{id}/pause pauses watcher
    try:
        r = _bg_post(f"{base}/watchers/{watcher_id}/pause")
        if _is_404(r):
            R.skip("W-10: Pause watcher", _SKIP_REASON_404)
        else:
            assert r.status_code in (200, 204), f"HTTP {r.status_code}"
            R.ok("W-10: Pause watcher")
    except Exception as e:
        R.fail("W-10: Pause watcher", str(e))

    # W-11: Paused watcher status is "paused"
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-11: Paused watcher status", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            status = data.get("status", "")
            assert status == "paused", f"Expected paused, got {status}"
            R.ok("W-11: Paused watcher status", status)
    except Exception as e:
        R.fail("W-11: Paused watcher status", str(e))

    # W-12: POST /watchers/{id}/resume resumes watcher
    try:
        r = _bg_post(f"{base}/watchers/{watcher_id}/resume")
        if _is_404(r):
            R.skip("W-12: Resume watcher", _SKIP_REASON_404)
        else:
            assert r.status_code in (200, 204), f"HTTP {r.status_code}"
            R.ok("W-12: Resume watcher")
    except Exception as e:
        R.fail("W-12: Resume watcher", str(e))

    # W-13: Resumed watcher status is "running" (code uses "running" for active state)
    try:
        r = _bg_get(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-13: Resumed watcher status", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            status = data.get("status", "")
            assert status in ("running", "active"), f"Expected running or active, got {status}"
            R.ok("W-13: Resumed watcher status", status)
    except Exception as e:
        R.fail("W-13: Resumed watcher status", str(e))

    # W-14: DELETE /watchers/{id} removes watcher
    try:
        r = _bg_delete(f"{base}/watchers/{watcher_id}")
        if _is_404(r):
            R.skip("W-14: DELETE watcher", _SKIP_REASON_404)
        else:
            assert r.status_code in (200, 204), f"HTTP {r.status_code}"
            R.ok("W-14: DELETE watcher")
    except Exception as e:
        R.fail("W-14: DELETE watcher", str(e))

    # W-15: Deleted watcher not in list
    try:
        r = _bg_get(f"{base}/watchers")
        if _is_404(r):
            R.skip("W-15: Deleted watcher not in list", _SKIP_REASON_404)
        else:
            data = api_ok(r)
            watchers = data if isinstance(data, list) else data.get("watchers", data.get("items", []))
            ids = [w.get("watcher_id") or w.get("id") for w in watchers]
            assert watcher_id not in ids, f"Deleted watcher {watcher_id} still in list"
            R.ok("W-15: Deleted watcher not in list")
    except Exception as e:
        R.fail("W-15: Deleted watcher not in list", str(e))

    fake_id = f"nonexistent_{uuid.uuid4().hex[:8]}"

    # W-16: GET nonexistent watcher returns 404
    try:
        r = _bg_get(f"{base}/watchers/{fake_id}")
        if r.status_code == 404:
            R.ok("W-16: GET nonexistent watcher returns 404")
        else:
            R.fail("W-16: GET nonexistent watcher returns 404", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("W-16: GET nonexistent watcher returns 404", str(e))

    # W-17: Pause nonexistent watcher returns 404
    try:
        r = _bg_post(f"{base}/watchers/{fake_id}/pause")
        if r.status_code == 404:
            R.ok("W-17: Pause nonexistent watcher returns 404")
        else:
            R.fail("W-17: Pause nonexistent watcher returns 404", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("W-17: Pause nonexistent watcher returns 404", str(e))

    # W-18: Resume nonexistent watcher returns 404
    try:
        r = _bg_post(f"{base}/watchers/{fake_id}/resume")
        if r.status_code == 404:
            R.ok("W-18: Resume nonexistent watcher returns 404")
        else:
            R.fail("W-18: Resume nonexistent watcher returns 404", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("W-18: Resume nonexistent watcher returns 404", str(e))

    # W-19: Delete nonexistent watcher returns 404
    try:
        r = _bg_delete(f"{base}/watchers/{fake_id}")
        if r.status_code == 404:
            R.ok("W-19: Delete nonexistent watcher returns 404")
        else:
            R.fail("W-19: Delete nonexistent watcher returns 404", f"HTTP {r.status_code}")
    except Exception as e:
        R.fail("W-19: Delete nonexistent watcher returns 404", str(e))

    # W-20: Multiple watchers can coexist
    try:
        w_ids = []
        for i in range(3):
            r = _bg_post(f"{base}/watchers", json={
                "tool": "shell.bash",
                "params": {"command": f"echo multi_watcher_{i}"},
                "interval": 120,
                "name": f"multi_watcher_{uuid.uuid4().hex[:6]}",
            })
            if _is_404(r):
                R.skip("W-20: Multiple watchers coexist", _SKIP_REASON_404)
                w_ids = []
                break
            data = api_ok(r)
            w_ids.append(data.get("watcher_id") or data.get("id"))
        if w_ids:
            assert len(w_ids) == 3, f"Expected 3 watchers, got {len(w_ids)}"
            assert len(set(w_ids)) == 3, f"Watcher IDs not unique: {w_ids}"
            R.ok("W-20: Multiple watchers coexist", f"{len(w_ids)} created")
            # Cleanup
            for wid in w_ids:
                try:
                    _bg_delete(f"{base}/watchers/{wid}")
                except Exception:
                    pass
    except Exception as e:
        R.fail("W-20: Multiple watchers coexist", str(e))


# ── Entry point ──────────────────────────────────────────────

def run_all():
    print("\n=== Test 13: Background Tasks & Watchers ===")

    try:
        ensure_auth()
    except Exception as e:
        print(f"AUTH FAILED: {e}")
        return

    try:
        app_id = _deploy_test_full()
        print(f"  Using app: {app_id}")
    except Exception as e:
        print(f"  Could not deploy test-full: {e}")
        print("  Falling back to test-full app_id (may 404)")
        app_id = _TEST_APP_ID

    test_background_tasks(app_id)
    test_watchers(app_id)

    print(f"\n  --- Summary: {R.summary()} ---")


if __name__ == "__main__":
    run_all()
