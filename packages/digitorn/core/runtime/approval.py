"""Approval queue — async-native pending approval management.

When an agent calls a tool that requires user approval (policy="approve"),
the call is enqueued here as an asyncio.Future.  The agent's coroutine
awaits the future (non-blocking to the event loop).  A consumer (CLI
prompt, API endpoint) resolves the future to approve or deny.

Key design:
    - One queue per app, shared across all agents
    - Each request has its own Future → only the requesting agent blocks
    - Other agents and other tool calls continue executing in parallel
    - Timeout prevents zombie requests (default 5 min)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

OnRequestCallback = Callable[["ApprovalRequest"], Coroutine[Any, Any, None]]
OnResolveCallback = Callable[
    ["ApprovalRequest", bool, str], Coroutine[Any, Any, None]
]


@dataclass
class ApprovalRequest:
    """A pending approval request."""

    request_id: str
    agent_id: str
    user_id: str
    tool_name: str
    tool_params: dict[str, Any]
    risk_level: str
    description: str
    app_id: str = ""
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    _future: asyncio.Future[bool] | None = field(
        default=None, repr=False, compare=False
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transport (Socket.IO envelope payload)."""
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "app_id": self.app_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_params": self.tool_params,
            "risk_level": self.risk_level,
            "description": self.description,
            "created_at": self.created_at,
        }


