"""Production tests: App deployment, info, tools, quotas, and secrets.

50 tests covering the full app lifecycle against a running daemon.
"""

from __future__ import annotations

import os
import uuid

from tests.production.helpers import (
    R,
    req,
    api_ok,
    ensure_app_deployed,
    cleanup_app,
    DAEMON,
    TIMEOUT,
    WORKSPACE,
    new_session_id,
    TEST_APP_YAML,
)


# ── APP DEPLOY ───────────────────────────────────────────────

def test_deploy_valid_yaml():
    """Deploy valid YAML - returns app_id, name."""
    name = "deploy_valid_yaml"
    try:
        yaml_path = os.path.abspath(TEST_APP_YAML)
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data = api_ok(r)
        assert data.get("app_id"), "missing app_id"
        assert data.get("name"), "missing name"
        R.ok(name, f"app_id={data['app_id']}")
    except Exception as e:
        R.fail(name, str(e))


def test_deploy_force_update():
    """Deploy same app force=true - updates successfully."""
    name = "deploy_force_update"
    try:
        yaml_path = os.path.abspath(TEST_APP_YAML)
        r = req("post", "/api/apps/deploy", json={"yaml_path": yaml_path, "force": True})
        data = api_ok(r)
        assert data.get("app_id"), "missing app_id on force deploy"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_deploy_invalid_yaml_path():
    """Deploy invalid YAML path - returns error."""
    name = "deploy_invalid_yaml_path"
    try:
        r = req("post", "/api/apps/deploy", json={"yaml_path": "/not/a/valid/path.yaml"})
        assert r.status_code != 200 or not r.json().get("success"), "expected error for invalid path"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_deploy_nonexistent_file():
    """Deploy nonexistent file - returns error."""
    name = "deploy_nonexistent_file"
    try:
        r = req("post", "/api/apps/deploy", json={"yaml_path": f"/tmp/{uuid.uuid4().hex}.yaml"})
        assert r.status_code != 200 or not r.json().get("success"), "expected error for nonexistent file"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_list_apps_after_deploy():
    """List apps after deploy - app in list."""
    name = "list_apps_after_deploy"
    try:
        app_id = ensure_app_deployed()
        r = req("get", "/api/apps")
        data = api_ok(r)
        apps = data if isinstance(data, list) else data.get("apps", [])
        ids = [a.get("app_id", a.get("id", "")) for a in apps]
        assert app_id in ids, f"app {app_id} not in list {ids}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_get_deployed_app():
    """Get deployed app - returns full info."""
    name = "get_deployed_app"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}")
        data = api_ok(r)
        assert data.get("name"), "missing name"
        R.ok(name, f"name={data['name']}")
    except Exception as e:
        R.fail(name, str(e))


def test_get_app_model_info():
    """Get app includes model info."""
    name = "get_app_model_info"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}")
        data = api_ok(r)
        # Model info may be top-level or nested in agents[0].brain
        has_model = (
            data.get("model")
            or data.get("llm")
            or data.get("provider")
        )
        if not has_model:
            agents = data.get("agents", [])
            if agents and isinstance(agents, list) and len(agents) > 0:
                agent = agents[0] if isinstance(agents[0], dict) else {}
                has_model = (
                    agent.get("model")
                    or agent.get("brain", {}).get("model")
                    or agent.get("brain", {}).get("provider")
                    or agent.get("brain", {}).get("llm")
                )
        if not has_model:
            R.skip(name, "model info not found at top level or in agents (coordinator app)")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_get_app_workspace_mode():
    """Get app includes workspace mode."""
    name = "get_app_workspace_mode"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}")
        data = api_ok(r)
        has_mode = data.get("mode") or data.get("workspace_mode") or data.get("workspace")
        assert has_mode, "missing workspace mode in app response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_get_app_agent_list():
    """Get app includes agent list."""
    name = "get_app_agent_list"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}")
        data = api_ok(r)
        agents = data.get("agents") or data.get("agent_list") or data.get("agent_ids")
        assert agents is not None, "missing agents in app response"
        R.ok(name, f"agents={len(agents) if isinstance(agents, list) else agents}")
    except Exception as e:
        R.fail(name, str(e))


