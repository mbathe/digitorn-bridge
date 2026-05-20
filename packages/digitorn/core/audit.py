"""Bank-grade audit logging helper - one function call, one row.

Usage from any route handler::

    from digitorn.core.audit import audit_log

    await audit_log(
        request,
        event_type="quota.set_app",
        target_app_id=app_id,
        before=old_quota,
        after=new_quota,
        message=f"Admin {caller} updated quota on {app_id}",
    )

The helper:

- Captures the actor's `user_id` + `roles` from `request.state`
- Pulls the real client IP (handles proxies via `X-Forwarded-For`)
- Writes synchronously via SQLAlchemy so the audit row is durable
  BEFORE the handler returns its 200 to the client. Audit that can
  be lost on crash is not audit.
- Append-only. Never UPDATE, never DELETE. Rows are keyed by
  autoincrement id + timestamp.
- Silent no-op when the DB isn't ready (bootstrap / standalone /
  tests) so the caller never has to guard.

Every sensitive mutation should be followed by an `audit_log`
call. If adding a new admin route, adding an audit line is part of
the PR checklist.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def audit_log(
    request: Any,
    *,
    event_type: str,
    target_user_id: str | None = None,
    target_app_id: str | None = None,
    target_resource: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    success: bool = True,
    message: str = "",
) -> None:
    """Write one append-only audit row.

    Caller MUST pass an `event_type` in the dotted lowercase
    namespace (`quota.set_app`, `user.delete`, `app.deploy`, …).
    Bulk listing queries filter on `event_type LIKE 'quota.%'`.
    """
    try:
        from digitorn.core.database import _engine
        if _engine is None:
            # DB not initialised (standalone CLI / unit tests). The
            # audit log is only meaningful in daemon mode - skip
            # silently rather than force callers to wrap in try.
            return
    except Exception as exc:
        logger.debug("audit_log: setup import failed: %s", exc)
        return

    actor_user_id = _safe_str(getattr(request.state, "user_id", None))
    actor_roles = list(getattr(request.state, "roles", None) or [])

    # Client IP + User-Agent - critical forensic fields. Prefer
    # X-Forwarded-For (reverse-proxy deployments) and fall back to
    # the TCP client host.
    ip_address = _extract_ip(request)
    user_agent = _safe_str(request.headers.get("user-agent"))[:512]

    try:
        from digitorn.core.history import record as _record
        # `sync=True`: audit rows MUST be durable before the HTTP
        # response ships - a 200 that the caller reads before the row
        # hits disk breaks the "audit is truth" contract.
        await _record(
            kind="audit",
            type=event_type,
            actor_user_id=actor_user_id,
            actor_roles=actor_roles,
            target_user_id=target_user_id,
            target_app_id=target_app_id,
            target_resource=target_resource,
            before=before,
            after=after,
            ip_address=ip_address,
            user_agent=user_agent,
            success=success,
            message=message[:2048] if message else "",
            sync=True,
        )
    except Exception as exc:
        # Audit write failure is an incident; log loud but don't raise
        # so the originating action still succeeds for the caller.
        logger.error(
            "audit_log_write_failed event=%s actor=%s target_app=%s "
            "target_user=%s: %s",
            event_type, actor_user_id, target_app_id,
            target_user_id, exc, exc_info=True,
        )


def _safe_str(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _extract_ip(request: Any) -> str | None:
    """Pull the real client IP, preferring X-Forwarded-For hop 0."""
    try:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            ip = fwd.split(",", 1)[0].strip()
            if ip:
                return ip[:64]
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client else None
        return host[:64] if host else None
    except Exception:
        return None


__all__ = ["audit_log"]
