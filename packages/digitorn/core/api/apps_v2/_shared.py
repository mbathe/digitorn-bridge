"""Shared helpers, models, and module-level state for apps_v2 package.

Extracted verbatim from the legacy ``apps.py`` so every sub-router can
import the exact same primitives. Do NOT add new logic here - this file
is a transparent re-export of the original helpers.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import re as _re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from digitorn.core.quota import QuotaPutRequest

# ── Concurrency control for agent turns ──────────────────────────────
# Limits how many agent turns can run concurrently across all apps.
# Beyond this, /messages returns 503 so the event loop is never starved.
_MAX_CONCURRENT_TURNS = int(os.environ.get("DIGITORN_MAX_CONCURRENT_TURNS", "400"))
_turn_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TURNS)
# Tracked tasks - prevents GC of fire-and-forget tasks + enables diagnostics
_active_turn_tasks: set[asyncio.Task] = set()

# Dots are allowed in app IDs (e.g. "my-org.app") but consecutive dots
# ("..") are forbidden to prevent path-traversal when the ID is used in
# filesystem or URL path construction.
_SAFE_ID_RE = _re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,127}$')

_agent_turns_lock = asyncio.Lock()

_MESSAGE_MAX_BYTES = 1_048_576  # 1 MiB - BUG-062 guard against DoS

# Sanity-check artifact downloads (Flutter dashboard preview).
_MAX_ARTIFACT_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB

# Match ``{{env.NAME}}`` / ``{{secret.NAME}}`` references in raw YAML.
_SECRET_REF_RE = re.compile(
    r"\{\{\s*(env|secret)\.([A-Za-z_][A-Za-z0-9_]*)\s*(?:\?\?[^}]*)?\}\}",
)


def _classify_error(exc: Exception) -> dict[str, Any]:
    """Classify an exception into a structured error dict for SSE clients.

    Returns a dict with:
        error:    Human-readable message
        code:     Machine-readable error code for the client to switch on
        category: Error category (billing, auth, rate_limit, provider, network, internal)
        retry:    Whether the client should offer a retry button
    """
    msg = str(exc)
    msg_lower = msg.lower()
    exc_type = type(exc).__name__

    # ── Credential first-use flow (picker dialog) ────────────────
    try:
        from digitorn.core.credentials import CredentialAuthRequired
        from digitorn.core.credentials.store import CredentialMissing
        if isinstance(exc, CredentialAuthRequired):
            data = exc.to_dict()
            data.update({
                "code": "credential_auth_required",
                "retry": False,
                "detail": msg[:500],
            })
            return data
        if isinstance(exc, CredentialMissing):
            data = exc.to_dict()
            data.update({
                "code": "credential_required",
                "retry": False,
                "detail": msg[:500],
                "error": (
                    f"Missing credential: {exc.field!r} for provider "
                    f"{exc.provider!r}. Please add it via the credential picker."
                ),
            })
            return data
        # New-style declarative `credential:` ref injection failure -
        # raised by `inject_session_time` when a required user-scoped
        # ref can't be resolved at chat start. Maps to the same
        # picker flow as `CredentialMissing`.
        from digitorn.core.credentials.injector import CredentialInjectError
        if isinstance(exc, CredentialInjectError):
            # Build the field_spec from the catalogue so the
            # picker has the right form schema. Without this the
            # picker only has the bare ref name and renders an
            # empty Label-only form (the same loop the gate-side
            # fix addresses for app-open).
            field_spec_payload: dict[str, Any] = {}
            provider = getattr(exc, "provider", None) or ""
            if provider:
                try:
                    from dataclasses import asdict, is_dataclass
                    from digitorn.core.credentials.catalog import default_catalog
                    from digitorn.core.credentials.handler import (
                        default_registry,
                    )
                    tpl = default_catalog.get(provider)
                    if tpl is not None:
                        try:
                            handler = default_registry.get(tpl.handler_type)
                            handler_defaults = handler.schema_fields()
                            fields = tpl.effective_fields(handler_defaults)
                        except Exception:
                            fields = []
                        field_spec_payload = {
                            "name": tpl.name,
                            "label": tpl.display_name or tpl.name,
                            "type": tpl.handler_type,
                            "fields": [
                                f.to_dict() if hasattr(f, "to_dict")
                                else (asdict(f) if is_dataclass(f) else dict(f))
                                for f in fields
                            ],
                        }
                except Exception as cat_exc:  # noqa: BLE001
                    logger.debug(
                        "inject_error_classify: catalogue lookup failed: %s",
                        cat_exc,
                    )
            return {
                "error": (
                    f"Credential {exc.ref!r} (scope {exc.scope!r}) for "
                    f"block {exc.block_path!r} could not be resolved: "
                    f"{exc.reason}"
                ),
                "code": "credential_required",
                "category": "auth",
                "retry": False,
                "detail": msg[:500],
                "ref": exc.ref,
                "scope": exc.scope,
                "block": exc.block_path,
                # Picker-shape payload: when the client supports the
                # declarative `credential:` block (web ≥ 2026-05,
                # Flutter ≥ 2026-05), it threads `target_name` /
                # `target_scope` back into create / OAuth so the
                # post-resolution credential lands under the EXACT
                # ref the injector expects. Without this fix, the
                # client falls back to `provider_name` as the cred
                # name → injector misses → picker re-emits forever.
                "target_name": exc.ref,
                "target_scope": exc.scope,
                "provider": provider,
                "provider_type": (
                    field_spec_payload.get("type") or "api_key"
                ),
                "field_spec": field_spec_payload,
                "candidates": [],
            }
    except Exception:
        pass

    if any(kw in msg_lower for kw in (
        "insufficient", "quota", "balance", "billing", "payment",
        "402", "exceeded your current quota", "budget",
    )):
        return {
            "error": "Insufficient balance or quota exceeded. Please check your API billing.",
            "code": "insufficient_balance",
            "category": "billing",
            "retry": False,
            "detail": msg[:500],
        }
    if any(kw in msg_lower for kw in (
        "auth", "401", "unauthorized", "invalid api key", "api key",
        "credentials", "token expired", "forbidden", "403",
    )):
        return {
            "error": "Authentication failed. Check your API key or token.",
            "code": "auth_error",
            "category": "auth",
            "retry": False,
            "detail": msg[:500],
        }
    if any(kw in msg_lower for kw in (
        "rate limit", "429", "too many requests", "throttl",
    )):
        return {
            "error": "Rate limited by the provider. Please wait a moment.",
            "code": "rate_limited",
            "category": "rate_limit",
            "retry": True,
            "detail": msg[:500],
        }
    if any(kw in msg_lower for kw in (
        "context length", "max.*token", "too long", "context window",
        "maximum context", "token limit",
    )):
        return {
            "error": "Message too long for the model's context window.",
            "code": "context_overflow",
            "category": "provider",
            "retry": False,
            "detail": msg[:500],
        }
    if any(kw in msg_lower for kw in (
        "connection", "timeout", "timed out", "unreachable",
        "dns", "ssl", "eof", "reset by peer",
    )) or "Connect" in exc_type or "Timeout" in exc_type:
        return {
            "error": "Network error connecting to the AI provider.",
            "code": "network_error",
            "category": "network",
            "retry": True,
            "detail": msg[:500],
        }
    if any(kw in msg_lower for kw in ("500", "502", "503", "504", "server error", "internal error")):
        return {
            "error": "The AI provider returned a server error. Try again.",
            "code": "provider_error",
            "category": "provider",
            "retry": True,
            "detail": msg[:500],
        }
    if "PermissionDenied" in exc_type or "permission" in msg_lower:
        return {
            "error": f"Permission denied: {msg[:200]}",
            "code": "permission_denied",
            "category": "security",
            "retry": False,
            "detail": msg[:500],
        }
    if "lock timeout" in msg_lower or "session lock" in msg_lower:
        return {
            "error": "Another turn is still running on this session. Wait for it to finish.",
            "code": "session_busy",
            "category": "internal",
            "retry": True,
            "detail": msg[:500],
        }
    return {
        "error": msg[:500] if msg else "An unexpected error occurred.",
        "code": "internal_error",
        "category": "internal",
        "retry": True,
        "detail": f"[{exc_type}] {msg[:500]}",
    }



def _validate_app_id(app_id: str) -> str | None:
    """Return an error string if *app_id* is unsafe, else None."""
    if not _SAFE_ID_RE.match(app_id):
        return f"Invalid app_id: '{app_id}'"
    if ".." in app_id:
        return f"App ID must not contain '..': '{app_id}'"
    if app_id.startswith(".") or app_id.endswith("."):
        return f"App ID must not start or end with '.': '{app_id}'"
    return None


def _build_history_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw LLM messages into structured turns for the web UI.

    Groups assistant tool_calls + tool results into segments, filters system messages,
    and produces a clean list of {role, content, seq, toolCalls?, thinking?} objects.

    ``seq`` is propagated from the input dicts when present (the
    history endpoint feeds this function with HistoryLog rows that
    carry their canonical daemon-allocated seq). Clients use ``seq``
    as the SOLE source of truth for chat ordering AND replay
    reconstruction - no synthetic indexes, no client-side counters.
    Unset only when called from a non-DB code path (legacy in-memory
    replay) - those paths fall back to generation order, which is
    the existing behavior.
    """
    from digitorn.core.cli.ui import _tool_label

    turns: list[dict[str, Any]] = []
    # Index tool results by call_id for fast lookup
    tool_results: dict[str, dict[str, Any]] = {}
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            result_content = m.get("content", "")
            # Try to parse JSON result
            parsed: Any = result_content
            if isinstance(result_content, str):
                try:
                    parsed = _json.loads(result_content)
                except (ValueError, TypeError):
                    pass
            tool_results[m["tool_call_id"]] = {"content": parsed}

    for m in messages:
        role = m.get("role", "")
        if role == "system" or role == "tool":
            continue
        if role == "user":
            turn: dict[str, Any] = {"role": "user", "content": m.get("content", "")}
            seq = m.get("seq")
            if isinstance(seq, int) and seq > 0:
                turn["seq"] = seq
            turns.append(turn)
            continue
        if role == "assistant":
            content = m.get("content", "") or ""
            tool_calls_raw = m.get("tool_calls", [])
            thinking = m.get("thinking", "")
            tool_calls = []
            for tc in tool_calls_raw:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args_raw = fn.get("arguments", {})
                if isinstance(args_raw, str):
                    try:
                        args_raw = _json.loads(args_raw)
                    except (ValueError, TypeError):
                        args_raw = {}
                call_id = tc.get("id", "")
                label, detail = _tool_label(name, args_raw if isinstance(args_raw, dict) else {})
                tr = tool_results.get(call_id, {})
                tool_calls.append({
                    "id": call_id,
                    "name": name,
                    "label": label,
                    "detail": detail,
                    "params": args_raw if isinstance(args_raw, dict) else {},
                    "result": tr.get("content"),
                    "status": "done",
                })

            seq = m.get("seq")
            seq = seq if isinstance(seq, int) and seq > 0 else None

            if tool_calls and not content.strip():
                # Emit both snake_case (Python SDK + spec) and camelCase
                # (legacy Flutter client) so existing consumers keep
                # working. The canonical key is `tool_calls`.
                turn: dict[str, Any] = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls,
                    "toolCalls": tool_calls,
                }
                if seq is not None:
                    turn["seq"] = seq
                if thinking:
                    turn["thinking"] = thinking
                turns.append(turn)
                continue

            turn = {"role": "assistant", "content": content}
            if seq is not None:
                turn["seq"] = seq
            if tool_calls:
                turn["tool_calls"] = tool_calls
                turn["toolCalls"] = tool_calls
            if thinking:
                turn["thinking"] = thinking
            if content.strip() or tool_calls:
                turns.append(turn)

    return turns