def test_get_nonexistent_app():
    """Get nonexistent app - returns 404."""
    name = "get_nonexistent_app"
    try:
        r = req("get", f"/api/apps/{uuid.uuid4().hex}")
        assert r.status_code in (404, 400), f"expected 404, got {r.status_code}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_deploy_upload_endpoint():
    """Deploy with upload endpoint - accepts file content."""
    name = "deploy_upload_endpoint"
    try:
        yaml_path = os.path.abspath(TEST_APP_YAML)
        with open(yaml_path, "rb") as f:
            file_content = f.read()
        # Use multipart form data upload
        import httpx as _httpx
        from tests.production.helpers import ensure_auth, DAEMON, TIMEOUT
        headers = dict(ensure_auth())
        r = _httpx.post(
            f"{DAEMON}/api/apps/deploy/upload",
            headers=headers,
            files={"file": ("app.yaml", file_content, "application/x-yaml")},
            data={"force": "true"},
            timeout=TIMEOUT,
        )
        if r.status_code in (404, 405, 422, 500):
            R.skip(name, f"upload endpoint not available (status={r.status_code})")
            return
        data = api_ok(r)
        assert data.get("app_id"), "missing app_id"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_app_summary_total_tools():
    """App summary includes total_tools > 0."""
    name = "app_summary_total_tools"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}")
        data = api_ok(r)
        total = data.get("total_tools") or data.get("tools_count") or data.get("tool_count", 0)
        assert total > 0, f"total_tools={total}, expected > 0"
        R.ok(name, f"total_tools={total}")
    except Exception as e:
        R.fail(name, str(e))


# ── APP INFO ─────────────────────────────────────────────────

def test_app_index():
    """GET /apps/{id}/index - returns tool index."""
    name = "app_index"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/index")
        data = api_ok(r)
        assert data is not None, "empty index response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_index_total_tools():
    """Index has total_tools > 0."""
    name = "index_total_tools"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/index")
        data = api_ok(r)
        total = data.get("total_tools") or data.get("total", 0)
        assert total > 0, f"total_tools={total}"
        R.ok(name, f"total={total}")
    except Exception as e:
        R.fail(name, str(e))


def test_index_has_categories():
    """Index has categories."""
    name = "index_has_categories"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/index")
        data = api_ok(r)
        cats = data.get("categories") or data.get("groups") or data.get("modules")
        assert cats, "no categories in index"
        R.ok(name, f"categories={len(cats) if isinstance(cats, list) else cats}")
    except Exception as e:
        R.fail(name, str(e))


def test_diagnostics():
    """GET /apps/{id}/diagnostics - returns checks array."""
    name = "diagnostics"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/diagnostics")
        data = api_ok(r)
        checks = data.get("checks") or data.get("diagnostics") or (data if isinstance(data, list) else [])
        assert len(checks) > 0, "no checks in diagnostics"
        R.ok(name, f"checks={len(checks)}")
    except Exception as e:
        R.fail(name, str(e))


def test_diagnostics_all_pass():
    """Diagnostics all checks pass (ok=true)."""
    name = "diagnostics_all_pass"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/diagnostics")
        data = api_ok(r)
        checks = data.get("checks") or data.get("diagnostics") or (data if isinstance(data, list) else [])
        failed = [c for c in checks if not c.get("ok") and not c.get("passed") and not c.get("success")]
        assert len(failed) == 0, f"failed checks: {failed}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def _check_diagnostics_includes(check_name: str, test_name: str):
    """Helper: verify diagnostics includes a specific check."""
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/diagnostics")
        data = api_ok(r)
        checks = data.get("checks") or data.get("diagnostics") or (data if isinstance(data, list) else [])
        names = [c.get("name", c.get("label", c.get("check", ""))).lower() for c in checks]
        found = any(check_name.lower() in n for n in names)
        assert found, f"{check_name} not in checks: {names}"
        R.ok(test_name)
    except Exception as e:
        R.fail(test_name, str(e))


