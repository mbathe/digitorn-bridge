"""Functional test infrastructure — starts daemon, deploys apps, runs HTTP tests.

Usage:
    pytest tests/functional/ -v --timeout=300

Requires:
    - The daemon must NOT be already running on the test port
    - httpx, pytest, pytest-asyncio installed
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 18741  # High port unlikely to conflict
BASE_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
APPS_DIR = Path(__file__).parent / "apps"
PYTHON = sys.executable

# Test user credentials
TEST_USER = "testadmin"
TEST_PASSWORD = "testpassword123!"  # 12+ chars for registration
TEST_EMAIL = "test@digitorn.local"


# ──────────────────────────────────────────────────────────────
# Daemon process management
# ──────────────────────────────────────────────────────────────

def _wait_for_daemon(url: str, timeout: float = 45) -> bool:
    """Poll /healthz until daemon is ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/healthz", timeout=3)
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError):
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="session")
def daemon_process():
    """Start the daemon for the entire test session."""
    env = os.environ.copy()
    env["DIGITORN_SERVER__AUTH_ENABLED"] = "false"
    env["DIGITORN_SERVER__PORT"] = str(DAEMON_PORT)
    env["DIGITORN_SERVER__HOST"] = DAEMON_HOST
    env["DIGITORN_SERVER__RATE_LIMIT_RPM"] = "10000"  # High limit for tests
    env["DIGITORN_LOGGING__LEVEL"] = "warning"

    log_file = Path(__file__).parent / "daemon.log"
    log_fh = open(log_file, "w")

    proc = subprocess.Popen(
        [PYTHON, "-m", "digitorn.core.server", "start",
         "--host", DAEMON_HOST,
         "--port", str(DAEMON_PORT),
         "--no-sandbox",
         "--log-level", "warning"],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    if not _wait_for_daemon(BASE_URL, timeout=60):
        proc.kill()
        proc.wait(timeout=5)
        log_fh.close()
        log_content = log_file.read_text(errors="replace")[-3000:]
        pytest.fail(
            f"Daemon failed to start within 60s.\n"
            f"OUTPUT: {log_content}"
        )

    yield proc

    # Shutdown
    if sys.platform == "win32":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    log_fh.close()


@pytest_asyncio.fixture(scope="session")
async def client(daemon_process) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client pre-configured for the daemon."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as c:
        yield c


@pytest_asyncio.fixture(scope="session")
async def auth_token(client: httpx.AsyncClient) -> str | None:
    """Register + login test user, return access token (or None if auth disabled)."""
    # Try health first — if auth is disabled, we don't need a token
    r = await client.get("/healthz")
    if r.status_code == 200:
        # Try an authed endpoint without token
        r2 = await client.get("/api/apps")
        if r2.status_code == 200:
            return None  # Auth disabled

    # Auth enabled — login directly (admin user already exists)
    r = await client.post("/auth/login", json={
        "username": TEST_USER,
        "password": TEST_PASSWORD,
    })
    if r.status_code == 200:
        return r.json()["access_token"]

    # Fallback: try register then login (first run, admin not created yet)
    await client.post("/auth/register", json={
        "username": TEST_USER,
        "password": TEST_PASSWORD,
        "email": TEST_EMAIL,
    })
    r = await client.post("/auth/login", json={
        "username": TEST_USER,
        "password": TEST_PASSWORD,
    })
    if r.status_code == 200:
        return r.json()["access_token"]
    return None


def _headers(token: str | None) -> dict[str, str]:
    """Build auth headers."""
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


@pytest_asyncio.fixture(scope="session")
async def headers(auth_token) -> dict[str, str]:
    """Auth headers dict for requests."""
    return _headers(auth_token)


# ──────────────────────────────────────────────────────────────
# App deployment helpers
# ──────────────────────────────────────────────────────────────

async def deploy_app(client: httpx.AsyncClient, yaml_name: str, hdrs: dict) -> dict:
    """Deploy a YAML app and return the response data.

    Retries up to 3 times if an attempt fails (e.g. concurrent undeploy
    from a previous test's teardown may still hold the deploy lock).
    """
    import asyncio
    yaml_path = str((APPS_DIR / yaml_name).resolve())
    data = {}
    last_error = ""
    for attempt in range(3):
        r = await client.post("/api/apps/deploy", json={
            "yaml_path": yaml_path,
            "force": True,
        }, headers=hdrs, timeout=30)
        data = r.json()
        if data.get("success"):
            return data
        last_error = data.get("error", str(data))
        await asyncio.sleep(0.5 * (attempt + 1))
    # Return the data as-is — test will see success=false
    # Log the error so failures are diagnosable
    import logging
    logging.getLogger("functional").warning(
        "deploy_app(%s) failed after 3 attempts: %s", yaml_name, last_error
    )
    return data


async def undeploy_app(client: httpx.AsyncClient, app_id: str, hdrs: dict):
    """Undeploy an app and wait for cleanup."""
    import asyncio
    await client.delete(f"/api/apps/{app_id}", headers=hdrs)
    await asyncio.sleep(0.3)  # Allow daemon to finish cleanup


# ──────────────────────────────────────────────────────────────
# Chat helpers — all tests use /messages + /events (the ONLY chat route)
# ──────────────────────────────────────────────────────────────

async def send_message(
    client: httpx.AsyncClient,
    app_id: str,
    session_id: str,
    message: str,
    hdrs: dict,
    workspace: str = "",
) -> dict:
    """Send a message via POST /sessions/{sid}/messages and return the response."""
    r = await client.post(
        f"/api/apps/{app_id}/sessions/{session_id}/messages",
        json={"message": message, "workspace": workspace},
        headers=hdrs,
        timeout=10,
    )
    return r.json()


async def send_and_wait(
    client: httpx.AsyncClient,
    app_id: str,
    session_id: str,
    message: str,
    hdrs: dict,
    workspace: str = "",
    timeout: float = 60.0,
    wait_for_result: bool = True,
) -> dict:
    """Send a message and wait for the result event via SSE.

    Returns the full result event data including content, tool_calls, usage, etc.
    This replaces the old /chat (sync) and /chat/stream (SSE) routes.
    """
    import asyncio
    import json as _json

    # Send message (returns 202 immediately)
    r = await client.post(
        f"/api/apps/{app_id}/sessions/{session_id}/messages",
        json={"message": message, "workspace": workspace},
        headers=hdrs,
        timeout=10,
    )
    send_data = r.json()
    if not send_data.get("success"):
        return send_data

    if not wait_for_result:
        return send_data

    # Poll the session until a result appears via events
    result_data = {}
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # Check session — is_active tells us if the turn is still running
        sr = await client.get(
            f"/api/apps/{app_id}/sessions/{session_id}",
            headers=hdrs, timeout=10,
        )
        sd = sr.json().get("data", {})
        if not sd.get("is_active", True):
            # Turn finished — get history to find the result
            hr = await client.get(
                f"/api/apps/{app_id}/sessions/{session_id}/history",
                headers=hdrs, timeout=10,
            )
            hd = hr.json().get("data", {})
            messages = hd.get("messages", [])
            events = hd.get("events", [])

            # Find the last assistant message
            content = ""
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    content = m.get("content", "")
                    break

            # Find result event
            for e in reversed(events):
                if e.get("type") == "turn_end":
                    result_data = e.get("data", {})
                    break

            # Build a result similar to what /chat returned
            return {
                "success": True,
                "data": {
                    "content": content or result_data.get("content", ""),
                    "session_id": session_id,
                    "tool_calls_count": result_data.get("tool_calls_count", 0),
                    "turns_used": result_data.get("turns_used", 0),
                    "truncated": result_data.get("truncated", False),
                    "error": result_data.get("error"),
                    "context": sd.get("context"),
                    "tokens": sd.get("tokens"),
                },
            }
        await asyncio.sleep(1.0)

    return {"success": False, "error": f"Timeout waiting for result after {timeout}s"}


async def collect_sse_events(
    client: httpx.AsyncClient,
    app_id: str,
    session_id: str,
    message: str,
    hdrs: dict,
    workspace: str = "",
    timeout: float = 60.0,
) -> list[dict]:
    """Send a message and collect ALL SSE events until result/error.

    Returns a list of parsed event dicts: [{"type": "...", "data": {...}}, ...]
    """
    import asyncio
    import json as _json

    events: list[dict] = []

    # Connect to SSE first
    async def _read_events():
        async with client.stream(
            "GET",
            f"/api/apps/{app_id}/sessions/{session_id}/events",
            headers=hdrs,
            timeout=httpx.Timeout(timeout + 5, connect=10),
        ) as stream:
            buffer = ""
            current_event = ""
            async for line in stream.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: "):
                    try:
                        data = _json.loads(line[6:])
                        events.append({"type": current_event, "data": data})
                        if current_event in ("result", "error"):
                            return  # Done
                    except _json.JSONDecodeError:
                        pass
                elif line == "":
                    current_event = ""

    # Start SSE reader
    reader_task = asyncio.create_task(_read_events())

    # Small delay to ensure SSE is connected
    await asyncio.sleep(0.3)

    # Send message
    await client.post(
        f"/api/apps/{app_id}/sessions/{session_id}/messages",
        json={"message": message, "workspace": workspace},
        headers=hdrs,
        timeout=10,
    )

    # Wait for reader to finish or timeout
    try:
        await asyncio.wait_for(reader_task, timeout=timeout)
    except asyncio.TimeoutError:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass

    return events


@pytest_asyncio.fixture
async def deployed_minimal(client, headers):
    """Deploy the minimal test app, yield app_id, then undeploy."""
    data = await deploy_app(client, "minimal.yaml", headers)
    app_id = "test-minimal"
    yield app_id
    await undeploy_app(client, app_id, headers)
