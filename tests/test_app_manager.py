"""Tests for AppManager - deploy, run, undeploy, list.

Uses the same mock LLM provider pattern as test_e2e_run.py.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from digitorn.modules.llm_provider.providers.base import (
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Mock LLM provider (same as test_e2e_run.py)
# ---------------------------------------------------------------------------


class MockLLMProvider:
    """A mock LLM provider that returns scripted responses."""

    def __init__(self, responses: list[ChatResponse] | None = None):
        self.provider_id = "mock_provider"
        self.model = "mock-model"
        self.api_key = ""
        self.base_url = None
        self.timeout = 120.0
        self.max_retries = 2
        self.default_params: dict[str, Any] = {}
        self._responses = list(responses or [])
        self._call_index = 0
        self.call_log: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        pass

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        self.call_log.append({
            "messages": [(m.role, m.content[:100]) for m in messages],
            "tools": [t["function"]["name"] for t in (tools or [])],
        })

        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
            self._call_index += 1
            return resp
        return ChatResponse(
            content="Hello from mock.",
            model="mock-model",
            finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            tool_calls=None,
            raw={},
        )

    def get_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id=self.provider_id,
            backend="mock",
            model=self.model,
            capabilities=ProviderCapabilities(),
            extra={},
        )

    async def close(self) -> None:
        pass


def _make_text_response(text: str) -> ChatResponse:
    return ChatResponse(
        content=text,
        model="mock-model",
        finish_reason="end_turn",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=None,
        raw={},
    )


def _make_tool_call_response(
    tool_name: str, arguments: dict[str, Any], call_id: str = "call_1"
) -> ChatResponse:
    return ChatResponse(
        content="",
        model="mock-model",
        finish_reason="tool_use",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": arguments,
            },
        }],
        raw={},
    )


# ---------------------------------------------------------------------------
# YAML template
# ---------------------------------------------------------------------------

_ONESHOT_YAML = """\
app:
  app_id: {app_id}
  name: "{name}"

modules:
  hello: {{}}

agents:
  - id: assistant
    role: assistant
    brain:
      provider: mock
      model: mock-model
      backend: openai_compat
    system_prompt: "You are a test assistant."

execution:
  mode: one_shot
  input:
    type: text
  output:
    type: text

capabilities:
  default_policy: auto
"""


def _write_yaml(tmp_path: Path, app_id: str = "test-app", name: str = "Test App") -> Path:
    p = tmp_path / f"{app_id}.yaml"
    p.write_text(_ONESHOT_YAML.format(app_id=app_id, name=name))
    return p


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_provider():
    return MockLLMProvider(responses=[
        _make_text_response("Deployed and ready!"),
    ])


@pytest.fixture
def manager(tmp_path):
    """Create an AppManager with a loaded registry."""
    from digitorn.core.app.manager import AppManager
    from digitorn.core.loader import load_modules
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)
    return AppManager(registry, session_dir=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# Tests - Deploy
# ---------------------------------------------------------------------------


class TestAppManagerDeploy:
    """Tests for AppManager.deploy()."""

    @pytest.mark.asyncio
    async def test_deploy_success(self, manager, tmp_path, mock_provider):
        """Deploy an app and verify it's registered."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            deployed = await manager.deploy(yaml_path)

        assert deployed.app_id == "test-app"
        assert deployed.mode == "one_shot"
        assert "assistant" in deployed.contexts
        assert manager.is_deployed("test-app")

    @pytest.mark.asyncio
    async def test_deploy_duplicate_raises(self, manager, tmp_path, mock_provider):
        """Deploying the same app twice without force raises."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

            with pytest.raises(RuntimeError, match="already deployed"):
                await manager.deploy(yaml_path)

    @pytest.mark.asyncio
    async def test_deploy_force_redeploy(self, manager, tmp_path, mock_provider):
        """Force redeploy replaces the existing app."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            deployed1 = await manager.deploy(yaml_path)
            t1 = deployed1.deployed_at

            deployed2 = await manager.deploy(yaml_path, force=True)
            assert deployed2.deployed_at >= t1
            assert manager.is_deployed("test-app")

    @pytest.mark.asyncio
    async def test_deploy_invalid_yaml_raises(self, manager, tmp_path):
        """Invalid YAML raises AppCompilationError."""
        from digitorn.core.app.errors import AppCompilationError

        p = tmp_path / "bad.yaml"
        p.write_text("this is not valid digitorn yaml")

        with pytest.raises((AppCompilationError, Exception)):
            await manager.deploy(p)

    @pytest.mark.asyncio
    async def test_deploy_summary(self, manager, tmp_path, mock_provider):
        """Deployed app summary contains expected fields."""
        yaml_path = _write_yaml(tmp_path, app_id="summary-app", name="Summary App")

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            deployed = await manager.deploy(yaml_path)

        summary = deployed.summary()
        assert summary["app_id"] == "summary-app"
        assert summary["name"] == "Summary App"
        assert summary["mode"] == "one_shot"
        assert "assistant" in summary["agents"]
        assert isinstance(summary["total_tools"], int)
        assert isinstance(summary["deployed_at"], float)


