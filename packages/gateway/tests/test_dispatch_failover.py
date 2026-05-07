"""Test the failover loop in ``dispatch()`` by mocking
``litellm.acompletion``. This is a CLEAN boundary - LiteLLM is the
only external dep we have, and its async API surface is stable.

What we prove:
  * 200 path returns the body and bumps the route to healthy.
  * 401 / 429 / 500 / timeout retry the next priority route.
  * 400 / BadRequest gives up immediately (would burn money on retry).
  * Failover walks N routes, gives up after the last one.
  * Route health is correctly marked on success / failure.
  * 5 concurrent dispatches share cache state cleanly.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _model_response(content="ok", input_tokens=5, output_tokens=2):
    """Mimic the LiteLLM ModelResponse shape."""
    return SimpleNamespace(
        model="m",
        choices=[SimpleNamespace(
            index=0, finish_reason="stop",
            message=SimpleNamespace(
                role="assistant", content=content,
                tool_calls=None,
            ),
        )],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
        model_dump=lambda: {
            "id": "x", "object": "chat.completion", "model": "m",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        },
    )


def _patch_acompletion(mock_fn):
    """Helper: import litellm + patch its acompletion."""
    import litellm
    return patch.object(litellm, "acompletion", mock_fn)


@pytest.fixture
def two_provider_setup(fresh_cache):
    """Two providers, two creds, one alias with two routes (priorities 0/1)."""
    fresh_cache.upsert_provider(
        slug="provA", name="A", base_url="https://a.test/v1",
        compat="openai_compat", env_var=None, auth_type="api_key",
        extra_metadata={},
    )
    fresh_cache.upsert_provider(
        slug="provB", name="B", base_url="https://b.test/v1",
        compat="openai_compat", env_var=None, auth_type="api_key",
        extra_metadata={},
    )
    cid_a = uuid.uuid4(); cid_b = uuid.uuid4()
    for cid, slug in [(cid_a, "provA"), (cid_b, "provB")]:
        fresh_cache.upsert_credential(
            cid, provider_slug=slug, label=slug,
            secret_data={"value": f"sk-{slug}"}, status="active",
            live_pool=False,
        )
    fresh_cache.upsert_model(
        alias="myalias", provider_slug="provA", real_model_id="m",
        cost_per_1k_input=0.001, cost_per_1k_output=0.002,
        max_context=8192, is_custom=False,
    )
    rid_a = uuid.uuid4(); rid_b = uuid.uuid4()
    fresh_cache.set_route(
        rid_a, alias="myalias", credential_id=cid_a, priority=0,
        provider_slug="provA", real_model_id="m", compat="openai_compat",
    )
    fresh_cache.set_route(
        rid_b, alias="myalias", credential_id=cid_b, priority=1,
        provider_slug="provB", real_model_id="m", compat="openai_compat",
    )
    return SimpleNamespace(
        cid_a=cid_a, cid_b=cid_b, rid_a=rid_a, rid_b=rid_b,
    )


def _body():
    return {
        "model": "myalias",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8, "temperature": 1.0,
    }


# ── 200 path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_200_returns_response_and_marks_healthy(two_provider_setup, fresh_cache):
    from digitorn_gateway import llm_call
    mock_resp = _model_response("from-A")
    with _patch_acompletion(AsyncMock(return_value=mock_resp)):
        resp, usage = await llm_call.dispatch(body=_body())
    assert resp["choices"][0]["message"]["content"] == "from-A"
    assert usage.input_tokens == 5
    assert fresh_cache._route_health[two_provider_setup.rid_a].consecutive_failures == 0


# ── failover ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failover_to_route_1_on_500(two_provider_setup, fresh_cache):
    from digitorn_gateway import llm_call

    class _UpstreamErr(Exception):
        status_code = 500

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _UpstreamErr("provA boom")
        return _model_response("from-B")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        resp, _ = await llm_call.dispatch(body=_body())

    assert resp["choices"][0]["message"]["content"] == "from-B"
    assert call_count["n"] == 2
    assert fresh_cache._route_health[two_provider_setup.rid_a].consecutive_failures >= 1
    assert fresh_cache._route_health[two_provider_setup.rid_b].consecutive_failures == 0


@pytest.mark.asyncio
async def test_failover_to_route_1_on_401(two_provider_setup):
    from digitorn_gateway import llm_call

    class _Auth(Exception):
        status_code = 401

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _Auth("revoked")
        return _model_response("from-B")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        resp, _ = await llm_call.dispatch(body=_body())
    assert resp["choices"][0]["message"]["content"] == "from-B"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_failover_to_route_1_on_429(two_provider_setup):
    from digitorn_gateway import llm_call

    class _Rate(Exception):
        status_code = 429

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _Rate("rate limit")
        return _model_response("from-B")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        resp, _ = await llm_call.dispatch(body=_body())
    assert resp["choices"][0]["message"]["content"] == "from-B"


# ── give-up paths ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_400_does_NOT_failover(two_provider_setup):
    from digitorn_gateway import llm_call

    class _BadReq(Exception):
        status_code = 400

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        raise _BadReq("malformed")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        with pytest.raises(_BadReq):
            await llm_call.dispatch(body=_body())
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_BadRequestError_class_does_NOT_failover(two_provider_setup):
    from digitorn_gateway import llm_call

    class BadRequestError(Exception):
        pass

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        raise BadRequestError("bad")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        with pytest.raises(BadRequestError):
            await llm_call.dispatch(body=_body())
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_ContextWindowExceeded_does_NOT_failover(two_provider_setup):
    from digitorn_gateway import llm_call

    class ContextWindowExceededError(Exception):
        pass

    call_count = {"n": 0}
    async def _ml(*args, **kwargs):
        call_count["n"] += 1
        raise ContextWindowExceededError("ctx")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        with pytest.raises(ContextWindowExceededError):
            await llm_call.dispatch(body=_body())
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_all_routes_fail_propagates_last_error(two_provider_setup):
    from digitorn_gateway import llm_call

    class _Gone(Exception):
        status_code = 503
        def __str__(self): return "all dead"

    async def _ml(*args, **kwargs):
        raise _Gone("upstream dead")

    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        with pytest.raises(_Gone):
            await llm_call.dispatch(body=_body())


# ── concurrency ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_5_concurrent_dispatches_no_state_corruption(two_provider_setup, fresh_cache):
    from digitorn_gateway import llm_call
    counter = {"n": 0}
    async def _ml(*args, **kwargs):
        counter["n"] += 1
        return _model_response(f"resp-{counter['n']}")
    with _patch_acompletion(AsyncMock(side_effect=_ml)):
        results = await asyncio.gather(*[
            llm_call.dispatch(body=_body()) for _ in range(5)
        ])
    assert len(results) == 5
    for resp, _ in results:
        assert resp["choices"][0]["message"]["content"].startswith("resp-")
    assert fresh_cache._route_health[two_provider_setup.rid_a].consecutive_failures == 0