def _get_workspace_status(workspace: str) -> dict[str, Any]:
    """Get git status for a workspace - server-side, all clients benefit."""
    import subprocess
    result: dict[str, Any] = {}
    try:
        # Branch
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=workspace, capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            result["branch"] = r.stdout.strip()
        else:
            return result  # Not a git repo

        # Ahead/behind
        try:
            ab = subprocess.run(
                ["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"],
                cwd=workspace, capture_output=True, text=True, timeout=3,
            )
            if ab.returncode == 0:
                parts = ab.stdout.strip().split()
                if len(parts) == 2:
                    result["ahead"] = int(parts[0])
                    result["behind"] = int(parts[1])
        except Exception:
            pass

        # Changed files
        r = subprocess.run(
            ["git", "status", "--porcelain", "-u"],
            cwd=workspace, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            changes = []
            _STATUS_MAP = {"M": "M", "A": "A", "D": "D", "R": "R", "?": "?", "U": "U"}
            for line in r.stdout.strip().split("\n"):
                if len(line) < 4:
                    continue
                raw_st = line[0:2].strip() or "?"
                full_path = line[3:].strip()
                st = "M"
                for c in raw_st:
                    if c in _STATUS_MAP:
                        st = _STATUS_MAP[c]
                        break
                short_name = full_path.rsplit("/", 1)[-1] if "/" in full_path else full_path
                changes.append({"status": st, "path": short_name, "full_path": full_path})
            result["changes"] = changes
    except Exception:
        pass
    return result


def _validate_id(value: str, name: str = "app_id") -> str:
    """Validate app_id / session_id - alphanumeric + dash/underscore/dot, 1-128 chars."""
    err = _validate_app_id(value)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return value


async def _inc_agent_turns(request: Request, delta: int = 1) -> None:
    """Atomically increment/decrement the active agent turns counter."""
    state = request.app.state
    if hasattr(state, "_active_agent_turns"):
        async with _agent_turns_lock:
            state._active_agent_turns += delta


async def _activate_preview_session(
    request: "Request",
    app_id: str,
    session_id: str,
    preview_module: Any,
    user_id: str | None = None,
    set_active: bool = False,
):
    """Resolve the session's workspace and activate it on the preview module.

    Centralises the wiring so every API entry point that mutates preview
    state correctly selects the filesystem vs DB backend based on whether
    the session has a user-chosen workspace. Returns the activated
    ``PreviewSessionState`` (or ``None`` if the module doesn't support it).

    ``set_active`` controls whether the preview module's ``_active_session_id``
    is updated. Observation paths (e.g. the Socket.IO rejoin snapshot)
    leave it alone so concurrent mutations keep their own scope. Mutation
    paths (e.g. ``/tools/{name}/execute``) must pass ``set_active=True``
    - otherwise the upcoming write resolves against whichever session
    happened to run last, leaking state across sessions.
    """
    if preview_module is None or not session_id:
        return None
    manager = _get_manager(request)
    ws = ""
    # Use the RAW request.state.user_id for session lookup (loopback keeps
    # "system" as the owner). `_caller_user_id` strips system/anonymous and
    # would cause a lookup mismatch against a session saved under "system".
    raw_uid = (
        getattr(request.state, "user_id", None)
        or user_id
        or "local"
    )
    try:
        sess = await manager.get_session(
            app_id, session_id, user_id=raw_uid,
        )
        ws = getattr(sess, "workspace", "") or "" if sess else ""
    except Exception:
        ws = ""
    try:
        if hasattr(preview_module, "activate_session"):
            return await preview_module.activate_session(
                session_id, user_id=user_id, workspace=ws or None,
                set_active=set_active,
            )
        if hasattr(preview_module, "hydrate_session") and not set_active:
            return await preview_module.hydrate_session(
                session_id, user_id=user_id, workspace=ws or None,
            )
        if hasattr(preview_module, "set_active_session"):
            preview_module.set_active_session(session_id, user_id=user_id)
    except Exception as exc:
        logger.warning(
            "activate_preview_session_failed sid=%s: %s", session_id, exc,
        )
    return None


def _caller_user_id(request: Request) -> str | None:
    """Pull the authenticated caller's user_id. Returns None in
    dev mode (no auth).

    The loopback auth bypass (in-process agent self-calls, 127.0.0.1)
    sets ``user_id='system'`` as a sentinel. That's admin context, not
    a real user scope - so we return None for it too. Otherwise the
    scope resolver would treat it as "user='system'" and fail to find
    the system install.
    """
    uid = getattr(request.state, "user_id", None)
    if not uid or uid in ("anonymous", "system"):
        return None
    return uid


def _get_deployed(request: Request, app_id: str):
    """Helper: look up a deployed app in the caller's visibility
    scope. Returns the DeployedApp or None.

    Resolution order (inside manager.get):
      1. Caller's user-scoped deploy
      2. System-scoped deploy
      3. Legacy bare-key deploy (backwards compat)

    Use this everywhere instead of bare ``manager.get(app_id)``.
    """
    manager = _get_manager(request)
    return manager.get(app_id, user_id=_caller_user_id(request))


def _raise_not_deployed(request: Request, app_id: str) -> None:
    """Raise the right HTTPException when an app_id isn't found.

    While the daemon is still warming up (``reload_from_db`` still
    running in the background), a missing app may just not have
    finished loading yet - we return 503 with a ``Retry-After`` header
    so well-behaved clients can back off instead of treating it as a
    permanent 404.
    """
    warming = bool(getattr(request.app.state, "warming_up", False))
    if warming:
        raise HTTPException(
            status_code=503,
            detail=(
                f"App '{app_id}' not yet loaded - daemon is warming up. "
                f"Retry in a few seconds, or poll /health for warming_up=false."
            ),
            headers={"Retry-After": "2"},
        )
    raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")


def _is_deployed(request: Request, app_id: str) -> bool:
    """Helper: is an app visible to the caller?"""
    manager = _get_manager(request)
    return manager.is_deployed(app_id, user_id=_caller_user_id(request))


def _require_permission(request: Request, permission: str) -> None:
    """Raise 403 if the authenticated user lacks the required permission.

    Permissions are populated by AuthMiddleware from the JWT token.
    The ``*`` wildcard (admin role) matches everything.
    """
    perms: list[str] = getattr(request.state, "permissions", [])
    if "*" in perms:
        return
    if permission in perms:
        return
    raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")


def _turn_event(
    type_: str,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    correlation_id: str,
    op_state,
    payload: dict | None = None,
) -> Any:
    """Build a turn-scoped :class:`SessionEvent` with the universal
    contract pre-filled. Helper for every ``/messages`` / queue /
    abort / resume path in this module so each emitter only writes
    the fields it actually owns.

    ``op_id`` defaults to the turn's ``correlation_id`` (so every
    event in one turn groups together), ``op_type`` is always
    ``TURN`` for this family of events.
    """
    from digitorn.core.events.envelope import (
        SessionEvent as _SE, OpType as _OT,
    )
    return _SE.build(
        type=type_,
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
        op_id=correlation_id or f"turn-{session_id}",
        op_type=_OT.TURN,
        op_state=op_state,
        correlation_id=correlation_id or "",
        payload=payload or {},
    )


async def _require_session_create_or_owner(
    request: Request, app_id: str, session_id: str,
) -> Any:
    """Variant of ``_require_session_access`` for POST /messages.

    POST /messages is the ONE endpoint where a fresh ``session_id`` is
    a legitimate thing (the first message creates the session on the
    fly, bound to the caller). So the rule here is:

      * anonymous → 401 (same as the strict check).
      * authenticated, session doesn't exist yet → pass through; the
        handler will create it bound to this caller.
      * authenticated, session exists under this caller → pass.
      * authenticated, session exists under ANOTHER caller → 404.

    The last branch is BUG-072: user B could inject a prompt into user
    A's live conversation and the LLM would reply to A as if A had
    written it. The 404 is deliberately indistinguishable from "no
    such session" to avoid leaking session-existence oracles.
    """
    uid = getattr(request.state, "user_id", None)
    if not uid or uid == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")
    manager = _get_manager(request)
    # Try the caller's own scope first (fast path, also the existing-session
    # happy path). If found, we're OK.
    try:
        own = await manager.get_session(app_id, session_id, user_id=uid)
    except Exception:
        own = None
    if own is not None:
        return own
    # The session may still exist under a different owner - look it up
    # at the store level with no user filter. If something comes back
    # that isn't ours, refuse; otherwise it's a genuinely new sid the
    # caller is allowed to use.
    store = getattr(manager, "_session_store", None)
    if store is None:
        return None
    try:
        any_owner = await asyncio.to_thread(
            store.get_any_owner, app_id, session_id,
        ) if hasattr(store, "get_any_owner") else None
    except Exception:
        any_owner = None
    if any_owner is not None and any_owner != uid:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return None


async def _require_session_access(
    request: Request, app_id: str, session_id: str,
) -> Any:
    """Ensure the caller is authenticated AND owns this session.

    Centralises the authorization check that EVERY ``/sessions/{sid}/*``
    handler must perform. Without it, Round 5 found seven CVE-level
    cross-user/anonymous leaks (BUG-070..076): ``/events``, ``/abort``,
    ``/messages``, ``/fork``, ``/export``, ``/queue``,
    ``/context-breakdown``, ``/workspace`` were all reachable by
    anybody holding the session_id, no matter who owned it.

    Behaviour:
      * anonymous caller (no JWT, no loopback, no dev-mode) → **401**
        - we intentionally do NOT fall through to the 404 path because
        an unauthenticated client should never enumerate session ids.
      * authenticated caller whose ``user_id`` does not own the session
        → **404** (no info-leak: a stolen sid is indistinguishable from
        a non-existent one).
      * owner → returns the ``ConversationSession`` object for reuse by
        the handler (saves one extra DB lookup).

    The helper uses ``manager.get_session`` which already enforces the
    ``user_id`` filter at the store level - we're promoting that same
    check from "nice fallback" to "non-bypassable precondition".
    """
    uid = getattr(request.state, "user_id", None)
    # ``system`` is the sentinel the loopback bypass uses for
    # unauthenticated in-process calls. Internal agents never reach
    # /api/apps/{id}/sessions/{sid}/* through HTTP (they use
    # AgentContext directly), so treating ``system`` the same as
    # ``anonymous`` here is safe and closes an anonymous-local-
    # process oracle on session endpoints.
    if not uid or uid in ("anonymous", "system"):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": (
                    "Session access requires an authenticated user."
                ),
            },
        )
    manager = _get_manager(request)
    try:
        sess = await manager.get_session(app_id, session_id, user_id=uid)
    except Exception:
        sess = None
    if sess is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{session_id}' not found",
        )
    return sess


