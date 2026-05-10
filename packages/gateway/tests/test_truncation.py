"""Auto-truncation safety net.

Two scopes covered:

  * Pure-function unit tests on ``truncation`` module: head_drop
    invariants (system always kept, last block always kept, tool
    pairs atomic), token counting fallback, max-context lookup.

  * Integration: Mode 1 (pre-flight reject 413) and Mode 2 (head_drop
    on cross-provider fallback) wired through ``dispatch`` /
    ``chat_completions``.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ── Pure-function unit tests ────────────────────────────────────────


def test_count_tokens_falls_back_when_litellm_fails():
    """If litellm.token_counter raises (network, broken cache, exotic
    shape), count_tokens MUST return the chars-per-token estimate
    instead of propagating - the gateway never crashes on tokenization."""
    from digitorn_gateway import truncation

    msgs = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "x" * 400},
    ]
    # Force the litellm path to raise so the fallback estimator runs.
    import litellm
    with patch.object(litellm, "token_counter", side_effect=RuntimeError("boom")):
        n = truncation.count_tokens("any-model", msgs)
    assert n > 0, "fallback estimator should never return 0 for non-empty input"
    # Estimator: (len(content)//4 + 8) per str message + 4 framing.
    # Loose bounds, just verify shape.
    assert 50 < n < 200


def test_check_overflow_unknown_max_returns_false():
    """An unknown context window MUST mean 'do not block' - safer to
    let the provider answer than to reject blindly."""
    from digitorn_gateway.truncation import check_overflow

    overflows, budget = check_overflow(1_000_000, max_context=None,
                                       max_output_tokens=512)
    assert overflows is False
    assert budget == 0


def test_check_overflow_subtracts_max_output_from_budget():
    from digitorn_gateway.truncation import check_overflow

    overflows, budget = check_overflow(
        request_tokens=10_000,
        max_context=8_192,
        max_output_tokens=1_024,
    )
    assert overflows is True
    assert budget == 8_192 - 1_024


def test_head_drop_keeps_system_and_last_block():
    """The dropping invariant: system messages are always preserved,
    the last user/assistant turn is always preserved."""
    from digitorn_gateway.truncation import head_drop

    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "msg-1 " * 200},
        {"role": "assistant", "content": "reply-1 " * 200},
        {"role": "user", "content": "msg-2 " * 200},
        {"role": "assistant", "content": "reply-2 " * 200},
        {"role": "user", "content": "the actual question now"},
    ]
    # Tight budget: only the last user message should fit besides system.
    kept, dropped = head_drop(msgs, max_input_tokens=120, model="gpt-4o-mini")
    assert kept[0]["role"] == "system", "system message must lead"
    assert kept[-1]["content"] == "the actual question now", (
        "last user message MUST be kept as the question"
    )
    assert dropped > 0, "expected some drops with a 120-token budget"


def test_head_drop_keeps_tool_pair_atomic():
    """An assistant message with tool_calls + its tool responses MUST
    be kept or dropped as a unit. Splitting the pair triggers
    invalid_request_error on every provider."""
    from digitorn_gateway.truncation import head_drop

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old user"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "function": {"name": "x"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "tool result"},
        {"role": "assistant", "content": "after the tool call"},
        {"role": "user", "content": "fresh question"},
    ]
    kept, _ = head_drop(msgs, max_input_tokens=80, model="gpt-4o-mini")
    # Walk kept and verify no orphan tool message appears WITHOUT its
    # parent assistant_with_tool_calls right before it.
    for i, m in enumerate(kept):
        if m.get("role") == "tool":
            prev = kept[i - 1] if i > 0 else None
            assert prev is not None and prev.get("tool_calls"), (
                f"orphan tool response at idx {i}: parent assistant "
                f"with tool_calls is missing. Kept = {kept}"
            )


def test_head_drop_handles_empty_or_system_only():
    from digitorn_gateway.truncation import head_drop

    assert head_drop([], 1000, "x") == ([], 0)

    only_sys = [{"role": "system", "content": "rules"}]
    out, dropped = head_drop(only_sys, 1000, "x")
    assert out == only_sys
    assert dropped == 0


def test_get_max_context_unknown_returns_none():
    from digitorn_gateway.truncation import get_max_context_for_model

    assert get_max_context_for_model("totally-fake-model-zzz-9999") is None
    assert get_max_context_for_model("") is None


def test_can_skip_tokenization_short_circuits_small_messages():
    """Hot-path safety guarantee: small messages must NEVER trigger
    real BPE tokenization. ``can_skip_tokenization`` is the byte-size
    pre-gate that proves overflow is impossible."""
    from digitorn_gateway.truncation import can_skip_tokenization

    small = [{"role": "user", "content": "Reply OK"}]
    assert can_skip_tokenization(small, max_context=128_000) is True


def test_can_skip_tokenization_aborts_on_huge_payload():
    from digitorn_gateway.truncation import can_skip_tokenization

    big = [{"role": "user", "content": "x" * 1_000_000}]
    assert can_skip_tokenization(big, max_context=128_000) is False


def test_can_skip_tokenization_unknown_max_skips():
    """When the model's max_context isn't known, return True so the
    caller doesn't waste a tokenizer call - dispatch will simply fall
    through to the upstream."""
    from digitorn_gateway.truncation import can_skip_tokenization

    assert can_skip_tokenization(
        [{"role": "user", "content": "test"}], max_context=None,
    ) is True


def test_get_max_context_known_returns_int():
    from digitorn_gateway.truncation import get_max_context_for_model

    # gpt-4o-mini exists in litellm.model_cost.
    v = get_max_context_for_model("gpt-4o-mini")
    assert v is not None and v > 1000, (
        f"expected a real context for gpt-4o-mini, got {v}"
    )


# ── Integration: Mode 2 wired through dispatch ───────────────────────


def _model_response(content="ok"):
    return SimpleNamespace(
        model="m",
        choices=[SimpleNamespace(
            index=0, finish_reason="stop",
            message=SimpleNamespace(
                role="assistant", content=content, tool_calls=None,
            ),
        )],
        usage=SimpleNamespace(
            prompt_tokens=5, completion_tokens=2, total_tokens=7,
        ),
        model_dump=lambda: {
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
            "usage": {
                "prompt_tokens": 5, "completion_tokens": 2,
                "total_tokens": 7,
            },
        },
    )


def _patch_acompletion(mock_fn):
    import litellm
    return patch.object(litellm, "acompletion", mock_fn)


@pytest.fixture
def small_context_route(fresh_cache):
    """Single route to a small-context model. Ensures Mode 2 will
    fire when the request is large."""
    fresh_cache.upsert_provider(
        slug="openai", name="O", base_url="https://api.openai.com/v1",
        compat="openai", env_var=None, auth_type="api_key",
        extra_metadata={},
    )
    fresh_cache.upsert_model(
        alias="small", provider_slug="openai",
        real_model_id="gpt-4o-mini",
        cost_per_1k_input=0.001, cost_per_1k_output=0.002,
        max_context=128_000, is_custom=False,
    )
    cid = uuid.uuid4(); rid = uuid.uuid4()
    fresh_cache.upsert_credential(
        cid, provider_slug="openai", label="o1",
        secret_data={"value": "sk-test"}, status="active", live_pool=False,
    )
    fresh_cache.set_route(
        rid, alias="small", credential_id=cid, priority=0,
        provider_slug="openai", real_model_id="gpt-4o-mini", compat="openai",
    )
    return SimpleNamespace(cred_id=cid, route_id=rid)


@pytest.mark.asyncio
async def test_truncate_disabled_does_not_modify_messages(
    small_context_route, fresh_cache,
):
    """Default path: ``truncate_enabled=False`` means head_drop is
    skipped entirely, the messages list reaches the upstream as-is."""
    from digitorn_gateway import llm_call
    from digitorn_gateway.config import (
        get_settings, override_settings, Settings,
    )

    seen_msgs: list = []

    async def _ml(*args, **kwargs):
        seen_msgs.append(list(kwargs.get("messages") or []))
        return _model_response("ok")

    long_msgs = (
        [{"role": "user", "content": "x" * 500_000}]
        + [{"role": "user", "content": "the real question"}]
    )
    body = {"model": "small", "messages": long_msgs, "max_tokens": 10}

    saved = get_settings()
    override_settings(Settings(truncate_enabled=False))
    try:
        with _patch_acompletion(AsyncMock(side_effect=_ml)):
            await llm_call.dispatch(body=body)
        # Messages reached upstream unchanged.
        assert len(seen_msgs[0]) == len(long_msgs)
    finally:
        override_settings(saved)


@pytest.mark.asyncio
async def test_truncate_enabled_drops_when_request_overflows_route(
    small_context_route, fresh_cache,
):
    """With Mode 2 on, a request that exceeds the route's context
    window gets head-dropped before the upstream call."""
    from digitorn_gateway import llm_call
    from digitorn_gateway.config import (
        get_settings, override_settings, Settings,
    )

    seen_msgs: list = []

    async def _ml(*args, **kwargs):
        seen_msgs.append(list(kwargs.get("messages") or []))
        return _model_response("ok")

    # gpt-4o-mini has 128k context; request 200k chars (~50k tokens
    # via litellm) is below; bulk it up to clearly overflow.
    chunk = "the quick brown fox jumps over the lazy dog. " * 100
    long_msgs = (
        [{"role": "user", "content": chunk * 50}] * 30
        + [{"role": "user", "content": "fresh question"}]
    )
    body = {"model": "small", "messages": long_msgs, "max_tokens": 100}

    trace = llm_call.DispatchTrace()
    saved = get_settings()
    override_settings(Settings(truncate_enabled=True))
    try:
        with _patch_acompletion(AsyncMock(side_effect=_ml)):
            await llm_call.dispatch(body=body, trace=trace)
        upstream_msgs = seen_msgs[0]
        assert len(upstream_msgs) < len(long_msgs), (
            f"expected head_drop to trim, got upstream={len(upstream_msgs)} "
            f"vs original={len(long_msgs)}"
        )
        assert upstream_msgs[-1]["content"] == "fresh question", (
            "last user message must survive the trim"
        )
        assert trace.truncated_dropped > 0
    finally:
        override_settings(saved)


@pytest.mark.asyncio
async def test_truncate_enabled_does_not_trim_small_request(
    small_context_route, fresh_cache,
):
    """A normal-sized request must NOT be trimmed even with the
    feature on - head_drop only fires when actually needed."""
    from digitorn_gateway import llm_call
    from digitorn_gateway.config import (
        get_settings, override_settings, Settings,
    )

    seen_msgs: list = []

    async def _ml(*args, **kwargs):
        seen_msgs.append(list(kwargs.get("messages") or []))
        return _model_response("ok")

    body = {
        "model": "small",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }

    trace = llm_call.DispatchTrace()
    saved = get_settings()
    override_settings(Settings(truncate_enabled=True))
    try:
        with _patch_acompletion(AsyncMock(side_effect=_ml)):
            await llm_call.dispatch(body=body, trace=trace)
        assert len(seen_msgs[0]) == 1
        assert trace.truncated_dropped == 0
    finally:
        override_settings(saved)
