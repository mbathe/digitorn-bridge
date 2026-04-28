"""E2E test fixtures - real LLM calls through the daemon API."""

from __future__ import annotations

import os
import uuid
import httpx
import pytest
from typing import Any

DAEMON_URL = os.environ.get("DIGITORN_TEST_DAEMON", "http://127.0.0.1:8000")
APP_ID = os.environ.get("DIGITORN_TEST_APP", "e2e-test")
TIMEOUT = float(os.environ.get("DIGITORN_TEST_TIMEOUT", "300"))


class E2EClient:
    """Client for E2E tests - sends messages and checks responses."""

    def __init__(self, daemon_url: str, app_id: str):
        self.daemon_url = daemon_url
        self.app_id = app_id
        self._client = httpx.Client(timeout=TIMEOUT)

    def chat(self, message: str, session_id: str | None = None) -> dict[str, Any]:
        sid = session_id or f"e2e-{uuid.uuid4().hex[:8]}"
        resp = self._client.post(
            f"{self.daemon_url}/api/apps/{self.app_id}/chat",
            json={"session_id": sid, "message": message},
        )
        data = resp.json()
        data["_session_id"] = sid
        return data

    def chat_session(self, messages: list[str]) -> list[dict[str, Any]]:
        sid = f"e2e-session-{uuid.uuid4().hex[:8]}"
        results = []
        for msg in messages:
            results.append(self.chat(msg, session_id=sid))
        return results

    def run(self, input_text: str) -> dict[str, Any]:
        resp = self._client.post(
            f"{self.daemon_url}/api/apps/{self.app_id}/run",
            json={"input": input_text},
        )
        return resp.json()

    def deploy(self, yaml_path: str) -> dict[str, Any]:
        resp = self._client.post(
            f"{self.daemon_url}/api/apps/deploy",
            json={"yaml_path": yaml_path, "force": True},
        )
        return resp.json()

    def list_apps(self) -> list[str]:
        resp = self._client.get(f"{self.daemon_url}/api/apps")
        data = resp.json()
        return [a["app_id"] for a in data.get("data", [])]

    def sessions(self) -> list[dict]:
        resp = self._client.get(f"{self.daemon_url}/api/apps/{self.app_id}/sessions")
        return resp.json().get("data", {}).get("sessions", [])

    def health(self) -> bool:
        try:
            resp = self._client.get(f"{self.daemon_url}/health")
            return resp.status_code == 200
        except Exception:
            return False

    def close(self):
        self._client.close()


def assert_success(result: dict) -> None:
    assert result.get("success"), f"Expected success, got error: {result.get('error')}"


def assert_has_content(result: dict) -> None:
    content = result.get("data", {}).get("content", "")
    assert content.strip(), f"Expected non-empty content, got empty"


def assert_content_contains(result: dict, *terms: str) -> None:
    content = result.get("data", {}).get("content", "").lower()
    for term in terms:
        assert term.lower() in content, f"Expected '{term}' in response, got: {content[:200]}"


def assert_used_tools(result: dict, min_calls: int = 1) -> None:
    tools = result.get("data", {}).get("tool_calls_count", 0)
    assert tools >= min_calls, f"Expected at least {min_calls} tool calls, got {tools}"


def assert_no_tools(result: dict) -> None:
    tools = result.get("data", {}).get("tool_calls_count", 0)
    assert tools == 0, f"Expected no tool calls, got {tools}"


def assert_no_error(result: dict) -> None:
    error = result.get("error")
    assert not error, f"Unexpected error: {error}"


@pytest.fixture(scope="session")
def client():
    c = E2EClient(DAEMON_URL, APP_ID)
    if not c.health():
        pytest.skip("Daemon not running")
    yield c
    c.close()


@pytest.fixture(scope="session")
def ensure_deployed(client: E2EClient):
    apps = client.list_apps()
    if APP_ID not in apps:
        pytest.skip(f"App '{APP_ID}' not deployed")