class ApprovalQueue:
    """Per-app approval queue backed by asyncio.Future.

    All operations are synchronous dict ops + Future resolution.
    Safe for single event loop usage (which is how asyncio works).
    For multi-worker deployments, each worker has its own queue.
    """

    def __init__(self, default_timeout: float = 300.0) -> None:
        self._pending: dict[str, ApprovalRequest] = {}
        self._on_request_callbacks: list[OnRequestCallback] = []
        # Resolution callbacks — fire on approve/deny/timeout so clients
        # can drop the pending badge and the frontend list stays in sync
        # with the server-side queue. Without this, the UI showed
        # "pending" indefinitely even after the user resolved the
        # request or the timeout fired.
        self._on_resolve_callbacks: list[OnResolveCallback] = []
        self._default_timeout = default_timeout

    def add_on_resolve(self, callback: "OnResolveCallback") -> None:
        """Register a callback fired when a request resolves (approve/deny/timeout)."""
        self._on_resolve_callbacks.append(callback)

    def remove_on_resolve(self, callback: "OnResolveCallback") -> None:
        try:
            self._on_resolve_callbacks.remove(callback)
        except ValueError:
            pass

    async def _fire_resolved(
        self,
        request: "ApprovalRequest",
        approved: bool,
        reason: str,
    ) -> None:
        for cb in list(self._on_resolve_callbacks):
            try:
                await cb(request, approved, reason)
            except Exception as exc:
                logger.warning("approval_resolve_notify_error: %s", exc)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def set_on_request(self, callback: OnRequestCallback | None) -> None:
        """Legacy single-callback setter — replaces all callbacks.

        Prefer add_on_request() for multi-subscriber support.
        """
        self._on_request_callbacks.clear()
        if callback is not None:
            self._on_request_callbacks.append(callback)

    def add_on_request(self, callback: OnRequestCallback) -> None:
        """Register a callback invoked when a new approval request is enqueued.

        Multiple callbacks are supported (e.g. multiple SSE connections).
        Signature: async (request: ApprovalRequest) -> None
        """
        self._on_request_callbacks.append(callback)

    def remove_on_request(self, callback: OnRequestCallback) -> None:
        """Remove a previously registered callback (e.g. when SSE connection closes)."""
        try:
            self._on_request_callbacks.remove(callback)
        except ValueError:
            pass

    async def enqueue(
        self,
        agent_id: str,
        tool_name: str,
        tool_params: dict[str, Any],
        risk_level: str = "medium",
        description: str = "",
        timeout: float | None = None,
        user_id: str = "local",
        app_id: str = "",
        session_id: str = "",
    ) -> tuple[bool, str]:
        """Enqueue a tool call for approval and wait for resolution.

        Returns:
            (approved, message) tuple:
            - (True, "")   → approved, re-execute the tool
            - (False, "")  → denied, no explanation
            - (False, msg) → denied with user message for the LLM

        The calling coroutine awaits the Future — other coroutines
        (other agents, other tool calls) continue executing.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()

        request = ApprovalRequest(
            request_id=str(uuid.uuid4()),
            agent_id=agent_id,
            user_id=user_id,
            app_id=app_id or getattr(self, "_app_id", ""),
            session_id=session_id,
            tool_name=tool_name,
            tool_params=tool_params,
            risk_level=risk_level,
            description=description,
            _future=future,
        )

        self._pending[request.request_id] = request
        logger.info(
            "approval_enqueue: %s (agent=%s, tool=%s)",
            request.request_id, agent_id, tool_name,
        )

        for cb in list(self._on_request_callbacks):
            try:
                await cb(request)
            except Exception as exc:
                logger.warning("approval_notify_error: %s", exc)

        effective_timeout = timeout if timeout is not None else self._default_timeout

        # BUG-008: the old code just awaited the future for 300 s and
        # emitted nothing until timeout, so the client spinner looked
        # frozen and had no way to tell the user "still waiting on your
        # approval". Periodically fire a progress callback on the same
        # resolve channel so the UI can show a countdown / banner.
        heartbeat_interval = 15.0
        progress_task: asyncio.Task[None] | None = None

        async def _progress_heartbeat() -> None:
            elapsed = 0.0
            while True:
                try:
                    await asyncio.sleep(heartbeat_interval)
                except asyncio.CancelledError:
                    return
                elapsed += heartbeat_interval
                if future.done():
                    return
                remaining = max(0.0, effective_timeout - elapsed)
                for cb in list(self._on_resolve_callbacks):
                    try:
                        progress_req = ApprovalRequest(
                            request_id=request.request_id,
                            agent_id=request.agent_id,
                            user_id=request.user_id,
                            tool_name=request.tool_name,
                            tool_params=request.tool_params,
                            risk_level=request.risk_level,
                            description=(
                                f"(still waiting — {int(remaining)}s left) "
                                f"{request.description}"
                            ),
                            app_id=request.app_id,
                            session_id=request.session_id,
                            created_at=request.created_at,
                        )
                        await cb(progress_req, False, "pending_heartbeat")
                    except Exception as exc:
                        logger.debug("approval_heartbeat_notify_error: %s", exc)

        try:
            progress_task = asyncio.create_task(_progress_heartbeat())
            result = await asyncio.wait_for(future, timeout=effective_timeout)
            approved_ok, msg = result
            await self._fire_resolved(request, approved_ok, msg or "")
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "approval_timeout: %s (agent=%s, tool=%s, timeout=%.0fs)",
                request.request_id, agent_id, tool_name, effective_timeout,
            )
            timeout_msg = f"Approval timed out after {effective_timeout:.0f}s (no response from user)."
            await self._fire_resolved(request, False, timeout_msg)
            return (False, timeout_msg)
        finally:
            # BUG-012: pop BEFORE firing any trailing callback so
            # ``list_pending()`` can never report a resolved request as
            # pending. The dict pop + _fire_resolved ordering matters.
            self._pending.pop(request.request_id, None)
            if progress_task is not None and not progress_task.done():
                progress_task.cancel()

    def list_pending_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return pending requests belonging to a specific user."""
        return [
            r.to_dict() for r in self._pending.values()
            if r.user_id == user_id
        ]

    def resolve(
        self, request_id: str, approved: bool, message: str = "",
        user_id: str | None = None,
    ) -> bool:
        """Resolve a pending request.

        Args:
            request_id: The request to resolve.
            approved: True to approve, False to deny.
            message: Free-form payload from the user. For ``ask_user``
                tools this carries the user's actual answer (option id,
                CSV of multi-select, free text, JSON form). For pure
                tool-approval flows it carries an optional reason.
                ALWAYS propagated regardless of ``approved`` — the
                caller decides what it means.
            user_id: If provided, validates that the resolver owns the request.

        Returns False if not found, already resolved, or wrong user.
        """
        request = self._pending.get(request_id)
        if request is None:
            return False
        if user_id is not None and request.user_id != user_id:
            logger.warning(
                "approval_resolve_denied: user=%s tried to resolve request owned by user=%s",
                user_id, request.user_id,
            )
            return False
        if request._future is None or request._future.done():
            return False
        request._future.set_result((approved, message))
        logger.info(
            "approval_resolved: %s tool=%s approved=%s payload_len=%d preview=%r",
            request_id, request.tool_name, approved,
            len(message), message[:80],
        )
        return True

    def list_pending(self) -> list[dict[str, Any]]:
        """List all pending requests (for API/UI)."""
        return [r.to_dict() for r in self._pending.values()]

    def cancel_all(self) -> int:
        """Cancel all pending requests (e.g., on shutdown). Returns count cancelled."""
        count = 0
        for req in list(self._pending.values()):
            if req._future is not None and not req._future.done():
                req._future.set_result((False, ""))
                count += 1
        self._pending.clear()
        return count
