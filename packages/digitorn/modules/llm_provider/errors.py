"""Provider-level error types."""

from __future__ import annotations

from typing import Optional

class ProviderError(RuntimeError):
    """Base class for daemon-side provider errors."""

class QuotaExceededError(ProviderError):
    """Gateway refused the call: user is over quota or blocked. Not retriable until `retry_after`."""

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
        # ISO-8601 timestamp of when the block expires.
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
    """Return a populated `QuotaExceededError` for the gateway's 429."""
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