def _refresh_deployed_agent_tools(deployed: Any, new_index: Any) -> None:
    """Refresh all agent contexts' tool lists after index rebuild.

    Called after MCP OAuth token injection when new tools become available.
    Updates ctx.tools and ctx.system_prompt so the LLM sees new tools.
    """
    from digitorn.modules.context_builder.builder import build_direct_tools
    from digitorn.modules.context_builder.prompt import build_system_prompt
    from digitorn.core.runtime.bootstrap import (
        _build_meta_tools_schema,
        _build_primitive_tools_schema,
        _choose_tool_injection,
    )

    cb = deployed.context_builder
    direct_tools = build_direct_tools(new_index)
    meta_tools = _build_meta_tools_schema(cb)

    for agent_id, ctx in deployed.contexts.items():
        tool_injection = _choose_tool_injection(
            total_tools=new_index.total_tools,
            context_window=ctx.context_config.max_tokens,
            direct_tools=direct_tools,
        )

        if tool_injection == "direct":
            primitive_tools = _build_primitive_tools_schema(
                cb,
                watchers_enabled=ctx.watchers_enabled,
                scheduler_enabled=deployed.compiled.execution.scheduler,
                channels_enabled=bool(deployed.compiled.channels),
            )
            agent_tools = direct_tools + primitive_tools
        else:
            agent_tools = meta_tools

        ctx.tools = agent_tools
        ctx.tool_injection = tool_injection

        agent_def = next(
            (a for a in deployed.compiled.agents if a.agent_id == agent_id), None,
        )
        if agent_def is not None:
            ctx.system_prompt = build_system_prompt(
                agent_id=agent_id,
                role=ctx.role,
                user_prompt=agent_def.system_prompt,
                index=new_index,
                native_tool_use=ctx.native_tool_use,
                tool_injection=tool_injection,
                tools=agent_tools,
                plan_first=ctx.plan_first,
                setup_summary=getattr(ctx, "_setup_summary", []),
                channels_info=getattr(ctx, "_channels_info", []),
                default_channel=getattr(ctx, "_default_channel", None),
            )

    logger.debug(
        "agent_tools_refreshed app=%s agents=%d",
        deployed.app_id, len(deployed.contexts),
    )


async def _drain_queue_next(
    request: "Request",
    app_id: str,
    session_id: str,
    user_id: str,
) -> None:
    """Kick a fresh drain of the session's queue. Pops the head and
    dispatches it; from there ``dispatch_turn`` chains the rest.

    Used by:

    * the orphan-queue watchdog in ``session_send_message`` (when a
      previous drain chain died unexpectedly), and
    * the post-abort resume path in ``abort_session_turn`` (after the
      user cancels a running turn, the next queued entry should still
      get dispatched).

    Normal chain dispatch (after a turn completes) is handled inside
    ``dispatch_turn`` itself via ``_schedule_chain`` - this helper is
    only the kick-starter for sessions that have a queue but no
    in-flight task.
    """
    from digitorn.core.app import message_queue as _mq
    entry = await _mq.next_queued(session_id)
    if entry is None:
        return  # queue empty - done

    async def _run_next():
        # Single source of truth: dispatch_turn owns cred check,
        # heartbeat, manager.chat(), error classification + event
        # emission. We just translate the outcome into a queue
        # terminal status and recursively chain to the next entry
        # (unless PAUSED, in which case the row stays alive for a
        # later resume signal - Step 5).
        from ._dispatch import (
            dispatch_turn, TurnEntry, TurnSource, TurnStatus,
        )
        outcome = await dispatch_turn(
            request, app_id, session_id,
            entry=TurnEntry(
                correlation_id=entry.correlation_id,
                message=entry.message,
                image_refs=entry.image_refs or None,
                queue_row_id=entry.id,
                position=entry.position,
            ),
            user_id=user_id,
            source=TurnSource.DRAIN,
        )
        if outcome.status == TurnStatus.PAUSED:
            # Mark the row terminal with `credential_required` so
            # is_turn_running stops returning True. The user retries
            # via the bubble's RETRY pill, which sends a fresh
            # message. Don't chain: the next queued entry likely
            # needs the same missing credential.
            try:
                await _mq.mark_failed(
                    entry.id, error_code="credential_required",
                )
                _mq.fail_awaiter(
                    entry.correlation_id,
                    RuntimeError("credential_required"),
                )
            except Exception:
                pass
            return
        # COMPLETED / FAILED / CANCELLED: dispatch_turn already flipped
        # the row + scheduled the next chain dispatch internally.
        # Nothing more for us to do.

    async def _guarded_next():
        async with _turn_semaphore:
            await _run_next()

    task = asyncio.create_task(_guarded_next())
    _active_turn_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _active_turn_tasks.discard(t)
    task.add_done_callback(_done)


def _context_advice(
    total: int, effective: int,
    sys_tokens: int, tools_tokens: int, msg_tokens: int, mem_tokens: int,
) -> list[str]:
    """Heuristic hints shown when context is tight."""
    tips: list[str] = []
    if total > effective:
        tips.append(
            f"OVERFLOW: {total}/{effective} - your next turn will be rejected."
        )
    elif total > effective * 0.9:
        tips.append(
            f"Tight: {total}/{effective} ({round(total/effective*100)}%) - compaction imminent."
        )
    if tools_tokens > sys_tokens and tools_tokens > 30000:
        tips.append(
            "Tool schemas dominate. Consider granting fewer tools per agent "
            "(``agents[].modules: [{filesystem: [read, write]}]``), or "
            "switch ``tool_injection: discovery`` to defer tool exposure."
        )
    if mem_tokens > 10000:
        tips.append(
            f"Memory snippet is {mem_tokens} tokens - check memory module "
            "``get_prompt_sections`` for oversized facts/procedures."
        )
    if msg_tokens > effective * 0.5:
        tips.append(
            "Message history is large - auto-compact should trigger soon. "
            "Force manually via the ``/compact`` hook or /abort + new session."
        )
    return tips


def _merge_resources(
    base: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Deep-merge snapshot resources - incoming wins on per-id conflicts."""
    out: dict[str, dict[str, Any]] = {
        ch: {rid: dict(payload) for rid, payload in items.items()}
        for ch, items in base.items()
    }
    for ch, items in (incoming or {}).items():
        bucket = out.setdefault(ch, {})
        for rid, payload in items.items():
            bucket[rid] = dict(payload)
    return out


async def _resolve_deployed_preview(
    request: Request, app_id: str,
) -> tuple[Any, Any]:
    """Common path: validate deploy, resolve deployed + preview_module."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module")
    return deployed, preview_module


def _strip_content_from_files(resources: dict[str, Any]) -> dict[str, Any]:
    """Return resources with file `content` stripped but everything else kept.

    For the lightweight code-snapshot endpoint - Flutter's explorer + SCM
    panel never need the raw content up front; it's fetched lazily when
    the user opens a file.
    """
    out: dict[str, Any] = {}
    for ch, items in (resources or {}).items():
        if ch != "files":
            out[ch] = items
            continue
        stripped: dict[str, Any] = {}
        for rid, payload in items.items():
            meta = dict(payload)
            meta.pop("content", None)
            stripped[rid] = meta
        out[ch] = stripped
    return out


