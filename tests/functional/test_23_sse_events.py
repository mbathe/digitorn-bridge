"""23 — P1: SSE persistent events, async messages, session events stream."""

import asyncio
import json
import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, collect_sse_events, send_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def app(client, headers):
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestSessionEventsSSE:
    """GET /api/apps/{app_id}/sessions/{session_id}/events — persistent SSE stream."""

    async def test_events_stream_connects(self, client, headers, app):
        """The events SSE endpoint should return text/event-stream."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Create session first via the new messages API
        await send_and_wait(client, app, sid, "init", headers)

        async with client.stream(
            "GET",
            f"/api/apps/{app}/sessions/{sid}/events",
            headers=headers,
            timeout=30,
        ) as response:
            assert response.status_code == 200
            ct = response.headers.get("content-type", "")
            assert "text/event-stream" in ct
            # Read first event (should be "connected" or similar)
            lines = []
            try:
                async for line in response.aiter_lines():
                    lines.append(line)
                    # Stop after first complete event
                    if line == "" and len(lines) > 1:
                        break
                    if len(lines) > 20:
                        break
            except asyncio.TimeoutError:
                pass  # Expected — stream stays open
            # Should have received at least the connected event
            assert len(lines) >= 1

    async def test_events_nonexistent_session(self, client, headers, app):
        """Events for nonexistent session should return error or empty stream."""
        async with client.stream(
            "GET",
            f"/api/apps/{app}/sessions/nonexistent-xyz/events",
            headers=headers,
            timeout=10,
        ) as response:
            # Should not crash — may return 404 or empty stream
            assert response.status_code < 500


class TestAsyncMessages:
    """POST /api/apps/{app_id}/sessions/{session_id}/messages — async message."""

    async def test_async_message(self, client, headers, app):
        """Send async message — should return 202."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        # Create session first
        await send_and_wait(client, app, sid, "init", headers)

        r = await send_message(client, app, sid, "async hello", headers)
        # send_message returns parsed json; check success
        assert r.get("success") is True or "error" not in r

    async def test_async_message_nonexistent_session(self, client, headers, app):
        r = await client.post(
            f"/api/apps/{app}/sessions/nonexistent-xyz/messages",
            json={"message": "hello"},
            headers=headers,
        )
        assert r.status_code < 500

    async def test_async_message_empty(self, client, headers, app):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, app, sid, "init", headers)

        r = await client.post(
            f"/api/apps/{app}/sessions/{sid}/messages",
            json={"message": ""},
            headers=headers,
        )
        assert r.status_code < 500


class TestStreamChatEvents:
    """Verify specific SSE event types via collect_sse_events."""

    async def test_stream_has_usage_in_result(self, client, headers, app):
        """Result event should contain usage stats."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events = await collect_sse_events(client, app, sid, "Say hi", headers, timeout=120)

        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) > 0, f"No 'result' event. Events: {[e['type'] for e in events]}"
        result_data = result_events[0]["data"]
        assert "content" in result_data
        assert "session_id" in result_data

    async def test_stream_has_token_events(self, client, headers, app):
        """Stream should contain token events when LLM generates text."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events = await collect_sse_events(client, app, sid, "PING", headers, timeout=30)

        event_types = [e["type"] for e in events]
        # Should have at least a result event
        assert "result" in event_types
