"""03 - Chat endpoints: message send + SSE events (new API)."""

import uuid

import pytest

from .conftest import deploy_app, undeploy_app, send_and_wait, collect_sse_events, send_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def chat_app(client, headers):
    """Deploy minimal app for chat tests."""
    await deploy_app(client, "minimal.yaml", headers)
    yield "test-minimal"
    await undeploy_app(client, "test-minimal", headers)


class TestSyncChat:
    """POST /api/apps/{app_id}/sessions/{session_id}/messages + poll for result"""

    async def test_basic_chat(self, client, headers, chat_app):
        session_id = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_and_wait(client, chat_app, session_id, "PING", headers)
        assert d["success"] is True
        assert d["data"] is not None
        assert "content" in d["data"]

    async def test_chat_creates_session(self, client, headers, chat_app):
        session_id = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, chat_app, session_id, "hello", headers)

        r = await client.get(
            f"/api/apps/{chat_app}/sessions/{session_id}",
            headers=headers,
        )
        d = r.json()
        assert d["success"] is True

    async def test_chat_multi_turn(self, client, headers, chat_app):
        """Multiple messages in same session maintain context."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        await send_and_wait(client, chat_app, sid, "My name is Alice", headers)
        d = await send_and_wait(client, chat_app, sid, "What is my name?", headers)
        assert d["success"] is True

    async def test_chat_nonexistent_app(self, client, headers):
        sid = f"test-{uuid.uuid4().hex[:8]}"
        d = await send_message(client, "nonexistent-xyz", sid, "hello", headers)
        assert d.get("success") is False or d.get("error") is not None


class TestStreamChat:
    """GET /api/apps/{app_id}/sessions/{session_id}/events - SSE"""

    async def test_stream_basic(self, client, headers, chat_app):
        """Should return SSE events."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events = await collect_sse_events(client, chat_app, sid, "PING", headers)
        assert len(events) >= 1

    async def test_stream_has_result_event(self, client, headers, chat_app):
        """Stream should end with a 'result' event."""
        sid = f"test-{uuid.uuid4().hex[:8]}"
        events = await collect_sse_events(client, chat_app, sid, "Say hello", headers)
        result_events = [e for e in events if e.get("type") == "result"]
        assert len(result_events) >= 1