def _validate_payload_against_schema(
    schema: dict[str, Any] | None,
    payload: dict[str, Any],
) -> list[str]:
    """Check a session payload against an app's declared schema.

    Returns the list of human-readable validation errors. Empty list
    means the payload is valid (or no schema is declared).

    Only enforces ``required`` constraints + presence/type sanity. We
    keep this deliberately lightweight: the form on the client already
    constrains values, this is just the server-side safety net.
    """
    if not schema:
        return []

    errors: list[str] = []
    prompt_cfg = schema.get("prompt") or {}
    metadata_cfg = schema.get("metadata") or []
    files_cfg = schema.get("files") or []

    user_prompt = (payload.get("prompt") or "").strip()
    user_metadata = payload.get("metadata") or {}
    user_files = payload.get("files") or []

    # Prompt
    if prompt_cfg.get("required") and not user_prompt:
        errors.append("payload.prompt is required")
    min_len = prompt_cfg.get("min_length")
    if min_len and len(user_prompt) < int(min_len):
        errors.append(f"payload.prompt must be at least {min_len} chars")

    # Metadata fields
    for fld in metadata_cfg:
        name = fld.get("name", "")
        if fld.get("required") and (
            name not in user_metadata or user_metadata.get(name) in (None, "")
        ):
            errors.append(f"payload.metadata.{name} is required")

    # File slots - at least ``max_count`` ≥ 1 file matching the slot's
    # mime list when required. We match by mime since slot ``name`` is
    # logical and never appears on uploaded files.
    for slot in files_cfg:
        if not slot.get("required"):
            continue
        accepted = slot.get("mime") or []
        match = [
            f for f in user_files
            if not accepted or _mime_matches(f.get("mime_type", ""), accepted)
        ]
        if not match:
            label = slot.get("label") or slot.get("name") or "file"
            errors.append(f"payload.files: missing required '{label}'")

    return errors


def _mime_matches(mime: str, accepted: list[str]) -> bool:
    """``image/png`` matches both ``image/png`` and ``image/*``."""
    mime = (mime or "").lower()
    for pat in accepted:
        pat = pat.lower()
        if pat == mime:
            return True
        if pat.endswith("/*") and mime.startswith(pat[:-1]):
            return True
    return False


def _assert_session_visible(session: dict[str, Any] | None, app_id: str, request: Request) -> dict[str, Any]:
    """Guard: session must exist, belong to this app, and be visible to caller."""
    if session is None:
        raise HTTPException(status_code=404, detail="Background session not found")
    if session.get("app_id") != app_id:
        raise HTTPException(status_code=404, detail="Background session not found")
    user_id = getattr(request.state, "user_id", None)
    perms = getattr(request.state, "permissions", [])
    if "*" not in perms and user_id and session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not your session")
    return session


def _get_bg_session_store(request: Request):
    """Get or create the BackgroundSessionStore."""
    from digitorn.core.app.background_session_store import BackgroundSessionStore
    manager = _get_manager(request)
    store = getattr(manager, "_bg_session_store", None)
    if store is None:
        from digitorn.core.database import get_session_factory
        store = BackgroundSessionStore(get_session_factory())
        manager._bg_session_store = store
    return store


def _get_activation_store(request: Request):
    """Get or create the ActivationStore from the database session factory."""
    from digitorn.core.app.activation_store import ActivationStore
    manager = _get_manager(request)
    store = getattr(manager, "_activation_store", None)
    if store is None:
        from digitorn.core.database import get_session_factory
        store = ActivationStore(get_session_factory())
        manager._activation_store = store
    return store


def _resolve_app_bundle_dir(request: Request, app_id: str, manager) -> Any:
    """Return the on-disk directory that contains a deployed app's
    companion files (YAML, icon, README, skills, assets/...).

    Tries the manager's bundle store first, falls back to the
    package install dir when the app was installed as a package,
    and returns None when neither is available.
    """
    from pathlib import Path
    try:
        bs = getattr(manager, "_bundle_store", None)
        if bs is not None:
            _d = bs.app_dir(app_id)
            if _d:
                p = Path(_d).resolve()
                if p.is_dir():
                    return p
    except Exception:
        pass
    pkg_registry = getattr(request.app.state, "package_registry", None)
    if pkg_registry is not None:
        try:
            import asyncio as _asyncio
            # ``package_registry.get`` is async - run the coroutine
            # via ``loop.run_until_complete`` in FastAPI handlers is
            # wrong; just return None and let the caller fall back.
            return None
        except Exception:
            pass
    return None


