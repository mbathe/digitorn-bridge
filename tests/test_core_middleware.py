"""Tests for the core middleware system (app-level + module-level)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from digitorn.core.middleware import (
    AppMiddlewareContext,
    AppMiddlewarePipeline,
    ContentFilterMiddleware,
    ModuleAuditMiddleware,
    ModuleCallContext,
    ModuleMiddlewarePipeline,
    ModuleRetryMiddleware,
    ModuleTimeoutMiddleware,
    PromptInjectMiddleware,
    RagInjectMiddleware,
    ResponseFilterMiddleware,
    SecretMaskMiddleware,
    build_app_pipeline,
    build_module_pipeline,
)


# ═══════════════════════════════════════════════════════════════════════
# App-level middleware tests
# ═══════════════════════════════════════════════════════════════════════


def _make_ctx(
    messages: list[dict[str, Any]] | None = None,
    system_prompt: str = "You are a helpful assistant.",
) -> AppMiddlewareContext:
    return AppMiddlewareContext(
        agent_id="test",
        system_prompt=system_prompt,
        messages=messages or [{"role": "user", "content": "hello"}],
        turn=0,
    )


# ── SecretMaskMiddleware ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_secret_mask_password():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "My password=SuperSecret123 is safe"},
    ])
    result = await mw.before(ctx)
    assert result is None  # no short-circuit
    assert "SuperSecret123" not in ctx.messages[0]["content"]
    assert "[MASKED]" in ctx.messages[0]["content"]


@pytest.mark.asyncio
async def test_secret_mask_api_key():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "Use api_key=sk-abc123def456 for auth"},
    ])
    await mw.before(ctx)
    assert "sk-abc123def456" not in ctx.messages[0]["content"]


@pytest.mark.asyncio
async def test_secret_mask_openai_key():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "key: sk-abcdefghijklmnopqrstuvwx"},
    ])
    await mw.before(ctx)
    assert "sk-abcdefghijklmnopqrstuvwx" not in ctx.messages[0]["content"]


@pytest.mark.asyncio
async def test_secret_mask_github_pat():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"},
    ])
    await mw.before(ctx)
    assert "ghp_ABCDEFGHIJ" not in ctx.messages[0]["content"]


@pytest.mark.asyncio
async def test_secret_mask_custom_patterns():
    mw = SecretMaskMiddleware(patterns=["my_custom_key"])
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "my_custom_key=sensitive_value here"},
    ])
    await mw.before(ctx)
    assert "sensitive_value" not in ctx.messages[0]["content"]


@pytest.mark.asyncio
async def test_secret_mask_no_change():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "No secrets here, just normal text"},
    ])
    await mw.before(ctx)
    assert ctx.messages[0]["content"] == "No secrets here, just normal text"
    assert mw.stats["masked_messages"] == 0


@pytest.mark.asyncio
async def test_secret_mask_skips_non_user():
    mw = SecretMaskMiddleware()
    ctx = _make_ctx(messages=[
        {"role": "assistant", "content": "password=leaked"},
        {"role": "user", "content": "hello"},
    ])
    await mw.before(ctx)
    # Assistant messages should NOT be masked
    assert "password=leaked" in ctx.messages[0]["content"]


# ── PromptInjectMiddleware ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_inject_append():
    mw = PromptInjectMiddleware(system="Always respond in French.")
    ctx = _make_ctx(system_prompt="You are helpful.")
    await mw.before(ctx)
    assert ctx.system_prompt.endswith("Always respond in French.")
    assert ctx.system_prompt.startswith("You are helpful.")


@pytest.mark.asyncio
async def test_prompt_inject_prepend():
    mw = PromptInjectMiddleware(system="IMPORTANT:", position="prepend")
    ctx = _make_ctx(system_prompt="You are helpful.")
    await mw.before(ctx)
    assert ctx.system_prompt.startswith("IMPORTANT:")
    assert "You are helpful." in ctx.system_prompt


@pytest.mark.asyncio
async def test_prompt_inject_empty():
    mw = PromptInjectMiddleware(system="")
    ctx = _make_ctx(system_prompt="Original.")
    await mw.before(ctx)
    assert ctx.system_prompt == "Original."


# ── ContentFilterMiddleware ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_content_filter_blocks():
    mw = ContentFilterMiddleware(block_patterns=["DROP TABLE", "rm -rf"])
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "Please run DROP TABLE users;"},
    ])
    result = await mw.before(ctx)
    assert result is not None  # short-circuit
    assert "blocked" in result.lower()
    assert mw.stats["blocked_count"] == 1


@pytest.mark.asyncio
async def test_content_filter_allows():
    mw = ContentFilterMiddleware(block_patterns=["DROP TABLE"])
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "SELECT * FROM users WHERE id = 1"},
    ])
    result = await mw.before(ctx)
    assert result is None  # no block


@pytest.mark.asyncio
async def test_content_filter_case_insensitive():
    mw = ContentFilterMiddleware(block_patterns=["drop table"])
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "DROP TABLE users;"},
    ])
    result = await mw.before(ctx)
    assert result is not None  # blocked


@pytest.mark.asyncio
async def test_content_filter_custom_message():
    mw = ContentFilterMiddleware(
        block_patterns=["danger"],
        rejection_message="Nope!",
    )
    ctx = _make_ctx(messages=[
        {"role": "user", "content": "danger zone"},
    ])
    result = await mw.before(ctx)
    assert result == "Nope!"


# ── RagInjectMiddleware ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_inject_with_retriever():
    async def retriever(query: str) -> list[str]:
        return [f"Chunk about: {query}", "Second chunk"]

    mw = RagInjectMiddleware(max_chunks=5)
    mw.set_retriever(retriever)
    ctx = _make_ctx(
        messages=[{"role": "user", "content": "What is digitorn?"}],
        system_prompt="You are helpful.",
    )
    await mw.before(ctx)
    assert "Relevant context" in ctx.system_prompt
    assert "Chunk about: What is digitorn?" in ctx.system_prompt
    assert mw.stats["inject_count"] == 1


@pytest.mark.asyncio
async def test_rag_inject_no_retriever():
    mw = RagInjectMiddleware()
    ctx = _make_ctx(system_prompt="Original.")
    await mw.before(ctx)
    assert ctx.system_prompt == "Original."


@pytest.mark.asyncio
async def test_rag_inject_truncation():
    async def retriever(query: str) -> list[str]:
        return ["x" * 500, "y" * 500, "z" * 500]

    mw = RagInjectMiddleware(max_chars=100)
    mw.set_retriever(retriever)
    ctx = _make_ctx(messages=[{"role": "user", "content": "query"}])
    await mw.before(ctx)
    # Should only include partial first chunk
    assert "Relevant context" in ctx.system_prompt


# ── ResponseFilterMiddleware ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_response_filter_max_length():
    mw = ResponseFilterMiddleware(max_length=50)
    ctx = _make_ctx()
    result = await mw.after(ctx, "x" * 100, [])
    assert len(result) <= 75  # 50 + "\n\n[Response truncated]"
    assert "truncated" in result


@pytest.mark.asyncio
async def test_response_filter_mask_secrets():
    mw = ResponseFilterMiddleware(mask_secrets=True)
    ctx = _make_ctx()
    result = await mw.after(ctx, "Your password=secret123 is set", [])
    assert "secret123" not in result


# ── AppMiddlewarePipeline ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_pipeline_before_order():
    """Middlewares run in order; first short-circuit wins."""
    mw1 = PromptInjectMiddleware(system="First")
    mw2 = PromptInjectMiddleware(system="Second")
    pipeline = AppMiddlewarePipeline([mw1, mw2])

    ctx = _make_ctx(system_prompt="Base.")
    result = await pipeline.run_before(ctx)
    assert result is None
    assert "First" in ctx.system_prompt
    assert "Second" in ctx.system_prompt


@pytest.mark.asyncio
async def test_app_pipeline_short_circuit():
    """Content filter short-circuits; later middlewares don't run."""
    filter_mw = ContentFilterMiddleware(block_patterns=["blocked"])
    inject_mw = PromptInjectMiddleware(system="Should not appear")
    pipeline = AppMiddlewarePipeline([filter_mw, inject_mw])

    ctx = _make_ctx(
        messages=[{"role": "user", "content": "this is blocked content"}],
        system_prompt="Base.",
    )
    result = await pipeline.run_before(ctx)
    assert result is not None  # short-circuited
    assert "Should not appear" not in ctx.system_prompt


