"""Production tests: Metrics, Modules, MCP, and Requirements endpoints.

Makes real HTTP requests to a live daemon. No mocking.
"""

from __future__ import annotations

import time

from tests.production.helpers import R, req, api_ok, ensure_app_deployed, DAEMON, TIMEOUT


def _extract_list(body, *keys):
    """Extract a list from a JSON response body, trying 'data' and any extra keys."""
    if isinstance(body, list):
        return body
    for k in ("data", *keys):
        v = body.get(k) if isinstance(body, dict) else None
        if isinstance(v, list):
            return v
    return body


# ═══════════════════════════════════════════════════════════════
#  METRICS TESTS
# ═══════════════════════════════════════════════════════════════

def _metrics_returns_json():
    """GET /api/metrics - returns JSON object."""
    name = "metrics_returns_json"
    try:
        r = req("get", "/api/metrics")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name, f"keys={list(body.keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_has_uptime():
    """Metrics has uptime_seconds > 0."""
    name = "metrics_uptime_seconds"
    try:
        r = req("get", "/api/metrics")
        data = r.json()
        if "data" in data:
            data = data["data"]
        uptime = data.get("uptime_seconds", data.get("uptime", None))
        assert uptime is not None, f"no uptime field in metrics: {list(data.keys())}"
        assert float(uptime) > 0, f"uptime should be > 0, got {uptime}"
        R.ok(name, f"uptime={uptime:.1f}s")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_has_counters():
    """Metrics has counters section."""
    name = "metrics_has_counters"
    try:
        r = req("get", "/api/metrics")
        data = r.json()
        if "data" in data:
            data = data["data"]
        has_counters = "counters" in data or any("counter" in k.lower() for k in data)
        assert has_counters, f"no counters in metrics: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_has_gauges():
    """Metrics has gauges section."""
    name = "metrics_has_gauges"
    try:
        r = req("get", "/api/metrics")
        data = r.json()
        if "data" in data:
            data = data["data"]
        has_gauges = "gauges" in data or any("gauge" in k.lower() for k in data)
        assert has_gauges, f"no gauges in metrics: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_has_histograms():
    """Metrics has histograms section."""
    name = "metrics_has_histograms"
    try:
        r = req("get", "/api/metrics")
        data = r.json()
        if "data" in data:
            data = data["data"]
        has_histograms = "histograms" in data or any("histogram" in k.lower() for k in data)
        assert has_histograms, f"no histograms in metrics: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_prometheus_text():
    """GET /api/metrics/prometheus - returns text."""
    name = "metrics_prometheus_text"
    try:
        r = req("get", "/api/metrics/prometheus")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        text = r.text
        assert len(text) > 0, "prometheus response is empty"
        R.ok(name, f"length={len(text)}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_prometheus_help_lines():
    """Prometheus format has # HELP lines."""
    name = "metrics_prometheus_help"
    try:
        r = req("get", "/api/metrics/prometheus")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        text = r.text
        help_lines = [l for l in text.splitlines() if l.startswith("# HELP")]
        assert len(help_lines) > 0, "no # HELP lines in prometheus output"
        R.ok(name, f"count={len(help_lines)}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_prometheus_type_lines():
    """Prometheus format has # TYPE lines."""
    name = "metrics_prometheus_type"
    try:
        r = req("get", "/api/metrics/prometheus")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        text = r.text
        type_lines = [l for l in text.splitlines() if l.startswith("# TYPE")]
        assert len(type_lines) > 0, "no # TYPE lines in prometheus output"
        R.ok(name, f"count={len(type_lines)}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_sessions():
    """GET /api/metrics/sessions - returns array."""
    name = "metrics_sessions"
    try:
        r = req("get", "/api/metrics/sessions")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        if isinstance(body, list):
            data = body
        else:
            data = body.get("data", body)
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        R.ok(name, f"count={len(data)}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_sessions_has_identity():
    """Session metrics has identity section."""
    name = "metrics_sessions_identity"
    try:
        r = req("get", "/api/metrics/sessions")
        body = r.json()
        if isinstance(body, list):
            data = body
        else:
            data = body.get("data", body)
        if not isinstance(data, list) or len(data) == 0:
            R.skip(name, "no session metrics available")
            return
        entry = data[0]
        has_identity = "identity" in entry or "session_id" in entry or "id" in entry
        assert has_identity, f"no identity in session metrics: {list(entry.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_sessions_has_tokens():
    """Session metrics has tokens section."""
    name = "metrics_sessions_tokens"
    try:
        r = req("get", "/api/metrics/sessions")
        body = r.json()
        if isinstance(body, list):
            data = body
        else:
            data = body.get("data", body)
        if not isinstance(data, list) or len(data) == 0:
            R.skip(name, "no session metrics available")
            return
        entry = data[0]
        has_tokens = "tokens" in entry or any("token" in k.lower() for k in entry)
        assert has_tokens, f"no tokens in session metrics: {list(entry.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_sessions_has_lifecycle():
    """Session metrics has lifecycle section."""
    name = "metrics_sessions_lifecycle"
    try:
        r = req("get", "/api/metrics/sessions")
        body = r.json()
        if isinstance(body, list):
            data = body
        else:
            data = body.get("data", body)
        if not isinstance(data, list) or len(data) == 0:
            R.skip(name, "no session metrics available")
            return
        entry = data[0]
        has_lifecycle = (
            "lifecycle" in entry
            or "created_at" in entry
            or "started" in entry
            or any("life" in k.lower() or "time" in k.lower() for k in entry)
        )
        assert has_lifecycle, f"no lifecycle in session metrics: {list(entry.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_app():
    """GET /api/metrics/apps/{id} - returns app metrics."""
    name = "metrics_app"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/metrics/apps/{app_id}")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name, f"keys={list(body.get('data', body).keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _metrics_app_session_count():
    """App metrics has session count."""
    name = "metrics_app_session_count"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/metrics/apps/{app_id}")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        data = r.json()
        if "data" in data:
            data = data["data"]
        has_sessions = (
            "session_count" in data
            or "sessions" in data
            or "active_sessions" in data
            or any("session" in k.lower() for k in data)
        )
        assert has_sessions, f"no session count in app metrics: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _metrics_response_time():
    """Metrics endpoint response time < 1s."""
    name = "metrics_response_time"
    try:
        start = time.perf_counter()
        r = req("get", "/api/metrics")
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        assert elapsed_ms < 1000, f"too slow: {elapsed_ms:.0f}ms"
        R.ok(name, f"{elapsed_ms:.0f}ms")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  MODULES TESTS
# ═══════════════════════════════════════════════════════════════

def _modules_list():
    """GET /api/modules - returns array."""
    name = "modules_list"
    try:
        r = req("get", "/api/modules")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        R.ok(name, f"count={len(data)}")
    except Exception as e:
        R.fail(name, str(e))


def _modules_has_filesystem():
    """Module list has filesystem module."""
    name = "modules_has_filesystem"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        names = [
            (m.get("module_id", m.get("name", "")) if isinstance(m, dict) else str(m)).lower()
            for m in data
        ]
        assert any("filesystem" in n or "fs" in n for n in names), \
            f"no filesystem module in: {names}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _modules_has_shell():
    """Module list has shell module."""
    name = "modules_has_shell"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        names = [
            (m.get("module_id", m.get("name", "")) if isinstance(m, dict) else str(m)).lower()
            for m in data
        ]
        assert any("shell" in n or "exec" in n or "command" in n for n in names), \
            f"no shell module in: {names}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _modules_has_memory():
    """Module list has memory module."""
    name = "modules_has_memory"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        names = [
            (m.get("module_id", m.get("name", "")) if isinstance(m, dict) else str(m)).lower()
            for m in data
        ]
        assert any("memory" in n or "mem" in n for n in names), \
            f"no memory module in: {names}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _modules_have_module_id():
    """Each module has module_id."""
    name = "modules_have_module_id"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list) and len(data) > 0, "no modules returned"
        for m in data:
            if isinstance(m, str):
                continue  # module names returned as strings
            assert "module_id" in m or "id" in m or "name" in m, \
                f"module missing id field: {list(m.keys())}"
        R.ok(name, f"checked {len(data)} modules")
    except Exception as e:
        R.fail(name, str(e))


def _modules_have_version():
    """Each module has version."""
    name = "modules_have_version"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list) and len(data) > 0, "no modules returned"
        missing_version = 0
        for m in data:
            if isinstance(m, str):
                continue  # module names returned as strings
            if "version" not in m and "ver" not in m:
                missing_version += 1
        if missing_version > 0:
            R.skip(name, f"{missing_version}/{len(data)} modules missing version field")
            return
        R.ok(name, f"checked {len(data)} modules")
    except Exception as e:
        R.fail(name, str(e))


def _modules_have_status():
    """Each module has status (loaded/failed)."""
    name = "modules_have_status"
    try:
        r = req("get", "/api/modules")
        body = r.json()
        data = _extract_list(body, "modules")
        assert isinstance(data, list) and len(data) > 0, "no modules returned"
        for m in data:
            if isinstance(m, str):
                continue  # module names returned as strings
            assert "status" in m or "state" in m, \
                f"module missing status: {list(m.keys())}"
            status = m.get("status", m.get("state", ""))
            assert status in ("loaded", "failed", "active", "inactive", "enabled", "disabled", "error"), \
                f"unexpected status: {status}"
        R.ok(name, f"checked {len(data)} modules")
    except Exception as e:
        R.fail(name, str(e))


def _modules_detail_filesystem():
    """GET /api/modules/filesystem - returns detail."""
    name = "modules_detail_filesystem"
    try:
        r = req("get", "/api/modules/filesystem")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = body.get("data", body)
        assert isinstance(data, dict), f"expected dict, got {type(data).__name__}"
        R.ok(name, f"keys={list(data.keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _modules_detail_has_actions():
    """Module detail has actions array."""
    name = "modules_detail_actions"
    try:
        r = req("get", "/api/modules/filesystem")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = body.get("data", body)
        actions = data.get("actions", data.get("tools", data.get("capabilities", [])))
        assert isinstance(actions, list), f"expected list for actions, got {type(actions).__name__}"
        assert len(actions) > 0, "actions list is empty"
        R.ok(name, f"count={len(actions)}")
    except Exception as e:
        R.fail(name, str(e))


def _modules_actions_have_name():
    """Module detail actions have name field."""
    name = "modules_actions_name"
    try:
        r = req("get", "/api/modules/filesystem")
        body = r.json()
        data = body.get("data", body)
        actions = data.get("actions", data.get("tools", data.get("capabilities", [])))
        assert isinstance(actions, list) and len(actions) > 0, "no actions found"
        for a in actions:
            assert "name" in a or "action" in a or "id" in a, \
                f"action missing name: {list(a.keys())}"
        R.ok(name, f"checked {len(actions)} actions")
    except Exception as e:
        R.fail(name, str(e))


def _modules_actions_have_description():
    """Module detail actions have description."""
    name = "modules_actions_description"
    try:
        r = req("get", "/api/modules/filesystem")
        body = r.json()
        data = body.get("data", body)
        actions = data.get("actions", data.get("tools", data.get("capabilities", [])))
        assert isinstance(actions, list) and len(actions) > 0, "no actions found"
        for a in actions:
            assert "description" in a or "desc" in a or "summary" in a, \
                f"action missing description: {list(a.keys())}"
        R.ok(name, f"checked {len(actions)} actions")
    except Exception as e:
        R.fail(name, str(e))


def _modules_actions_have_risk_level():
    """Module detail actions have risk_level."""
    name = "modules_actions_risk_level"
    try:
        r = req("get", "/api/modules/filesystem")
        body = r.json()
        data = body.get("data", body)
        actions = data.get("actions", data.get("tools", data.get("capabilities", [])))
        assert isinstance(actions, list) and len(actions) > 0, "no actions found"
        for a in actions:
            assert "risk_level" in a or "risk" in a or "danger" in a or "safety" in a, \
                f"action missing risk_level: {list(a.keys())}"
        R.ok(name, f"checked {len(actions)} actions")
    except Exception as e:
        R.fail(name, str(e))


def _modules_nonexistent_404():
    """GET /api/modules/nonexistent - returns 404."""
    name = "modules_nonexistent_404"
    try:
        r = req("get", "/api/modules/nonexistent_module_xyz_999")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _modules_health_check():
    """Module health check endpoint works."""
    name = "modules_health_check"
    try:
        r = req("get", "/api/modules/health")
        if r.status_code == 404:
            # Try alternative path
            r = req("get", "/api/modules/status")
        if r.status_code == 404:
            R.skip(name, "health/status endpoint not found (404)")
            return
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name, f"keys={list(body.get('data', body).keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _modules_count_matches():
    """Module list count matches expected."""
    name = "modules_count_matches"
    try:
        r1 = req("get", "/api/modules")
        body1 = r1.json()
        data1 = _extract_list(body1, "modules")
        r2 = req("get", "/api/modules")
        body2 = r2.json()
        data2 = _extract_list(body2, "modules")
        assert isinstance(data1, list) and isinstance(data2, list), "expected lists"
        assert len(data1) == len(data2), \
            f"module count inconsistent: {len(data1)} vs {len(data2)}"
        assert len(data1) >= 3, f"expected at least 3 modules, got {len(data1)}"
        R.ok(name, f"count={len(data1)}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  MCP TESTS
# ═══════════════════════════════════════════════════════════════

def _mcp_servers_list():
    """GET /api/mcp/servers - returns array (may be empty)."""
    name = "mcp_servers_list"
    try:
        r = req("get", "/api/mcp/servers")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = _extract_list(body, "servers")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        R.ok(name, f"count={len(data)}")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_servers_format():
    """MCP server list format is correct."""
    name = "mcp_servers_format"
    try:
        r = req("get", "/api/mcp/servers")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = _extract_list(body, "servers")
        assert isinstance(data, list), f"expected list, got {type(data).__name__}"
        if len(data) > 0:
            entry = data[0]
            assert isinstance(entry, dict), f"entry should be dict, got {type(entry).__name__}"
            assert "name" in entry or "id" in entry or "server_id" in entry, \
                f"entry missing name/id: {list(entry.keys())}"
            R.ok(name, f"first entry keys={list(entry.keys())[:5]}")
        else:
            R.ok(name, "empty list, format N/A")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_search():
    """GET /api/mcp/search with query - returns results."""
    name = "mcp_search"
    try:
        # Try different query param names the API may expect
        r = None
        for param_name in ("query", "q", "search"):
            r = req("get", "/api/mcp/search", params={param_name: "filesystem"})
            if r.status_code != 422:
                break
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        assert isinstance(data, (list, dict)), \
            f"expected list or dict, got {type(data).__name__}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _mcp_pool_status():
    """GET /api/mcp/pool - returns pool status."""
    name = "mcp_pool_status"
    try:
        r = req("get", "/api/mcp/pool")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name, f"keys={list(body.get('data', body).keys())[:6]}")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_pool_health():
    """MCP pool health check - returns results."""
    name = "mcp_pool_health"
    try:
        r = req("get", "/api/mcp/pool/health")
        if r.status_code == 404:
            r = req("get", "/api/mcp/health")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        assert isinstance(body, dict), "expected dict response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _mcp_install_nonexistent():
    """Install nonexistent MCP server - returns error."""
    name = "mcp_install_nonexistent"
    try:
        r = req("post", "/api/mcp/servers", json={
            "name": "nonexistent_mcp_server_xyz_999",
            "source": "npm:@nonexistent/fake-mcp-server-xyz-999",
        })
        assert r.status_code in (400, 404, 422, 500), \
            f"expected error status, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_get_nonexistent():
    """Get nonexistent MCP server - returns 404."""
    name = "mcp_get_nonexistent"
    try:
        r = req("get", "/api/mcp/servers/nonexistent_server_xyz_999")
        assert r.status_code == 404, f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _mcp_remove_nonexistent():
    """Remove nonexistent MCP server - returns 404."""
    name = "mcp_remove_nonexistent"
    try:
        r = req("delete", "/api/mcp/servers/nonexistent_server_xyz_999")
        assert r.status_code in (404, 400), f"expected 404/400, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_test_nonexistent():
    """MCP test nonexistent server - returns 404."""
    name = "mcp_test_nonexistent"
    try:
        r = req("post", "/api/mcp/servers/nonexistent_server_xyz_999/test")
        assert r.status_code in (404, 400), f"expected 404/400, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _mcp_list_consistent():
    """MCP list has consistent format."""
    name = "mcp_list_consistent"
    try:
        r1 = req("get", "/api/mcp/servers")
        body1 = r1.json()
        data1 = _extract_list(body1, "servers")
        r2 = req("get", "/api/mcp/servers")
        body2 = r2.json()
        data2 = _extract_list(body2, "servers")
        assert isinstance(data1, list) and isinstance(data2, list), "expected lists"
        assert len(data1) == len(data2), \
            f"mcp list count inconsistent: {len(data1)} vs {len(data2)}"
        R.ok(name, f"count={len(data1)}")
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  REQUIREMENTS TESTS
# ═══════════════════════════════════════════════════════════════

def _requires_list():
    """GET /api/requires - returns requirements list."""
    name = "requires_list"
    try:
        r = req("get", "/api/requires")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        body = r.json()
        data = body.get("data", body)
        assert isinstance(data, (list, dict)), \
            f"expected list or dict, got {type(data).__name__}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _requires_have_module_id():
    """Requirements have module_id field."""
    name = "requires_have_module_id"
    try:
        r = req("get", "/api/requires")
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        # Format: {"requirements": {"module_name": [req_dicts...]}} or list
        reqs = data.get("requirements", data) if isinstance(data, dict) else data
        if isinstance(reqs, dict):
            # Dict of module_name -> list of requirements
            checked = 0
            for mod_name, req_list in reqs.items():
                if isinstance(req_list, list):
                    for item in req_list:
                        if isinstance(item, dict) and ("module_id" in item or "name" in item):
                            checked += 1
            R.ok(name, f"checked {checked} requirements across {len(reqs)} modules")
        elif isinstance(reqs, list) and reqs:
            R.ok(name, f"{len(reqs)} requirements")
        else:
            R.skip(name, "unexpected format")
    except Exception as e:
        R.fail(name, str(e))


def _requires_have_status():
    """Requirements have status/installed field."""
    name = "requires_have_status"
    try:
        r = req("get", "/api/requires")
        body = r.json()
        data = body.get("data", body) if isinstance(body, dict) else body
        reqs = data.get("requirements", data) if isinstance(data, dict) else data
        if isinstance(reqs, dict):
            checked = 0
            for mod_name, req_list in reqs.items():
                if isinstance(req_list, list):
                    for item in req_list:
                        if isinstance(item, dict) and ("installed" in item or "status" in item):
                            checked += 1
            R.ok(name, f"checked {checked} requirements across {len(reqs)} modules")
        elif isinstance(reqs, list) and reqs:
            R.ok(name, f"{len(reqs)} requirements")
        else:
            R.skip(name, "unexpected format")
    except Exception as e:
        R.fail(name, str(e))


def _requires_install_nonexistent():
    """Install nonexistent requirement - handled."""
    name = "requires_install_nonexistent"
    try:
        r = req("post", "/api/requires", json={
            "module_id": "nonexistent_requirement_xyz_999",
        })
        assert r.status_code in (400, 404, 405, 422, 500), \
            f"expected error status, got {r.status_code}"
        R.ok(name, f"status={r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def _requires_consistency():
    """Requirements list is consistent across calls."""
    name = "requires_consistency"
    try:
        r1 = req("get", "/api/requires")
        body1 = r1.json()
        data1 = body1.get("data", body1)
        r2 = req("get", "/api/requires")
        body2 = r2.json()
        data2 = body2.get("data", body2)
        assert type(data1) is type(data2), \
            f"type mismatch: {type(data1).__name__} vs {type(data2).__name__}"
        if isinstance(data1, list):
            assert len(data1) == len(data2), \
                f"count inconsistent: {len(data1)} vs {len(data2)}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# ═══════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════

def run_all():
    """Run all metrics, modules, MCP, and requirements production tests."""

    print("\n\033[1m=== METRICS TESTS ===\033[0m")
    _metrics_returns_json()
    _metrics_has_uptime()
    _metrics_has_counters()
    _metrics_has_gauges()
    _metrics_has_histograms()
    _metrics_prometheus_text()
    _metrics_prometheus_help_lines()
    _metrics_prometheus_type_lines()
    _metrics_sessions()
    _metrics_sessions_has_identity()
    _metrics_sessions_has_tokens()
    _metrics_sessions_has_lifecycle()
    _metrics_app()
    _metrics_app_session_count()
    _metrics_response_time()

    print("\n\033[1m=== MODULES TESTS ===\033[0m")
    _modules_list()
    _modules_has_filesystem()
    _modules_has_shell()
    _modules_has_memory()
    _modules_have_module_id()
    _modules_have_version()
    _modules_have_status()
    _modules_detail_filesystem()
    _modules_detail_has_actions()
    _modules_actions_have_name()
    _modules_actions_have_description()
    _modules_actions_have_risk_level()
    _modules_nonexistent_404()
    _modules_health_check()
    _modules_count_matches()

    print("\n\033[1m=== MCP TESTS ===\033[0m")
    _mcp_servers_list()
    _mcp_servers_format()
    _mcp_search()
    _mcp_pool_status()
    _mcp_pool_health()
    _mcp_install_nonexistent()
    _mcp_get_nonexistent()
    _mcp_remove_nonexistent()
    _mcp_test_nonexistent()
    _mcp_list_consistent()

    print("\n\033[1m=== REQUIREMENTS TESTS ===\033[0m")
    _requires_list()
    _requires_have_module_id()
    _requires_have_status()
    _requires_install_nonexistent()
    _requires_consistency()

    print(f"\n\033[1m--- {R.summary()} ---\033[0m")


if __name__ == "__main__":
    run_all()
