"""End-to-end tests for the middleware pipeline.

Tests the complete flow: YAML with middleware → compile → bootstrap → agent turn.
Uses a mock LLM provider to avoid API dependencies.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from digitorn.modules.llm_provider.providers.base import (
    ChatMessage,
    ChatResponse,
    ProviderCapabilities,
    ProviderInfo,
    TokenUsage,
)


# ── Mock LLM ────────────────────────────────────────────────────────


class MockLLMProvider:
    def __init__(self, responses=None):
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

    async def initialize(self):
        pass

    async def chat(self, messages, **kwargs):
        self.call_log.append({
            "messages": messages,
            "system_prompt": messages[0].content if messages and messages[0].role == "system" else "",
            "user_messages": [m.content for m in messages if m.role == "user"],
        })
        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
            self._call_index += 1
            return resp
        return ChatResponse(
            content="Mock response.",
            model="mock-model",
            finish_reason="end_turn",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            tool_calls=None,
            raw={},
        )

    def get_info(self):
        return ProviderInfo(
            provider_id=self.provider_id,
            backend="mock",
            model=self.model,
            capabilities=ProviderCapabilities(tool_use=True),
            extra={},
        )

    async def close(self):
        pass


def _text_response(text):
    return ChatResponse(
        content=text,
        model="mock-model",
        finish_reason="end_turn",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        tool_calls=None,
        raw={},
    )


def _write_yaml(tmp_path, content):
    p = tmp_path / "app.yaml"
    p.write_text(textwrap.dedent(content))
    return p


async def _compile_and_run(yaml_path, mock_provider, message="hello"):
    """Compile, bootstrap, and run a single agent turn."""
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.agent_loop import agent_turn
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=mock_provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    ctx = boot_result["contexts"]["assistant"]

    # Build messages
    messages = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": message},
    ]

    result = await agent_turn(ctx, messages, max_turns=5, timeout=10.0)
    return result, ctx, mock_provider


# ═══════════════════════════════════════════════════════════════════════
# E2E Tests
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_content_filter_blocks_dangerous_input(tmp_path):
    """Content filter middleware short-circuits on dangerous patterns."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: filter-test
          name: "Filter Test"

        middleware:
          - content_filter:
              block_patterns: ["DROP TABLE", "rm -rf"]
              rejection_message: "BLOCKED by middleware."

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    provider = MockLLMProvider()
    result, ctx, _ = await _compile_and_run(
        yaml_path, provider, message="Please run DROP TABLE users;"
    )

    # The middleware should short-circuit — no LLM call
    assert result.content == "BLOCKED by middleware."
    assert len(provider.call_log) == 0  # LLM was never called


@pytest.mark.asyncio
async def test_content_filter_allows_safe_input(tmp_path):
    """Content filter passes through safe messages."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: filter-pass-test
          name: "Filter Pass Test"

        middleware:
          - content_filter:
              block_patterns: ["DROP TABLE"]

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    provider = MockLLMProvider([_text_response("Safe response!")])
    result, ctx, _ = await _compile_and_run(
        yaml_path, provider, message="What is Python?"
    )

    assert result.content == "Safe response!"
    assert len(provider.call_log) == 1  # LLM was called


@pytest.mark.asyncio
async def test_prompt_inject_modifies_system_prompt(tmp_path):
    """Prompt inject middleware appends to system prompt."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: inject-test
          name: "Inject Test"

        middleware:
          - prompt_inject:
              system: "INJECTED RULE: Always say bonjour."
              position: append

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    provider = MockLLMProvider([_text_response("Bonjour!")])
    result, ctx, _ = await _compile_and_run(yaml_path, provider, message="Hi")

    # Verify the LLM received the injected system prompt
    assert len(provider.call_log) == 1
    system_prompt = provider.call_log[0]["system_prompt"]
    assert "INJECTED RULE: Always say bonjour." in system_prompt
    assert "You are helpful." in system_prompt


@pytest.mark.asyncio
async def test_mask_secrets_in_messages(tmp_path):
    """Secret mask middleware removes secrets from user messages."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: mask-test
          name: "Mask Test"

        middleware:
          - mask_secrets: {}

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    provider = MockLLMProvider([_text_response("OK, I won't use your key.")])
    result, ctx, _ = await _compile_and_run(
        yaml_path, provider,
        message="My password=SuperSecret123 and api_key=sk-abc123def456ghi789jkl012",
    )

    # Verify the LLM did NOT see the raw secrets
    assert len(provider.call_log) == 1
    user_msgs = provider.call_log[0]["user_messages"]
    user_text = " ".join(user_msgs)
    assert "SuperSecret123" not in user_text
    assert "sk-abc123def456ghi789jkl012" not in user_text


@pytest.mark.asyncio
async def test_response_filter_truncates(tmp_path):
    """Response filter middleware truncates long responses."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: truncate-test
          name: "Truncate Test"

        middleware:
          - response_filter:
              max_length: 50

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    long_response = "A" * 200
    provider = MockLLMProvider([_text_response(long_response)])
    result, ctx, _ = await _compile_and_run(yaml_path, provider, message="Tell me a lot")

    # Response should be truncated
    assert len(result.content) < 200
    assert "[Response truncated]" in result.content


