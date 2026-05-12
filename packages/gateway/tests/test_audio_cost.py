"""Unit tests for audio cost computation.

Covers the same invariants as the chat-side cost.py audit:
  * honest-zero default when price unset
  * round to 8 decimals
  * negative inputs clamped to 0
  * duration extraction from upstream verbose_json
  * file-size fallback when upstream omits duration
"""
from __future__ import annotations

from digitorn_gateway.audio_cost import (
    compute_audio_cost,
    compute_audio_cost_for_resolved,
    estimate_duration_from_size,
    extract_duration_seconds,
    seconds_to_minutes,
)


def test_seconds_to_minutes_basic():
    assert seconds_to_minutes(60) == 1.0
    assert seconds_to_minutes(30) == 0.5
    assert seconds_to_minutes(0) == 0.0
    # Negative input clamped (defensive: a malformed upstream response
    # must not produce a negative-cost credit).
    assert seconds_to_minutes(-10) == 0.0
    # None / "0" handled safely.
    assert seconds_to_minutes(None) == 0.0  # type: ignore[arg-type]


def test_compute_audio_cost_openai_whisper_1():
    # OpenAI whisper-1 = $0.006/minute. 60 sec audio = $0.006.
    cost = compute_audio_cost(duration_seconds=60, price_per_minute=0.006)
    assert cost == 0.006

    # 30 seconds = half a minute = $0.003
    cost = compute_audio_cost(duration_seconds=30, price_per_minute=0.006)
    assert cost == 0.003


def test_compute_audio_cost_groq_whisper_large_v3():
    # Groq whisper-large-v3 = $0.111/hour = $0.00185/min.
    cost = compute_audio_cost(duration_seconds=60, price_per_minute=0.00185)
    assert cost == 0.00185

    # 10 minutes
    cost = compute_audio_cost(duration_seconds=600, price_per_minute=0.00185)
    assert cost == 0.0185


def test_compute_audio_cost_honest_zero_default():
    # No price configured = no cost, even with 10 minutes of audio.
    cost = compute_audio_cost(duration_seconds=600, price_per_minute=0.0)
    assert cost == 0.0


def test_compute_audio_cost_none_price_treated_as_zero():
    # Defensive: a None coming through from getattr() falls back to 0.
    cost = compute_audio_cost(
        duration_seconds=60, price_per_minute=None,  # type: ignore[arg-type]
    )
    assert cost == 0.0


def test_compute_audio_cost_rounded_to_8_decimals():
    # 1 second @ $0.006/min = 0.0001 (already < 8 decimals so stable).
    cost = compute_audio_cost(duration_seconds=1, price_per_minute=0.006)
    assert cost == 0.0001
    # Sub-cent precision preserved.
    cost = compute_audio_cost(duration_seconds=1, price_per_minute=0.00001)
    # 1/60 * 0.00001 = 1.6667e-7 -> rounded to 8 dp = 0.00000017
    assert cost == 0.00000017


def test_compute_audio_cost_for_resolved_full():
    class _R:
        cost_per_minute_audio = 0.006
    cost = compute_audio_cost_for_resolved(duration_seconds=60, resolved=_R())
    assert cost == 0.006


def test_compute_audio_cost_for_resolved_missing_attr_safe():
    """A resolved object without the new attr (cache before reload)
    must not crash; falls back to 0 cost."""
    class _R:
        pass
    cost = compute_audio_cost_for_resolved(duration_seconds=600, resolved=_R())
    assert cost == 0.0


def test_extract_duration_from_verbose_json():
    # Real OpenAI verbose_json shape.
    resp = {
        "task": "transcribe",
        "language": "english",
        "duration": 12.34,
        "text": "hello world",
        "segments": [{"id": 0, "start": 0.0, "end": 12.34, "text": "hello world"}],
    }
    assert extract_duration_seconds(resp) == 12.34


def test_extract_duration_fallback_to_last_segment_end():
    # Some providers omit the top-level ``duration`` and only carry per-segment ``end``.
    resp = {
        "text": "hi",
        "segments": [
            {"start": 0.0, "end": 1.5},
            {"start": 1.5, "end": 3.2},
        ],
    }
    assert extract_duration_seconds(resp) == 3.2


def test_extract_duration_handles_none_and_garbage():
    assert extract_duration_seconds(None) == 0.0
    assert extract_duration_seconds({}) == 0.0
    assert extract_duration_seconds({"duration": "not-a-number"}) == 0.0
    assert extract_duration_seconds({"duration": -5}) == 0.0  # clamped
    # Wrong type for whole response.
    assert extract_duration_seconds("garbage") == 0.0  # type: ignore[arg-type]


def test_estimate_duration_from_size():
    # 16 kB heuristic = 1 second/16000 bytes.
    assert estimate_duration_from_size(0) == 0.0
    assert estimate_duration_from_size(16_000) == 1.0
    assert estimate_duration_from_size(160_000) == 10.0
    # Negative / nonsense values stay safe.
    assert estimate_duration_from_size(-100) == 0.0


def test_end_to_end_consistency():
    """Real provider response -> extract duration -> compute cost
    against the same model's seeded per-minute price. Equivalent to
    what the audio_routes endpoint does in production."""
    resp = {"text": "hello", "duration": 90.0}  # 90 sec = 1.5 min
    class _R:
        cost_per_minute_audio = 0.006  # whisper-1
    duration = extract_duration_seconds(resp)
    cost = compute_audio_cost_for_resolved(
        duration_seconds=duration, resolved=_R(),
    )
    # 1.5 * 0.006 = 0.009
    assert duration == 90.0
    assert cost == 0.009
