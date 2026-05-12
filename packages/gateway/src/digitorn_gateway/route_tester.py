"""Per-route test runner.

Runs a tiny LLM dispatch through ONE specific route exactly the way
the production hot path would, returning rich diagnostic info. Unlike
``route_prober._probe_one`` which is optimised for the background
sweep, this one:

  * Honours every provider-specific kwarg (AWS Bedrock SigV4 keys,
    Vertex AI project + service-account, Azure api_version + deployment,
    GitHub Copilot OAuth dispatch headers, openai_compat base_url
    overrides, ``responses``-only models).
  * Captures structured diagnostic info: latency, http status when
    available, classified error_class, hint, real provider name + real
    model id used by the upstream, content preview of the answer.
  * Never recors quota, never writes to ``gateway_usage_events``, never
    affects the route's ``mark_route_*`` health state. It's an
    administrator probe, not a user request.
  * Hard 15s timeout so a hung credential can't tie up the dashboard.

Returns a dict the dashboard renders directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn_gateway.config_cache import get_cache, ResolvedDispatch

logger = logging.getLogger(__name__)


# Hard cap so a runaway provider can't DoS the dashboard.
TEST_TIMEOUT_S = 15.0


async def test_one_route(rd: ResolvedDispatch) -> dict[str, Any]:
    """Run a single tiny dispatch through ``rd`` and return a structured
    result. NEVER raises - errors land inside the returned dict.

    The kwargs assembly mirrors ``llm_call.dispatch`` so we exercise
    the SAME code path the production traffic does. If a provider
    works here, it works in dispatch (and vice-versa)."""
    import litellm
    from digitorn_gateway.llm_call import (
        _litellm_model_from_compat,
        classify_upstream_error,
    )

    started = time.perf_counter()
    result: dict[str, Any] = {
        "ok": False,
        "route_id": str(rd.route_id) if rd.route_id else None,
        "provider": rd.provider_slug,
        "real_model": rd.real_model_id,
        "compat": rd.compat,
        "latency_ms": 0,
    }

    # Build kwargs the same way dispatch does.
    litellm_model = _litellm_model_from_compat(
        rd.compat, rd.provider_slug, rd.real_model_id,
    )
    kwargs: dict[str, Any] = {
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 5,
        "temperature": 0,
    }
    if rd.api_key:
        kwargs["api_key"] = rd.api_key
    if rd.base_url:
        kwargs["api_base"] = rd.base_url
    if rd.extra_headers:
        kwargs["extra_headers"] = dict(rd.extra_headers)

    # Provider-specific kwargs that live in extra_body sentinels.
    eb = rd.extra_body or {}
    if "_aws_bedrock_kwargs" in eb and isinstance(eb["_aws_bedrock_kwargs"], dict):
        kwargs.update(eb["_aws_bedrock_kwargs"])
    if "_vertex_kwargs" in eb and isinstance(eb["_vertex_kwargs"], dict):
        kwargs.update(eb["_vertex_kwargs"])
    if "_azure_kwargs" in eb and isinstance(eb["_azure_kwargs"], dict):
        az = eb["_azure_kwargs"]
        if "api_version" in az:
            kwargs["api_version"] = az["api_version"]
        deployment = az.get("_default_deployment")
        if deployment and "/" not in litellm_model.split("azure/", 1)[-1]:
            litellm_model = f"azure/{deployment}"

    # /responses-only models (OpenAI o-series / new Anthropic shapes).
    from digitorn_gateway.responses_compat import (
        is_responses_only,
        chat_body_to_responses_kwargs,
        responses_to_chat_completion,
    )
    use_responses = is_responses_only(rd.real_model_id)

    try:
        if use_responses:
            rkw = chat_body_to_responses_kwargs({
                "messages": kwargs["messages"], "max_tokens": 5,
            })
            rkw.update({k: v for k, v in kwargs.items() if k in (
                "api_key", "api_base", "extra_headers", "api_version",
            )})
            raw = await asyncio.wait_for(
                litellm.aresponses(model=litellm_model, **rkw),
                timeout=TEST_TIMEOUT_S,
            )
            resp = responses_to_chat_completion(
                raw, fallback_model=rd.real_model_id,
            )
        else:
            resp = await asyncio.wait_for(
                litellm.acompletion(model=litellm_model, **kwargs),
                timeout=TEST_TIMEOUT_S,
            )
            if hasattr(resp, "model_dump"):
                resp = resp.model_dump()
            elif hasattr(resp, "dict"):
                resp = resp.dict()
            else:
                resp = dict(resp)
    except asyncio.TimeoutError:
        result["latency_ms"] = int(TEST_TIMEOUT_S * 1000)
        result["error_class"] = "timeout"
        result["error"] = f"upstream did not respond within {TEST_TIMEOUT_S}s"
        return result
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        http_status, error_class, hint = classify_upstream_error(exc)
        result.update({
            "ok": False,
            "latency_ms": latency_ms,
            "http_status": http_status,
            "error_class": error_class,
            "error": hint,
        })
        return result

    latency_ms = int((time.perf_counter() - started) * 1000)
    # Pull tokens + content preview from the response.
    usage = resp.get("usage") or {}
    choices = resp.get("choices") or []
    content = ""
    finish_reason = None
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        finish_reason = choices[0].get("finish_reason")

    result.update({
        "ok": True,
        "latency_ms": latency_ms,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "response_preview": (content[:200] if isinstance(content, str) else str(content)[:200]),
        "finish_reason": finish_reason,
    })
    return result