def _try_resize_image(
    bundle_dir: Any, source: Any, max_size: int,
) -> Any:
    """Return a resized variant of ``source`` at most ``max_size``
    pixels on the longest side. Returns the cached file path on
    success, or None when the asset isn't a resizable raster or
    Pillow isn't installed.

    Result is cached under ``<bundle_dir>/.digitorn/resized/``.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.debug(
            "asset resize: Pillow not installed, serving original",
        )
        return None

    from pathlib import Path as _Path
    src = _Path(source)
    ext = src.suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        # SVG, PDF, GIF, etc. - don't resize
        return None

    # Clamp size
    max_size = max(16, min(max_size, 2048))

    cache_dir = _Path(bundle_dir) / ".digitorn" / "resized"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rel = src.relative_to(_Path(bundle_dir)).as_posix().replace("/", "__")
    cache_key = f"{max_size}_{rel}"
    cached = cache_dir / cache_key
    if cached.exists():
        try:
            # Cache invalidation: if source is newer, regenerate
            if cached.stat().st_mtime >= src.stat().st_mtime:
                return cached
        except OSError:
            pass

    try:
        with Image.open(src) as img:
            img.thumbnail((max_size, max_size))
            # Preserve mode for PNG transparency
            save_kwargs: dict[str, Any] = {}
            if ext in (".jpg", ".jpeg"):
                if img.mode != "RGB":
                    img = img.convert("RGB")
                save_kwargs["quality"] = 85
                save_kwargs["optimize"] = True
            img.save(cached, **save_kwargs)
        return cached
    except Exception as exc:
        logger.debug("asset resize failed for %s: %s", src, exc)
        return None


async def _try_serve_static_dist(
    request: Request,
    app_id: str,
    path: str,
):
    """Serve an app's pre-built ``web/dist/`` bundle if it exists.

    This is the production-mode preview: instead of spawning a Vite dev
    server and proxying it (heavy, one process per app, can hang on
    rename swaps), the daemon serves the static files directly. Zero
    process per app. The agent still drives live updates via the
    preview SSE stream.

    Returns a Response if dist/ is found and the requested file is
    readable, None otherwise (caller falls back to the dev-server proxy).
    """
    from pathlib import Path as _Path
    from starlette.responses import FileResponse
    from starlette.responses import Response as _Resp

    deployed = _get_deployed(request, app_id)
    if not deployed:
        return None

    # Bundle dir resolution. ``compiled.source_path.parent`` wins when
    # the deploy went through ``_deploy_from_bundle`` (the compiler
    # stamped it). Otherwise delegate to the canonical resolver which
    # walks the SAME chain as preview warmup:
    #   registry USER → registry SYSTEM → disk USER → disk SYSTEM →
    #   source-tree builtin.
    # Pulling the user_id from request.state lets us serve a user's
    # private install of an app without falling through to the system
    # version.
    from digitorn.core.packages.resolver import resolve_app_install_dir

    bundle_dir: _Path | None = None
    source_path = (
        getattr(deployed.compiled, "source_path", None)
        if hasattr(deployed, "compiled")
        else None
    )
    if source_path is not None:
        bundle_dir = _Path(source_path).parent
    else:
        user_id = getattr(request.state, "user_id", None)
        pkg_registry = getattr(request.app.state, "package_registry", None)
        bundle_dir = await resolve_app_install_dir(
            app_id, user_id=user_id, registry=pkg_registry,
        )

    dist_root: _Path | None = None
    if bundle_dir is not None:
        candidate = bundle_dir / "web" / "dist"
        if (candidate / "index.html").is_file():
            dist_root = candidate
    if dist_root is None:
        return None

    rel = (path or "").lstrip("/")
    if not rel:
        target = dist_root / "index.html"
    else:
        target = (dist_root / rel).resolve()
        try:
            target.relative_to(dist_root.resolve())
        except ValueError:
            return _Resp(status_code=403)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            target = dist_root / "index.html"
    # Reuse the asset-rewrite logic from session-scoped static so the
    # declarative-dist path also serves a self-contained iframe.
    session_id = (request.query_params.get("session_id") or "").strip()
    name = (request.query_params.get("name") or "default").strip() or "default"
    return _maybe_rewrite_html_response(str(target), app_id, session_id, name)


# A fingerprint of the form ``-AbCd123EFG`` (Vite/Webpack/Next-style
# content hash) followed by an asset extension means the file is
# immutable: the URL changes when the content changes. Long-cache
# safely. Anything else (HTML, source files, manifest.json) gets
# no-cache.
_HASHED_ASSET_RE = re.compile(
    r"-[A-Za-z0-9_]{6,}\.(?:js|mjs|cjs|css|woff2?|ttf|otf|"
    r"png|jpe?g|webp|gif|svg|ico|map)$"
)


def _static_cache_headers(file_path: str) -> dict[str, str]:
    """Cache headers for files served from disk.

    HTML entries are no-store (so a redeploy isn't masked by a stale
    shell). Hashed assets (``foo-AbCd123.js``) get the immutable
    long-cache treatment because the URL changes when the content
    changes. Everything else: no header (let the default kick in).
    """
    headers: dict[str, str] = {}
    p = file_path.lower()
    if p.endswith(".html") or p.endswith(".htm"):
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        headers["Pragma"] = "no-cache"
        headers["Expires"] = "0"
    elif _HASHED_ASSET_RE.search(file_path):
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return headers


def _serve_static_attachment(
    attachment: Any,
    requested_path: str,
    *,
    app_id: str | None = None,
    session_id: str | None = None,
    name: str | None = None,
):
    """Serve a file from a ``PreviewStatic`` attachment.

    ``attachment.abs_path`` is already resolved + sandbox-checked at
    attach-time. Here we just walk the request path under it, defend
    against ``..`` traversal one more time, and serve via FileResponse.

    For HTML responses, asset URLs that start with ``/`` (Vite/CRA/Next
    default) are rewritten to be prefixed with the preview route so the
    browser fetches them back through the same auth-allowlisted path
    instead of hitting the daemon root and getting a 401.
    """
    from starlette.responses import FileResponse, Response

    base = attachment.abs_path
    if not base or not os.path.isdir(base):
        return None

    rel = (requested_path or "").lstrip("/").replace("\\", "/")
    if not rel:
        rel = attachment.index_file or "index.html"

    target = os.path.normpath(os.path.join(base, rel))
    base_norm = os.path.normpath(base)
    if not target.startswith(base_norm):
        return None
    if os.path.isdir(target):
        target = os.path.join(target, attachment.index_file or "index.html")
    if not os.path.isfile(target):
        if rel != (attachment.index_file or "index.html"):
            spa_target = os.path.join(base_norm, attachment.index_file or "index.html")
            if os.path.isfile(spa_target):
                return _maybe_rewrite_html_response(
                    spa_target, app_id, session_id, name,
                )
        return None

    return _maybe_rewrite_html_response(target, app_id, session_id, name)


# Root-absolute asset references break under our preview routing
# because the daemon root requires auth and the preview route doesn't.
# Rewriting these to start with the preview URL keeps every fetch
# inside the auth-allowlisted ``/api/apps/<id>/preview/*`` path.
#
# Covered by these regexes:
#   • HTML: href/src/action/data-src/poster on any tag (Vite, CRA,
#     Next export, Astro, Nuxt, Remix, plain HTML).
#   • HTML: srcset with multiple comma-separated entries.
#   • CSS: ``url(/foo)`` inside any stylesheet (font-face, background,
#     border-image, ...). Stylesheets are root-relative because the
#     CSS spec resolves them against the *stylesheet*'s own URL, not
#     the document URL — a ``<base>`` tag in HTML doesn't reach them.
#
# Skipped on purpose:
#   • Protocol-relative (``//cdn``) and full URLs (``http://``,
#     ``https://``, ``data:``) so external CDNs still load.
#   • JS string literals (``fetch('/api/x')``, dynamic imports) —
#     too risky to regex, and SPA backends should use PreviewProxy
#     anyway.
_HTML_ROOT_ASSET_RE = _re.compile(
    rb'((?:href|src|action|data-src|poster|formaction|manifest)\s*=\s*["\'])/(?!/)',
    flags=_re.IGNORECASE,
)
# srcset is comma-separated: "/a 1x, /b 2x, /c 3x". Match each "/foo"
# preceded by quote-or-comma-space. Run AFTER _HTML_ROOT_ASSET_RE.
_HTML_SRCSET_RE = _re.compile(
    rb'(srcset\s*=\s*["\'])([^"\']+)(["\'])',
    flags=_re.IGNORECASE,
)
# CSS url(/foo) — single-quoted, double-quoted, or bare. Whitespace
# tolerated around the URL.
_CSS_URL_RE = _re.compile(
    rb'(url\(\s*["\']?)/(?!/)',
    flags=_re.IGNORECASE,
)


def _rewrite_srcset_value(match, prefix: bytes) -> bytes:
    """Rewrite each entry of a srcset attribute. Splits on commas,
    rewrites root-absolute paths in each candidate, joins back."""
    pre = match.group(1)
    raw = match.group(2)
    post = match.group(3)
    parts = [p.strip() for p in raw.split(b",")]
    rewritten_parts = []
    for p in parts:
        if p.startswith(b"/") and not p.startswith(b"//"):
            rewritten_parts.append(prefix + p[1:])
        else:
            rewritten_parts.append(p)
    return pre + b", ".join(rewritten_parts) + post


def _maybe_rewrite_html_response(
    target: str,
    app_id: str | None,
    session_id: str | None,
    name: str | None,
):
    """Return a FileResponse for plain assets, or a Response with
    rewritten content for HTML / CSS files. Rewrite is conditional on
    ``app_id`` being available (some legacy callers don't pass it).

    The function is broader than the name implies: it covers HTML and
    CSS because both reference root-absolute URLs that our auth
    allowlist doesn't cover. JavaScript bundles are left alone — too
    risky to regex-rewrite, and any backend-API call from the bundle
    needs PreviewProxy not PreviewStatic anyway.
    """
    from starlette.responses import FileResponse, Response

    headers = _static_cache_headers(target)
    target_lower = target.lower()
    is_html = target_lower.endswith(".html") or target_lower.endswith(".htm")
    is_css = target_lower.endswith(".css")
    if not is_html and not is_css:
        return FileResponse(target, headers=headers)
    if not app_id:
        return FileResponse(target, headers=headers)
    try:
        with open(target, "rb") as fh:
            body = fh.read()
    except OSError:
        return FileResponse(target, headers=headers)

    prefix = f"/api/apps/{app_id}/preview/".encode("ascii")

    if is_css:
        rewritten = _CSS_URL_RE.sub(b"\\1" + prefix, body)
        headers = dict(headers)
        headers["Content-Type"] = "text/css; charset=utf-8"
        headers["Content-Length"] = str(len(rewritten))
        return Response(content=rewritten, headers=headers)

    # HTML path. Protect two kinds of substrings from rewriting:
    #
    # 1. ``<base>`` tags — author chose them deliberately; rewriting
    #    their value would corrupt deliberate subpath deployments.
    # 2. ``<script>`` blocks — JS string literals like
    #    ``fetch('/api/x')`` or ``import('/lazy.js')`` look like
    #    root-absolute URLs but rewriting them mid-string would
    #    break the JS. SPAs that need backend calls should use
    #    PreviewProxy (documented in the tool prompt).
    #
    # We replace each protected substring with a unique placeholder
    # before running the rewrites, then restore them afterward.
    placeholders: dict[bytes, bytes] = {}

    def _protect(m: _re.Match) -> bytes:
        placeholder = f"__DIGITORN_PROTECT_{len(placeholders)}__".encode("ascii")
        placeholders[placeholder] = m.group(0)
        return placeholder

    # Step 1: protect <base> tags so the asset rewrite doesn't corrupt
    # the author's deliberate base URL.
    rewritten, base_count = _re.subn(
        rb"<base\b[^>]*>", _protect, body, flags=_re.IGNORECASE,
    )
    has_existing_base = base_count > 0

    # Step 2: rewrite HTML attributes FIRST. This catches the ``src``
    # of ``<script src="/main.js">`` BEFORE we protect the script
    # block — otherwise the src would be inside a protected zone
    # and never get prefixed.
    rewritten = _HTML_ROOT_ASSET_RE.sub(b"\\1" + prefix, rewritten)
    rewritten = _HTML_SRCSET_RE.sub(
        lambda m: _rewrite_srcset_value(m, prefix), rewritten,
    )

    # Step 3: protect <script>...</script> bodies. JS string literals
    # like ``fetch('/api/x')`` or ``import('/lazy.js')`` look like
    # root-absolute URLs but rewriting them would break the JS. Apps
    # that need backend calls should use PreviewProxy. The src
    # attribute was already rewritten in step 2 and stays rewritten
    # because it's part of the open tag — preserved by replacement
    # of the whole script block back into the output.
    rewritten = _re.sub(
        rb"<script\b[^>]*>.*?</script>",
        _protect, rewritten, flags=_re.IGNORECASE | _re.DOTALL,
    )

    # Step 4: CSS pass — root-absolute ``url(/...)`` inside inline
    # ``<style>`` blocks AND ``style="..."`` HTML attributes. With
    # script bodies now protected, this won't touch JS strings
    # containing ``url(`` patterns.
    rewritten = _CSS_URL_RE.sub(b"\\1" + prefix, rewritten)

    # Step 5: restore protected substrings (base + scripts).
    for placeholder, original in placeholders.items():
        rewritten = rewritten.replace(placeholder, original, 1)

    # Inject a base tag right after <head> so RELATIVE URLs the build
    # may emit (Vite ``base: "./"``, CRA ``"homepage": "."``) also
    # resolve under the preview route. Skip the inject when the
    # document already declares a <base href> — overriding it would
    # break apps that intentionally pin their own base.
    if not has_existing_base:
        # Match opening <head> with optional attributes (<head lang="...">
        # is common in Astro / 11ty output). Inject right after the tag.
        head_match = _re.search(rb"<head\b[^>]*>", rewritten, _re.IGNORECASE)
        if head_match:
            base_tag = b'<base href="' + prefix + b'">'
            insert_at = head_match.end()
            rewritten = rewritten[:insert_at] + base_tag + rewritten[insert_at:]

    headers = dict(headers)
    headers["Content-Type"] = "text/html; charset=utf-8"
    headers["Content-Length"] = str(len(rewritten))
    return Response(content=rewritten, headers=headers)


async def _proxy_preview_http(
    request: Request,
    app_id: str,
    path: str,
):
    """Serve a preview asset.

    Resolution order:
      1. Session-scoped attachment from the ``web_preview`` module —
         the LLM has called ``PreviewProxy(port=...)`` or
         ``PreviewStatic(path=...)`` for this (session_id, name).
         Type drives the behaviour (proxy to port, or serve files from
         a workspace dir).
      2. App ships a pre-built ``web/dist/`` at its install dir —
         declarative static preview, no LLM action required.
      3. ``404 Not Found`` with a hint pointing at PreviewProxy /
         PreviewStatic.
    """
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

    session_id = (request.query_params.get("session_id") or "").strip()
    name = (request.query_params.get("name") or "default").strip() or "default"

    # Subresource fallback: when the iframe HTML loaded with
    # ``?session_id=X`` references assets via root-absolute paths
    # (``<script src="/assets/...">``), our HTML rewriter rewrites the
    # path prefix but leaves the query string off. The browser then
    # fetches assets WITHOUT ``session_id=`` and the attachment
    # lookup misses. Recover by parsing the ``Referer`` header — when
    # set to the iframe HTML URL, it carries the session_id and name
    # we need.
    if not session_id:
        referer = request.headers.get("referer") or ""
        if referer:
            try:
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(referer)
                qs = parse_qs(parsed.query)
                ref_sid = (qs.get("session_id") or [""])[0].strip()
                ref_name = (qs.get("name") or ["default"])[0].strip() or "default"
                if ref_sid:
                    session_id = ref_sid
                    name = ref_name
            except Exception:
                pass

    web_preview_mod = None
    try:
        web_preview_mod = deployed.modules.get("web_preview") if hasattr(deployed, "modules") else None
    except Exception:
        web_preview_mod = None

    if web_preview_mod is not None and session_id:
        try:
            attachment = web_preview_mod.get_attachment(session_id, name)
        except Exception as exc:
            logger.debug("web_preview_lookup_failed: %s", exc)
            attachment = None
        if attachment is not None:
            if attachment.type == "static":
                resp = _serve_static_attachment(
                    attachment, path,
                    app_id=app_id, session_id=session_id, name=name,
                )
                if resp is not None:
                    return resp
                raise HTTPException(
                    status_code=404,
                    detail=f"File not found in static attachment '{name}': {path or '(root)'}",
                )
            if attachment.type == "proxy":
                # Direct-connect mode: redirect the iframe to the
                # publicly reachable URL. The browser then talks to
                # the dev server WITHOUT the daemon as a middleman —
                # no buffer, no cache rewrites, no WS bridge needed,
                # HMR works trivially. The Socket.IO ``web_preview:
                # attach`` event already gave the client a fresh URL;
                # this 302 path is just a safety net for legacy
                # clients that loaded ``/preview/?...`` directly
                # (deep link, manual reload, missed handshake event).
                from starlette.responses import RedirectResponse
                from digitorn.modules.web_preview.module import WebPreviewModule
                target = WebPreviewModule.render_public_url(
                    host=attachment.host,
                    port=attachment.port,
                    app_id=app_id,
                    session_id=session_id,
                    name=name,
                )
                qs = request.url.query
                if qs:
                    sep = "&" if "?" in target else "?"
                    target = f"{target}{sep}{qs}"
                return RedirectResponse(url=target, status_code=307)

    # No session attachment — try the app's own static dist (declarative case).
    static_resp = await _try_serve_static_dist(request, app_id, path)
    if static_resp is not None:
        return static_resp

    # Browser GET (iframe): return a styled HTML page so the user sees a
    # clean "preview not ready yet" screen instead of raw JSON. Probes
    # (HEAD) and programmatic callers (Accept: application/json) keep
    # the structured 404 so the frontend availability store and any
    # API consumer keep working unchanged.
    accept = (request.headers.get("accept") or "").lower()
    wants_html = (
        request.method == "GET"
        and "text/html" in accept
        and "application/json" not in accept
    )
    if wants_html:
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            status_code=404,
            content=_render_no_preview_html(app_id, name),
        )
    raise HTTPException(
        status_code=404,
        detail=(
            f"No preview attached for app '{app_id}', session "
            f"'{session_id or '(none)'}', name '{name}'. The agent "
            f"can attach one via PreviewProxy(port=...) or "
            f"PreviewStatic(path=...)."
        ),
    )


def _render_no_preview_html(app_id: str, name: str) -> str:
    """Standalone HTML shown inside the iframe when nothing is attached.

    User-facing copy (no SDK names): the page is loaded inside the
    Preview tab and may be the first thing a non-technical user sees,
    so we communicate intent ("being prepared") instead of mechanism.

    Auto-polls every 3 s so that when the agent finally attaches, the
    iframe's next load gets the real content (302 redirect for proxy,
    file content for static) without the user having to refresh.
    """
    name_label = "" if name == "default" else name
    name_chip = (
        f'<span class="chip">{name_label}</span>' if name_label else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Preparing preview · {app_id}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #fbfbfd;
    --bg-grad-1: #ffffff;
    --bg-grad-2: #f3f4f8;
    --text: #0a0a14;
    --text-muted: #5b5e6c;
    --text-soft: #8a8d99;
    --border: rgba(10, 10, 20, 0.08);
    --card: rgba(255, 255, 255, 0.7);
    --card-shadow: 0 1px 2px rgba(10, 10, 20, 0.04),
                   0 8px 24px -8px rgba(10, 10, 20, 0.08);
    --accent: #5b6cf2;
    --accent-glow: rgba(91, 108, 242, 0.18);
    --grid: rgba(10, 10, 20, 0.04);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0a0a0f;
      --bg-grad-1: #11121a;
      --bg-grad-2: #06070b;
      --text: #f4f5fa;
      --text-muted: #a1a4b3;
      --text-soft: #6b6e7d;
      --border: rgba(255, 255, 255, 0.08);
      --card: rgba(20, 22, 32, 0.65);
      --card-shadow: 0 1px 2px rgba(0, 0, 0, 0.4),
                     0 16px 40px -12px rgba(0, 0, 0, 0.6);
      --accent: #8b9aff;
      --accent-glow: rgba(139, 154, 255, 0.22);
      --grid: rgba(255, 255, 255, 0.035);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0; height: 100%; width: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display",
      "Segoe UI Variable", "Segoe UI", Inter, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    background: var(--bg);
    color: var(--text);
    overflow: hidden;
  }}
  body {{
    display: flex; align-items: center; justify-content: center;
    padding: 24px;
    background:
      radial-gradient(ellipse 80% 60% at 50% 0%, var(--accent-glow), transparent 70%),
      radial-gradient(ellipse 60% 50% at 50% 100%, var(--bg-grad-2), transparent 70%),
      linear-gradient(180deg, var(--bg-grad-1) 0%, var(--bg) 100%);
  }}
  .grid {{
    position: fixed; inset: 0; pointer-events: none; z-index: 0;
    background-image:
      linear-gradient(var(--grid) 1px, transparent 1px),
      linear-gradient(90deg, var(--grid) 1px, transparent 1px);
    background-size: 32px 32px;
    mask-image: radial-gradient(ellipse at center, #000 30%, transparent 75%);
    -webkit-mask-image: radial-gradient(ellipse at center, #000 30%, transparent 75%);
  }}
  .card {{
    position: relative; z-index: 1;
    width: 100%; max-width: 440px;
    padding: 44px 36px 32px;
    /* Solid fallback first (covers WebViews without backdrop-filter
       support - older Chromium, some Android WebViews). The
       transparent glass treatment is a progressive enhancement. */
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid var(--border);
    border-radius: 16px;
    box-shadow: var(--card-shadow);
    text-align: center;
    animation: rise 600ms cubic-bezier(0.2, 0.8, 0.2, 1) both;
  }}
  @media (prefers-color-scheme: dark) {{
    .card {{ background: rgba(20, 22, 32, 0.96); }}
  }}
  @supports (backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)) {{
    .card {{
      background: var(--card);
      backdrop-filter: blur(20px) saturate(140%);
      -webkit-backdrop-filter: blur(20px) saturate(140%);
    }}
  }}
  @keyframes rise {{
    from {{ opacity: 0; transform: translateY(8px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}
  .pulse {{
    width: 56px; height: 56px;
    margin: 0 auto 22px;
    position: relative;
    display: grid; place-items: center;
  }}
  .pulse::before, .pulse::after {{
    content: ""; position: absolute; inset: 0;
    border-radius: 50%;
    border: 1px solid var(--accent);
    opacity: 0;
    animation: ripple 2.4s cubic-bezier(0.2, 0.8, 0.2, 1) infinite;
  }}
  .pulse::after {{ animation-delay: 1.2s; }}
  @keyframes ripple {{
    0%   {{ opacity: 0.55; transform: scale(0.4); }}
    80%  {{ opacity: 0;    transform: scale(1.6); }}
    100% {{ opacity: 0;    transform: scale(1.6); }}
  }}
  .dot {{
    width: 14px; height: 14px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 0 6px var(--accent-glow);
    animation: breathe 2.4s ease-in-out infinite;
  }}
  @keyframes breathe {{
    0%, 100% {{ transform: scale(1);    opacity: 1;    }}
    50%      {{ transform: scale(0.92); opacity: 0.78; }}
  }}
  h1 {{
    font-size: 19px;
    font-weight: 600;
    letter-spacing: -0.01em;
    margin: 0 0 10px;
    color: var(--text);
  }}
  p {{
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--text-muted);
    margin: 0 0 24px;
    max-width: 320px;
    margin-left: auto; margin-right: auto;
  }}
  .meta {{
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--card);
    font-size: 11.5px;
    font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
    color: var(--text-soft);
    letter-spacing: 0.01em;
  }}
  .meta .sep {{
    width: 3px; height: 3px;
    border-radius: 50%;
    background: var(--text-soft);
    opacity: 0.5;
  }}
  .chip {{ color: var(--text-muted); }}
  .status {{
    margin-top: 20px;
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 11.5px;
    color: var(--text-soft);
    letter-spacing: 0.02em;
  }}
  .status .live {{
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--accent);
    animation: blink 1.6s ease-in-out infinite;
  }}
  @keyframes blink {{
    0%, 100% {{ opacity: 1;    }}
    50%      {{ opacity: 0.35; }}
  }}
</style>
</head>
<body>
  <div class="grid"></div>
  <main class="card">
    <div class="pulse"><div class="dot"></div></div>
    <h1>Preparing your preview</h1>
    <p>Your assistant is setting things up. This usually takes a few seconds.</p>
    <div class="meta">
      <span>{app_id}</span>
      {('<span class="sep"></span>' + name_chip) if name_chip else ''}
    </div>
    <div class="status">
      <span class="live"></span>
      <span>Listening for the next change</span>
    </div>
  </main>
  <script>
    // Quietly re-fetch the iframe URL every 3 s. As soon as the agent
    // attaches, the daemon stops returning this placeholder and the
    // iframe swaps to the real content. No user action required.
    setTimeout(function () {{ location.reload(); }}, 3000);
  </script>
</body>
</html>"""


