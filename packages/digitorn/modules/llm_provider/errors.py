"""Provider-level error types.

Distinct exceptions for the cases the runtime needs to react to
specifically (no-retry for terminal billing/quota, fallback brain
swap for transient billing, …). Generic provider errors keep
flowing as plain ``Exception`` / ``RuntimeError`` like before.

This module is import-cheap: no I/O, no SQLA, no external deps.
The ``Exception`` subclasses carry structured fields the runtime can
forward to the user-facing error event without having to re-parse
the LLM provider's response body.
"""

from __future__ import annotations

from typing import Optional


class ProviderError(RuntimeError):
    """Base class for daemon-side provider errors. Lets callers
    catch all provider-related issues without picking up unrelated
    ``RuntimeError`` instances raised by other modules."""


class QuotaExceededError(ProviderError):
    """The gateway has refused the call because the user is over
    quota (or has an active block). Different from a transient
    rate-limit: retry will keep failing until ``retry_after``.

    The runtime catches this BEFORE the provider's blind 429-retry
    loop wastes time on repeated calls.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: Optional[str] = None,
        metric: Optional[str] = None,
        window: Optional[str] = None,
        limit_value: Optional[float] = None,
        actual_value: Optional[float] = None,
        retry_after: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.metric = metric
        self.window = window
        self.limit_value = limit_value
        self.actual_value = actual_value
        # ISO-8601 timestamp of when the block expires. The runtime
        # forwards this in the user-facing event so the frontend can
        # show "quota resets at ..." without parsing the message.
        self.retry_after = retry_after

    def to_payload(self) -> dict:
        """Structured representation for the error event payload."""
        out: dict = {"reason": self.reason}
        if self.metric is not None:
            out["metric"] = self.metric
        if self.window is not None:
            out["window"] = self.window
        if self.limit_value is not None:
            out["limit"] = self.limit_value
        if self.actual_value is not None:
            out["actual"] = self.actual_value
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


def parse_quota_exceeded(
    status_code: int,
    body: object,
    fallback_message: str = "Quota exceeded",
) -> Optional[QuotaExceededError]:
    """Return a populated ``QuotaExceededError`` if ``body`` matches
    the gateway's 429 quota_exceeded shape, otherwise ``None``.

    The gateway sends::

        HTTP/1.1 429
        {"detail": {
            "code": "quota_exceeded",
            "reason": "...",
            "metric": "tokens_input",
            "window": "per_day",
            "limit": 1000000,
            "actual": 1050000,
            "retry_after": "2026-05-06T00:00:00+00:00"
        }}

    Some clients (LiteLLM, the openai SDK) wrap this differently; we
    accept both ``{"detail": {...}}`` and ``{...}`` at the top level.
    """
    if status_code != 429:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail", body)
    if not isinstance(detail, dict):
        return None
    if detail.get("code") != "quota_exceeded":
        return None
    return QuotaExceededError(
        fallback_message,
        reason=detail.get("reason"),
        metric=detail.get("metric"),
        window=detail.get("window"),
        limit_value=detail.get("limit"),
        actual_value=detail.get("actual"),
        retry_after=detail.get("retry_after"),
    )