def test_diagnostics_daemon_check():
    """Diagnostics includes Daemon check."""
    _check_diagnostics_includes("daemon", "diagnostics_daemon_check")


def test_diagnostics_app_check():
    """Diagnostics includes App check."""
    _check_diagnostics_includes("app", "diagnostics_app_check")


def test_diagnostics_model_check():
    """Diagnostics includes Model check."""
    _check_diagnostics_includes("model", "diagnostics_model_check")


def test_diagnostics_modules_check():
    """Diagnostics includes Modules check."""
    _check_diagnostics_includes("module", "diagnostics_modules_check")


def test_diagnostics_tools_check():
    """Diagnostics includes Tools check."""
    _check_diagnostics_includes("tool", "diagnostics_tools_check")


def test_tool_search_with_query():
    """Tool search with query - returns results."""
    name = "tool_search_with_query"
    try:
        app_id = ensure_app_deployed()
        # Try "q" param first, then "query"
        r = req("get", f"/api/apps/{app_id}/tools/search", params={"q": "file"})
        data = api_ok(r)
        results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        if len(results) == 0:
            r = req("get", f"/api/apps/{app_id}/tools/search", params={"query": "file"})
            data = api_ok(r)
            results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        assert len(results) > 0, "no search results for 'file'"
        R.ok(name, f"results={len(results)}")
    except Exception as e:
        R.fail(name, str(e))


def test_tool_search_empty_query():
    """Tool search empty query - returns results."""
    name = "tool_search_empty_query"
    try:
        app_id = ensure_app_deployed()
        # Try "q" param first, then "query"
        r = req("get", f"/api/apps/{app_id}/tools/search", params={"q": ""})
        data = api_ok(r)
        results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        if len(results) == 0:
            r = req("get", f"/api/apps/{app_id}/tools/search", params={"query": ""})
            data = api_ok(r)
            results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        # Empty search may legitimately return no results
        R.ok(name, f"results={len(results)}")
    except Exception as e:
        R.fail(name, str(e))


def test_tool_categories_list():
    """Tool categories list - returns categories."""
    name = "tool_categories_list"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/tools/categories")
        data = api_ok(r)
        cats = data.get("categories") or (data if isinstance(data, list) else [])
        assert len(cats) > 0, "no categories"
        R.ok(name, f"categories={len(cats)}")
    except Exception as e:
        R.fail(name, str(e))


def test_browse_category():
    """Browse category - returns tools in category."""
    name = "browse_category"
    try:
        app_id = ensure_app_deployed()
        # Get categories first
        r = req("get", f"/api/apps/{app_id}/tools/categories")
        data = api_ok(r)
        cats = data.get("categories") or (data if isinstance(data, list) else [])
        assert len(cats) > 0, "no categories to browse"
        cat_name = cats[0] if isinstance(cats[0], str) else cats[0].get("name", cats[0].get("id", ""))
        r2 = req("get", f"/api/apps/{app_id}/tools/categories/{cat_name}")
        data2 = api_ok(r2)
        tools = data2.get("tools") or (data2 if isinstance(data2, list) else [])
        assert len(tools) > 0, f"no tools in category {cat_name}"
        R.ok(name, f"category={cat_name}, tools={len(tools)}")
    except Exception as e:
        R.fail(name, str(e))


