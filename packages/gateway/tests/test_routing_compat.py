"""Real tests for ``_litellm_model_from_compat``.

Wrong prefix = wrong upstream call = wasted money / 404. This is the
table-driven contract that EVERY new compat dialect must comply with.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def fn():
    from digitorn_gateway.llm_call import _litellm_model_from_compat
    return _litellm_model_from_compat


@pytest.mark.parametrize("compat,slug,real,want", [
    # OpenAI itself: bare model id (no prefix).
    ("openai", "openai", "gpt-4o", "gpt-4o"),
    ("openai", "openai", "gpt-4o-mini", "gpt-4o-mini"),
    # Anthropic-shape: bare (LiteLLM routes via SDK).
    ("anthropic", "anthropic", "claude-sonnet-4", "claude-sonnet-4"),
    ("anthropic", "claude_code", "claude-haiku-4-5", "claude-haiku-4-5"),
    # Bedrock: ``bedrock/<model>``.
    ("bedrock", "aws_bedrock", "anthropic.claude-haiku-3-5",
     "bedrock/anthropic.claude-haiku-3-5"),
    # Vertex AI: ``vertex_ai/<model>``.
    ("vertex_ai", "vertex_ai", "claude-sonnet-4@anthropic",
     "vertex_ai/claude-sonnet-4@anthropic"),
    # Azure: ``azure/<deployment>``.
    ("azure", "azure_openai", "gpt-4o", "azure/gpt-4o"),
    # OpenAI-compat: routed via ``openai/<model>`` + base_url override.
    ("openai_compat", "together_ai", "meta-llama/Llama-3.3",
     "openai/meta-llama/Llama-3.3"),
    ("openai_compat", "fireworks_ai", "accounts/x/m",
     "openai/accounts/x/m"),
    ("openai_compat", "github_copilot", "gpt-4o", "openai/gpt-4o"),
    # Anything else (deepseek, gemini, mistral, ...) -> ``<slug>/<model>``.
    ("openai", "deepseek", "deepseek-chat", "deepseek/deepseek-chat"),
    ("openai", "gemini", "gemini-2.0-flash", "gemini/gemini-2.0-flash"),
    ("openai", "mistral", "mistral-large", "mistral/mistral-large"),
])
def test_compat_truth_table(fn, compat, slug, real, want):
    assert fn(compat, slug, real) == want


def test_unknown_compat_falls_through_to_slug(fn):
    """A new dialect we haven't taught yet must default to ``slug/model``
    (the legacy LiteLLM convention) so dispatch keeps working without
    a code release."""
    assert fn("future_dialect", "newprovider", "model-v9") == \
        "newprovider/model-v9"


def test_provider_from_model_inference():
    """``_provider_from_model`` is best-effort attribution for usage
    tracking. Wrong attribution = wrong cost lookup."""
    from digitorn_gateway.llm_call import _provider_from_model
    assert _provider_from_model("anthropic/claude-sonnet-4") == "anthropic"
    assert _provider_from_model("openai/gpt-4o") == "openai"
    assert _provider_from_model("claude-sonnet-4") == "anthropic"
    assert _provider_from_model("gpt-4o") == "openai"
    assert _provider_from_model("o1-preview") == "openai"
    assert _provider_from_model("gemini-2.0-flash") == "gemini"
    assert _provider_from_model("totally-unknown-name") == "unknown"


def test_failover_eligibility():
    """The dispatch path retries on retriable errors. 400-class errors
    must NOT failover (the request is broken, every provider will reject)."""
    from digitorn_gateway.llm_call import _is_failover_eligible

    class _Exc(Exception):
        def __init__(self, msg, status=None):
            super().__init__(msg)
            self.status_code = status

    # 400 = malformed request, no retry.
    assert _is_failover_eligible(_Exc("bad request", 400)) is False

    # Context window overflow: same body fails on every provider.
    class _CtxExc(Exception):
        pass
    _CtxExc.__name__ = "ContextWindowExceededError"
    assert _is_failover_eligible(_CtxExc("too long")) is False

    # BadRequestError class name -> no retry.
    class _BR(Exception):
        pass
    _BR.__name__ = "BadRequestError"
    assert _is_failover_eligible(_BR("nope")) is False

    # 401 / 429 / 500 / connection -> retry next route.
    assert _is_failover_eligible(_Exc("auth", 401)) is True
    assert _is_failover_eligible(_Exc("rate", 429)) is True
    assert _is_failover_eligible(_Exc("internal", 500)) is True
    assert _is_failover_eligible(_Exc("timeout", None)) is True
