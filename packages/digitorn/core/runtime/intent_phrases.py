"""Progressive intent-phrase generation for the Lovable-style strict"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Dev trace sink: writing to a dedicated file so external test scripts can
# verify the dispatch path without needing the daemon's stdout.
_TRACE_PATH = Path.home() / ".digitorn" / "logs" / "intent_phrases.log"


def _trace(msg: str) -> None:
    try:
        _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _TRACE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception as exc:
        logger.debug("intent_phrases best-effort block failed: %s", exc)


async def generate_and_emit_phrases(
    ctx: Any,
    user_message: str,
    correlation_id: str,
) -> None:
    """Generate phrases for this turn and emit the `intent_phrases`"""
    try:
        _trace(f"dispatch_called user_msg={user_message[:60]!r} corr={correlation_id}")
        config = _resolve_strict_config(ctx)
        if config is None:
            _trace("dispatch_skipped reason=strict_mode_off_or_no_config")
            return  # strict_mode off - no phrases needed
        phrases_cfg, _strict = config
        _trace(f"dispatch_proceeding source={phrases_cfg.source}")

        # Pick the source path.
        source = phrases_cfg.source
        phrases: list[str] = []
        actual_source: str = source

        if source == "static":
            phrases = _pick_static_phrases(phrases_cfg.static)
            actual_source = "static"
        elif source == "llm":
            phrases = await _generate_via_llm(
                ctx,
                user_message=user_message,
                cfg=phrases_cfg.llm,
            )
            actual_source = "llm" if phrases else "llm_empty"
        else:  # "auto"
            phrases = await _generate_via_llm(
                ctx,
                user_message=user_message,
                cfg=phrases_cfg.llm,
            )
            if phrases:
                actual_source = "llm"
            else:
                phrases = _pick_static_phrases(phrases_cfg.static)
                actual_source = "static_fallback"

        # Emit even if empty - the frontend uses an empty list as a
        # signal to switch to its own client-side default cycle.
        await _emit_phrases(
            ctx,
            phrases=phrases,
            source=actual_source,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Never raise to caller - the detached task swallows errors
        # so a broken phrase pipeline never disrupts the real turn.
        logger.warning("intent_phrases generation failed: %s", exc)


def _resolve_strict_config(ctx: Any) -> tuple[Any, bool] | None:
    """Return `(IntentPhrasesConfig, strict_mode_bool)` when"""
    tool_calls = getattr(ctx, "_chat_tool_calls", None)
    if tool_calls is None:
        return None
    strict_mode = bool(getattr(tool_calls, "strict_mode", False))
    if not strict_mode:
        return None
    phrases_cfg = getattr(tool_calls, "intent_phrases", None)
    if phrases_cfg is None:
        return None
    return phrases_cfg, strict_mode


def _pick_static_phrases(static_cfg: Any) -> list[str]:
    """Pick one phrase per known phase from the static matrix."""
    phases: dict[str, list[str]] = getattr(static_cfg, "phases", {}) or {}
    ordered_keys = (
        "analyzing", "thinking", "tool_streaming",
        "between_tools", "finalizing",
    )
    out: list[str] = []
    for key in ordered_keys:
        choices = phases.get(key) or []
        if choices:
            out.append(random.choice(choices))
    # Include any extra phase keys the operator defined (unknown to
    # this module but valid for a custom frontend renderer).
    for key, choices in phases.items():
        if key in ordered_keys:
            continue
        if choices:
            out.append(random.choice(choices))
    return out


async def _generate_via_llm(
    ctx: Any,
    *,
    user_message: str,
    cfg: Any,
) -> list[str]:
    """Fire one cheap LLM call through the gateway to produce the"""
    try:
        import httpx  # type: ignore
    except ImportError:
        logger.debug("httpx not installed - intent_phrases LLM path skipped")
        return []

    from digitorn.core.config import get_settings
    from digitorn.core.runtime.request_context import get_inbound_user_jwt

    settings = get_settings()
    gw_base = settings.runtime.gateway_base_url.rstrip("/")
    if gw_base.endswith("/v1"):
        url = f"{gw_base}/chat/completions"
    else:
        url = f"{gw_base}/v1/chat/completions"

    user_jwt = (
        getattr(ctx, "user_jwt", None)
        or get_inbound_user_jwt()
        or None
    )
    if not user_jwt:
        logger.debug("intent_phrases: no user JWT available, skipping LLM call")
        return []

    prompt_tpl = cfg.prompt
    prompt = prompt_tpl.format(
        user_message=user_message[:2000],  # cap input
        min=cfg.min_phrases,
        max=cfg.max_phrases,
    )

    body: dict[str, Any] = {
        "model": cfg.gateway_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 200,
    }
    headers = {
        "Authorization": f"Bearer {user_jwt}",
        "Content-Type": "application/json",
    }
    app_id = getattr(ctx, "app_id", None)
    session_id = getattr(ctx, "session_id", None)
    if app_id:
        headers["X-Digitorn-App-Id"] = str(app_id)
    if session_id:
        headers["X-Digitorn-Session-Id"] = str(session_id)

    logger.info("intent_phrases LLM dispatch model=%s url=%s", cfg.gateway_model, url)
    _trace(f"llm_dispatch model={cfg.gateway_model} url={url}")
    try:
        async with httpx.AsyncClient(timeout=float(cfg.timeout_seconds)) as http:
            resp = await http.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            logger.info(
                "intent_phrases gateway non-2xx status=%d body=%s",
                resp.status_code, resp.text[:200],
            )
            _trace(f"llm_response_non2xx status={resp.status_code} body={resp.text[:200]!r}")
            return []
        data = resp.json()
    except asyncio.TimeoutError:
        logger.info("intent_phrases gateway timeout after %.1fs", cfg.timeout_seconds)
        _trace(f"llm_timeout after_s={cfg.timeout_seconds}")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.info("intent_phrases gateway error: %s", exc)
        _trace(f"llm_error exc={exc!r}")
        return []

    # Extract the assistant message content
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return []

    phrases = _parse_phrases(content, cfg)
    _trace(f"llm_response_parsed count={len(phrases)} raw={content[:200]!r} phrases={phrases[:6]!r}")
    return phrases


def _parse_phrases(content: str, cfg: Any) -> list[str]:
    """Extract a phrase list from the model's response."""
    if not content:
        return []
    text = content.strip()

    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    # First try: parse as JSON array directly.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            phrases = [str(x).strip() for x in parsed if isinstance(x, (str, int, float))]
            phrases = [p for p in phrases if p]
            return phrases[: cfg.max_phrases]
    except (json.JSONDecodeError, ValueError):
        pass

    # Second try: regex out a JSON array from inside prose.
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                phrases = [str(x).strip() for x in parsed if isinstance(x, (str, int, float))]
                phrases = [p for p in phrases if p]
                return phrases[: cfg.max_phrases]
        except (json.JSONDecodeError, ValueError):
            pass

    # Last resort: parse line by line, drop empties and obvious
    # non-phrase lines.
    lines = [ln.strip("-*•·  ").strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and 3 <= len(ln.split()) <= 10]
    return lines[: cfg.max_phrases]


async def _emit_phrases(
    ctx: Any,
    *,
    phrases: list[str],
    source: str,
    correlation_id: str,
) -> None:
    """Publish the `intent_phrases` event so the frontend can switch"""
    bus = getattr(ctx, "event_bus", None) or getattr(ctx, "_event_bus", None)
    if bus is None:
        return
    sid = getattr(ctx, "session_id", "") or ""
    app_id = getattr(ctx, "app_id", "") or "default"
    user_id = getattr(ctx, "user_id", "") or "local"
    if not sid:
        return
    logger.info(
        "intent_phrases emit source=%s count=%d phrases=%s sid=%s corr=%s",
        source, len(phrases), phrases[:6], sid, correlation_id,
    )
    _trace(f"emit source={source} count={len(phrases)} phrases={phrases[:6]!r} corr={correlation_id}")
    try:
        await bus.publish(f"{app_id}:{user_id}:{sid}", {
            "type": "intent_phrases",
            "phrases": phrases,
            "source": source,
            "correlation_id": correlation_id,
            "session_id": sid,
            "app_id": app_id,
        })
    except Exception:  # noqa: BLE001
        logger.debug("intent_phrases SSE emit failed", exc_info=True)