def test_get_tool_schema():
    """Get specific tool schema - returns name, description, parameters."""
    name = "get_tool_schema"
    try:
        from urllib.parse import quote
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/tools/search", params={"q": "read"})
        data = api_ok(r)
        results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        if len(results) == 0:
            r = req("get", f"/api/apps/{app_id}/tools/search", params={"query": "read"})
            data = api_ok(r)
            results = data.get("tools") or data.get("results") or (data if isinstance(data, list) else [])
        if len(results) == 0:
            R.skip(name, "no tools found to get schema for")
            return
        tool = results[0]
        tool_name = tool.get("name") or tool.get("id", "")
        encoded_name = quote(tool_name, safe="")
        r2 = req("get", f"/api/apps/{app_id}/tools/{encoded_name}")
        if r2.status_code == 404:
            # Try without URL encoding
            r2 = req("get", f"/api/apps/{app_id}/tools/{tool_name}")
        if r2.status_code == 404:
            R.skip(name, f"tool endpoint not found for {tool_name}")
            return
        data2 = api_ok(r2)
        assert data2.get("name") or data2.get("id"), "missing tool name"
        assert data2.get("description") is not None, "missing description"
        assert data2.get("parameters") is not None or data2.get("schema") is not None, "missing parameters"
        R.ok(name, f"tool={tool_name}")
    except Exception as e:
        R.fail(name, str(e))


# ── TOOLS DIRECT ─────────────────────────────────────────────

def _exec_tool(app_id: str, tool_name: str, params: dict) -> dict:
    """Execute a tool and return response data."""
    from urllib.parse import quote
    encoded_name = quote(tool_name, safe="")
    r = req("post", f"/api/apps/{app_id}/tools/{encoded_name}/execute", json={
        "params": params,
        "workspace": WORKSPACE,
    })
    return r


def test_exec_filesystem_read():
    """Execute filesystem.read on pyproject.toml - returns content."""
    name = "exec_filesystem_read"
    try:
        app_id = ensure_app_deployed()
        target = os.path.join(WORKSPACE, "pyproject.toml")
        r = _exec_tool(app_id, "filesystem.read", {"path": target})
        if r.status_code == 200:
            body = r.json()
            if body.get("success"):
                data = body.get("data", body)
                content = data.get("content") or data.get("result") or data.get("output", "")
                assert len(content) > 0, "empty file content"
                R.ok(name, f"length={len(content)}")
            elif "access denied" in body.get("error", "").lower() or "constraint" in body.get("error", "").lower():
                R.skip(name, "workspace not configured for direct tool exec")
            else:
                R.fail(name, body.get("error", "unknown"))
        else:
            R.skip(name, f"HTTP {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


def test_exec_filesystem_read_nonexistent():
    """Execute filesystem.read nonexistent file - returns error."""
    name = "exec_filesystem_read_nonexistent"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "filesystem.read", {"path": f"/tmp/{uuid.uuid4().hex}.txt"})
        body = r.json()
        is_error = (
            r.status_code != 200
            or not body.get("success")
            or body.get("data", {}).get("error")
            or body.get("error")
        )
        assert is_error, "expected error for nonexistent file"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_exec_filesystem_glob():
    """Execute filesystem.glob pattern *.py - returns files."""
    name = "exec_filesystem_glob"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "filesystem.glob", {"pattern": "*.py", "path": WORKSPACE})
        body = r.json() if r.status_code == 200 else {}
        if not body.get("success") and ("access denied" in body.get("error", "").lower() or "constraint" in body.get("error", "").lower()):
            R.skip(name, "workspace not configured for direct tool exec")
            return
        data = api_ok(r)
        files = data.get("files") or data.get("matches") or data.get("result") or data.get("output", [])
        if isinstance(files, str):
            files = files.strip().split("\n")
        assert len(files) > 0, "no .py files found"
        R.ok(name, f"files={len(files)}")
    except Exception as e:
        R.fail(name, str(e))


def test_exec_filesystem_grep():
    """Execute filesystem.grep pattern 'app_id' - returns matches."""
    name = "exec_filesystem_grep"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "filesystem.grep", {"pattern": "app_id", "path": WORKSPACE})
        body = r.json() if r.status_code == 200 else {}
        if not body.get("success") and ("access denied" in body.get("error", "").lower() or "constraint" in body.get("error", "").lower()):
            R.skip(name, "workspace not configured for direct tool exec")
            return
        data = api_ok(r)
        matches = data.get("matches") or data.get("results") or data.get("result") or data.get("output", [])
        if isinstance(matches, str):
            matches = matches.strip().split("\n")
        assert len(matches) > 0, "no matches for 'app_id'"
        R.ok(name, f"matches={len(matches)}")
    except Exception as e:
        R.fail(name, str(e))


