"""Tests for the llm_provider module.

Verifies provider lifecycle, configuration, chat dispatch, and
parameter handling without making real API calls.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.llm_provider.module import LLMProviderModule
from digitorn.modules.llm_provider.params import (
    ChatParams,
    ConfigureParams,
    GetProviderInfoParams,
    ListProvidersParams,
    MessageParam,
    RemoveParams,
    UpdateDefaultsParams,
)
from digitorn.modules.llm_provider.providers.base import (
    BaseLLMProvider,
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    StreamChunk,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Fake provider for testing (no real SDK needed)
# ---------------------------------------------------------------------------


class FakeProvider(BaseLLMProvider):
    """In-memory fake provider for unit tests."""

    BACKEND = "fake"

    def __init__(self, provider_id: str, model: str, **kwargs: Any) -> None:
        # Strip keys not accepted by BaseLLMProvider
        kwargs.pop("provider_hint", None)
        super().__init__(provider_id=provider_id, model=model, **kwargs)
        self.initialized = False
        self.closed = False
        self.chat_calls: list[dict] = []
        self._response = ChatResponse(
            content="Hello from fake!",
            model=model,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def initialize(self) -> None:
        self.initialized = True

    async def chat(self, messages, **kwargs) -> ChatResponse:
        self.chat_calls.append({"messages": messages, **kwargs})
        return self._response

    async def chat_stream(self, messages, **kwargs) -> AsyncIterator[StreamChunk]:
        self.chat_calls.append({"messages": messages, "stream": True, **kwargs})
        yield StreamChunk(delta="Hello ")
        yield StreamChunk(delta="world!")
        yield StreamChunk(
            delta="",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    def get_info(self) -> ProviderInfo:
        return ProviderInfo(
            provider_id=self.provider_id,
            backend=self.BACKEND,
            model=self.model,
            capabilities=ProviderCapabilities(streaming=True, tool_use=True),
        )

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_module() -> LLMProviderModule:
    """Create a fresh LLMProviderModule."""
    return LLMProviderModule()


def _inject_fake(module: LLMProviderModule, provider_id: str = "test", model: str = "fake-model") -> FakeProvider:
    """Inject a fake provider into the module (bypassing configure action)."""
    fake = FakeProvider(provider_id=provider_id, model=model)
    fake.initialized = True
    module._providers[provider_id] = fake
    return fake


# ---------------------------------------------------------------------------
# Tests - Provider types
# ---------------------------------------------------------------------------


class TestProviderTypes:

    def test_chat_message_fields(self):
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.name is None

    def test_token_usage_defaults(self):
        usage = TokenUsage()
        assert usage.total_tokens == 0
        assert usage.cache_read_tokens == 0

    def test_chat_response(self):
        resp = ChatResponse(content="hi", model="gpt-4o")
        assert resp.content == "hi"
        assert resp.finish_reason is None
        assert resp.tool_calls is None

    def test_provider_capabilities(self):
        caps = ProviderCapabilities(tool_use=True, vision=True, max_context_window=128000)
        assert caps.tool_use is True
        assert caps.max_context_window == 128000

    def test_stream_chunk(self):
        chunk = StreamChunk(delta="hello", finish_reason="stop")
        assert chunk.delta == "hello"
        assert chunk.finish_reason == "stop"


# ---------------------------------------------------------------------------
# Tests - Base provider
# ---------------------------------------------------------------------------


class TestBaseProvider:

    def test_merge_params_overrides(self):
        provider = FakeProvider("test", "m1", default_params={"temperature": 0.5, "max_tokens": 100})
        merged = provider._merge_params(temperature=0.9, top_p=None)
        assert merged["temperature"] == 0.9  # Overridden
        assert merged["max_tokens"] == 100  # Default preserved
        assert "top_p" not in merged  # None not included

    def test_merge_params_no_defaults(self):
        provider = FakeProvider("test", "m1")
        merged = provider._merge_params(temperature=0.5)
        assert merged == {"temperature": 0.5}


# ---------------------------------------------------------------------------
# Tests - Module lifecycle
# ---------------------------------------------------------------------------


class TestModuleLifecycle:

    @pytest.mark.asyncio
    async def test_on_stop_closes_all(self):
        module = _make_module()
        f1 = _inject_fake(module, "p1")
        f2 = _inject_fake(module, "p2")

        await module.on_stop()

        assert f1.closed
        assert f2.closed
        assert len(module._providers) == 0

    @pytest.mark.asyncio
    async def test_on_config_update_providers(self):
        module = _make_module()

        # Patch _create_provider to return a fake
        created = {}

        def fake_create(provider_id, backend, model, **kw):
            f = FakeProvider(provider_id, model, **kw)
            created[provider_id] = f
            return f

        module._create_provider = fake_create

        await module.on_config_update({
            "providers": {
                "brain_a": {"backend": "openai_compat", "model": "gpt-4o"},
                "brain_b": {"backend": "anthropic", "model": "claude-sonnet-4-20250514"},
            }
        })

        assert "brain_a" in module._providers
        assert "brain_b" in module._providers
        assert created["brain_a"].initialized
        assert created["brain_b"].initialized

    def test_state_snapshot(self):
        module = _make_module()
        fake = _inject_fake(module, "test", "fake-model")
        fake.default_params = {"temperature": 0.5}

        snap = module.state_snapshot()
        assert "providers" in snap
        assert "test" in snap["providers"]
        assert snap["providers"]["test"]["model"] == "fake-model"
        # API key should NOT be in snapshot
        assert "api_key" not in snap["providers"]["test"]

    @pytest.mark.asyncio
    async def test_restore_state(self):
        module = _make_module()

        created = {}

        def fake_create(provider_id, backend, model, **kw):
            f = FakeProvider(provider_id, model, **kw)
            created[provider_id] = f
            return f

        module._create_provider = fake_create

        await module.restore_state({
            "providers": {
                "restored": {
                    "backend": "openai_compat",
                    "model": "gpt-4o",
                    "base_url": "https://api.openai.com/v1",
                    "timeout": 60.0,
                    "max_retries": 1,
                    "default_params": {"temperature": 0.3},
                }
            }
        })

        assert "restored" in module._providers
        assert created["restored"].initialized


# ---------------------------------------------------------------------------
# Tests - configure action
# ---------------------------------------------------------------------------


class TestConfigureAction:

    @pytest.mark.asyncio
    async def test_configure_success(self):
        module = _make_module()

        # Patch _create_provider
        def fake_create(**kw):
            return FakeProvider(kw["provider_id"], kw["model"])

        module._create_provider = lambda **kw: FakeProvider(kw["provider_id"], kw["model"])

        params = ConfigureParams(
            provider_id="my_brain",
            backend="openai_compat",
            model="gpt-4o",
        )
        result = await module.configure(params)

        assert result.success
        assert result.data["provider_id"] == "my_brain"
        assert result.data["model"] == "gpt-4o"
        assert "my_brain" in module._providers

    @pytest.mark.asyncio
    async def test_configure_replaces_existing(self):
        module = _make_module()
        old = _inject_fake(module, "my_brain", "old-model")

        module._create_provider = lambda **kw: FakeProvider(kw["provider_id"], kw["model"])

        params = ConfigureParams(
            provider_id="my_brain",
            backend="openai_compat",
            model="new-model",
        )
        result = await module.configure(params)

        assert result.success
        assert old.closed  # Old provider was closed
        assert module._providers["my_brain"].model == "new-model"

    @pytest.mark.asyncio
    async def test_configure_import_error(self):
        module = _make_module()

        # Simulate ImportError during initialize
        class FailProvider(FakeProvider):
            async def initialize(self):
                raise ImportError("openai not installed")

        module._create_provider = lambda **kw: FailProvider(kw["provider_id"], kw["model"])

        params = ConfigureParams(provider_id="fail", backend="openai_compat", model="x")
        result = await module.configure(params)

        assert not result.success
        assert "not installed" in result.error


# ---------------------------------------------------------------------------
# Tests - chat action
# ---------------------------------------------------------------------------


class TestChatAction:

    @pytest.mark.asyncio
    async def test_chat_success(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")

        params = ChatParams(
            provider_id="brain",
            messages=[MessageParam(role="user", content="Hello!")],
        )
        result = await module.chat(params)

        assert result.success
        assert result.data["content"] == "Hello from fake!"
        assert result.data["usage"]["total_tokens"] == 15
        assert result.data["streamed"] is False
        assert len(fake.chat_calls) == 1

    @pytest.mark.asyncio
    async def test_chat_with_params(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")

        params = ChatParams(
            provider_id="brain",
            messages=[MessageParam(role="user", content="Hi")],
            temperature=0.8,
            max_tokens=100,
            top_p=0.9,
        )
        result = await module.chat(params)

        assert result.success
        call = fake.chat_calls[0]
        assert call["temperature"] == 0.8
        assert call["max_tokens"] == 100

    @pytest.mark.asyncio
    async def test_chat_streaming(self):
        module = _make_module()
        _inject_fake(module, "brain")

        params = ChatParams(
            provider_id="brain",
            messages=[MessageParam(role="user", content="Stream me!")],
            stream=True,
        )
        result = await module.chat(params)

        assert result.success
        assert result.data["content"] == "Hello world!"
        assert result.data["streamed"] is True

    @pytest.mark.asyncio
    async def test_chat_provider_not_found(self):
        module = _make_module()

        params = ChatParams(
            provider_id="nonexistent",
            messages=[MessageParam(role="user", content="Hi")],
        )
        result = await module.chat(params)

        assert not result.success
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_chat_exception(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")
        fake._response = None  # Will cause AttributeError

        async def bad_chat(messages, **kw):
            raise ConnectionError("API unreachable")

        fake.chat = bad_chat

        params = ChatParams(
            provider_id="brain",
            messages=[MessageParam(role="user", content="Hi")],
        )
        result = await module.chat(params)

        assert not result.success
        assert "API unreachable" in result.error

    @pytest.mark.asyncio
    async def test_chat_converts_messages(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")

        params = ChatParams(
            provider_id="brain",
            messages=[
                MessageParam(role="system", content="You are helpful."),
                MessageParam(role="user", content="Hello!"),
            ],
        )
        result = await module.chat(params)

        assert result.success
        call_msgs = fake.chat_calls[0]["messages"]
        assert len(call_msgs) == 2
        assert isinstance(call_msgs[0], ChatMessage)
        assert call_msgs[0].role == "system"
        assert call_msgs[1].role == "user"


# ---------------------------------------------------------------------------
# Tests - remove action
# ---------------------------------------------------------------------------


class TestRemoveAction:

    @pytest.mark.asyncio
    async def test_remove_success(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")

        params = RemoveParams(provider_id="brain")
        result = await module.remove(params)

        assert result.success
        assert fake.closed
        assert "brain" not in module._providers

    @pytest.mark.asyncio
    async def test_remove_not_found(self):
        module = _make_module()

        params = RemoveParams(provider_id="nonexistent")
        result = await module.remove(params)

        assert not result.success
        assert "not found" in result.error


# ---------------------------------------------------------------------------
# Tests - list_providers action
# ---------------------------------------------------------------------------


class TestListProviders:

    @pytest.mark.asyncio
    async def test_list_empty(self):
        module = _make_module()
        result = await module.list_providers(ListProvidersParams())

        assert result.success
        assert result.data["count"] == 0
        assert result.data["providers"] == []

    @pytest.mark.asyncio
    async def test_list_multiple(self):
        module = _make_module()
        _inject_fake(module, "brain_a", "model-a")
        _inject_fake(module, "brain_b", "model-b")

        result = await module.list_providers(ListProvidersParams())

        assert result.success
        assert result.data["count"] == 2
        ids = {p["provider_id"] for p in result.data["providers"]}
        assert ids == {"brain_a", "brain_b"}


# ---------------------------------------------------------------------------
# Tests - get_provider_info action
# ---------------------------------------------------------------------------


class TestGetProviderInfo:

    @pytest.mark.asyncio
    async def test_get_info_success(self):
        module = _make_module()
        fake = _inject_fake(module, "brain")
        fake.default_params = {"temperature": 0.5}

        params = GetProviderInfoParams(provider_id="brain")
        result = await module.get_provider_info(params)

        assert result.success
        assert result.data["model"] == "fake-model"
        assert result.data["default_params"]["temperature"] == 0.5
        assert result.data["capabilities"]["streaming"] is True

    @pytest.mark.asyncio
    async def test_get_info_not_found(self):
        module = _make_module()

        params = GetProviderInfoParams(provider_id="nope")
        result = await module.get_provider_info(params)

        assert not result.success


# ---------------------------------------------------------------------------
# Tests - update_defaults action
# ---------------------------------------------------------------------------


class TestUpdateDefaults:

    @pytest.mark.asyncio
    async def test_update_temperature(self):
        module = _make_module()
        _inject_fake(module, "brain")

        params = UpdateDefaultsParams(provider_id="brain", temperature=0.9)
        result = await module.update_defaults(params)

        assert result.success
        assert result.data["default_params"]["temperature"] == 0.9

    @pytest.mark.asyncio
    async def test_update_multiple(self):
        module = _make_module()
        _inject_fake(module, "brain")

        params = UpdateDefaultsParams(
            provider_id="brain",
            temperature=0.5,
            max_tokens=2048,
            top_p=0.95,
            extra={"frequency_penalty": 0.2},
        )
        result = await module.update_defaults(params)

        assert result.success
        dp = result.data["default_params"]
        assert dp["temperature"] == 0.5
        assert dp["max_tokens"] == 2048
        assert dp["top_p"] == 0.95
        assert dp["frequency_penalty"] == 0.2

    @pytest.mark.asyncio
    async def test_update_not_found(self):
        module = _make_module()

        params = UpdateDefaultsParams(provider_id="nope", temperature=0.5)
        result = await module.update_defaults(params)

        assert not result.success


# ---------------------------------------------------------------------------
# Tests - _create_provider dispatch
# ---------------------------------------------------------------------------


class TestCreateProvider:

    def test_anthropic_backend(self):
        module = _make_module()
        with patch("digitorn.modules.llm_provider.providers.anthropic.AnthropicProvider") as mock:
            mock.return_value = MagicMock()
            provider = module._create_provider(
                provider_id="test",
                backend="anthropic",
                model="claude-sonnet-4-20250514",
            )
            mock.assert_called_once()

    def test_openai_compat_backend(self):
        module = _make_module()
        with patch("digitorn.modules.llm_provider.providers.openai_compat.OpenAICompatProvider") as mock:
            mock.return_value = MagicMock()
            provider = module._create_provider(
                provider_id="test",
                backend="openai_compat",
                model="gpt-4o",
                provider_hint="openai",
            )
            mock.assert_called_once()

    def test_unknown_backend_raises(self):
        module = _make_module()
        with pytest.raises(ValueError, match="Unknown backend"):
            module._create_provider(
                provider_id="test",
                backend="unknown",
                model="whatever",
            )


# ---------------------------------------------------------------------------
# Tests - _configure_from_dict
# ---------------------------------------------------------------------------


class TestConfigureFromDict:

    @pytest.mark.asyncio
    async def test_extra_keys_become_default_params(self):
        module = _make_module()

        created_providers = {}

        def fake_create(provider_id, backend, model, **kw):
            f = FakeProvider(provider_id, model, **kw)
            created_providers[provider_id] = f
            return f

        module._create_provider = fake_create

        await module._configure_from_dict("brain", {
            "backend": "openai_compat",
            "model": "deepseek-chat",
            "provider_hint": "deepseek",
            "temperature": 0.2,
            "max_tokens": 8192,
        })

        provider = module._providers["brain"]
        assert provider.default_params.get("temperature") == 0.2
        assert provider.default_params.get("max_tokens") == 8192

    @pytest.mark.asyncio
    async def test_provider_key_as_hint(self):
        """The 'provider' key (from brain config) is treated as provider_hint."""
        module = _make_module()

        hints = {}

        def fake_create(provider_id, backend, model, **kw):
            hints[provider_id] = kw.get("provider_hint")
            return FakeProvider(provider_id, model, **kw)

        module._create_provider = fake_create

        await module._configure_from_dict("brain", {
            "provider": "deepseek",
            "model": "deepseek-chat",
        })

        assert hints["brain"] == "deepseek"


# ---------------------------------------------------------------------------
# Tests - OpenAI compat URL resolution
# ---------------------------------------------------------------------------


class TestOpenAICompatURLResolution:

    def test_known_providers(self):
        from digitorn.modules.llm_provider.providers.openai_compat import resolve_base_url

        assert "deepseek" in resolve_base_url("deepseek", None)
        assert "groq" in resolve_base_url("groq", None)
        assert "localhost:11434" in resolve_base_url("ollama", None)
        assert "mistral" in resolve_base_url("mistral", None)
        assert "together" in resolve_base_url("together", None)

    def test_explicit_url_wins(self):
        from digitorn.modules.llm_provider.providers.openai_compat import resolve_base_url

        assert resolve_base_url("deepseek", "http://custom:8000/v1") == "http://custom:8000/v1"

    def test_unknown_hint_defaults_to_openai(self):
        from digitorn.modules.llm_provider.providers.openai_compat import resolve_base_url

        assert "openai" in resolve_base_url("unknown_provider", None)


class TestOpenAICompatResponsesAPI:

    def test_detects_openai_responses_only_models(self):
        from digitorn.modules.llm_provider.providers.openai_compat import _uses_openai_responses_api

        assert _uses_openai_responses_api("openai", "https://api.openai.com/v1", "gpt-5.1-codex-mini") is True
        assert _uses_openai_responses_api("openai", "https://api.openai.com/v1", "gpt-5") is True
        assert _uses_openai_responses_api("openai", "https://api.openai.com/v1", "gpt-4.1-mini") is False
        assert _uses_openai_responses_api("groq", "https://api.groq.com/openai/v1", "gpt-5.1-codex-mini") is False

    def test_converts_messages_to_responses_input(self):
        from digitorn.modules.llm_provider.providers.base import ChatMessage
        from digitorn.modules.llm_provider.providers.openai_compat import _convert_messages_to_responses_input

        instructions, items = _convert_messages_to_responses_input([
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Read foo.py"),
            ChatMessage(
                role="assistant",
                content="I'll inspect the file.",
                tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "filesystem.read", "arguments": "{\"path\":\"foo.py\"}"},
                }],
            ),
            ChatMessage(role="tool", content="{\"ok\":true}", tool_call_id="call_1"),
        ])

        assert instructions == "You are helpful."
        assert items[0]["role"] == "user"
        assert items[1]["role"] == "assistant"
        assert items[2]["type"] == "function_call"
        assert items[2]["call_id"] == "call_1"
        assert items[3]["type"] == "function_call_output"
        assert items[3]["call_id"] == "call_1"


# ---------------------------------------------------------------------------
# Tests - Anthropic capabilities
# ---------------------------------------------------------------------------


class TestAnthropicCapabilities:

    def test_known_model(self):
        from digitorn.modules.llm_provider.providers.anthropic import _capabilities_for

        caps = _capabilities_for("claude-opus-4-20250514")
        assert caps.max_context_window == 200_000
        assert caps.tool_use is True

    def test_prefix_match(self):
        from digitorn.modules.llm_provider.providers.anthropic import _capabilities_for

        caps = _capabilities_for("claude-opus-4")
        assert caps.max_context_window == 200_000

    def test_unknown_model_defaults(self):
        from digitorn.modules.llm_provider.providers.anthropic import _capabilities_for

        caps = _capabilities_for("some-unknown-model")
        assert caps.streaming is True
        assert caps.max_context_window == 200_000


# ---------------------------------------------------------------------------
# Tests - Manifest
# ---------------------------------------------------------------------------


class TestManifest:

    def test_manifest_has_all_actions(self):
        module = _make_module()
        manifest = module.get_manifest()

        action_names = {a.name for a in manifest.actions}
        assert action_names == {
            "configure", "chat", "remove",
            "list_providers", "get_provider_info", "update_defaults",
        }

    def test_manifest_metadata(self):
        module = _make_module()
        manifest = module.get_manifest()

        assert "llm" in manifest.tags
        assert manifest.author == "Digitorn Team"
