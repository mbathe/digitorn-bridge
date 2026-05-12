"""HTTP surface for audio transcription. OpenAI-compatible.

POST /v1/audio/transcriptions    multipart/form-data
  file:             audio blob (required)
  model:            alias from the catalogue (required)
  language:         BCP-47 hint (optional)
  prompt:           seed text (optional)
  response_format:  "json" | "verbose_json" | "text" | "srt" | "vtt"
                    (default: "json", same as OpenAI)
  temperature:      0-1 (optional)

The endpoint mirrors OpenAI's contract so existing clients (the daemon's
``transcribe.py``, the OpenAI Python SDK pointed at the gateway, third-party
tools) work unchanged. We add the same ``X-Digitorn-*`` response headers as
the chat path (served_by, attempts, route_id, failover trail).

Isolation: every exception is caught and mapped to an HTTP response
specific to this endpoint. The chat path never sees an audio failure.

Quota / cost / attribution: every successful call writes one row to
``gateway_usage_events`` with ``kind='transcription'``, ``audio_seconds``
populated, and the cost computed from the catalogue's per-minute price.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import Response

from digitorn_gateway.auth import GatewayPrincipal, require_principal
from digitorn_gateway.audio_cost import (
    compute_audio_cost_for_resolved,
    estimate_duration_from_size,
    extract_duration_seconds,
)
from digitorn_gateway.audio_dispatch import (
    AudioDispatchTrace,
    AudioModelNotConfigured,
    dispatch_transcription,
)
from digitorn_gateway.config import get_settings
from digitorn_gateway.llm_call import classify_upstream_error

logger = logging.getLogger(__name__)

router = APIRouter()


# Per-request soft caps. Catches obviously-broken uploads early before
# the dispatch path touches LiteLLM. The gateway-wide
# ``settings.max_request_bytes`` still applies at the body level.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB matches OpenAI's whisper-1 cap
_MAX_AUDIO_BYTES_HARD = 100 * 1024 * 1024  # absolute ceiling, never bypass


def _read_attribution_headers(request: Request) -> dict[str, str | None]:
    """Mirror the chat path: pull X-Digitorn-* IDs off the inbound
    request so usage_events rows carry the same attribution columns
    for transcription as they do for completion."""
    h = request.headers
    return {
        "run_id": h.get("x-digitorn-run-id") or None,
        "agent_id": h.get("x-digitorn-agent-id") or None,
        "app_id": h.get("x-digitorn-app-id") or None,
        "external_sid": h.get("x-digitorn-session-id") or None,
    }


def _trace_to_headers(trace: AudioDispatchTrace) -> dict[str, str]:
    out: dict[str, str] = {}
    if trace.served_by:
        out["X-Digitorn-Served-By"] = trace.served_by
    if trace.attempts:
        out["X-Digitorn-Attempts"] = str(trace.attempts)
    if trace.trail and trace.attempts > 1:
        out["X-Digitorn-Failover-Trail"] = ",".join(trace.trail)
    if trace.route_id:
        out["X-Digitorn-Route-Id"] = trace.route_id
    return out


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(...),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float | None = Form(None),
    principal: GatewayPrincipal = Depends(require_principal),
):
    """OpenAI-compatible transcription endpoint.

    Quirks vs OpenAI:
      - We ALWAYS request verbose_json upstream so we can read the
        duration for cost computation. The response to the client is
        downgraded to whatever ``response_format`` they asked for.
      - When the upstream response doesn't carry duration (some Azure
        deployments, some Groq edge cases), we fall back to a
        file-size heuristic so cost tracking stays non-zero. The
        ``audio_seconds`` column reflects the estimate, NOT zero.
    """
    settings = get_settings()

    # 1. Body validation - cheap, before any provider round-trip.
    if not model or not model.strip():
        raise HTTPException(400, detail="missing_field: model")
    audio_bytes = await file.read()
    size = len(audio_bytes)
    if size == 0:
        raise HTTPException(400, detail="empty_audio_file")
    if size > _MAX_AUDIO_BYTES_HARD:
        raise HTTPException(413, detail=f"audio_too_large: {size}B > {_MAX_AUDIO_BYTES_HARD}B")
    if size > _MAX_AUDIO_BYTES:
        # Soft cap warning only; upstream will reject if it cares.
        logger.warning(
            "audio_oversized user=%s size=%d (over soft cap %d, but under hard cap)",
            principal.user_id, size, _MAX_AUDIO_BYTES,
        )

    # 2. Quota pre-check. Re-uses the same engine as chat completions.
    try:
        from digitorn_gateway.main import _quota_check_or_raise
        _quota_check_or_raise(principal.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("audio_quota_check_failed user=%s err=%s", principal.user_id, exc)
        # Fail open on quota engine bugs - don't punish callers for our
        # internal state issues. Matches chat-path behaviour.

    attribution = _read_attribution_headers(request)
    trace = AudioDispatchTrace()

    # 3. Dispatch. Errors map to HTTP via classify_upstream_error so
    # 400 stays 400 (audio-too-short, unsupported codec) and 502 stays
    # 502 (network / upstream timeout).
    try:
        upstream_resp, telemetry = await dispatch_transcription(
            audio_bytes=audio_bytes,
            filename=file.filename or "audio.webm",
            alias=model.strip(),
            language=language,
            prompt=prompt,
            response_format="verbose_json",  # always upstream, downgrade below
            temperature=temperature,
            trace=trace,
        )
    except AudioModelNotConfigured as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "audio_model_not_configured",
                "category": "configuration",
                "model": model,
                "message": str(exc),
            },
        )
    except Exception as exc:
        http_status, error_class, hint = classify_upstream_error(exc)
        # Even when dispatch failed, write a usage row so the failure
        # is observable. Cost = 0 because no audio was processed.
        asyncio.create_task(_safe_record(
            user_id=principal.user_id,
            provider="unknown",
            model=model,
            audio_seconds=0.0,
            cost_usd=0.0,
            latency_ms=0.0,
            error_class=error_class,
            kind="transcription",
            **attribution,
        ))
        raise HTTPException(
            status_code=http_status,
            detail={
                "code": error_class,
                "message": hint,
                "model": model,
            },
        )

    # 4. Cost + duration.
    duration = extract_duration_seconds(upstream_resp)
    if duration <= 0:
        duration = estimate_duration_from_size(size)
    # Build a synthetic ResolvedDispatch-like for cost. We could plumb
    # the actual resolved object through dispatch_transcription, but
    # telemetry's cost_per_minute is the only field we need.
    class _R:
        cost_per_minute_audio = telemetry["cost_per_minute"]
    cost = compute_audio_cost_for_resolved(
        duration_seconds=duration, resolved=_R(),
    )

    # 5. Record usage. Fire-and-forget so the response leaves the
    # gateway immediately. record_event swallows DB errors itself.
    asyncio.create_task(_safe_record(
        user_id=principal.user_id,
        provider=telemetry["provider"],
        model=model,
        audio_seconds=duration,
        cost_usd=cost,
        latency_ms=telemetry["latency_ms"],
        served_by=trace.served_by or telemetry["provider"],
        attempts=trace.attempts or 1,
        failover_trail=trace.trail if trace.attempts > 1 else None,
        kind="transcription",
        **attribution,
    ))

    # 6. Feed the quota engine so plan caps on audio_seconds /
    # transcriptions actually fire on subsequent calls. The engine
    # itself decides whether the user should be blocked - we just
    # hand it the deltas. is_transcription=True scopes the deltas to
    # the audio metrics; chat counters stay untouched.
    if settings.quota_enabled:
        asyncio.create_task(_safe_quota_record(
            user_id=principal.user_id,
            model_alias=model,
            provider=telemetry["provider"],
            audio_seconds=duration,
            cost_usd=cost,
            latency_ms=telemetry["latency_ms"],
        ))

    # 6. Format the response according to the caller's response_format.
    # OpenAI's transcription endpoint returns text/plain for "text",
    # text/vtt for "vtt", etc.; everything else is JSON.
    text = (upstream_resp.get("text") or "").strip()
    headers = _trace_to_headers(trace)

    if response_format in ("text",):
        return Response(content=text, media_type="text/plain", headers=headers)
    if response_format == "srt":
        return Response(content=_to_srt(upstream_resp), media_type="text/srt", headers=headers)
    if response_format == "vtt":
        return Response(content=_to_vtt(upstream_resp), media_type="text/vtt", headers=headers)
    if response_format == "verbose_json":
        return Response(
            content=json.dumps(upstream_resp),
            media_type="application/json",
            headers=headers,
        )
    # Default "json" shape: just ``{"text": "..."}``
    return Response(
        content=json.dumps({"text": text}),
        media_type="application/json",
        headers=headers,
    )


async def _safe_record(**kwargs: Any) -> None:
    """Best-effort usage_events write. Swallows DB issues so the audio
    flow never leaks errors via uncaught task exceptions."""
    try:
        from digitorn_gateway.usage_events import record_event
        await record_event(**kwargs)
    except Exception as exc:
        logger.warning("audio_record_event_failed err=%s", exc)


async def _safe_quota_record(
    *,
    user_id: str,
    model_alias: str,
    provider: str,
    audio_seconds: float,
    cost_usd: float,
    latency_ms: float,
) -> None:
    """Feed the quota engine. Failures are logged + swallowed so a
    DB or supervisor glitch never prevents the response from leaving
    or surfaces as an uncaught task exception."""
    try:
        from digitorn_gateway.quota import UsageRecord, get_engine
        await get_engine().record(UsageRecord(
            user_id=user_id,
            model_alias=model_alias,
            provider=provider,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            success=True,
            audio_seconds=audio_seconds,
            is_transcription=True,
        ))
    except Exception as exc:
        logger.warning("audio_quota_record_failed err=%s", exc)


def _to_srt(resp: dict[str, Any]) -> str:
    """Minimal SRT serialiser from a verbose_json response. Returns an
    empty string when no segments are present (caller-side decision to
    fail soft rather than 500)."""
    segments = resp.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return ""
    out: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start + 1.0))
        text = (seg.get("text") or "").strip()
        out.append(f"{i}\n{_srt_ts(start)} --> {_srt_ts(end)}\n{text}\n")
    return "\n".join(out)


def _to_vtt(resp: dict[str, Any]) -> str:
    segments = resp.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return "WEBVTT\n\n"
    out = ["WEBVTT", ""]
    for seg in segments:
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start + 1.0))
        text = (seg.get("text") or "").strip()
        out.append(f"{_vtt_ts(start)} --> {_vtt_ts(end)}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def _srt_ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def _vtt_ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = int((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}.{ms:03d}"
