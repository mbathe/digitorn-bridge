"""Per-request context plumbing for outbound LLM calls.

The agent loop knows the 5 IDs that identify a turn's lineage
(``user_id``, ``app_id``, ``session_id``, ``run_id``, ``agent_id``),
but the LLM provider classes that build the outbound HTTP request
sit several layers below. Threading those IDs through every method
signature would require touching every provider, every wrapper, and
the LiteLLM bridge - too invasive.

A ``ContextVar`` exposes them at zero call-site change. ``agent_turn``
sets the var at entry and resets at exit. The provider's HTTP layer
(httpx event hook, default_headers builder) reads the var and
injects the matching ``X-Digitorn-*`` headers, which the gateway
parses to attribute cost in ``gateway_usage_events``.

Coupling: the runtime is the only writer. Providers are the only
readers. The data class is intentionally tiny so the read path
(every LLM call) stays fast.

Note: this is *outbound* request context (daemon → gateway), not
*inbound* (frontend → daemon). Don't confuse with the FastAPI
``Request`` object, which carries inbound headers and is owned by
the API layer.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Identifiers + bearer token attached to every outbound LLM call.

    The 5 IDs (user / app / session / run / agent) flow as
    ``X-Digitorn-*`` headers so the gateway can attribute cost.
    The ``user_jwt`` is what the gateway uses to AUTHENTICATE the call
    (`Authorization: Bearer <jwt>`). Different concerns: identity vs
    auth.
    """

    user_id: str | None = None
    app_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    # Raw JWT of the user that initiated the inbound HTTP request to
    # the daemon. Forwarded as the bearer when the brain is routed
    # through the gateway. NEVER goes out as a custom header - it's
    # passed via the standard ``Authorization`` header by the
    # provider's HTTP layer.
    user_jwt: str | None = None

    def to_headers(self) -> dict[str, str]:
        """Return only the IDs that are set, as ``X-Digitorn-*`` headers.

        Empty dict when nothing is set. The provider passes this
        directly to ``httpx`` / the SDK's per-request ``extra_headers``.
        """
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


# Separate ContextVar for the inbound user JWT, posted by the FastAPI
# auth middleware as soon as a request lands and the bearer token has
# been verified. The runtime reads it later (typically inside
# ``agent_turn``) and bakes it into the ``RequestContext`` so the
# outbound LLM provider can forward it as the gateway bearer.
#
# Why a separate ContextVar (not reusing ``_current``):
#   * The middleware runs BEFORE the runtime knows anything about
#     the agent run / session. ``_current`` is owned by the runtime;
#     stuffing the JWT in there from the middleware would couple
#     two layers that have different lifecycles.
#   * The JWT lives across asyncio.create_task boundaries (we
#     dispatch the turn in a background task spawned during the
#     request handler). ContextVars propagate naturally.
_inbound_user_jwt: ContextVar[Optional[str]] = ContextVar(
    "digitorn_inbound_user_jwt", default=None,
)


def set_inbound_user_jwt(token: Optional[str]) -> object:
    """Store the verified bearer token for the current async context.

    Called by the FastAPI auth middleware after ``verify_access_token``
    succeeds. Returns a token the caller MAY pass to
    ``reset_inbound_user_jwt`` if they want explicit cleanup; FastAPI's
    request scope generally handles this implicitly.
    """
    return _inbound_user_jwt.set(token)


def reset_inbound_user_jwt(token: object) -> None:
    """Pop a previously-set inbound JWT (rarely needed - request scope
    handles cleanup automatically when a non-leaking middleware uses
    the standard set/reset pattern)."""
    try:
        _inbound_user_jwt.reset(token)  # type: ignore[arg-type]
    except (LookupError, ValueError):
        pass


def get_inbound_user_jwt() -> Optional[str]:
    """Return the verified bearer token of the inbound request, or
    ``None`` outside a request scope."""
    return _inbound_user_jwt.get()


def set_request_context(ctx: RequestContext) -> object:
    """Store ``ctx`` for the current async task. Returns a token the
    caller passes to ``reset_request_context`` to restore the previous
    value when the scope ends. ContextVars propagate across ``await``
    and ``asyncio.create_task``, so sub-agents inherit the parent's
    context until they overwrite it themselves."""
    return _current.set(ctx)


def reset_request_context(token: object) -> None:
    """Restore the previous value pushed by ``set_request_context``."""
    try:
        _current.reset(token)  # type: ignore[arg-type]
    except (LookupError, ValueError):
        # Token already reset / cross-loop - safe to ignore.
        pass


def get_request_context() -> Optional[RequestContext]:
    """Read the current outbound-call context, or ``None`` if unset.

    Providers call this in their HTTP construction path. ``None`` means
    the call was issued from somewhere outside an ``agent_turn`` scope
    (e.g., a CLI tool, a background activation, a test); the provider
    sends the request without the ``X-Digitorn-*`` headers and the
    gateway records ``NULL`` for those columns.
    """
    return _current.get()


def get_request_headers() -> dict[str, str]:
    """Convenience for providers: the headers dict, empty if no ctx."""
    rc = _current.get()
    return rc.to_headers() if rc is not None else {}