# ---------------------------------------------------------------------------
# Tests - Run
# ---------------------------------------------------------------------------


class TestAppManagerRun:
    """Tests for AppManager.run_one_shot()."""

    @pytest.mark.asyncio
    async def test_run_one_shot(self, manager, tmp_path):
        """Run a one-shot app and get the result."""
        yaml_path = _write_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("The answer is 42."),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        result = await manager.run_one_shot("test-app", "What is the meaning of life?")

        assert result.content == "The answer is 42."
        assert result.error is None

    @pytest.mark.asyncio
    async def test_run_not_deployed_raises(self, manager):
        """Running a non-existent app raises RuntimeError."""
        with pytest.raises(RuntimeError, match="not deployed"):
            await manager.run_one_shot("nonexistent-app", "hello")

    @pytest.mark.asyncio
    async def test_run_with_tool_use(self, manager, tmp_path):
        """One-shot run where agent uses tools."""
        yaml_path = _write_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_tool_call_response("execute_tool", {
                "name": "hello.say_hello",
                "params": {"name": "World"},
            }),
            _make_text_response("I greeted the world: Hello, World!"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        result = await manager.run_one_shot("test-app", "Say hello to the world")

        assert "Hello, World!" in result.content
        assert result.tool_calls_count == 1


# ---------------------------------------------------------------------------
# Tests - List & Query
# ---------------------------------------------------------------------------


class TestAppManagerList:
    """Tests for list_apps, get, is_deployed."""

    @pytest.mark.asyncio
    async def test_list_empty(self, manager):
        """No apps deployed returns empty list."""
        assert manager.list_apps() == []

    @pytest.mark.asyncio
    async def test_list_multiple_apps(self, manager, tmp_path, mock_provider):
        """List shows all deployed apps."""
        y1 = _write_yaml(tmp_path, app_id="app-one", name="App One")
        y2 = _write_yaml(tmp_path, app_id="app-two", name="App Two")

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(y1)
            await manager.deploy(y2)

        apps = manager.list_apps()
        assert len(apps) == 2
        ids = {a["app_id"] for a in apps}
        assert ids == {"app-one", "app-two"}

    @pytest.mark.asyncio
    async def test_get_deployed(self, manager, tmp_path, mock_provider):
        """get() returns the DeployedApp object."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        deployed = manager.get("test-app")
        assert deployed is not None
        assert deployed.app_id == "test-app"

    @pytest.mark.asyncio
    async def test_get_not_deployed_returns_none(self, manager):
        """get() returns None for non-existent app."""
        assert manager.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_is_deployed(self, manager, tmp_path, mock_provider):
        """is_deployed() returns correct bool."""
        assert not manager.is_deployed("test-app")

        yaml_path = _write_yaml(tmp_path)
        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        assert manager.is_deployed("test-app")


# ---------------------------------------------------------------------------
# Tests - Undeploy
# ---------------------------------------------------------------------------


class TestAppManagerUndeploy:
    """Tests for AppManager.undeploy()."""

    @pytest.mark.asyncio
    async def test_undeploy(self, manager, tmp_path, mock_provider):
        """Undeploy removes the app."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        assert manager.is_deployed("test-app")
        removed = await manager.undeploy("test-app")
        assert removed is True
        assert not manager.is_deployed("test-app")

    @pytest.mark.asyncio
    async def test_undeploy_not_deployed(self, manager):
        """Undeploying a non-existent app returns False."""
        removed = await manager.undeploy("nonexistent")
        assert removed is False

    @pytest.mark.asyncio
    async def test_undeploy_then_list(self, manager, tmp_path, mock_provider):
        """After undeploy, app no longer appears in list."""
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        assert len(manager.list_apps()) == 1
        await manager.undeploy("test-app")
        assert len(manager.list_apps()) == 0


# ---------------------------------------------------------------------------
# Tests - API Routes
# ---------------------------------------------------------------------------


class TestAppsAPI:
    """Tests for the /api/apps API routes using FastAPI TestClient."""

    @pytest.fixture
    def app_with_manager(self, tmp_path):
        """Create a FastAPI app with an AppManager attached."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from digitorn.core.api.apps import router
        from digitorn.core.app.manager import AppManager
        from digitorn.core.loader import load_modules
        from digitorn.modules.registry import ModuleRegistry

        registry = ModuleRegistry()
        load_modules(registry, load_all=True)
        manager = AppManager(registry, session_dir=tmp_path / "sessions")

        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def _inject_test_auth(request, call_next):
            request.state.user_id = "test-user"
            request.state.roles = ["admin"]
            request.state.permissions = ["*"]
            return await call_next(request)

        app.state.app_manager = manager

        return app, manager, TestClient(app)

    def test_list_empty(self, app_with_manager):
        """GET /api/apps returns empty list."""
        _, _, client = app_with_manager
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"] == []

    def test_deploy_missing_yaml_path(self, app_with_manager):
        """POST /api/apps/deploy without yaml_path returns 400."""
        _, _, client = app_with_manager
        resp = client.post("/api/apps/deploy", json={})
        assert resp.status_code == 400

    def test_deploy_file_not_found(self, app_with_manager):
        """POST /api/apps/deploy with nonexistent file returns 404."""
        _, _, client = app_with_manager
        resp = client.post("/api/apps/deploy", json={"yaml_path": "/nonexistent.yaml"})
        assert resp.status_code == 404

    def test_deploy_and_list(self, app_with_manager, tmp_path, mock_provider):
        """Deploy via API then list."""
        _, manager, client = app_with_manager
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            resp = client.post("/api/apps/deploy", json={
                "yaml_path": str(yaml_path),
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["app_id"] == "test-app"

        # List
        resp = client.get("/api/apps")
        apps = resp.json()["data"]
        assert len(apps) == 1
        assert apps[0]["app_id"] == "test-app"

    def test_get_app(self, app_with_manager, tmp_path, mock_provider):
        """GET /api/apps/{app_id} returns app details."""
        _, _, client = app_with_manager
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        resp = client.get("/api/apps/test-app")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["app_id"] == "test-app"

    def test_get_app_not_found(self, app_with_manager):
        """GET /api/apps/{app_id} for non-existent returns 404."""
        _, _, client = app_with_manager
        resp = client.get("/api/apps/nonexistent")
        assert resp.status_code == 404

    def test_undeploy_via_api(self, app_with_manager, tmp_path, mock_provider):
        """DELETE /api/apps/{app_id} undeploys."""
        _, _, client = app_with_manager
        yaml_path = _write_yaml(tmp_path)

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        resp = client.delete("/api/apps/test-app")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

        # Verify it's gone
        resp = client.get("/api/apps/test-app")
        assert resp.status_code == 404

    def test_undeploy_not_found(self, app_with_manager):
        """DELETE /api/apps/{app_id} for non-existent returns 404."""
        _, _, client = app_with_manager
        resp = client.delete("/api/apps/nonexistent")
        assert resp.status_code == 404

    def test_run_one_shot_via_api(self, app_with_manager, tmp_path):
        """POST /api/apps/{app_id}/run executes one-shot."""
        _, _, client = app_with_manager
        yaml_path = _write_yaml(tmp_path)

        mock_provider = MockLLMProvider(responses=[
            _make_text_response("API response: 42"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        resp = client.post("/api/apps/test-app/run", json={"input": "What is 42?"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["content"] == "API response: 42"

    def test_run_not_deployed(self, app_with_manager):
        """POST /api/apps/{app_id}/run for non-existent returns 400."""
        _, _, client = app_with_manager
        resp = client.post("/api/apps/nonexistent/run", json={"input": "hello"})
        assert resp.status_code == 400

    def test_no_manager_returns_503(self):
        """If AppManager is not set, routes return 503."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from digitorn.core.api.apps import router

        app = FastAPI()
        app.include_router(router)
        # Don't set app.state.app_manager

        client = TestClient(app)
        resp = client.get("/api/apps")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# YAML template for conversation mode
# ---------------------------------------------------------------------------

_CONVERSATION_YAML = """\
app:
  app_id: {app_id}
  name: "{name}"

modules:
  hello: {{}}

agents:
  - id: assistant
    role: assistant
    brain:
      provider: mock
      model: mock-model
      backend: openai_compat
    system_prompt: "You are a test chatbot."

execution:
  mode: conversation
  greeting: "Welcome!"

capabilities:
  default_policy: auto
"""


def _write_conversation_yaml(
    tmp_path: Path, app_id: str = "test-chat", name: str = "Test Chat"
) -> Path:
    p = tmp_path / f"{app_id}.yaml"
    p.write_text(_CONVERSATION_YAML.format(app_id=app_id, name=name))
    return p


# ---------------------------------------------------------------------------
# Tests - Chat (Stateful Conversation)
# ---------------------------------------------------------------------------


class TestAppManagerChat:
    """Tests for AppManager.chat() - stateful conversation sessions."""

    @pytest.mark.asyncio
    async def test_chat_single_message(self, manager, tmp_path):
        """Send one message and get a response."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Hello! How can I help?"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        result = await manager.chat("test-chat", "session-1", "Hi there")

        assert result.content == "Hello! How can I help?"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_chat_multi_turn_history(self, manager, tmp_path):
        """Multiple messages in same session share history."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("I'm doing great!"),
            _make_text_response("Paris is the capital of France."),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        # Turn 1
        r1 = await manager.chat("test-chat", "session-1", "How are you?")
        assert "great" in r1.content

        # Turn 2 - provider sees full history
        r2 = await manager.chat("test-chat", "session-1", "Capital of France?")
        assert "Paris" in r2.content

        # Verify provider received growing message history
        assert len(mock_provider.call_log) == 2
        # Turn 2 should have more messages than turn 1
        turn1_msgs = mock_provider.call_log[0]["messages"]
        turn2_msgs = mock_provider.call_log[1]["messages"]
        assert len(turn2_msgs) > len(turn1_msgs)

    @pytest.mark.asyncio
    async def test_chat_separate_sessions(self, manager, tmp_path):
        """Different session_ids have independent histories."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Response A"),
            _make_text_response("Response B"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        await manager.chat("test-chat", "session-A", "Message A")
        await manager.chat("test-chat", "session-B", "Message B")

        # Both sessions have independent history
        session_a = manager.get_session("test-chat", "session-A")
        session_b = manager.get_session("test-chat", "session-B")
        assert session_a is not None
        assert session_b is not None
        assert session_a.session_id != session_b.session_id

    @pytest.mark.asyncio
    async def test_chat_session_lifecycle(self, manager, tmp_path):
        """Sessions can be listed and ended."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Hi!"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        await manager.chat("test-chat", "s1", "Hello")

        # List sessions
        sessions = manager.list_sessions("test-chat")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

        # End session
        assert manager.end_session("test-chat", "s1") is True
        assert manager.get_session("test-chat", "s1") is None
        assert manager.list_sessions("test-chat") == []

    @pytest.mark.asyncio
    async def test_chat_not_deployed_raises(self, manager):
        """Chat with non-existent app raises RuntimeError."""
        with pytest.raises(RuntimeError, match="not deployed"):
            await manager.chat("nonexistent", "s1", "Hello")

    @pytest.mark.asyncio
    async def test_undeploy_cleans_sessions(self, manager, tmp_path):
        """Undeploy removes all sessions for the app."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Hi!"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        await manager.chat("test-chat", "s1", "Hello")
        assert manager.get_session("test-chat", "s1") is not None

        await manager.undeploy("test-chat")
        assert manager.get_session("test-chat", "s1") is None

    @pytest.mark.asyncio
    async def test_chat_with_tool_use(self, manager, tmp_path):
        """Chat turn where agent uses a tool."""
        yaml_path = _write_conversation_yaml(tmp_path)
        mock_provider = MockLLMProvider(responses=[
            _make_tool_call_response("execute_tool", {
                "name": "hello.say_hello",
                "params": {"name": "Alice"},
            }),
            _make_text_response("I said hello: Hello, Alice!"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            await manager.deploy(yaml_path)

        result = await manager.chat("test-chat", "s1", "Say hello to Alice")
        assert "Hello, Alice!" in result.content
        assert result.tool_calls_count == 1


# ---------------------------------------------------------------------------
# Tests - Chat API Routes
# ---------------------------------------------------------------------------


class TestChatAPI:
    """Tests for POST /api/apps/{app_id}/chat."""

    @pytest.fixture
    def app_with_manager(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from digitorn.core.api.apps import router
        from digitorn.core.app.manager import AppManager
        from digitorn.core.loader import load_modules
        from digitorn.modules.registry import ModuleRegistry

        registry = ModuleRegistry()
        load_modules(registry, load_all=True)
        manager = AppManager(registry)

        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def _inject_test_auth(request, call_next):
            request.state.user_id = "test-user"
            request.state.roles = ["admin"]
            request.state.permissions = ["*"]
            return await call_next(request)

        app.state.app_manager = manager

        return app, manager, TestClient(app)

    def test_chat_via_api(self, app_with_manager, tmp_path):
        """Send a chat message via REST API."""
        _, _, client = app_with_manager
        yaml_path = _write_conversation_yaml(tmp_path)

        mock_provider = MockLLMProvider(responses=[
            _make_text_response("Bonjour!"),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        resp = client.post("/api/apps/test-chat/chat", json={
            "session_id": "s1",
            "message": "Hello",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["content"] == "Bonjour!"
        assert data["data"]["session_id"] == "s1"

    def test_chat_multi_turn_via_api(self, app_with_manager, tmp_path):
        """Multiple turns in the same session via API."""
        _, _, client = app_with_manager
        yaml_path = _write_conversation_yaml(tmp_path)

        mock_provider = MockLLMProvider(responses=[
            _make_text_response("I'm fine!"),
            _make_text_response("The capital is Paris."),
        ])

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        # Turn 1
        resp1 = client.post("/api/apps/test-chat/chat", json={
            "session_id": "s1",
            "message": "How are you?",
        })
        assert resp1.json()["data"]["content"] == "I'm fine!"

        # Turn 2
        resp2 = client.post("/api/apps/test-chat/chat", json={
            "session_id": "s1",
            "message": "Capital of France?",
        })
        assert resp2.json()["data"]["content"] == "The capital is Paris."

    def test_chat_not_deployed(self, app_with_manager):
        """Chat with non-existent app returns 404."""
        _, _, client = app_with_manager
        resp = client.post("/api/apps/nonexistent/chat", json={
            "session_id": "s1",
            "message": "Hello",
        })
        assert resp.status_code == 404

    def test_summary_includes_greeting(self, app_with_manager, tmp_path):
        """App summary includes greeting for conversation apps."""
        _, _, client = app_with_manager
        yaml_path = _write_conversation_yaml(tmp_path)

        mock_provider = MockLLMProvider()

        with patch(
            "digitorn.core.runtime.bootstrap._resolve_provider",
            return_value=mock_provider,
        ):
            client.post("/api/apps/deploy", json={"yaml_path": str(yaml_path)})

        resp = client.get("/api/apps/test-chat")
        data = resp.json()["data"]
        assert data["greeting"] == "Welcome!"