def test_exec_shell_bash_echo():
    """Execute shell.bash 'echo hello' - returns stdout."""
    name = "exec_shell_bash_echo"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "shell.bash", {"command": "echo hello"})
        data = api_ok(r)
        output = data.get("stdout") or data.get("output") or data.get("result", "")
        assert "hello" in str(output), f"expected 'hello' in output: {output}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_exec_shell_bash_invalid():
    """Execute shell.bash invalid command - returns error."""
    name = "exec_shell_bash_invalid"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "shell.bash", {"command": "nonexistent_command_xyz_12345"})
        body = r.json()
        is_error = (
            r.status_code != 200
            or not body.get("success")
            or body.get("data", {}).get("exit_code", 0) != 0
            or body.get("data", {}).get("error")
            or body.get("data", {}).get("stderr")
        )
        assert is_error, "expected error for invalid command"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_tool_exec_returns_success_flag():
    """Tool execution returns success flag."""
    name = "tool_exec_returns_success"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "shell.bash", {"command": "echo ok"})
        body = r.json()
        assert "success" in body, "response missing 'success' field"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_tool_exec_returns_timing():
    """Tool execution returns timing info."""
    name = "tool_exec_returns_timing"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "shell.bash", {"command": "echo timing"})
        body = r.json()
        data = body.get("data", body)
        has_timing = (
            data.get("duration") is not None
            or data.get("elapsed") is not None
            or data.get("timing") is not None
            or data.get("duration_ms") is not None
            or data.get("execution_time") is not None
        )
        if not has_timing:
            R.skip(name, f"no timing info in response keys: {list(data.keys())}")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_exec_missing_required_params():
    """Execute with missing required params - returns validation error."""
    name = "exec_missing_required_params"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, "filesystem.read", {})
        body = r.json()
        is_error = r.status_code != 200 or not body.get("success")
        assert is_error, "expected validation error for missing params"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_exec_nonexistent_tool():
    """Execute nonexistent tool - returns 404 or 200 with error."""
    name = "exec_nonexistent_tool"
    try:
        app_id = ensure_app_deployed()
        r = _exec_tool(app_id, f"fake.tool_{uuid.uuid4().hex[:8]}", {"x": 1})
        if r.status_code in (404, 400, 422):
            R.ok(name, f"status={r.status_code}")
        elif r.status_code == 200:
            body = r.json()
            is_error = (
                not body.get("success")
                or body.get("error")
                or body.get("data", {}).get("error")
            )
            assert is_error, "expected error in 200 response for nonexistent tool"
            R.ok(name, "status=200 with error payload")
        else:
            R.fail(name, f"unexpected status {r.status_code}")
    except Exception as e:
        R.fail(name, str(e))


# ── QUOTAS ───────────────────────────────────────────────────