def _serialise_widget_node(node: Any) -> dict[str, Any]:
    """Pydantic WidgetNode → JSON dict, recursively flattening children."""
    if node is None:
        return None
    if hasattr(node, "model_dump"):
        return node.model_dump(by_alias=True, exclude_none=True)
    return node


def _serialise_widgets(widgets_cfg: Any) -> dict[str, Any]:
    if widgets_cfg is None:
        return {"version": 1, "chat_side": None, "workspace_tabs": [], "modals": {}, "inline": {}}
    return widgets_cfg.model_dump(by_alias=True, exclude_none=True)


async def _execute_widget_tool(
    deployed: Any,
    tool: str,
    args: dict[str, Any],
    session_id: str | None = None,
) -> Any:
    """Resolve a tool by name and execute it through the app's modules.

    Walks the deployed app's modules until one accepts the action.
    Tool naming convention: ``module.action`` (e.g. ``filesystem.read``)
    or short PascalCase (e.g. ``Read``) - both routed via the runtime
    tool name resolver.
    """
    from digitorn.core.runtime.tool_names import to_fqn

    fqn = to_fqn(tool)
    if "." not in fqn:
        raise ValueError(f"cannot resolve tool {tool!r}")
    module_id, action_name = fqn.split(".", 1)
    module = deployed.modules.get(module_id)
    if module is None:
        raise ValueError(f"module {module_id!r} not loaded for this app")

    if hasattr(module, "set_active_session") and session_id:
        module.set_active_session(session_id)

    handler = getattr(module, action_name, None)
    if handler is None:
        raise ValueError(f"action {action_name!r} not found on module {module_id!r}")

    # Build the params model from the action's @action registry entry.
    registry = getattr(module, "_action_registry", {}) or {}
    spec = registry.get(action_name)
    if spec and getattr(spec, "params_model", None):
        params = spec.params_model(**args)
        result = await handler(params)
    else:
        result = await handler(args)

    if hasattr(result, "data"):
        return {"success": result.success, "data": result.data, "error": result.error}
    return result


