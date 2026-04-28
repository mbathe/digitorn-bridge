"""E2E Tests: Streaming - SSE events, token streaming."""

import pytest
import httpx
from tests.e2e.conftest import *


class TestSSEStreaming:
    def test_stream_endpoint_exists(self, client, ensure_deployed):
        resp = httpx.post(
            f"{client.daemon_url}/api/apps/{client.app_id}/chat/stream",
            json={"session_id": "e2e-stream-1", "message": "Say hello"},
            timeout=60.0,
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    def test_stream_has_events(self, client, ensure_deployed):
        events = []
        with httpx.stream(
            "POST",
            f"{client.daemon_url}/api/apps/{client.app_id}/chat/stream",
            json={"session_id": "e2e-stream-2", "message": "What is 1+1?"},
            timeout=60.0,
        ) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])
                if len(events) > 5:
                    break
        assert len(events) > 0

    def test_stream_has_result(self, client, ensure_deployed):
        import json as _json
        result_content = ""
        with httpx.stream(
            "POST",
            f"{client.daemon_url}/api/apps/{client.app_id}/chat/stream",
            json={"session_id": "e2e-stream-3", "message": "Say OK"},
            timeout=60.0,
        ) as resp:
            event_type = ""
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: ") and event_type == "result":
                    data = _json.loads(line[6:])
                    result_content = data.get("content", "")
                    break
        assert result_content.strip()
