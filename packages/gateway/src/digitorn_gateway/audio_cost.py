"""Audio transcription cost computation.

Whisper-style models are billed per minute of audio, not per token.
The dispatch path supplies the audio duration (extracted from the
upstream's ``verbose_json`` response when available, fallback to
file-size heuristic otherwise); we apply the per-minute price from
the catalogue.

Honest-default policy mirrors the cache pricing module: a model with
``cost_per_minute_audio = 0`` (default for fresh catalogue rows or
operators who haven't seeded the column) contributes $0. Never
silently substitute a fallback price.
"""
from __future__ import annotations

from typing import Any


def seconds_to_minutes(seconds: float | int) -> float:
    """Convert raw seconds to fractional minutes. Negative input clamped
    to 0 so a malformed upstream response can't produce a credit-style
    negative cost."""
    return max(0.0, float(seconds or 0.0)) / 60.0


def compute_audio_cost(
    *,
    duration_seconds: float | int,
    price_per_minute: float,
) -> float:
    """USD cost for a transcription of ``duration_seconds`` length,
    rounded to 8 decimals to match the chat-side precision.

    The catalogue ``cost_per_minute_audio`` column drives ``price_per_minute``.
    If unset (= 0) the call costs nothing - operators who haven't seeded
    a price get free transcription rather than guessed bills.
    """
    minutes = seconds_to_minutes(duration_seconds)
    cost = minutes * float(price_per_minute or 0.0)
    return round(cost, 8)


def compute_audio_cost_for_resolved(
    *,
    duration_seconds: float | int,
    resolved: Any,
) -> float:
    """Convenience wrapper that reads the per-minute price off a
    ``ResolvedDispatch`` (or ``CachedModel``) instance. Same honest-zero
    default if the attribute is missing."""
    p = getattr(resolved, "cost_per_minute_audio", 0) or 0
    return compute_audio_cost(
        duration_seconds=duration_seconds,
        price_per_minute=p,
    )


def extract_duration_seconds(upstream_resp: dict[str, Any] | None) -> float:
    """Pull the audio duration out of an upstream transcription response.

    OpenAI / Groq / Azure return ``verbose_json`` with a ``duration`` field
    (in seconds) when we request that response_format. ``json`` (the
    default in the OpenAI API) does NOT carry duration, in which case the
    caller can fall back to a file-size heuristic.
    """
    if not isinstance(upstream_resp, dict):
        return 0.0
    # OpenAI / LiteLLM verbose_json returns ``duration`` at the top level
    # (float seconds). Some providers nest it under ``segments[-1].end``.
    if "duration" in upstream_resp:
        try:
            return max(0.0, float(upstream_resp["duration"]))
        except (TypeError, ValueError):
            pass
    segments = upstream_resp.get("segments") or []
    if isinstance(segments, list) and segments:
        last = segments[-1]
        if isinstance(last, dict) and "end" in last:
            try:
                return max(0.0, float(last["end"]))
            except (TypeError, ValueError):
                pass
    return 0.0


# File-size-to-duration heuristic when the upstream returned a plain
# ``json`` response. Opus/Webm 64kbps stereo ≈ 8 kB/s. We use a
# conservative 16 kB/s (covers low-bitrate Opus and most cheap browser
# MediaRecorder outputs) so we OVER-charge slightly rather than
# under-bill on missing-duration. Operators can adjust by editing this
# constant if their typical client emits a known different bitrate.
_HEURISTIC_BYTES_PER_SECOND = 16_000


def estimate_duration_from_size(byte_count: int) -> float:
    """Last-ditch fallback when the provider didn't return duration.

    Returns 0 for zero-byte uploads. Otherwise applies a fixed-rate
    estimate that's biased slightly high so cost tracking stays
    conservative (better to slightly over-bill than miss revenue).
    """
    if not byte_count or byte_count <= 0:
        return 0.0
    return float(byte_count) / float(_HEURISTIC_BYTES_PER_SECOND)
