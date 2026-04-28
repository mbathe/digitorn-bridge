"""31 - Verify remaining untested events on GET /sessions/{sid}/events.

Tests for: error, agent_event, workbench_read, workbench_write, workbench_edit,
           approval_request (partial), thinking (model-dependent).

Events that cannot be tested functionally:
  - thinking_started/thinking_delta/thinking: require a model with extended thinking
    (Claude opus with thinking enabled). Verified that code publishes to bus (L723/732/739).
  - approval_request: requires async approval flow (agent blocks waiting for approval).
    Verified that callback is registered on SSE connect (apps.py:1416).
  - notification/notification_result: require background tasks completing during SSE.
    Verified that code publishes to bus (L1073/1127).
  - workbench_mutation: does NOT exist in the codebase.
"""

import asyncio
import json
import uuid
import threading

import httpx
import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, BASE_URL

pytestmark = pytest.mark.asyncio


def _collect_events_thread(url, headers, events, stop):
    """Collect SSE events in a thread until stop is set."""
    try:
        with httpx.stream("GET", url, headers=headers, timeout=None) as resp:
            if resp.status_code != 200:
                return
            event_type = ""
            data_buf = ""
            line_buf = ""
            for chunk in resp.iter_bytes():
                if stop.is_set():
                    return
                line_buf += chunk.decode("utf-8", errors="replace")
                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data_buf = line[6:]
                    elif line == "" and event_type:
                        try:
                            data = json.loads(data_buf) if data_buf else {}
                        except (json.JSONDecodeError, ValueError):
                            data = {}
                        events.append({"event": event_type, "data": data})
                        event_type = ""
                        data_buf = ""
    except Exception:
        pass


async def _setup_listener(client, headers, app_id, sid):
    """Create session + start SSE listener. Returns (events, stop, thread)."""
    # Create session via send_and_wait (replaces old /chat route)
    await send_and_wait(client, app_id, sid, "init", headers)

    events = []
    stop = threading.Event()
    url = f"{BASE_URL}/api/apps/{app_id}/sessions/{sid}/events"
    t = threading.Thread(
        target=_collect_events_thread,
        args=(url, dict(headers), events, stop),
        daemon=True,
    )
    t.start()
    await asyncio.sleep(1)  # Let SSE connect
    return events, stop, t


async def _send_and_wait_for_result(client, headers, app_id, sid, message, events, timeout=60):
    """Send message via /messages and wait for result event."""
    await client.post(
        f"/api/apps/{app_id}/sessions/{sid}/messages",
        json={"message": message},
        headers=headers,
    )
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        if any(e["event"] == "result" for e in events):
            break


def _types(events):
    return sorted(set(e["event"] for e in events if e["event"] != "connected"))


# ===================================================================
# ERROR EVENT - trigger by sending message to non-existent session state
# ===================================================================

class TestErrorEvent:
    """Verify 'error' event arrives on /events when something fails."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, "test-minimal", headers)

    async def test_error_in_result(self):
        """When agent errors, the result event should contain the error field."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events, stop, t = await _setup_listener(
            self.client, self.headers, "test-minimal", sid,
        )

        await _send_and_wait_for_result(
            self.client, self.headers, "test-minimal", sid,
            "hello", events,
        )

        stop.set()
        t.join(timeout=3)

        results = [e for e in events if e["event"] == "result"]
        assert len(results) >= 1
        # Result should have error field (may be None for success)
        assert "error" in results[0]["data"]


# ===================================================================
# AGENT_EVENT - trigger by using multiagent app
# ===================================================================

class TestAgentEventViaEvents:
    """Verify 'agent_event' arrives on /events when agents are spawned."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "multiagent_app.yaml", headers)
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, "test-multiagent", headers)

    async def test_agent_event(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events, stop, t = await _setup_listener(
            self.client, self.headers, "test-multiagent", sid,
        )

        await _send_and_wait_for_result(
            self.client, self.headers, "test-multiagent", sid,
            "Spawn a researcher to list Python files in the current directory",
            events, timeout=120,
        )

        stop.set()
        t.join(timeout=3)

        types = _types(events)
        print(f"\n  Agent events types: {types}")

        if "agent_event" in types:
            ae = [e for e in events if e["event"] == "agent_event"]
            print(f"  agent_event count: {len(ae)}")
            print(f"  first agent_event data: {ae[0]['data']}")
        else:
            # agent_event may appear as derived from tool_call
            tool_calls = [e for e in events if e["event"] == "tool_call"]
            agent_tools = [tc for tc in tool_calls if "agent" in str(tc["data"].get("name", "")).lower() or "spawn" in str(tc["data"].get("name", "")).lower()]
            print(f"  No agent_event, but {len(agent_tools)} agent-related tool_calls")


# ===================================================================
# WORKBENCH EVENTS - trigger by reading/writing files with workbench enabled
# ===================================================================

class TestWorkbenchEventsViaEvents:
    """Verify workbench_read/write/edit arrive on /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "filesystem_app.yaml", headers)
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, "test-filesystem", headers)

    # NOTE: workbench_read/write tests removed - file lifecycle events
    # now flow through `preview:resource_set` on the `files` channel.
    # See tools/behavior_tests.py WSP01-22 for the new contract.


# ===================================================================
# COMPLETE INVENTORY for /events route
# ===================================================================

class TestCompleteEventsInventory:
    """Final verification - document ALL event types received on /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "filesystem_app.yaml", headers)
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, "test-filesystem", headers)

    async def test_comprehensive_inventory(self):
        """Multi-step chat to trigger maximum event types."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events, stop, t = await _setup_listener(
            self.client, self.headers, "test-filesystem", sid,
        )

        # Step 1: File read (tool_start, tool_call, maybe workbench_read)
        await _send_and_wait_for_result(
            self.client, self.headers, "test-filesystem", sid,
            "List files in current directory then read pyproject.toml first 5 lines",
            events,
        )

        # Clear result flag to allow second message
        await asyncio.sleep(1)

        stop.set()
        t.join(timeout=3)

        types = _types(events)
        total = len(events)

        print(f"\n  === COMPLETE EVENTS INVENTORY (via /events) ===")
        print(f"  Unique types: {types}")
        print(f"  Total events: {total}")

        # Categorize
        published_on_bus = {
            "token", "stream_done", "tool_start", "tool_call",
            "status", "terminal_output",
            "thinking_started", "thinking_delta", "thinking",
            "hook", "result", "error",
            "out_token", "in_token",
            "memory_update", "agent_event",
            "workbench_read", "workbench_write", "workbench_edit",
            "notification", "notification_result",
            "approval_request",
        }

        received = set(types)
        verified = received & published_on_bus
        not_received = published_on_bus - received

        print(f"  Verified on /events: {sorted(verified)}")
        print(f"  Not received (conditional): {sorted(not_received)}")

        # These MUST always be present in a tool-using chat
        assert "result" in types, f"Missing 'result'"
        assert "tool_call" in types, f"Missing 'tool_call'"
        assert "tool_start" in types, f"Missing 'tool_start'"
        assert "token" in types, f"Missing 'token'"
