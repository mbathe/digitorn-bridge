"""Per-request context plumbing for outbound LLM calls."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identifiers + bearer token attached to every outbound LLM call."""

    user_id: str | None = None
    app_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    user_jwt: str | None = None

    def to_headers(self) -> dict[str, str]:
        """Return only the IDs that are set, as `X-Digitorn-*` headers."""
        out: dict[str, str] = {}
        if self.user_id:
            out["X-Digitorn-User-Id"] = self.user_id
        if self.app_id:
            out["X-Digitorn-App-Id"] = self.app_id
        if self.session_id:
            out["X-Digitorn-Session-Id"] = self.session_id
        if self.run_id:
            out["X-Digitorn-Run-Id"] = self.run_id
        if self.agent_id:
            out["X-Digitorn-Agent-Id"] = self.agent_id
        return out


_current: ContextVar[Optional[RequestContext]] = ContextVar(
    "digitorn_request_context", default=None,
)


_inbound_user_jwt: ContextVar[Optional[str]] = ContextVar(
    "digitorn_inbound_user_jwt", default=None,
)


def set_inbound_user_jwt(token: Optional[str]) -> object:
    """Store the verified bearer token for the current async context."""
    return _inbound_user_jwt.set(token)


def reset_inbound_user_jwt(token: object) -> None:
    """Pop a previously-set inbound JWT (rarely needed - request scope"""
    try:
        _inbound_user_jwt.reset(token)  # type: ignore[arg-type]
    except (LookupError, ValueError):
        pass


def get_inbound_user_jwt() -> Optional[str]:
    """Return the verified bearer token of the inbound request, or"""
    return _inbound_user_jwt.get()


def set_request_context(ctx: RequestContext) -> object:
    """Store `ctx` for the current async task"""
    return _current.set(ctx)


def reset_request_context(token: object) -> None:
    """Restore the previous value pushed by `set_request_context`."""
    try:
        _current.reset(token)  # type: ignore[arg-type]
    except (LookupError, ValueError):
        # Token already reset / cross-loop - safe to ignore.
        pass


def get_request_context() -> Optional[RequestContext]:
    """Read the current outbound-call context, or `None` if unset."""
    return _current.get()


def get_request_headers() -> dict[str, str]:
    """Convenience for providers: the headers dict, empty if no ctx."""
    rc = _current.get()
    return rc.to_headers() if rc is not None else {}