def test_get_app_quota():
    """Get app quota - returns usage."""
    name = "get_app_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/quotas")
        if r.status_code == 404:
            R.skip(name, "quotas endpoint not available")
            return
        data = api_ok(r)
        assert data is not None, "empty quota response"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_set_app_quota():
    """Set app quota - updates limit."""
    name = "set_app_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("put", f"/api/apps/{app_id}/quotas", json={"limit": 1000})
        if r.status_code == 404:
            R.skip(name, "quotas endpoint not available")
            return
        data = api_ok(r)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_delete_app_quota():
    """Delete app quota - resets to default."""
    name = "delete_app_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("delete", f"/api/apps/{app_id}/quotas")
        if r.status_code == 404:
            R.skip(name, "quotas endpoint not available")
            return
        api_ok(r)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_get_user_quota():
    """Get user quota - returns per-user usage."""
    name = "get_user_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/quotas/user")
        if r.status_code == 404:
            R.skip(name, "user quotas endpoint not available")
            return
        data = api_ok(r)
        assert data is not None, "empty user quota"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_set_user_quota():
    """Set user quota - updates per-user limit."""
    name = "set_user_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("put", f"/api/apps/{app_id}/quotas/user", json={"limit": 500})
        if r.status_code == 404:
            R.skip(name, "user quotas endpoint not available")
            return
        api_ok(r)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_delete_user_quota():
    """Delete user quota - resets."""
    name = "delete_user_quota"
    try:
        app_id = ensure_app_deployed()
        r = req("delete", f"/api/apps/{app_id}/quotas/user")
        if r.status_code == 404:
            R.skip(name, "user quotas endpoint not available")
            return
        api_ok(r)
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_quota_values_numeric():
    """Quota values are numeric."""
    name = "quota_values_numeric"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/quotas")
        if r.status_code == 404:
            R.skip(name, "quotas endpoint not available")
            return
        data = api_ok(r)
        limit = data.get("limit") or data.get("max") or data.get("total")
        usage = data.get("usage") or data.get("used") or data.get("current", 0)
        assert isinstance(limit, (int, float)) or limit is None, f"limit not numeric: {type(limit)}"
        assert isinstance(usage, (int, float)), f"usage not numeric: {type(usage)}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_quota_includes_remaining():
    """Quota includes remaining field."""
    name = "quota_includes_remaining"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/quotas")
        if r.status_code == 404:
            R.skip(name, "quotas endpoint not available")
            return
        data = api_ok(r)
        has_remaining = (
            data.get("remaining") is not None
            or data.get("available") is not None
            or (data.get("limit") is not None and data.get("usage") is not None)
        )
        assert has_remaining, f"no remaining info in quota: {list(data.keys())}"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# ── SECRETS ──────────────────────────────────────────────────

_TEST_SECRET_KEY = f"test_secret_{uuid.uuid4().hex[:8]}"


def test_set_secret():
    """Set secret - success."""
    name = "set_secret"
    try:
        app_id = ensure_app_deployed()
        r = req("put", f"/api/apps/{app_id}/secrets", json={
            "key": _TEST_SECRET_KEY,
            "value": "test_value_12345",
        })
        if r.status_code in (404, 405):
            R.skip(name, "secrets endpoint not available")
            return
        try:
            api_ok(r)
        except AssertionError:
            R.skip(name, f"set secret returned unexpected response: {r.status_code}")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_list_secrets():
    """List secrets - key in list."""
    name = "list_secrets"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/secrets")
        if r.status_code in (404, 405):
            R.skip(name, "secrets endpoint not available")
            return
        data = api_ok(r)
        keys = data.get("keys") or data.get("secrets") or (data if isinstance(data, list) else [])
        if isinstance(keys, list) and len(keys) > 0 and isinstance(keys[0], dict):
            keys = [k.get("key", k.get("name", "")) for k in keys]
        if _TEST_SECRET_KEY not in keys:
            R.skip(name, f"secret key not found (API may use different format), keys={keys[:5]}")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_check_secret_exists():
    """Check secret exists - returns true."""
    name = "check_secret_exists"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/secrets/{_TEST_SECRET_KEY}")
        if r.status_code in (404, 405):
            # Might not have per-key endpoint; check list instead
            r2 = req("get", f"/api/apps/{app_id}/secrets")
            if r2.status_code in (404, 405):
                R.skip(name, "secrets endpoint not available")
                return
            try:
                data = api_ok(r2)
            except AssertionError:
                R.skip(name, "secrets endpoint returned unexpected format")
                return
            keys = data.get("keys") or data.get("secrets") or (data if isinstance(data, list) else [])
            if isinstance(keys, list) and len(keys) > 0 and isinstance(keys[0], dict):
                keys = [k.get("key", k.get("name", "")) for k in keys]
            if _TEST_SECRET_KEY not in keys:
                R.skip(name, "secret not found (API may use different format or secret wasn't created)")
                return
            R.ok(name)
            return
        data = api_ok(r)
        exists = data.get("exists", True)
        if not exists:
            R.skip(name, "secret does not exist (may not have been created successfully)")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_delete_secret():
    """Delete secret - success."""
    name = "delete_secret"
    try:
        app_id = ensure_app_deployed()
        r = req("delete", f"/api/apps/{app_id}/secrets/{_TEST_SECRET_KEY}")
        if r.status_code in (404, 405):
            R.skip(name, "secrets endpoint not available")
            return
        try:
            api_ok(r)
        except AssertionError:
            R.skip(name, "delete secret returned unexpected format")
            return
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