def _get_quota_store(request: Request):
    """Pull the shared ``QuotaStore``.

    Preference order:
      1. ``app.state.quota_store`` - injected at boot.
      2. ``manager._quota_store`` - lives on the AppManager, shares the
         session KV backend so definitions + counters survive restart.
      3. Lazy init against the rate_limiter's KV backend - for early
         boot paths where the manager isn't up yet.
    """
    store = getattr(request.app.state, "quota_store", None)
    if store is not None:
        return store
    manager = getattr(request.app.state, "app_manager", None)
    if manager is not None:
        store = getattr(manager, "_quota_store", None)
        if store is not None:
            request.app.state.quota_store = store
            return store
    limiter = _get_rate_limiter(request)
    from digitorn.core.quota import QuotaStore
    store = QuotaStore(limiter._backend)
    request.app.state.quota_store = store
    return store


def _require_admin_for_quota(request: Request) -> None:
    """Quota routes are admin-only - we enforce ``*`` or ``admin`` perm."""
    perms = list(getattr(request.state, "permissions", []) or [])
    if "*" in perms or "admin" in perms or "apps:admin" in perms:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Quota management is admin-only. Ask your daemon administrator "
            "to configure quotas for this app or user."
        ),
    )


def _usage_snapshot(
    limiter: Any, app_id: str, user_id: str | None = None,
) -> dict[str, Any]:
    """Current rolling counters (what's already been consumed).

    For v1 we only track request RPM counters through the rate limiter.
    Token / cost / message counters need provider-side hooks and are
    reported as zero until those hooks ship - the admin UI should still
    display the limit itself.
    """
    try:
        u = limiter.get_usage(app_id, user_id=user_id)
    except Exception as exc:
        logger.debug("quota usage snapshot failed: %s", exc)
        u = {}
    requests_minute = int(u.get("current", 0) if isinstance(u, dict) else 0)
    return {
        "requests": {
            "last_minute": requests_minute,
            "last_hour": None,   # not tracked yet - requires wider window
            "last_day": None,
        },
        "tokens": {
            "input_last_minute": None,
            "output_last_minute": None,
            "total_today": None,
        },
        "cost_usd": {
            "today": None,
            "this_month": None,
        },
        "concurrent_sessions": None,
        "window_seconds": u.get("window_seconds") if isinstance(u, dict) else None,
    }


def _walk_yaml_for_secrets(
    node: Any,
    path: list[str],
    hits: dict[str, dict[str, Any]],
    *,
    provider_hint: str | None = None,
    agent_hint: str | None = None,
) -> None:
    """Recursive DFS over the parsed YAML, collecting every secret reference
    AND the owning provider / agent when it can be inferred from the
    surrounding structure.

    ``hits`` maps ``secret_key`` → dict with:
        - ``locations``: dotted paths where the secret appears
        - ``providers``: set of canonical provider names inferred
          from the enclosing ``agents[i].brain.provider`` or
          ``modules.llm_provider.config.providers.{pid}.provider``
        - ``agents``: set of agent ids (when the reference lives
          inside an ``agents[i]`` block)

    The inference is best-effort and conservative - when the walker
    cannot tell, the fields stay empty and the client is left to
    infer from the secret name itself.
    """
    if isinstance(node, str):
        for match in _SECRET_REF_RE.finditer(node):
            key = match.group(2)
            entry = hits.setdefault(
                key, {"locations": [], "providers": set(), "agents": set()},
            )
            entry["locations"].append(".".join(path))
            if provider_hint:
                entry["providers"].add(provider_hint)
            if agent_hint:
                entry["agents"].add(agent_hint)
        return

    if isinstance(node, dict):
        # Descend each child, extending the current hints where the
        # child is a known provider/agent root.
        for k, v in node.items():
            child_path = path + [str(k)]
            child_provider = provider_hint
            child_agent = agent_hint

            # ── agents[i] - inline brains ────────────────────────
            # ``agents[i].brain`` carries a ``provider:`` field
            # (deepseek, anthropic, …). When we dive into the
            # brain subtree, adopt it as the enclosing provider
            # and the agent id as the enclosing agent.
            if k == "brain" and isinstance(v, dict):
                p = v.get("provider")
                if isinstance(p, str) and p:
                    child_provider = p
                # agent id is one level up (the parent dict)
                parent_id = node.get("id") if isinstance(node, dict) else None
                if isinstance(parent_id, str) and parent_id:
                    child_agent = parent_id

            # ── modules.llm_provider.config.providers.{pid} ─────
            # Each named provider entry has its own ``provider``
            # field; if missing, the dict key itself IS the
            # provider name in the single-provider flat form.
            if (
                len(path) >= 3
                and path[-3:] == ["modules", "llm_provider", "config"]
                and k == "providers"
                and isinstance(v, dict)
            ):
                # The children of "providers" are per-pid dicts.
                for pid, pconf in v.items():
                    sub_provider = None
                    if isinstance(pconf, dict):
                        sub_provider = pconf.get("provider") or pid
                    _walk_yaml_for_secrets(
                        pconf, child_path + [str(pid)], hits,
                        provider_hint=sub_provider or provider_hint,
                        agent_hint=agent_hint,
                    )
                continue

            _walk_yaml_for_secrets(
                v, child_path, hits,
                provider_hint=child_provider,
                agent_hint=child_agent,
            )
        return

    if isinstance(node, list):
        for i, item in enumerate(node):
            _walk_yaml_for_secrets(
                item, path + [f"[{i}]"], hits,
                provider_hint=provider_hint,
                agent_hint=agent_hint,
            )
        return


def _get_manager(request: Request):
    """Get the AppManager from app state."""
    manager = getattr(request.app.state, "app_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="AppManager not available - daemon may still be starting",
        )
    return manager