@pytest.mark.asyncio
async def test_multiple_middlewares_compose(tmp_path):
    """Multiple middlewares compose in order."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: compose-test
          name: "Compose Test"

        middleware:
          - mask_secrets: {}
          - content_filter:
              block_patterns: ["EVIL"]
          - prompt_inject:
              system: "Be kind."

        modules:
          hello: {}

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "Base prompt."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    # Test 1: blocked by content filter
    provider1 = MockLLMProvider()
    result1, _, _ = await _compile_and_run(
        yaml_path, provider1, message="Do EVIL things"
    )
    assert "blocked" in result1.content.lower()
    assert len(provider1.call_log) == 0

    # Test 2: safe message goes through with injected prompt
    provider2 = MockLLMProvider([_text_response("Kind response!")])
    result2, _, _ = await _compile_and_run(
        yaml_path, provider2, message="Be nice with password=secret123"
    )
    assert result2.content == "Kind response!"
    # Secrets masked
    user_text = " ".join(provider2.call_log[0]["user_messages"])
    assert "secret123" not in user_text
    # Prompt injected
    assert "Be kind." in provider2.call_log[0]["system_prompt"]


@pytest.mark.asyncio
async def test_module_middleware_audit(tmp_path):
    """Module-level audit middleware wraps module execution."""
    yaml_path = _write_yaml(tmp_path, """
        app:
          app_id: mod-audit-test
          name: "Module Audit Test"

        modules:
          hello:
            middleware:
              - audit:
                  log_params: true

        agents:
          - id: assistant
            role: assistant
            brain:
              provider: mock
              model: mock-model
              backend: openai_compat
            system_prompt: "You are helpful."

        execution:
          mode: one_shot

        capabilities:
          default_policy: auto
    """)

    # The hello module should have a middleware pipeline attached
    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.runtime.bootstrap import bootstrap
    from digitorn.modules.registry import ModuleRegistry

    registry = ModuleRegistry()
    load_modules(registry, load_all=True)

    compiler = AppYAMLCompiler(registry)
    compiled = compiler.compile_file(yaml_path)

    provider = MockLLMProvider()
    with patch(
        "digitorn.core.runtime.bootstrap._resolve_provider",
        return_value=provider,
    ):
        boot_result = await bootstrap(compiled, registry)

    modules = boot_result["modules"]
    hello_module = modules.get("hello")
    assert hello_module is not None
    assert hello_module._middleware_pipeline is not None
    assert len(hello_module._middleware_pipeline.middlewares) == 1


@pytest.mark.asyncio
async def test_middleware_registry_integration(tmp_path):
    """Middleware resolved via TOML registry (not hardcoded fallback)."""
    from digitorn.core.middleware_store import get_middleware_registry

    registry = get_middleware_registry()
    registry.discover()

    # Verify all 11 built-ins are loadable
    for desc in registry.list_all():
        instance = registry.instantiate(desc.middleware_id)
        assert instance is not None, f"Failed to instantiate: {desc.middleware_id}"
        # Verify it has the right interface
        if desc.level == "app":
            assert hasattr(instance, "before"), f"{desc.middleware_id} missing before()"
            assert hasattr(instance, "after"), f"{desc.middleware_id} missing after()"
        else:
            assert callable(instance), f"{desc.middleware_id} not callable"


@pytest.mark.asyncio
async def test_scaffold_install_use_uninstall(tmp_path):
    """Full lifecycle: create scaffold → install → discover → uninstall."""
    from digitorn.core.middleware_store import (
        create_middleware_scaffold,
        get_middleware_registry,
        install_middleware,
        uninstall_middleware,
    )

    # 1. Create scaffold
    scaffold_dir = create_middleware_scaffold(
        "e2e_test_mw", level="app", output_dir=tmp_path,
    )
    assert (scaffold_dir / "digitorn-middleware.toml").exists()
    assert (scaffold_dir / "middleware.py").exists()

    # 2. Install
    desc = install_middleware(scaffold_dir)
    assert desc.middleware_id == "e2e_test_mw"

    # 3. Discover and verify
    reg = get_middleware_registry()
    reg.discover()
    found = reg.get_descriptor("e2e_test_mw")
    assert found is not None
    assert found.source == "user"

    # 4. Instantiate
    instance = reg.instantiate("e2e_test_mw")
    assert instance is not None
    assert hasattr(instance, "before")

    # 5. Uninstall
    removed = uninstall_middleware("e2e_test_mw")
    assert removed is True

    # 6. No longer discoverable
    reg2 = get_middleware_registry()
    reg2.discover()
    # After re-discover, the uninstalled middleware should be gone
    # (but we need a fresh registry since _descriptors are cached)
    from digitorn.core.middleware_store import MiddlewareRegistry
    fresh = MiddlewareRegistry()
    fresh.discover()
    assert fresh.get_descriptor("e2e_test_mw") is None