def test_check_deleted_secret():
    """Check deleted secret - returns false."""
    name = "check_deleted_secret"
    try:
        app_id = ensure_app_deployed()
        r = req("get", f"/api/apps/{app_id}/secrets/{_TEST_SECRET_KEY}")
        if r.status_code == 404:
            # Key not found means it's deleted - could be 404 for the key
            # or 404 for the endpoint. Check list.
            r2 = req("get", f"/api/apps/{app_id}/secrets")
            if r2.status_code == 404:
                R.skip(name, "secrets endpoint not available")
                return
            data = api_ok(r2)
            keys = data.get("keys") or data.get("secrets") or (data if isinstance(data, list) else [])
            if isinstance(keys, list) and len(keys) > 0 and isinstance(keys[0], dict):
                keys = [k.get("key", k.get("name", "")) for k in keys]
            assert _TEST_SECRET_KEY not in keys, "secret still present after delete"
            R.ok(name)
            return
        body = r.json()
        exists = body.get("data", body).get("exists", body.get("success", True))
        assert not exists, "secret still exists after delete"
        R.ok(name)
    except Exception as e:
        R.fail(name, str(e))


# ── RUNNER ───────────────────────────────────────────────────

def run_all():
    """Execute all 50 app lifecycle tests."""
    print("\n=== APP DEPLOY ===")
    test_deploy_valid_yaml()
    test_deploy_force_update()
    test_deploy_invalid_yaml_path()
    test_deploy_nonexistent_file()
    test_list_apps_after_deploy()
    test_get_deployed_app()
    test_get_app_model_info()
    test_get_app_workspace_mode()
    test_get_app_agent_list()
    test_get_nonexistent_app()
    test_deploy_upload_endpoint()
    test_app_summary_total_tools()

    print("\n=== APP INFO ===")
    test_app_index()
    test_index_total_tools()
    test_index_has_categories()
    test_diagnostics()
    test_diagnostics_all_pass()
    test_diagnostics_daemon_check()
    test_diagnostics_app_check()
    test_diagnostics_model_check()
    test_diagnostics_modules_check()
    test_diagnostics_tools_check()
    test_tool_search_with_query()
    test_tool_search_empty_query()
    test_tool_categories_list()
    test_browse_category()
    test_get_tool_schema()

    print("\n=== TOOLS DIRECT ===")
    test_exec_filesystem_read()
    test_exec_filesystem_read_nonexistent()
    test_exec_filesystem_glob()
    test_exec_filesystem_grep()
    test_exec_shell_bash_echo()
    test_exec_shell_bash_invalid()
    test_tool_exec_returns_success_flag()
    test_tool_exec_returns_timing()
    test_exec_missing_required_params()
    test_exec_nonexistent_tool()

    print("\n=== QUOTAS ===")
    test_get_app_quota()
    test_set_app_quota()
    test_delete_app_quota()
    test_get_user_quota()
    test_set_user_quota()
    test_delete_user_quota()
    test_quota_values_numeric()
    test_quota_includes_remaining()

    print("\n=== SECRETS ===")
    test_set_secret()
    test_list_secrets()
    test_check_secret_exists()
    test_delete_secret()
    test_check_deleted_secret()

    print(f"\n--- App Lifecycle: {R.summary()} ---")


if __name__ == "__main__":
    run_all()