def _get_rate_limiter(request: Request):
    """Get the RateLimiter from app state."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise HTTPException(
            status_code=503,
            detail="RateLimiter not available - daemon may still be starting",
        )
    return limiter


class DeployRequest(BaseModel):
    """Request body for deploying an app."""

    yaml_path: str | None = None
    force: bool = False
    secrets: dict[str, str] | None = None
    # Scope of the install:
    #   - ``"user"``  : the install belongs to the caller (the default
    #                   for non-admin callers). Visible only to them
    #                   AND fully manageable (delete / redeploy) by
    #                   them via the same DELETE / POST endpoints
    #                   without needing admin perms.
    #   - ``"system"``: install is global, visible to every user.
    #                   Only an admin caller (perm "*") can deploy at
    #                   this scope; non-admins requesting it get
    #                   downgraded to "user" by the endpoint.
    #   - ``None``    : let the endpoint pick — admins get system,
    #                   everyone else gets user.
    scope: Literal["system", "user"] | None = None


class RunRequest(BaseModel):
    """Request body for running a one-shot app."""

    input: str
    input_type: str = "text"


class ChatRequest(BaseModel):
    """Request body for a conversation message."""

    session_id: str
    message: str
    workspace: str | None = None


class AppSummary(BaseModel):
    """Summary of a deployed app."""

    app_id: str
    name: str
    version: str
    mode: str
    agents: list[str]
    modules: list[str]
    total_tools: int
    total_categories: int
    deployed_at: float
    workspace_mode: str = "auto"
    greeting: str = ""


class AppResponse(BaseModel):
    """Standard API response wrapper."""

    success: bool
    data: Any = None
    error: str | None = None


class ValidateRequest(BaseModel):
    yaml_path: str


class PipelineRequest(BaseModel):
    input: str
    steps: list[dict[str, Any]] = []


class NotificationCheckRequest(BaseModel):
    session_id: str


class SessionMessageRequest(BaseModel):
    # BUG-091 + BUG-092: reject ONLY the audio/audios/audio_refs
    # fields that used to be silently dropped - any other unknown
    # field is still tolerated so new client-side additions don't
    # break the chat. The previous revision used ``extra="forbid"``
    # which rejected ANY unknown field and broke sending messages
    # from clients that ship fields like ``metadata``/``attachments``.
    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def _reject_audio_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for _k in ("audio", "audios", "audio_refs", "audio_ref"):
                if _k in data and data[_k] not in (None, "", [], {}):
                    # Raise as ValueError - FastAPI converts it to a
                    # clean 422 with the field name + guidance.
                    raise ValueError(
                        f"Field '{_k}' is not accepted. POST the blob "
                        f"to /api/transcribe and include the returned "
                        f"text in 'message'."
                    )
        return data

    # BUG-062: a 50 MiB message was accepted in 2.6s with zero
    # protection, and four of them in parallel stalled the event loop
    # ~60s (BUG-063). Pydantic enforces the cap before the body ever
    # reaches the handler - the client gets a clean 422 instead of a
    # silent stall.
    message: str = Field(..., max_length=_MESSAGE_MAX_BYTES)
    workspace: str | None = None
    images: list[dict[str, Any]] | None = None  # [{data: "base64...", mime: "image/png", name: "screenshot.png"}]
    queue_mode: str | None = Field(
        default=None,
        description=(
            "Queue behavior: 'async' (default, 202 + SSE events) or "
            "'wait' (legacy, block until turn finishes). Omit to use "
            "session.queue.default_mode from config."
        ),
    )
    client_message_id: str | None = Field(
        default=None,
        description=(
            "Optional client-generated idempotency key. Echoed back in "
            "the `user_message` event so the client can match its "
            "optimistic bubble to the authoritative server echo instead "
            "of deduping by content + turn."
        ),
    )


class CreateSessionRequest(BaseModel):
    """Body for `POST /sessions` - atomic session creation + first message.

    Sessions can no longer be created empty: every new session is born
    with a first user message. This eliminates "ghost sessions" - rows
    in the DB created by a curious client that opens a session and
    walks away without ever sending anything. The frontend never sees
    a session it can't list a message in.

    The message is dispatched through the same per-session FIFO queue
    as ``POST /sessions/{sid}/messages``; the response includes the
    correlation_id + state envelope so the caller can wire its UI to
    the live event stream immediately. Subsequent messages reuse
    ``POST /sessions/{sid}/messages`` (existing endpoint, unchanged).

    When ``workspace_path`` is provided, the session is bound to that
    filesystem directory and the preview/workspace persistence backend
    switches to filesystem mode (state lives in
    ``{workspace_path}/.digitorn/sessions/{sid}/`` instead of the daemon DB).
    Apps that declare ``execution.workspace_mode: required`` MUST receive
    a ``workspace_path`` here - otherwise the request is rejected with a
    400 ``workspace_required`` before any DB write.
    """
    message: str = Field(..., min_length=1, max_length=_MESSAGE_MAX_BYTES)
    workspace_path: str | None = None
    images: list[dict[str, Any]] | None = None
    queue_mode: str | None = Field(
        default=None,
        description=(
            "Queue behavior for the first message: 'async' (default, "
            "202 + SSE events) or 'wait' (legacy, block until turn "
            "finishes). Omit to use session.queue.default_mode."
        ),
    )
    client_message_id: str | None = Field(
        default=None,
        description=(
            "Optional client-generated idempotency key. Echoed back in "
            "the ``user_message`` event so the optimistic bubble can "
            "be reconciled."
        ),
    )


class WorkspaceImportRequest(BaseModel):
    """Payload for importing / fork'ing a workspace snapshot."""
    snapshot: dict[str, Any] = Field(
        default_factory=dict,
        description="Full snapshot in the shape returned by GET /workspace "
                    "(keys: state, resources, seq).",
    )
    replace: bool = Field(
        default=True,
        description="If True, wipe the destination snapshot before importing. "
                    "If False, merge into existing state/resources.",
    )


class WorkspaceForkRequest(BaseModel):
    """Payload for forking a snapshot into a new session."""
    target_session_id: str | None = Field(
        default=None,
        description="Optional explicit session id for the fork. "
                    "If omitted the daemon creates one.",
    )
    title: str | None = Field(
        default=None,
        description="Optional session title override.",
    )


class FileActionRequest(BaseModel):
    """Body for file validation actions."""
    path: str = Field(..., description="Workspace-relative file path.")


class HunksActionRequest(BaseModel):
    path: str = Field(..., description="Workspace-relative file path.")
    hunks: list = Field(
        default_factory=list,
        description="Hunk indices (int) or hashes (12-char str).",
    )


class WritebackRequest(BaseModel):
    content: str = Field(..., description="New file content.")
    auto_approve: bool = Field(default=False, description="Snapshot as baseline immediately.")
    source: str = Field(default="user", description="Attribution - 'user' / 'import' / 'script'.")


class CommitRequest(BaseModel):
    message: str = Field(..., description="Commit message.")
    files: list[str] | None = Field(
        default=None, description="Explicit paths (null = all approved).",
    )
    push: bool = Field(default=False, description="git push after commit.")


class LspRpcRequest(BaseModel):
    """Body for ``POST /lsp/request``.

    ``method`` + ``params`` follow the Language Server Protocol spec
    (textDocument/hover, textDocument/definition, textDocument/references,
    textDocument/completion, textDocument/rename, textDocument/signatureHelp,
    textDocument/documentSymbol, …).

    **Phase 3 additions** - abort + debounce semantics:

    - ``request_id`` (optional client uuid) - correlation id for the
      companion ``POST /lsp/cancel`` endpoint. When omitted, the daemon
      mints one and returns it in the response.
    - ``supersede_previous`` (default ``true``) - auto-cancel any
      in-flight request for the same ``(session, path, method)`` triple
      when it's a keystroke-driven method (completion, hover,
      signatureHelp). Set ``false`` on user-initiated references / rename
      so the result always lands.
    """
    path: str = Field(
        ..., description=(
            "File path. Absolute or workspace-relative. Used to route "
            "to the right language server via its registered extensions."
        ),
    )
    method: str = Field(
        ..., description="LSP method name (e.g. 'textDocument/hover').",
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Raw LSP request params. ``textDocument.uri`` auto-filled "
            "from ``path`` if omitted; everything else is passed through."
        ),
    )
    timeout_seconds: float = Field(
        default=10.0, ge=0.1, le=60.0,
        description="Server-side timeout for the RPC call.",
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "Client correlation id - use it to cancel this specific "
            "request later. Daemon mints one if omitted."
        ),
    )
    supersede_previous: bool = Field(
        default=True,
        description=(
            "When True, a new request for the same (session, path, method) "
            "triple cancels any in-flight request for that triple. Right "
            "default for keystroke methods; set False for user-initiated "
            "ones (references, rename)."
        ),
    )


class LspCancelRequest(BaseModel):
    """Body for ``POST /lsp/cancel`` - cancel an in-flight LSP request."""
    request_id: str = Field(
        ..., description="Correlation id returned by /lsp/request.",
    )


class BackgroundSessionCreateRequest(BaseModel):
    name: str = ""
    params: dict[str, Any] = {}
    routing_keys: dict[str, str] = {}
    workspace: str = ""


class PayloadSetRequest(BaseModel):
    """Body for PUT /background-sessions/{sid}/payload."""

    prompt: str | None = None
    metadata: dict[str, Any] | None = None


class BackgroundTaskRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


class BackgroundTaskActionRequest(BaseModel):
    timeout: float = 60.0


class WatcherCreateRequest(BaseModel):
    """Request body for creating a watcher."""

    tool: str
    params: dict[str, Any] = {}
    interval: float = 30.0
    label: str = ""
    notify_when: str = "on_change"
    notify_config: dict[str, Any] = {}


class ToolExecuteRequest(BaseModel):
    params: dict[str, Any] = {}
    session_id: str | None = None


class WidgetActionRequest(BaseModel):
    widget_id: str
    action_id: str | None = None
    type: str  # tool | http | chat | set_state | refresh | sequence | open_modal | close | …
    payload: dict[str, Any] = {}
    form: dict[str, Any] = {}
    state: dict[str, Any] = {}
    session_id: str | None = None


class InteractRequest(BaseModel):
    """User interaction with a workspace widget."""
    module_id: str
    widget: str
    action: str
    state: dict[str, Any] = {}


class DisableRequest(BaseModel):
    """Optional body for POST /api/apps/{id}/disable.

    A free-text ``reason`` is persisted on the DB row so other admins
    can see why the app was taken down (e.g. "security incident
    2026-04-17", "migrating to new API key", "user request via
    support ticket #1234").
    """
    reason: str | None = None


class ApprovalResolveRequest(BaseModel):
    """Request body for approving/denying a pending action.

    Accepts the user's response under any of these field names so the
    Flutter / web clients can use whatever convention they prefer:
    ``message``, ``response``, ``answer``, ``value``, ``reply``,
    ``user_response``. The first non-empty one wins. ``message``
    remains the canonical name for backwards compat.
    """

    model_config = {"extra": "allow"}

    request_id: str
    approved: bool
    message: str = ""
    response: str = ""
    answer: str = ""
    value: str = ""
    reply: str = ""
    user_response: str = ""

    def resolved_payload(self) -> str:
        for name in ("message", "response", "answer", "value", "reply", "user_response"):
            v = getattr(self, name, "") or ""
            if v:
                return v
        return ""


class SecretSetRequest(BaseModel):
    """Request body for setting a secret."""

    value: str


class SecretsBulkSetRequest(BaseModel):
    """Body for PUT /{app_id}/secrets - set multiple secrets at once."""

    secrets: dict[str, str] = Field(
        ...,
        description=(
            "Map of secret name → value. Empty values are rejected. "
            "Existing secrets with the same name are overwritten."
        ),
    )


class OAuthCallbackParams(BaseModel):
    """Query params from OAuth callback."""

    code: str
    state: str


class InjectOAuthTokenRequest(BaseModel):
    """Request body for injecting an OAuth token into an MCP server."""
    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str | None = None