@pytest.mark.asyncio
async def test_app_pipeline_after_reverse_order():
    """After hooks run in reverse order."""
    order: list[str] = []

    class TrackMW:
        def __init__(self, name: str):
            self.name = name
        async def before(self, ctx):
            return None
        async def after(self, ctx, response, tool_calls):
            order.append(self.name)
            return response

    pipeline = AppMiddlewarePipeline([TrackMW("a"), TrackMW("b"), TrackMW("c")])
    ctx = _make_ctx()
    await pipeline.run_after(ctx, "response", [])
    assert order == ["c", "b", "a"]


# ── build_app_pipeline factory ───────────────────────────────────────


def test_build_app_pipeline_none():
    assert build_app_pipeline(None) is None
    assert build_app_pipeline([]) is None


def test_build_app_pipeline_basic():
    pipeline = build_app_pipeline([
        {"mask_secrets": {"patterns": ["custom"]}},
        {"prompt_inject": {"system": "Be nice."}},
        {"content_filter": {"block_patterns": ["evil"]}},
    ])
    assert pipeline is not None
    assert len(pipeline.middlewares) == 3


def test_build_app_pipeline_string_shorthand():
    pipeline = build_app_pipeline(["mask_secrets", "response_filter"])
    assert pipeline is not None
    assert len(pipeline.middlewares) == 2


