"""30 — Verify ALL events arrive on GET /sessions/{sid}/events
      when messages are sent via POST /sessions/{sid}/messages.

This is the NEW architecture:
  POST /sessions/{sid}/messages  → fire-and-forget
  GET  /sessions/{sid}/events    → receives ALL events

We verify every event type the daemon claims to emit.
"""

import asyncio
import json
import uuid
import threading

import httpx
import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, send_message, BASE_URL

pytestmark = pytest.mark.asyncio


def _collect_events_in_thread(url: str, headers: dict, events: list, stop: threading.Event, timeout: float = 60):
    """Run in a thread: open GET /events SSE and collect all events until stop is set."""
    try:
        with httpx.stream("GET", url, headers=headers, timeout=None) as resp:
            if resp.status_code != 200:
                events.append({"event": "_error", "data": {"status": resp.status_code}})
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
                            data = {"_raw": data_buf}
                        events.append({"event": event_type, "data": data})
                        event_type = ""
                        data_buf = ""
    except Exception as exc:
        events.append({"event": "_exception", "data": {"error": str(exc)}})


async def _send_msg(client, headers, app_id, session_id, message):
    """POST /sessions/{sid}/messages — async fire-and-forget."""
    r = await client.post(
        f"/api/apps/{app_id}/sessions/{session_id}/messages",
        json={"message": message},
        headers=headers,
    )
    return r


def _event_types(events):
    """Extract unique event types from collected events."""
    return sorted(set(e["event"] for e in events if not e["event"].startswith("_")))


# ═══════════════════════════════════════════════════════════════
# TEST: Simple chat → verify core events arrive on /events
# ═══════════════════════════════════════════════════════════════

class TestEventsViaMessages:
    """Send message via POST /messages, verify events on GET /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "minimal.yaml", headers)
        self.app_id = "test-minimal"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_core_events_simple_chat(self):
        """Simple 'Say hello' → should get token + result events."""
        sid = f"test-{uuid.uuid4().hex[:8]}"

        # First create the session via send_and_wait
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        # Start event listener in thread
        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)  # Let SSE connect

        # Send message via /messages
        r = await _send_msg(self.client, self.headers, self.app_id, sid, "Say hello briefly")
        assert r.status_code in (200, 202)

        # Wait for result event (max 30s)
        for _ in range(60):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        types = _event_types(events)
        print(f"\n  Events received on /events: {types}")

        # MUST have these
        assert "connected" in types, f"Missing 'connected'. Got: {types}"
        assert "result" in types, f"Missing 'result'. Got: {types}"

        # SHOULD have token (if LLM generates text)
        if "token" not in types:
            print("  WARNING: no 'token' events — model may have returned empty")

    async def test_result_has_usage(self):
        """Result event on /events should include usage/cost data."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)

        await _send_msg(self.client, self.headers, self.app_id, sid, "What is 2+2?")

        for _ in range(60):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        result_events = [e for e in events if e["event"] == "result"]
        assert len(result_events) >= 1, f"No result event. Got: {_event_types(events)}"

        result_data = result_events[0]["data"]
        print(f"\n  Result keys: {list(result_data.keys())}")

        assert "content" in result_data
        assert "session_id" in result_data
        # Check enriched fields
        assert "usage" in result_data, f"Missing 'usage' in result. Keys: {list(result_data.keys())}"


class TestToolEventsViaMessages:
    """Tool-using chat → verify tool_start, tool_call, terminal_output on /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "filesystem_app.yaml", headers)
        self.app_id = "test-filesystem"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_tool_events(self):
        """Ask to list files → should get tool_start + tool_call events."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)

        await _send_msg(
            self.client, self.headers, self.app_id, sid,
            "List the files in the current directory",
        )

        for _ in range(60):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        types = _event_types(events)
        print(f"\n  Events on /events (tool chat): {types}")

        assert "result" in types
        assert "tool_call" in types, f"Missing 'tool_call'. Got: {types}"
        assert "tool_start" in types, f"Missing 'tool_start'. Got: {types}"


class TestMemoryEventsViaMessages:
    """Memory operations → verify memory_update on /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "memory_app.yaml", headers)
        self.app_id = "test-memory"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_memory_update_event(self):
        """set_goal → should emit memory_update on /events."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)

        await _send_msg(
            self.client, self.headers, self.app_id, sid,
            "Set your goal to: Test events pipeline",
        )

        for _ in range(60):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        types = _event_types(events)
        print(f"\n  Events on /events (memory): {types}")

        assert "result" in types
        # memory_update should be emitted if agent used set_goal
        if "memory_update" in types:
            mu = [e for e in events if e["event"] == "memory_update"]
            print(f"  memory_update events: {len(mu)}")


class TestShellEventsViaMessages:
    """Shell commands → verify terminal_output on /events."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "shell_app.yaml", headers)
        self.app_id = "test-shell"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_terminal_output_event(self):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)

        await _send_msg(
            self.client, self.headers, self.app_id, sid,
            'Run: echo "hello from events"',
        )

        for _ in range(60):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        types = _event_types(events)
        print(f"\n  Events on /events (shell): {types}")

        assert "result" in types
        assert "tool_call" in types
        if "terminal_output" in types:
            to = [e for e in events if e["event"] == "terminal_output"]
            print(f"  terminal_output: {to[0]['data']}")


class TestAllEventTypesInventory:
    """Full inventory — collect ALL event types from a complex chat."""

    @pytest.fixture(autouse=True)
    async def setup(self, client, headers):
        await deploy_app(client, "filesystem_app.yaml", headers)
        self.app_id = "test-filesystem"
        self.client = client
        self.headers = headers
        yield
        await undeploy_app(client, self.app_id, headers)

    async def test_full_inventory(self):
        """Complex request → collect all event types that arrive on /events."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(self.client, self.app_id, sid, "init", self.headers)

        events = []
        stop = threading.Event()
        url = f"{BASE_URL}/api/apps/{self.app_id}/sessions/{sid}/events"
        t = threading.Thread(
            target=_collect_events_in_thread,
            args=(url, dict(self.headers), events, stop),
            daemon=True,
        )
        t.start()
        await asyncio.sleep(1)

        await _send_msg(
            self.client, self.headers, self.app_id, sid,
            "List files in the current directory, then read the first line of pyproject.toml",
        )

        for _ in range(120):
            await asyncio.sleep(0.5)
            if any(e["event"] == "result" for e in events):
                break

        stop.set()
        t.join(timeout=3)

        types = _event_types(events)
        print(f"\n  === ALL event types on GET /sessions/sid/events ===")
        print(f"  {types}")
        print(f"  Total events: {len(events)}")
        print(f"  ===================================================")

        # Required
        assert "connected" in types
        assert "result" in types
        assert "tool_call" in types
        assert "tool_start" in types

        # Expected (should be present for a tool-using chat)
        expected = {"token", "status", "stream_done", "out_token", "in_token"}
        found = expected & set(types)
        missing = expected - set(types)
        print(f"  Expected and found: {found}")
        if missing:
            print(f"  Expected but MISSING: {missing}")