def test_build_app_pipeline_unknown():
    pipeline = build_app_pipeline([{"unknown_mw": {}}])
    assert pipeline is None


# ═══════════════════════════════════════════════════════════════════════
# Module-level middleware tests
# ═══════════════════════════════════════════════════════════════════════


async def _fake_handler(action: str, params: Any) -> dict:
    return {"action": action, "result": "ok"}


# ── ModuleAuditMiddleware ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_module_audit():
    audit = ModuleAuditMiddleware(log_params=True)
    pipeline = ModuleMiddlewarePipeline([audit])

    result = await pipeline.execute("filesystem", "read", {"path": "/tmp"}, _fake_handler)
    assert result["result"] == "ok"
    assert audit.stats["total_calls"] == 1
    assert audit.stats["total_errors"] == 0


@pytest.mark.asyncio
async def test_module_audit_error():
    audit = ModuleAuditMiddleware()

    async def fail_handler(action, params):
        raise ValueError("boom")

    pipeline = ModuleMiddlewarePipeline([audit])
    with pytest.raises(ValueError):
        await pipeline.execute("db", "query", {}, fail_handler)
    assert audit.stats["total_errors"] == 1


# ── ModuleRetryMiddleware ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_module_retry_success():
    call_count = 0

    async def flaky(action, params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("transient")
        return {"ok": True}

    retry = ModuleRetryMiddleware(max_attempts=3, base_delay=0.01)
    pipeline = ModuleMiddlewarePipeline([retry])
    result = await pipeline.execute("mod", "act", {}, flaky)
    assert result["ok"] is True
    assert call_count == 2


@pytest.mark.asyncio
async def test_module_retry_exhausted():
    async def always_fail(action, params):
        raise ConnectionError("permanent")

    retry = ModuleRetryMiddleware(max_attempts=2, base_delay=0.01)
    pipeline = ModuleMiddlewarePipeline([retry])
    with pytest.raises(ConnectionError):
        await pipeline.execute("mod", "act", {}, always_fail)


# ── ModuleTimeoutMiddleware ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_module_timeout_ok():
    timeout = ModuleTimeoutMiddleware(seconds=5.0)
    pipeline = ModuleMiddlewarePipeline([timeout])
    result = await pipeline.execute("mod", "act", {}, _fake_handler)
    assert result["result"] == "ok"


@pytest.mark.asyncio
async def test_module_timeout_exceeded():
    async def slow(action, params):
        await asyncio.sleep(5)
        return {}

    timeout = ModuleTimeoutMiddleware(seconds=0.05)
    pipeline = ModuleMiddlewarePipeline([timeout])
    with pytest.raises(asyncio.TimeoutError):
        await pipeline.execute("mod", "act", {}, slow)


# ── ModuleMiddlewarePipeline composition ─────────────────────────────


@pytest.mark.asyncio
async def test_module_pipeline_order():
    """Middlewares wrap in declared order."""
    order: list[str] = []

    class TrackMW:
        def __init__(self, name):
            self.name = name
        async def __call__(self, ctx, next_):
            order.append(f"{self.name}_before")
            result = await next_(ctx)
            order.append(f"{self.name}_after")
            return result

    pipeline = ModuleMiddlewarePipeline([TrackMW("a"), TrackMW("b")])
    await pipeline.execute("mod", "act", {}, _fake_handler)
    assert order == ["a_before", "b_before", "b_after", "a_after"]


# ── build_module_pipeline factory ────────────────────────────────────


def test_build_module_pipeline_none():
    assert build_module_pipeline(None) is None
    assert build_module_pipeline([]) is None


def test_build_module_pipeline_basic():
    pipeline = build_module_pipeline([
        {"audit": {"log_params": True}},
        {"retry": {"max_attempts": 5}},
        {"timeout": {"seconds": 10}},
    ])
    assert pipeline is not None
    assert len(pipeline.middlewares) == 3


def test_build_module_pipeline_string_shorthand():
    pipeline = build_module_pipeline(["audit", "timeout"])
    assert pipeline is not None
    assert len(pipeline.middlewares) == 2
