"""Digitorn — App management API routes.

    Session/chat events stream through Socket.IO (/events namespace), NOT
    HTTP SSE. See ``core/events/socketio_bus.py``. This module keeps the
    REST endpoints for deploy/undeploy, session CRUD, tool discovery,
    background tasks, watchers, approvals, quota.

    --- App lifecycle ---
    POST   /deploy                         Deploy from YAML path
    POST   /deploy/upload                  Deploy from file upload
    GET    /                               List deployed apps
    GET    /{app_id}                       Get app details
    POST   /{app_id}/run                   One-shot execution
    DELETE /{app_id}                       Undeploy

    --- Sessions (SDK) ---
    GET    /{app_id}/sessions              List sessions
    GET    /{app_id}/sessions/{sid}        Get session metadata
    GET    /{app_id}/sessions/{sid}/history Full message history
    DELETE /{app_id}/sessions/{sid}        Delete session
    POST   /{app_id}/sessions/{sid}/messages Send message (events via Socket.IO)

    --- Background tasks ---
    POST   /{app_id}/background-tasks      Launch task
    GET    /{app_id}/background-tasks      List all tasks
    GET    /{app_id}/background-tasks/{id} Get status + result
    DELETE /{app_id}/background-tasks/{id} Cancel task
    POST   /{app_id}/background-tasks/{id}/wait  Wait with timeout

    --- Watchers (persistent monitoring) ---
    POST   /{app_id}/watchers              Create watcher
    GET    /{app_id}/watchers              List all watchers
    GET    /{app_id}/watchers/{wid}        Get watcher status + history
    DELETE /{app_id}/watchers/{wid}        Stop watcher
    POST   /{app_id}/watchers/{wid}/pause  Pause watcher
    POST   /{app_id}/watchers/{wid}/resume Resume watcher

    --- Tool discovery ---
    GET    /{app_id}/tools/search?query=   Semantic + keyword search
    GET    /{app_id}/tools/categories      List categories
    GET    /{app_id}/tools/categories/{c}  Browse category (paginated)
    GET    /{app_id}/tools/{name}          Full tool schema
    POST   /{app_id}/tools/{name}/execute  Execute tool directly

    --- Index ---
    GET    /{app_id}/index                 Full tool index structure

    --- Notifications ---
    POST   /{app_id}/notifications         Drain bg notifications
    GET    /{app_id}/notifications/active   Quick bg task check

    --- Approvals ---
    GET    /{app_id}/approvals             List pending
    POST   /{app_id}/approve               Resolve (approve/deny)

    --- Rate limiting ---
    GET    /{app_id}/quota                 Get usage
    PUT    /{app_id}/quota                 Set custom limit
    DELETE /{app_id}/quota                 Reset to default

    --- Secrets ---
    GET    /{app_id}/secrets               List secret keys
    GET    /{app_id}/secrets/{key}         Check if secret exists
    PUT    /{app_id}/secrets/{key}         Set secret
    DELETE /{app_id}/secrets/{key}         Delete secret

    --- OAuth2 (MCP) ---
    GET    /{app_id}/oauth/authorize       Start OAuth flow
    GET    /{app_id}/oauth/callback        OAuth callback (code exchange)
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

import os
import re
import re as _re

# ── Concurrency control for agent turns ──────────────────────────────
# Limits how many agent turns can run concurrently across all apps.
# Beyond this, /messages returns 503 so the event loop is never starved.
_MAX_CONCURRENT_TURNS = int(os.environ.get("DIGITORN_MAX_CONCURRENT_TURNS", "400"))
_turn_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TURNS)
# Tracked tasks — prevents GC of fire-and-forget tasks + enables diagnostics
_active_turn_tasks: set[asyncio.Task] = set()

# Dots are allowed in app IDs (e.g. "my-org.app") but consecutive dots
# ("..") are forbidden to prevent path-traversal when the ID is used in
# filesystem or URL path construction.
_SAFE_ID_RE = _re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,127}$')


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
    and produces a clean list of {role, content, toolCalls?, thinking?} objects.
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
            turns.append({"role": "user", "content": m.get("content", "")})
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
                if thinking:
                    turn["thinking"] = thinking
                turns.append(turn)
                continue

            turn = {"role": "assistant", "content": content}
            if tool_calls:
                turn["tool_calls"] = tool_calls
                turn["toolCalls"] = tool_calls
            if thinking:
                turn["thinking"] = thinking
            if content.strip() or tool_calls:
                turns.append(turn)

    return turns


# ── Error classification (structured errors for ALL clients) ─────────


def _classify_error(exc: Exception) -> dict[str, Any]:
    """Classify an exception into a structured error dict for event clients.

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
    # This takes precedence over generic auth so the frontend can
    # react with a credential picker rather than an error toast.
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
    except Exception:
        pass

    # ── Session busy (lock contention, not a real crash) ─────────
    # Thrown by manager.chat when a previous turn on the same session
    # is still running. The frontend should disable the composer until
    # the current turn finishes (or the user aborts it) instead of
    # retrying blindly.
    if "session lock timeout" in msg_lower:
        return {
            "error": (
                "A previous turn is still running on this session. "
                "Wait for it to finish or click Abort before sending "
                "another message."
            ),
            "code": "session_busy",
            "category": "concurrency",
            "retry": False,
            "detail": msg[:500],
        }

    # ── Billing / Quota ──────────────────────────────────────────
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

    # ── Authentication ───────────────────────────────────────────
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

    # ── Rate Limit ───────────────────────────────────────────────
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

    # ── Context Overflow ─────────────────────────────────────────
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

    _network_exc_types = (
        "ReadError", "ConnectError", "ConnectTimeout", "ReadTimeout",
        "WriteError", "WriteTimeout", "PoolTimeout",
        "RemoteProtocolError", "StreamError", "StreamClosed",
        "NetworkError", "ProtocolError", "ChunkedEncodingError",
    )
    if (
        any(kw in msg_lower for kw in (
            "connection", "timeout", "timed out", "unreachable",
            "dns", "ssl", "eof", "reset by peer", "peer closed",
            "read error", "stream", "chunked",
        ))
        or exc_type in _network_exc_types
        or "Connect" in exc_type
        or "Timeout" in exc_type
    ):
        return {
            "error": (
                f"Network error connecting to the AI provider ({exc_type})."
                " The stream was interrupted — usually transient at high"
                " context size. Click retry."
            ),
            "code": "network_error",
            "category": "network",
            "retry": True,
            "detail": msg[:500] or exc_type,
        }

    # ── Provider Error (5xx) ─────────────────────────────────────
    if any(kw in msg_lower for kw in ("500", "502", "503", "504", "server error", "internal error")):
        return {
            "error": "The AI provider returned a server error. Try again.",
            "code": "provider_error",
            "category": "provider",
            "retry": True,
            "detail": msg[:500],
        }

    # ── Permission Denied ────────────────────────────────────────
    if "PermissionDenied" in exc_type or "permission" in msg_lower:
        return {
            "error": f"Permission denied: {msg[:200]}",
            "code": "permission_denied",
            "category": "security",
            "retry": False,
            "detail": msg[:500],
        }

    # ── Session Lock Timeout ─────────────────────────────────────
    if "lock timeout" in msg_lower or "session lock" in msg_lower:
        return {
            "error": "Another turn is still running on this session. Wait for it to finish.",
            "code": "session_busy",
            "category": "internal",
            "retry": True,
            "detail": msg[:500],
        }

    # ── Generic / Unknown ────────────────────────────────────────
    return {
        "error": msg[:500] if msg else "An unexpected error occurred.",
        "code": "internal_error",
        "category": "internal",
        "retry": True,
        "detail": f"[{exc_type}] {msg[:500]}",
    }


# ── Workspace helpers (server-side, all clients benefit) ──────────


def _get_workspace_status(workspace: str) -> dict[str, Any]:
    """Get git status for a workspace — server-side, all clients benefit."""
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
    """Validate app_id / session_id — alphanumeric + dash/underscore/dot, 1-128 chars."""
    err = _validate_app_id(value)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return value


_agent_turns_lock = asyncio.Lock()


async def _inc_agent_turns(request: Request, delta: int = 1) -> None:
    """Atomically increment/decrement the active agent turns counter."""
    state = request.app.state
    if hasattr(state, "_active_agent_turns"):
        async with _agent_turns_lock:
            state._active_agent_turns += delta


router = APIRouter(prefix="/api/apps", tags=["apps"])


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
    — otherwise the upcoming write resolves against whichever session
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
    a real user scope — so we return None for it too. Otherwise the
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
    if not uid or uid in ("anonymous", "system"):
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
    # The session may still exist under a different owner — look it up
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
        — we intentionally do NOT fall through to the 404 path because
        an unauthenticated client should never enumerate session ids.
      * authenticated caller whose ``user_id`` does not own the session
        → **404** (no info-leak: a stolen sid is indistinguishable from
        a non-existent one).
      * owner → returns the ``ConversationSession`` object for reuse by
        the handler (saves one extra DB lookup).

    The helper uses ``manager.get_session`` which already enforces the
    ``user_id`` filter at the store level — we're promoting that same
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





class DeployRequest(BaseModel):
    """Request body for deploying an app."""

    yaml_path: str | None = None
    force: bool = False
    secrets: dict[str, str] | None = None


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


class ValidateRequest(BaseModel):
    yaml_path: str


@router.post("/validate", response_model=AppResponse)
async def validate_app(request: Request, body: ValidateRequest) -> AppResponse:
    """Validate an app YAML file without deploying it.

    Compiles the YAML against the loaded module registry and returns
    app metadata (modules, agents, security) or compilation errors.
    """
    raw_path = Path(body.yaml_path)
    if raw_path.is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks are not allowed.")
    yaml_path = raw_path.resolve()
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="YAML file not found.")

    manager = _get_manager(request)
    try:
        compiled = manager._compiler.compile_file(yaml_path)
    except Exception as exc:
        errors = getattr(exc, "errors", [str(exc)])
        return AppResponse(success=False, error=f"Validation failed ({len(errors)} error(s))", data={
            "errors": errors,
        })

    constrained = [mid for mid, m in compiled.modules.items() if m.constraints]
    return AppResponse(success=True, data={
        "app_id": compiled.meta.app_id,
        "name": compiled.meta.name,
        "version": compiled.meta.version,
        "modules": list(compiled.module_ids),
        "agents": [a.agent_id for a in compiled.agents],
        "setup_steps": sum(len(m.setup_steps) for m in compiled.modules.values()),
        "constrained_modules": constrained,
        "security_policy": compiled.security_profile.default_policy if compiled.security_profile else None,
        "max_risk_level": compiled.security_profile.max_risk_level if compiled.security_profile else None,
        "skills": len(compiled.skills),
    })


@router.post("/deploy", response_model=AppResponse)
async def deploy_app(request: Request, body: DeployRequest) -> AppResponse:
    """Deploy an app from a YAML file path.

    The YAML file must be accessible from the daemon's filesystem.

    Two modes:
    - sync=true (default for backward compat): waits for deploy, returns result
    - sync=false: returns 202 immediately, deploy runs in background.
      Poll GET /api/apps/{app_id} to check when it's ready.
    """
    _require_permission(request, "apps:deploy")
    manager = _get_manager(request)

    if not body.yaml_path:
        raise HTTPException(status_code=400, detail="yaml_path is required")

    raw_path = Path(body.yaml_path)
    if raw_path.is_symlink():
        raise HTTPException(status_code=400, detail="Symlinks are not allowed in YAML paths.")
    yaml_path = raw_path.resolve()
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail="YAML file not found.")
    if not str(yaml_path).endswith((".yaml", ".yml")):
        raise HTTPException(
            status_code=400,
            detail="Only .yaml/.yml files are accepted.",
        )

    # Quick compile check (fast, doesn't block) to catch YAML errors early.
    # Forward any inline secrets so `{{env.SECRET_NAME}}` references resolve
    # during the pre-flight compile — otherwise valid deploys fail here with
    # bogus "Environment variable X not found" errors before the background
    # deploy has a chance to apply the same secrets.
    try:
        compiled = manager._compiler.compile_file(yaml_path, secrets=body.secrets)
        app_id = compiled.meta.app_id
    except Exception as exc:
        errors = getattr(exc, "errors", [str(exc)])
        return AppResponse(success=False, error=f"App compilation failed ({len(errors)} error(s)): {'; '.join(str(e) for e in errors[:5])}")

    # Async deploy — run in background, return immediately
    # BUG-080: the old flow swallowed deploy failures — POST returned
    # {status:"deploying"}, a subsequent GET /apps/{id} 404'd, and
    # nothing explained why. Record the last error per app on the
    # manager so /diagnostics + a new /api/apps/{id}/deploy-status
    # route can surface it.
    async def _deploy_bg():
        try:
            deployed = await manager.deploy(
                yaml_path, force=body.force, inline_secrets=body.secrets,
            )
            if body.secrets:
                for k, v in body.secrets.items():
                    await manager.set_secret(deployed.app_id, k, v)
            logger.info("deploy_complete app=%s", app_id)
            try:
                if hasattr(manager, "_deploy_errors"):
                    manager._deploy_errors.pop(app_id, None)
            except Exception:
                pass
        except Exception as exc:
            logger.error("deploy_failed app=%s: %s", app_id, exc, exc_info=True)
            try:
                import time as _time, traceback as _tb
                store = getattr(manager, "_deploy_errors", None)
                if store is None:
                    store = {}
                    manager._deploy_errors = store
                store[app_id] = {
                    "app_id": app_id,
                    "error": f"{type(exc).__name__}: {exc}"[:800],
                    "traceback": "".join(
                        _tb.format_exception(type(exc), exc, exc.__traceback__)
                    )[:4000],
                    "yaml_path": str(yaml_path),
                    "failed_at": _time.time(),
                }
            except Exception:
                logger.debug("deploy_error_store_failed", exc_info=True)

    asyncio.create_task(_deploy_bg())

    return AppResponse(success=True, data={
        "app_id": app_id,
        "name": compiled.meta.name,
        "version": compiled.meta.version,
        "status": "deploying",
        "message": "Deployment started. Poll GET /api/apps/{app_id} to check status.",
    })


@router.get("/{app_id}/deploy-status", response_model=AppResponse)
async def get_deploy_status(request: Request, app_id: str) -> AppResponse:
    """Return the last known deploy outcome for an app.

    BUG-080: POST ``/deploy`` used to return ``status:"deploying"``
    and silently drop the error if the background deploy failed — the
    client had no way to distinguish "still running" from "failed".
    This route surfaces the stored error (if any) so the caller can
    show a meaningful message.

    Shape::

        { "deployed": true, "app_id": "...", "error": null }
        { "deployed": false, "app_id": "...", "error": "...", "traceback": "..." }
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    is_deployed_now = manager.is_deployed(
        app_id, user_id=_caller_user_id(request),
    )
    errors = getattr(manager, "_deploy_errors", {}) or {}
    err = errors.get(app_id)
    data: dict[str, Any] = {
        "app_id": app_id,
        "deployed": is_deployed_now,
        "error": err.get("error") if err else None,
    }
    if err:
        data["traceback"] = err.get("traceback", "")[:2000]
        data["failed_at"] = err.get("failed_at")
        data["yaml_path"] = err.get("yaml_path")
    return AppResponse(success=True, data=data)


@router.post("/deploy/upload", response_model=AppResponse)
async def deploy_app_upload(
    request: Request,
    file: UploadFile = File(...),
    force: bool = Form(False),
    secrets: str | None = Form(None),
    assets: str | None = Form(None),
    scope: str = Form("system"),
) -> AppResponse:
    """Deploy an app by uploading a YAML file + its referenced assets.

    An app is almost never a single YAML file — it also needs skill
    markdown files, agent prompt files, and any other asset the YAML
    references with a relative path. This endpoint accepts the YAML
    itself AND a JSON-encoded dict of ``{relative_path: content}`` for
    every companion asset. The daemon writes everything into a single
    temporary directory so the compiler can resolve all relative paths
    normally.

    Form fields:
      - ``file``   : the YAML file (multipart upload, max 1 MB)
      - ``force``  : optional, ``"true"`` to overwrite an existing deployment
      - ``secrets``: optional, a JSON-encoded ``{"KEY": "value", ...}`` map
                     of secrets that will be injected at compile time to
                     resolve ``{{env.KEY}}`` references without relying on
                     the daemon's environment.
      - ``assets`` : optional, a JSON-encoded ``{rel_path: content}`` map
                     of every companion file referenced by the YAML
                     (skills/*.md, agent prompts, etc). Paths MUST be
                     forward-slash relative paths, no absolute paths and
                     no ``..`` segments — both are rejected for safety.
                     Max 5 MB total across all assets.

    Example::

        POST /api/apps/deploy/upload
        Content-Type: multipart/form-data
        Authorization: Bearer <token>

        file    = <bytes of app.yaml>
        force   = "true"
        secrets = '{"DEEPSEEK_API_KEY": "sk-..."}'
        assets  = '{"skills/commit.md": "# Commit\\n...",
                    "skills/review.md": "# Review\\n..."}'
    """
    _require_permission(request, "apps:deploy")
    manager = _get_manager(request)

    _MAX_YAML_SIZE = 1_048_576       # 1 MB
    _MAX_ASSETS_TOTAL = 5_242_880    # 5 MB combined
    _MAX_ASSET_PATH_LEN = 512

    content = await file.read(_MAX_YAML_SIZE + 1)
    if len(content) > _MAX_YAML_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"YAML file too large (max {_MAX_YAML_SIZE // 1024} KB).",
        )

    inline_secrets: dict[str, str] | None = None
    if secrets:
        try:
            parsed = _json.loads(secrets)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"'secrets' must be a JSON object: {exc}",
            )
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=400,
                detail="'secrets' must be a JSON object of key/value strings.",
            )
        inline_secrets = {str(k): str(v) for k, v in parsed.items()}

    asset_map: dict[str, str] = {}
    if assets:
        try:
            parsed_assets = _json.loads(assets)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"'assets' must be a JSON object: {exc}",
            )
        if not isinstance(parsed_assets, dict):
            raise HTTPException(
                status_code=400,
                detail="'assets' must be a JSON object of relpath/content strings.",
            )
        total_size = 0
        for rel, body in parsed_assets.items():
            if not isinstance(rel, str) or not isinstance(body, str):
                raise HTTPException(
                    status_code=400,
                    detail="'assets' keys and values must both be strings.",
                )
            if len(rel) > _MAX_ASSET_PATH_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=f"asset path too long: {rel[:80]}...",
                )
            # Normalise and reject any path that tries to escape the
            # temp dir (absolute, contains .., Windows drive letters).
            norm = rel.replace("\\", "/").strip()
            while norm.startswith("./"):
                norm = norm[2:]
            if (
                not norm
                or norm.startswith("/")
                or ".." in norm.split("/")
                or ":" in norm
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"unsafe asset path rejected: {rel!r}",
                )
            total_size += len(body.encode("utf-8"))
            if total_size > _MAX_ASSETS_TOTAL:
                raise HTTPException(
                    status_code=413,
                    detail=f"assets too large (max {_MAX_ASSETS_TOTAL // 1024} KB total).",
                )
            asset_map[norm] = body

    # Create a dedicated temp DIRECTORY so the YAML AND its assets live
    # together. The compiler resolves `./skills/commit.md` from the YAML
    # parent dir, so we need a real directory layout on disk.
    tmp_dir = Path(tempfile.mkdtemp(prefix="digitorn-deploy-"))
    yaml_filename = file.filename or "app.yaml"
    # Strip any path separators from the filename — only the basename.
    yaml_filename = Path(yaml_filename).name or "app.yaml"
    yaml_path = tmp_dir / yaml_filename

    try:
        yaml_path.write_bytes(content)
        for rel, body in asset_map.items():
            asset_path = tmp_dir / rel
            asset_path.parent.mkdir(parents=True, exist_ok=True)
            # Defence in depth: confirm we're still inside tmp_dir after
            # path resolution (symlink tricks, mixed separators, ...).
            try:
                asset_path.resolve().relative_to(tmp_dir.resolve())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"asset path escapes temp dir: {rel}",
                )
            asset_path.write_text(body, encoding="utf-8")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    logger.info(
        "deploy_upload_received yaml_filename=%s yaml_bytes=%d assets_count=%d "
        "secrets_count=%d tmp_dir=%s",
        yaml_filename, len(content), len(asset_map),
        len(inline_secrets or {}), tmp_dir,
    )

    try:
        compiled = manager._compiler.compile_file(yaml_path, secrets=inline_secrets)
        app_id = compiled.meta.app_id
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        errors = getattr(exc, "errors", [str(exc)])
        error_msg = f"Compilation failed: {'; '.join(str(e) for e in errors[:5])}"
        # Add a helpful hint when the failure is clearly due to missing
        # asset uploads rather than a YAML problem.
        if len(asset_map) == 0 and any(
            "file not found" in str(e).lower() for e in errors
        ):
            error_msg += (
                "\n\nHint: the client uploaded 0 assets. If your YAML "
                "references skill files (skills/*.md) or agent prompt "
                "files, you must send them in the 'assets' form field as a "
                "JSON map of {relative_path: content}. See "
                "POST /api/apps/deploy/upload docs."
            )
        return AppResponse(success=False, error=error_msg)

    # Scope resolution: ``scope=user`` ties the install to the caller's
    # JWT user_id; ``scope=system`` installs globally. Non-admin callers
    # cannot deploy a system install when a ``scope=user`` was requested
    # because the manager would try to read user_id and fail.
    caller_user_id = _caller_user_id(request) or None
    deploy_scope = scope if scope in ("system", "user") else "system"
    deploy_owner = (
        caller_user_id if deploy_scope == "user" else None
    )

    async def _deploy_upload_bg():
        try:
            deployed = await manager.deploy(
                yaml_path,
                force=force,
                inline_secrets=inline_secrets,
                scope=deploy_scope,
                owner_user_id=deploy_owner,
            )
            if inline_secrets:
                for k, v in inline_secrets.items():
                    await manager.set_secret(deployed.app_id, k, v)
            logger.info(
                "deploy_upload_complete app=%s scope=%s assets=%d",
                app_id, deploy_scope, len(asset_map),
            )
        except Exception as exc:
            logger.error(
                "deploy_upload_failed app=%s scope=%s: %s",
                app_id, deploy_scope, exc,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    asyncio.create_task(_deploy_upload_bg())

    return AppResponse(success=True, data={
        "app_id": app_id,
        "name": compiled.meta.name,
        "status": "deploying",
        "asset_count": len(asset_map),
        "message": "Deployment started. Poll GET /api/apps/{app_id} to check status.",
    })


@router.get("", response_model=AppResponse)
async def list_apps(
    request: Request,
    include_disabled: bool = False,
) -> AppResponse:
    """List deployed apps visible to the caller.

    A regular user sees: their own user-scoped deploys + every
    system-scoped deploy (user overrides shadow system). Apps
    belonging to OTHER users are never returned.

    ``include_disabled=true``: **admin-only** flag — appends every
    app in the DB with ``disabled=True`` so admins can see
    de-activated apps to re-enable or purge. Non-admins get the
    flag silently ignored (still only see deployed apps).
    """
    manager = _get_manager(request)
    apps = list(manager.list_apps(user_id=_caller_user_id(request)))

    # Admin-only: append disabled apps from DB so they're visible for
    # re-enable / purge. Silently ignored for non-admins.
    if include_disabled:
        perms = list(getattr(request.state, "permissions", []) or [])
        if "*" in perms:
            # Admin: full view, all scopes.
            try:
                apps.extend(await manager.list_disabled_apps())
            except Exception as exc:
                logger.warning("list_disabled_apps failed: %s", exc, exc_info=True)
        else:
            # Non-admin: only their own user-scoped disabled installs
            # plus any disabled system installs.
            try:
                apps.extend(await manager.list_disabled_apps(
                    user_id=_caller_user_id(request) or None,
                ))
            except Exception as exc:
                logger.warning("list_disabled_apps failed: %s", exc, exc_info=True)

    return AppResponse(success=True, data=apps)


@router.get("/{app_id}", response_model=AppResponse)
async def get_app(request: Request, app_id: str) -> AppResponse:
    """Get details of a deployed app the caller can see.

    Disabled apps are not returned here — use
    ``GET /api/apps?include_disabled=true`` as admin to see them.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    return AppResponse(success=True, data=deployed.summary())


@router.get("/{app_id}/ui-config", response_model=AppResponse)
async def get_app_ui_config(request: Request, app_id: str) -> AppResponse:
    """Return ONLY the client-UI-relevant config flags for an app.

    Safe to call from any authenticated user — it strictly allow-lists
    fields that are safe to expose to a frontend (booleans, render
    modes, layout hints). Never leaks prompts, secrets, api_keys,
    webhook URLs, hook logic, or capability grants.

    Rationale: the Flutter / web client needs to adapt its UI based on
    per-app config (``auto_approve`` → hide approve buttons;
    ``render_mode`` → canvas vs iframe; ``preview.enabled`` → show
    web preview pane). Previous proposal was to return the full YAML
    via ``?include_yaml=true`` — that was a leak (system_prompts,
    inline api_keys, internal webhook paths). This endpoint exposes
    only the narrow subset the UI cares about.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    compiled = getattr(deployed, "compiled", None)
    modules_cfg: dict[str, Any] = {}
    workspace_cfg: dict[str, Any] = {}
    preview_cfg: dict[str, Any] = {}

    # Allow-list fields per module. Adding a new field here is an
    # explicit decision — reject the temptation to dump everything.
    _WS_ALLOW = {"render_mode", "entry_file", "title", "sync_to_disk",
                 "lint", "auto_approve"}
    _PREVIEW_ALLOW = {"enabled", "port"}

    if compiled is not None:
        mods = getattr(compiled, "modules", {}) or {}
        ws_block = mods.get("workspace")
        if ws_block is not None:
            ws_cfg = getattr(ws_block, "config", {}) or {}
            if isinstance(ws_cfg, dict):
                workspace_cfg = {k: v for k, v in ws_cfg.items() if k in _WS_ALLOW}
        pv_block = mods.get("preview")
        if pv_block is not None:
            pv_cfg = getattr(pv_block, "config", {}) or {}
            if isinstance(pv_cfg, dict):
                preview_cfg = {k: v for k, v in pv_cfg.items() if k in _PREVIEW_ALLOW}

    # Top-level workspace: block (render_mode, entry_file, title) — same
    # shape as the summary's ``workspace`` field but filtered.
    top_ws = getattr(compiled, "workspace", None) if compiled is not None else None
    top_workspace = {}
    if top_ws is not None:
        for k in ("render_mode", "entry_file", "title"):
            v = getattr(top_ws, k, None)
            if v is not None:
                top_workspace[k] = v

    return AppResponse(success=True, data={
        "app_id": app_id,
        "workspace_config": workspace_cfg,
        "preview_config": preview_cfg,
        "workspace": top_workspace,
    })


@router.post("/{app_id}/run", response_model=AppResponse)
async def run_app(request: Request, app_id: str, body: RunRequest) -> AppResponse:
    """Run a deployed one-shot app.

    Returns the agent's response. Only works for one_shot mode apps.
    For conversation mode, use WebSocket/Socket.IO.
    """
    _validate_id(app_id)
    manager = _get_manager(request)

    await _inc_agent_turns(request)
    try:
        result = await manager.run_one_shot(app_id, body.input)
        return AppResponse(
            success=result.error is None,
            data={
                "content": result.content,
                "tool_calls_count": result.tool_calls_count,
                "turns_used": result.turns_used,
                "truncated": result.truncated,
            },
            error=result.error,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        await _inc_agent_turns(request, -1)


class PipelineRequest(BaseModel):
    input: str
    steps: list[dict[str, Any]] = []


@router.post("/{app_id}/pipeline", response_model=AppResponse)
async def run_pipeline(request: Request, app_id: str, body: PipelineRequest) -> AppResponse:
    """Execute a pipeline of app calls.

    If the app has a pipeline defined in YAML, uses that.
    Otherwise, uses the steps provided in the request body.
    """
    _validate_id(app_id)
    manager = _get_manager(request)

    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    raw_steps = body.steps
    if not raw_steps and deployed and hasattr(deployed, "compiled"):
        raw_steps = getattr(deployed.compiled, "pipeline", [])

    if not raw_steps:
        raise HTTPException(status_code=400, detail="No pipeline steps defined")

    from digitorn.core.pipeline import compile_pipeline, execute_pipeline

    try:
        steps = compile_pipeline(raw_steps)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = await execute_pipeline(steps, body.input)

    return AppResponse(
        success=result.success,
        data={
            "final_output": result.final_output,
            "steps": [
                {
                    "app_id": s.app_id,
                    "success": s.success,
                    "output": s.output[:500],
                    "duration": round(s.duration, 2),
                    "error": s.error,
                }
                for s in result.steps
            ],
            "total_duration": round(result.total_duration, 2),
        },
        error=result.error or None,
    )










class NotificationCheckRequest(BaseModel):
    session_id: str


@router.post("/{app_id}/notifications")
async def check_notifications(request: Request, app_id: str, body: NotificationCheckRequest):
    """Check for background task notifications and stream an agent response if any.

    Returns SSE stream identical to chat/stream if notifications exist,
    or an empty 204 response if nothing is pending.
    """
    _validate_id(app_id)
    manager = _get_manager(request)

    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    if not manager.has_active_bg_tasks(app_id):
        deployed = _get_deployed(request, app_id)
        cb = deployed.context_builder if deployed else None
        if cb is None or not hasattr(cb, "drain_bg_notifications"):
            return AppResponse(success=True, data={"notifications": 0})
        pending = cb.drain_bg_notifications(session_id=body.session_id)
        if not pending:
            return AppResponse(success=True, data={"notifications": 0})
        # Re-queue into the session's queue
        session_queue = cb._get_notification_queue(body.session_id)
        for n in pending:
            session_queue.put_nowait(n)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=500)

    async def on_tool_call(name: str, params: dict, result: Any, call_id: str = "") -> None:
        ok, err = True, ""
        if isinstance(result, dict):
            ok = result.get("success", True)
            err = result.get("error", "")
        elif hasattr(result, "success"):
            ok = result.success
            err = getattr(result, "error", "") or ""
        await queue.put({
            "event": "tool_call",
            "data": {"id": call_id, "name": name, "params": params, "success": ok, "error": err},
        })

    async def _run():
        try:
            result = await manager.check_notifications(
                app_id, body.session_id,
                on_tool_call=on_tool_call,
            )
            if result is None:
                await queue.put({
                    "event": "result",
                    "data": {"content": "", "notifications": 0},
                })
            else:
                await queue.put({
                    "event": "result",
                    "data": {
                        "content": result.content,
                        "session_id": body.session_id,
                        "notifications": 1,
                        "tool_calls_count": result.tool_calls_count,
                        "error": result.error,
                    },
                })
        except Exception as exc:
            await queue.put({
                "event": "error",
                "data": {"error": str(exc)},
            })
        await queue.put(None)

    async def event_generator():
        task = asyncio.create_task(_run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    yield "event: timeout\ndata: {}\n\n"
                    break
                if item is None:
                    break
                event = item["event"]
                data = _json.dumps(item["data"], ensure_ascii=False, default=str)
                yield f"event: {event}\ndata: {data}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{app_id}/notifications/active")
async def has_active_bg_tasks(request: Request, app_id: str) -> AppResponse:
    """Quick check if any background tasks are active for this app.

    Returns ``active: false`` (not 404) when the app is not deployed,
    since this endpoint is polled continuously by the CLI — a 404 would
    spam the server logs with useless error entries.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        return AppResponse(success=True, data={"active": False})
    active = manager.has_active_bg_tasks(app_id)
    return AppResponse(success=True, data={"active": active})


_MESSAGE_MAX_BYTES = 1_048_576  # 1 MiB — BUG-062 guard against DoS


class SessionMessageRequest(BaseModel):
    # BUG-091 + BUG-092: reject ONLY the audio/audios/audio_refs
    # fields that used to be silently dropped — any other unknown
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
                    # Raise as ValueError — FastAPI converts it to a
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
    # reaches the handler — the client gets a clean 422 instead of a
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


# NOTE: The session SSE endpoint (GET /{app_id}/sessions/{session_id}/events)
# has been removed. Clients now receive events via Socket.IO on the
# `/events` namespace — join `session:{session_id}` and listen for
# "event" frames. See core/events/socketio_bus.py.
#
# Background notification polling (previously tied to SSE lifecycle)
# is now a daemon-level task started by the lifespan. See
# manager.start_notification_poller().
#
# Approval callbacks are registered at deploy() time and publish
# directly to the session bus, so they no longer depend on a client
# being connected.


@router.post("/{app_id}/sessions/{session_id}/messages")
async def session_send_message(
    request: Request,
    app_id: str,
    session_id: str,
    body: SessionMessageRequest,
) -> AppResponse:
    """Send a message to a session. Events arrive via Socket.IO.

    **Queueing (Phase 3 — per-session FIFO queue)**

    When a turn is already running on this session, the message is
    enqueued instead of failing with ``session_busy``. The dispatcher
    picks the head of the queue as soon as the running turn finishes.
    The queue is persisted across daemon restarts.

    ``queue_mode`` controls the response:

    - ``async`` (default, recommended) — returns 202 immediately with
      ``{correlation_id, position, queue_depth}``. The client tracks the
      message via SSE events ``message_queued``, ``message_started``,
      ``message_done`` / ``message_cancelled``.
    - ``wait`` — legacy: block until the turn finishes, return the
      message data. Equivalent to the pre-queue behaviour for simple
      clients.

    Over-capacity (``session.queue.max_depth``) returns 429 + a
    ``queue_full`` event.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    # Strong deploy check — not only does the manager know the app,
    # the DeployedApp must have a usable entry_context + modules. Apps
    # that survived a bootstrap crash can linger in `_deployed` with
    # a half-built state ("ghost apps"); POST /messages used to return
    # 200 for these but the dispatcher silently dropped everything.
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    _deployed_check = _get_deployed(request, app_id)
    if _deployed_check is None or getattr(_deployed_check, "entry_context", None) is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"App '{app_id}' is in a degraded state — deployed but "
                f"not fully initialized. Re-deploy to recover."
            ),
        )
    # BUG-072: reject cross-user message injection. New sessions still
    # pass (the handler creates them bound to the caller).
    await _require_session_create_or_owner(request, app_id, session_id)

    _user_id = getattr(request.state, "user_id", None)
    _workspace = body.workspace

    # Process images if provided
    _image_refs: list[dict[str, Any]] = []
    if body.images:
        try:
            from digitorn.core.image_store import get_image_store
            store = get_image_store()
            for img in body.images[:10]:  # Max 10 images
                mime = img.get("mime", "image/png")
                # BUG-092: some clients posted audio blobs through the
                # ``images`` field expecting the daemon to figure it
                # out. The blob then got stored as an ``image_ref``
                # with an audio MIME, which downstream vision
                # providers happily forwarded as a broken image. Refuse
                # non-image MIMEs here so the mistake surfaces.
                if mime and not mime.lower().startswith("image/"):
                    raise HTTPException(
                        status_code=415,
                        detail={
                            "error": "non_image_in_images_field",
                            "got": mime,
                            "message": (
                                "The ``images`` field only accepts "
                                "image/* blobs. For audio, POST to "
                                "/api/transcribe first and include "
                                "the returned text in ``message``."
                            ),
                        },
                    )
                data = img.get("data", "")
                name = img.get("name", "image")
                if data:
                    ref = await store.store_base64(
                        data, mime, session_id, alt_text=name,
                    )
                    _image_refs.append(ref.to_dict())
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("image_upload_failed: %s", exc)

    # ── Phase 3: per-session message queue ────────────────────────────
    #
    # Strategy:
    #   1. Always persist the message to the queue — gives us FIFO,
    #      crash-recovery, and cancellation for free.
    #   2. When the session has nothing in-flight, dispatch immediately.
    #      When it does, a post-turn hook drains the next queued msg
    #      (see _drain_queue_next below).
    #   3. ``queue_mode`` controls only the HTTP response shape:
    #      async = 202 with correlation_id, wait = block on awaiter.
    from digitorn.core.app import message_queue as _mq
    from digitorn.core.config import get_settings as _get_settings

    _qcfg = _get_settings().session.queue
    _mode = body.queue_mode or _qcfg.default_mode
    _uid = _user_id or "local"
    _bus_key = manager.event_bus.session_key(app_id, session_id, _uid)

    _skip_queue = True
    _reserved = False
    if _qcfg.enabled:
        _qdepth = await _mq.depth_for_session(session_id)
        _turn_running = await manager.is_turn_running(app_id, session_id)
        # A session with an approval pending still holds the turn's
        # future — `is_turn_running` returns False (the coroutine is
        # awaiting) but fast-pathing a new message would race with the
        # blocked turn and re-execute earlier logic. Treat pending
        # approvals as equivalent to a running turn so the new message
        # queues behind them.
        _has_pending_approval = False
        try:
            deployed_for_check = _get_deployed(request, app_id)
            aq = getattr(deployed_for_check, "approval_queue", None) if deployed_for_check else None
            if aq is not None:
                for r in aq.list_pending():
                    if r.get("session_id") == session_id:
                        _has_pending_approval = True
                        break
        except Exception:
            pass
        if (
            _qdepth == 0
            and not _turn_running
            and not _has_pending_approval
            and body.queue_mode != "replace_last"
            and not _qcfg.auto_merge
        ):
            _reserved = manager.reserve_session(app_id, session_id)
            _skip_queue = _reserved
        else:
            _skip_queue = False

    if _qcfg.enabled and not _skip_queue:
        # Three enqueue strategies — the mode picks which helper runs.
        #
        # replace_last: if the tail of the queue is still queued,
        #   overwrite it with this new message in place. Client UX:
        #   "oops wrong message, use this one instead".
        #
        # auto_merge (config-driven): if a recent queued message from
        #   the same user is < auto_merge_window_s old, fold the new
        #   content into it — saves an LLM call when the user fires
        #   rapid follow-ups.
        #
        # default: plain append.
        merged = False
        replaced = False
        try:
            if body.queue_mode == "replace_last":
                entry, replaced = await _mq.replace_last_or_enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                )
            elif _qcfg.auto_merge:
                entry, merged = await _mq.merge_or_enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    window_seconds=_qcfg.auto_merge_window_s,
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                )
            else:
                entry = await _mq.enqueue(
                    app_id=app_id, session_id=session_id, user_id=_uid,
                    message=body.message,
                    image_refs=_image_refs or [],
                    ttl_seconds=_qcfg.ttl_seconds,
                    max_depth=_qcfg.max_depth,
                )
        except _mq.QueueFullError as exc:
            try:
                await manager.event_bus.publish(_bus_key, {
                    "type": "queue_full",
                    "data": {
                        "depth": exc.depth, "max": exc.max_depth,
                        "session_id": session_id,
                    },
                })
            except Exception:
                pass
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session queue full ({exc.depth}/{exc.max_depth}). "
                    "Cancel pending messages or wait before sending more."
                ),
            )

        # Emit the right event for the client. A merge or replace is
        # NOT a new entry in the UI — the existing row was mutated, so
        # we publish a dedicated event with the same correlation_id so
        # the client updates in place instead of appending.
        try:
            current_depth = await _mq.depth_for_session(session_id)
            if merged:
                _evt = "message_merged"
            elif replaced:
                _evt = "message_replaced"
            else:
                _evt = "message_queued"
            await manager.event_bus.publish(_bus_key, {
                "type": _evt,
                "data": {
                    "correlation_id": entry.correlation_id,
                    "position": entry.position,
                    "queue_depth": current_depth,
                    "message_preview": (entry.message or "")[:200],
                    "merged": merged,
                    "replaced": replaced,
                },
            })
        except Exception:
            pass

        if not merged and not replaced:
            try:
                await manager.event_bus.publish(_bus_key, {
                    "type": "user_message",
                    "data": {
                        "session_id": session_id,
                        "role": "user",
                        "content": entry.message,
                        "images": [
                            img.get("id") or img.get("ref")
                            for img in (entry.image_refs or [])
                            if isinstance(img, dict)
                        ],
                        "correlation_id": entry.correlation_id,
                        "client_message_id": body.client_message_id or "",
                        "pending": True,
                    },
                })
            except Exception:
                pass

        _turn_active = await manager.is_turn_running(app_id, session_id)
        if _turn_active:
            if _mode == "wait":
                fut = _mq.awaiter_future(entry.correlation_id)
                try:
                    await fut
                except Exception as exc:
                    raise HTTPException(status_code=500, detail=str(exc))
                return AppResponse(
                    success=True,
                    data={
                        "session_id": session_id,
                        "status": "completed",
                        "correlation_id": entry.correlation_id,
                    },
                )
            return AppResponse(
                success=True,
                data={
                    "session_id": session_id,
                    "status": "queued",
                    "correlation_id": entry.correlation_id,
                    "position": entry.position,
                    "queue_depth": current_depth,
                    "merged": merged,
                    "replaced": replaced,
                },
            )

        # Nothing running. Atomically mark the head as running and
        # dispatch it. If our own row isn't the head (some earlier
        # queued row exists), dispatch whichever is the head — the
        # drain chain handles the rest.
        _head = await _mq.next_queued(session_id)
        if _head is None:
            # Rare race: the head was cancelled between our checks.
            return AppResponse(
                success=True,
                data={
                    "session_id": session_id,
                    "status": "queued",
                    "correlation_id": entry.correlation_id,
                    "position": entry.position,
                },
            )
        _active_correlation_id = _head.correlation_id
        _active_queue_row_id = _head.id
        # Update body.message so _run_turn uses the head's content.
        # Normal case: head == our entry. Edge case: head is an
        # earlier row we didn't know about — we still drain it.
        if _head.correlation_id != entry.correlation_id:
            body.message = _head.message
            _image_refs = list(_head.image_refs or [])
    else:
        import uuid as _uuid
        _active_correlation_id = f"fp-{_uuid.uuid4().hex[:12]}"
        _active_queue_row_id = ""

        try:
            await manager.event_bus.publish(_bus_key, {
                "type": "user_message",
                "data": {
                    "session_id": session_id,
                    "role": "user",
                    "content": body.message,
                    "images": [
                        img.get("id") or img.get("ref")
                        for img in (_image_refs or [])
                        if isinstance(img, dict)
                    ],
                    "correlation_id": _active_correlation_id,
                    "client_message_id": body.client_message_id or "",
                    "pending": False,
                },
            })
        except Exception:
            pass

        try:
            await manager.event_bus.publish(_bus_key, {
                "type": "message_started",
                "data": {
                    "correlation_id": _active_correlation_id,
                    "session_id": session_id,
                    "position": 0,
                    "fast_path": True,
                },
            })
        except Exception:
            pass

    async def _run_turn():
        await _inc_agent_turns(request)
        cancelled = False
        _heartbeat_task: asyncio.Task | None = None
        if _qcfg.enabled and _active_queue_row_id:
            async def _hb_loop():
                while True:
                    try:
                        await asyncio.sleep(30)
                        await _mq.heartbeat(_active_queue_row_id)
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        pass
            _heartbeat_task = asyncio.create_task(_hb_loop())
        try:
            try:
                from digitorn.core.credentials import (
                    ensure_user_credentials_for_app,
                )
                deployed = _get_deployed(request, app_id)
                if deployed is not None:
                    cred_store = getattr(
                        request.app.state, "credential_store", None,
                    )
                    logger.info(
                        "turn_cred_resolve app=%s session=%s user=%s has_store=%s",
                        app_id, session_id, _user_id or "local",
                        cred_store is not None,
                    )
                    await ensure_user_credentials_for_app(
                        deployed_app=deployed,
                        user_id=_user_id or "local",
                        credential_store=cred_store,
                    )
            except Exception:
                raise

            await manager.chat(
                app_id, session_id, body.message,
                user_id=_user_id,
                workspace=_workspace,
                image_refs=_image_refs if _image_refs else None,
                correlation_id=_active_correlation_id or None,
                client_message_id=body.client_message_id,
            )
            try:
                _sess_after = await manager.get_session(
                    app_id, session_id, user_id=_user_id,
                )
                if _sess_after and getattr(_sess_after, "interrupted", False):
                    cancelled = True
            except Exception:
                pass
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            # Lock contention isn't a crash — a previous turn is still
            # running. Downgrade the log level so these don't pollute
            # error dashboards + skip the full traceback (it's noisy
            # and rarely actionable for this path).
            _is_busy = "session lock timeout" in str(exc).lower()
            if _is_busy:
                logger.warning(
                    "session_busy app=%s session=%s: previous turn still running",
                    app_id, session_id,
                )
            else:
                logger.error(
                    "agent_turn_crashed app=%s session=%s: %s",
                    app_id, session_id, exc, exc_info=True,
                )
            error_data = _classify_error(exc)
            bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
            # Credential-flow errors get their own event type so the
            # Flutter client can open the picker dialog directly instead
            # of showing a generic error toast.
            _evt_type = "error"
            _code = error_data.get("code")
            if _code in ("credential_required", "credential_auth_required"):
                _evt_type = "credential_required"
            try:
                await manager.event_bus.publish(bus_key, {
                    "type": _evt_type,
                    "data": error_data,
                })
            except Exception as pub_exc:
                logger.error(
                    "Failed to publish error event for %s/%s: %s (original: %s)",
                    app_id, session_id, pub_exc, error_data,
                )
        finally:
            if _heartbeat_task is not None and not _heartbeat_task.done():
                _heartbeat_task.cancel()
            await _inc_agent_turns(request, -1)
            # Emit the terminal event UNCONDITIONALLY once we have a
            # correlation_id. Previously this was gated behind
            # `_qcfg.enabled`, so apps running with the queue disabled
            # (or on the fast path when queue was enabled) never saw
            # `message_done` — the frontend stayed in a spinner forever.
            # That was BUG-039 on digitorn-builder (840s turns ending
            # silently). Only apps that truly abort mid-turn emit
            # `message_cancelled`; a normal completion always gets
            # `message_done`.
            if _active_correlation_id:
                terminal_type = "message_cancelled" if cancelled else "message_done"
                try:
                    await manager.event_bus.publish(_bus_key, {
                        "type": terminal_type,
                        "data": {
                            "correlation_id": _active_correlation_id,
                            "session_id": session_id,
                            "fast_path": not _active_queue_row_id,
                        },
                    })
                except Exception:
                    pass
            if _qcfg.enabled and _active_queue_row_id:
                try:
                    if cancelled:
                        await _mq.mark_cancelled(_active_queue_row_id)
                        _mq.fail_awaiter(
                            _active_correlation_id,
                            RuntimeError("turn cancelled"),
                        )
                    else:
                        await _mq.mark_done(_active_queue_row_id)
                        _mq.resolve_awaiter(
                            _active_correlation_id, {"status": "completed"},
                        )
                except Exception as exc:
                    logger.debug("queue_mark_done_failed: %s", exc)
            if _qcfg.enabled:
                try:
                    await _drain_queue_next(
                        request, app_id, session_id, _uid,
                    )
                except Exception as exc:
                    logger.warning("queue_drain_failed: %s", exc)

    # ── Dispatch agent turn to a worker thread ────────────────────────
    # The turn runs in its own event loop inside a thread from the worker
    # pool. The main event loop stays free for HTTP/SSE at all times.
    # A semaphore caps concurrency — beyond _MAX_CONCURRENT_TURNS the
    # endpoint returns 503 immediately instead of starving the daemon.
    if _turn_semaphore.locked() and _turn_semaphore._value == 0:
        if _reserved:
            manager.release_session(app_id, session_id)
        return AppResponse(
            success=False,
            data={"error": "Server busy — too many concurrent agent turns", "retry": True},
        )

    async def _guarded_turn():
        async with _turn_semaphore:
            await _run_turn()

    task = asyncio.create_task(_guarded_turn())
    _active_turn_tasks.add(task)

    def _on_turn_done(t: asyncio.Task) -> None:
        _active_turn_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            logger.error(
                "TURN_TASK_CRASHED app=%s session=%s: %s",
                app_id, session_id, exc, exc_info=exc,
            )

    task.add_done_callback(_on_turn_done)

    return AppResponse(
        success=True,
        data={
            "session_id": session_id,
            "status": "accepted",
            "correlation_id": _active_correlation_id or None,
            "client_message_id": body.client_message_id,
        },
    )


async def _drain_queue_next(
    request: "Request", app_id: str, session_id: str, user_id: str,
) -> None:
    """After a turn finishes, pull the next queued message for this
    session and dispatch it in the same request context. Recursively
    chains turns until the queue is empty — preserves FIFO without
    needing a global dispatcher.

    Safe to call when the queue is empty (no-op).
    """
    from digitorn.core.app import message_queue as _mq
    entry = await _mq.next_queued(session_id)
    if entry is None:
        return  # queue empty — done

    manager = _get_manager(request)
    bus_key = manager.event_bus.session_key(app_id, session_id, user_id)
    try:
        await manager.event_bus.publish(bus_key, {
            "type": "message_started",
            "data": {
                "correlation_id": entry.correlation_id,
                "session_id": session_id,
                "position": entry.position,
            },
        })
    except Exception:
        pass

    async def _run_next():
        await _inc_agent_turns(request)
        try:
            from digitorn.core.credentials import (
                ensure_user_credentials_for_app,
            )
            deployed = _get_deployed(request, app_id)
            if deployed is not None:
                cred_store = getattr(
                    request.app.state, "credential_store", None,
                )
                try:
                    await ensure_user_credentials_for_app(
                        deployed_app=deployed,
                        user_id=user_id,
                        credential_store=cred_store,
                    )
                except Exception:
                    raise

            await manager.chat(
                app_id, session_id, entry.message,
                user_id=user_id,
                image_refs=entry.image_refs or None,
                correlation_id=entry.correlation_id,
            )
        except Exception as exc:
            is_busy = "session lock timeout" in str(exc).lower()
            if is_busy:
                logger.warning(
                    "queue_drain_busy app=%s session=%s", app_id, session_id,
                )
            else:
                logger.error(
                    "queue_drain_crashed app=%s session=%s: %s",
                    app_id, session_id, exc, exc_info=True,
                )
            error_data = _classify_error(exc)
            try:
                await manager.event_bus.publish(bus_key, {
                    "type": "error",
                    "data": {**error_data, "correlation_id": entry.correlation_id},
                })
            except Exception:
                pass
            try:
                await _mq.mark_failed(
                    entry.id, error_code=error_data.get("code") or "internal",
                )
                _mq.fail_awaiter(entry.correlation_id, exc)
            except Exception:
                pass
        else:
            try:
                await manager.event_bus.publish(bus_key, {
                    "type": "message_done",
                    "data": {
                        "correlation_id": entry.correlation_id,
                        "session_id": session_id,
                    },
                })
            except Exception:
                pass
            try:
                await _mq.mark_done(entry.id)
                _mq.resolve_awaiter(
                    entry.correlation_id, {"status": "completed"},
                )
            except Exception:
                pass
        finally:
            await _inc_agent_turns(request, -1)
            # Chain to the next queued entry for this session.
            try:
                await _drain_queue_next(request, app_id, session_id, user_id)
            except Exception as exc:
                logger.warning("queue_drain_chain_failed: %s", exc)

    async def _guarded_next():
        async with _turn_semaphore:
            await _run_next()

    task = asyncio.create_task(_guarded_next())
    _active_turn_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _active_turn_tasks.discard(t)
    task.add_done_callback(_done)


# ── Queue inspection + cancellation endpoints ────────────────────────


@router.get(
    "/{app_id}/sessions/{session_id}/context-breakdown",
    response_model=AppResponse,
)
async def get_context_breakdown(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Debug: what's eating the session's context window right now.

    Returns a token-estimate per injection surface:

    - ``system_prompt`` — the full prompt the LLM sees (identity, tool
      instructions, behavioral guidelines, setup_summary, skills,
      module sections).
    - ``tools_schema`` — JSON schema of every tool (in-schema tokens).
    - ``messages`` — everything in ``ConversationSession.messages``
      (system + user + assistant + tool).
    - ``memory_injected`` — the memory module's rendered prompt
      section (goal, todos, facts).
    - ``setup_summary`` — setup step outputs injected at bootstrap.
    - ``skills`` — skill .md content concatenated.
    - ``total`` — sum matching what ``_call_llm`` actually sends.

    Use this when hitting context overflows to identify the offender.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # BUG-076: reject anonymous / cross-user context-breakdown peeking.
    session = await _require_session_access(request, app_id, session_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")

    from digitorn.core.runtime.compaction import estimate_tokens
    ctx = deployed.entry_context

    def _est(text: str | None) -> int:
        if not text:
            return 0
        return max(1, len(text) // 3)

    # 1. Messages (what's in the session)
    msg_tokens = estimate_tokens(session.messages or [])

    # 2. System prompt — reconstruct what the agent_loop uses
    try:
        from digitorn.core.runtime.messages import to_chat_messages
        sys_prompt = ctx.system_prompt or ""
    except Exception:
        sys_prompt = ""
    sys_tokens = _est(sys_prompt)

    # 3. Tools schema
    tools = ctx.tools or []
    tools_json = ""
    try:
        import json as _json
        tools_json = _json.dumps(tools, default=str)
    except Exception:
        pass
    tools_tokens = _est(tools_json)
    if not tools_tokens:
        # Fallback: 90 tokens per tool heuristic
        tools_tokens = len(tools) * 90

    # 4. Memory injected section
    mem_tokens = 0
    try:
        mem = ctx.memory_module
        if mem is not None and hasattr(mem, "get_prompt_sections"):
            sections = mem.get_prompt_sections(session_id=session_id) or []
            total_text = "\n".join(
                s.get("content", "") if isinstance(s, dict) else str(s)
                for s in sections
            )
            mem_tokens = _est(total_text)
    except Exception:
        pass

    # 5. Setup summary
    setup_text = ""
    try:
        setup = ctx.setup_summary or {}
        if isinstance(setup, dict):
            import json as _json
            setup_text = _json.dumps(setup, default=str)
    except Exception:
        pass
    setup_tokens = _est(setup_text)

    # 6. Skills content
    skills_text = ""
    try:
        agent = getattr(ctx, "agent_def", None)
        if agent is not None:
            skills_text = getattr(agent, "skills_content", "") or ""
    except Exception:
        pass
    skills_tokens = _est(skills_text)

    # 7. Context config
    cc = ctx.context_config
    max_tokens = cc.max_tokens if cc else 131072
    output_reserved = cc.output_reserved if cc else 8192
    effective = max_tokens - output_reserved

    total = sys_tokens + tools_tokens + msg_tokens
    pressure = total / max(effective, 1)

    return AppResponse(success=True, data={
        "session_id": session_id,
        "context_window": max_tokens,
        "output_reserved": output_reserved,
        "effective_max": effective,
        "total_estimated": total,
        "pressure": round(pressure, 4),
        "budget_remaining": max(0, effective - total),
        "will_overflow": total > effective,
        "breakdown": {
            "system_prompt": sys_tokens,
            "tools_schema": tools_tokens,
            "messages": msg_tokens,
            # Informational — already counted inside system_prompt:
            "_memory_injected": mem_tokens,
            "_setup_summary": setup_tokens,
            "_skills": skills_tokens,
        },
        "tool_count": len(tools),
        "message_count": len(session.messages or []),
        "advice": _context_advice(total, effective, sys_tokens, tools_tokens, msg_tokens, mem_tokens),
    })


def _context_advice(
    total: int, effective: int,
    sys_tokens: int, tools_tokens: int, msg_tokens: int, mem_tokens: int,
) -> list[str]:
    """Heuristic hints shown when context is tight."""
    tips: list[str] = []
    if total > effective:
        tips.append(
            f"OVERFLOW: {total}/{effective} — your next turn will be rejected."
        )
    elif total > effective * 0.9:
        tips.append(
            f"Tight: {total}/{effective} ({round(total/effective*100)}%) — compaction imminent."
        )
    if tools_tokens > sys_tokens and tools_tokens > 30000:
        tips.append(
            "Tool schemas dominate. Consider granting fewer tools per agent "
            "(``agents[].modules: [{filesystem: [read, write]}]``), or "
            "switch ``tool_injection: discovery`` to defer tool exposure."
        )
    if mem_tokens > 10000:
        tips.append(
            f"Memory snippet is {mem_tokens} tokens — check memory module "
            "``get_prompt_sections`` for oversized facts/procedures."
        )
    if msg_tokens > effective * 0.5:
        tips.append(
            "Message history is large — auto-compact should trigger soon. "
            "Force manually via the ``/compact`` hook or /abort + new session."
        )
    return tips


@router.get(
    "/{app_id}/sessions/{session_id}/events",
    response_model=AppResponse,
)
async def list_session_events(
    request: Request, app_id: str, session_id: str,
    since_seq: int = 0,
    since_ts: str | None = None,
    limit: int = 500,
) -> AppResponse:
    """Fetch the persistent event log for a session.

    Unlike the in-memory ring buffer (which covers only the last N events
    per user), this endpoint reads from the ``session_events`` DB table
    that captures every meaningful event (hooks, tool calls, message
    lifecycle, errors, agent spawns, …) with timestamp + seq. Useful
    when:

    - The Flutter client reopens a session older than the ring buffer.
    - A new device joins and needs the full history.
    - Audit / compliance needs a provable turn-by-turn trace.

    Token-level events are NOT logged (reconstructed from persisted
    message content). Everything else IS.

    ``since_seq`` or ``since_ts`` filter to only events after the watermark.

    **Relationship to Socket.IO ``join_session``**

    The Socket.IO join flow pushes three distinct groups of events:

    1. *Durable replay* — the same ``session_events`` rows this HTTP
       route returns (identical shape, identical filter).
    2. *Preview/workspace hydration* — ``preview:snapshot``,
       ``workspace:snapshot`` emitted once at join time from the
       in-memory preview module (NOT persisted to ``session_events``).
    3. *Bootstrap side-channels* — occasional channel/widget snapshots.

    So ``count(/events) <= count(socket events)``: if the two numbers
    disagree, the difference is hydration, not a missing durable row.
    To verify parity, compare only the envelopes whose ``seq`` is set.

    Events are scoped to the caller's ``user_id`` — an admin querying
    another user's session gets nothing here (same rule as Socket.IO).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    # BUG-070 + BUG-073: refuse anonymous + cross-user access.
    await _require_session_access(request, app_id, session_id)

    limit = max(1, min(limit, 5000))
    user_id = getattr(request.state, "user_id", "") or ""

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionEvent
    from sqlalchemy import select
    sf = get_session_factory()
    async with sf() as db:
        stmt = (
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .order_by(SessionEvent.seq.asc())
            .limit(limit)
        )
        if user_id:
            stmt = stmt.where(SessionEvent.user_id == user_id)
        if since_seq:
            stmt = stmt.where(SessionEvent.seq > since_seq)
        if since_ts:
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(since_ts.replace("Z", "+00:00"))
                stmt = stmt.where(SessionEvent.ts > ts)
            except Exception:
                pass
        r = await db.execute(stmt)
        rows = r.scalars().all()
    return AppResponse(success=True, data={
        "session_id": session_id,
        "count": len(rows),
        "total": len(rows),
        "events": [
            {
                "type": row.type,
                "kind": row.kind,
                "seq": row.seq,
                "ts": row.ts.isoformat() if row.ts else None,
                "payload": row.payload or {},
                "correlation_id": row.correlation_id or None,
            }
            for row in rows
        ],
        "note": (
            "Socket.IO join_session also pushes preview/workspace "
            "hydration events that are not in this durable log."
        ),
    })


@router.get(
    "/{app_id}/sessions/{session_id}/active-ops",
    response_model=AppResponse,
)
async def list_active_ops(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """List non-terminal operations for a session.

    This is the reconnect probe. Every event the daemon emits carries
    ``{op_id, op_type, op_state}`` (see :mod:`digitorn.core.events.envelope`),
    so the current state of any in-flight tool / sub-agent / approval /
    compaction / turn can be reconstructed by grouping ``session_events``
    rows by ``op_id`` and keeping the latest. This route does that
    group-by server-side and returns ONLY the ops whose latest
    ``op_state`` is still non-terminal (``pending``, ``running``,
    ``waiting_approval``).

    Typical client flow after a reconnect::

        # 1. replay everything since last known seq (gives the turn
        #    timeline for rendering bubbles, tool chips, thinking, …)
        GET /sessions/{sid}/events?since_seq=<last>

        # 2. ask the server what's still alive right now
        GET /sessions/{sid}/active-ops
        → [{op_id, op_type, op_state, started_at, last_ts, ...}, ...]

    Without (2), a client that disconnected DURING a long tool call
    has to guess. With (2), the spinner is restored instantly.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    # Cross-user / anonymous access → 404, same contract as other
    # per-session endpoints.
    await _require_session_access(request, app_id, session_id)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import SessionEvent
    from digitorn.core.events.envelope import TERMINAL_STATES, OpState
    from sqlalchemy import select

    user_id = getattr(request.state, "user_id", "") or ""
    terminal_names = {s.value for s in TERMINAL_STATES}

    # Scan persisted events, group by op_id, keep the latest.
    ops: dict[str, dict[str, Any]] = {}
    sf = get_session_factory()
    async with sf() as db:
        stmt = (
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .where(SessionEvent.user_id == user_id)
            .order_by(SessionEvent.seq.asc())
        )
        r = await db.execute(stmt)
        rows = r.scalars().all()

    for row in rows:
        payload = row.payload or {}
        op_id = payload.get("op_id") or row.correlation_id
        if not op_id:
            continue
        op_type = payload.get("op_type")
        op_state = payload.get("op_state")
        # Backward-compat: old rows without the contract — infer from
        # type so the endpoint still gives something useful during the
        # migration window.
        if not op_type or not op_state:
            from digitorn.core.events.envelope import (
                _LEGACY_OP_TYPE, _LEGACY_OP_STATE,
            )
            _ot = _LEGACY_OP_TYPE.get(row.type)
            _os = _LEGACY_OP_STATE.get(row.type)
            op_type = op_type or (_ot.value if _ot else "system")
            op_state = op_state or (_os.value if _os else "running")
        entry = ops.setdefault(op_id, {
            "op_id": op_id,
            "op_type": op_type,
            "op_state": op_state,
            "op_parent_id": payload.get("op_parent_id"),
            "first_seq": row.seq,
            "started_at": row.ts.isoformat() if row.ts else None,
            "last_seq": row.seq,
            "last_ts": row.ts.isoformat() if row.ts else None,
            "last_type": row.type,
            "correlation_id": row.correlation_id or None,
        })
        # Later events update the running view of the op.
        entry["op_state"] = op_state
        entry["last_seq"] = row.seq
        entry["last_ts"] = row.ts.isoformat() if row.ts else None
        entry["last_type"] = row.type
        if payload.get("op_parent_id"):
            entry["op_parent_id"] = payload["op_parent_id"]

    active = [e for e in ops.values() if e["op_state"] not in terminal_names]
    # Stable ordering: oldest-first by first_seq so the client can
    # render them in the order they started.
    active.sort(key=lambda e: (e.get("first_seq") or 0))

    return AppResponse(success=True, data={
        "app_id": app_id,
        "session_id": session_id,
        "active_ops": active,
        "count": len(active),
        "terminal_states": sorted(terminal_names),
        "scanned_events": len(rows),
    })


@router.get("/{app_id}/sessions/{session_id}/queue", response_model=AppResponse)
async def list_session_queue(
    request: Request, app_id: str, session_id: str,
    include_finished: bool = False,
) -> AppResponse:
    """List pending + running messages for this session.

    Use ``include_finished=true`` to also see recently completed /
    failed / cancelled entries (kept briefly for tracking).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    # BUG-076: reject anonymous / cross-user queue peeking.
    await _require_session_access(request, app_id, session_id)
    from digitorn.core.app import message_queue as _mq
    entries = await _mq.list_for_session(session_id, include_finished=include_finished)
    return AppResponse(success=True, data={
        "session_id": session_id,
        "entries": [e.to_dict() for e in entries],
        "total": len(entries),
    })


@router.delete(
    "/{app_id}/sessions/{session_id}/queue/{entry_id}",
    response_model=AppResponse,
)
async def cancel_queued_message(
    request: Request, app_id: str, session_id: str, entry_id: str,
) -> AppResponse:
    """Cancel a queued (not yet running) message. Running messages must
    be aborted via ``POST /abort``."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    _validate_id(entry_id, "entry_id")
    from digitorn.core.app import message_queue as _mq
    ok = await _mq.cancel(session_id, entry_id)
    if ok:
        manager = _get_manager(request)
        _uid = getattr(request.state, "user_id", None) or "local"
        bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
        try:
            await manager.event_bus.publish(bus_key, {
                "type": "message_cancelled",
                "data": {"entry_id": entry_id, "session_id": session_id},
            })
        except Exception:
            pass
    return AppResponse(
        success=ok,
        data={"cancelled": ok, "entry_id": entry_id},
        error=None if ok else "entry not found or already running",
    )


@router.post(
    "/{app_id}/sessions/{session_id}/queue/clear",
    response_model=AppResponse,
)
async def clear_session_queue(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Cancel every queued (non-running) message for this session."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    from digitorn.core.app import message_queue as _mq
    n = await _mq.clear(session_id)
    manager = _get_manager(request)
    _uid = getattr(request.state, "user_id", None) or "local"
    bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
    try:
        await manager.event_bus.publish(bus_key, {
            "type": "queue_cleared",
            "data": {"session_id": session_id, "cancelled": n},
        })
    except Exception:
        pass
    return AppResponse(success=True, data={"cancelled": n})


class CreateSessionRequest(BaseModel):
    """Optional body for `POST /sessions` — workspace selection at creation.

    When ``workspace_path`` is provided, the session is bound to that
    filesystem directory immediately and the preview/workspace persistence
    backend switches to filesystem mode (state lives in
    ``{workspace_path}/.digitorn/sessions/{sid}/`` instead of the daemon DB).
    """
    workspace_path: str | None = None


@router.post("/{app_id}/sessions", response_model=AppResponse)
async def create_session(
    request: Request, app_id: str,
    body: CreateSessionRequest | None = None,
) -> AppResponse:
    """Create a new conversation session.

    Returns a server-generated session_id that the client uses for all
    subsequent calls (/messages, /events, /history, /abort, etc.).

    The session is initialized empty — no messages, no workspace, unless
    ``workspace_path`` is provided in the body. When set, the directory
    must exist and be writable; the daemon will persist session state
    inside ``{workspace_path}/.digitorn/sessions/{session_id}/``.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    import uuid as _uuid
    from datetime import datetime, timezone
    from pathlib import Path as _Path

    user_id = getattr(request.state, "user_id", None) or "local"
    session_id = str(_uuid.uuid4())

    ws_path = (body.workspace_path if body else None) or ""
    if ws_path:
        try:
            p = _Path(ws_path).expanduser().resolve()
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
            if not p.is_dir():
                raise HTTPException(
                    status_code=400,
                    detail=f"workspace_path is not a directory: {p}",
                )
            test = p / ".digitorn"
            test.mkdir(parents=True, exist_ok=True)
            ws_path = str(p)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"workspace_path unusable: {exc}",
            )

    # Create the session in the store so it appears in listings immediately
    from digitorn.core.app.sessions import ConversationSession
    session = ConversationSession(
        session_id=session_id,
        app_id=app_id,
        user_id=user_id,
        workspace=ws_path,
    )

    # Get the deployed app's system prompt for initialization
    deployed = _get_deployed(request, app_id)
    if deployed:
        effective_prompt = deployed.entry_context.system_prompt or ""
        if effective_prompt:
            session.add_system(effective_prompt)

        # Apply greeting if configured
        greeting = getattr(deployed.compiled.execution, "greeting", "")
        if greeting and greeting.strip():
            session.greeting = greeting.strip()

    await asyncio.to_thread(manager._session_store.put, session)

    # Compute initial context estimate
    context = {}
    if deployed:
        entry_ctx = deployed.entry_context
        system_prompt = entry_ctx.system_prompt or ""
        tools = entry_ctx.tools or []
        cc = entry_ctx.context_config

        sys_tokens = len(system_prompt) // 4
        tools_tokens = len(tools) * 90
        max_tok = cc.max_tokens if cc else 200000
        out_reserved = cc.output_reserved if cc else 4096
        effective = max_tok - out_reserved
        total = sys_tokens + tools_tokens

        context = {
            "max_tokens": max_tok,
            "output_reserved": out_reserved,
            "effective_max": effective,
            "system_prompt_tokens": sys_tokens,
            "system_prompt_pct": round(sys_tokens / max(effective, 1) * 100, 2),
            "tools_schema_tokens": tools_tokens,
            "tools_schema_pct": round(tools_tokens / max(effective, 1) * 100, 2),
            "message_history_tokens": 0,
            "message_history_pct": 0,
            "total_estimated_tokens": total,
            "pressure": round(total / max(effective, 1), 4),
            "available_tokens": max(0, effective - total),
            "compactions": 0,
        }

    preview_url: str | None = None
    if deployed is not None:
        # Dev-server mode (preview.enabled: true) — preview_manager exists.
        # Static-dist mode (preview.enabled: false) — no preview_manager but
        # the preview module is loaded and web/dist/ serves the UI.
        has_preview = (
            getattr(deployed, "preview_manager", None) is not None
            or "preview" in (deployed.modules or {})
        )
        if has_preview:
            # Include the caller's JWT so the iframe can authenticate
            # its Socket.IO connection without a separate login flow.
            _auth_hdr = request.headers.get("authorization", "")
            _preview_token = _auth_hdr.split(" ", 1)[1] if _auth_hdr.startswith("Bearer ") else ""
            preview_url = (
                f"/api/apps/{app_id}/preview-server/proxy/"
                f"?session_id={session_id}"
            )
            if _preview_token:
                preview_url += f"&token={_preview_token}"

    return AppResponse(success=True, data={
        "session_id": session_id,
        "app_id": app_id,
        "title": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": False,
        "message_count": 0,
        "greeting": getattr(session, "greeting", ""),
        "context": context,
        "preview_url": preview_url,
        "workspace": ws_path,
    })


@router.get("/{app_id}/sessions", response_model=AppResponse)
async def list_sessions(
    request: Request,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """List sessions for an app with pagination.

    Query params:
        limit:  max sessions to return (default 50, max 200, 0 = all)
        offset: skip first N sessions (for pagination)

    Response includes ``total`` for the frontend to know if there are more.
    """
    _validate_id(app_id)
    limit = max(0, min(limit, 200))
    offset = max(0, offset)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    user_id = getattr(request.state, "user_id", None)
    total = await manager.count_sessions(app_id, user_id=user_id)
    sessions = await manager.list_sessions(app_id, user_id=user_id, limit=limit, offset=offset)
    for s in sessions:
        s["is_active"] = manager.is_session_active(app_id, s.get("session_id", ""))
    return AppResponse(success=True, data={
        "sessions": sessions,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/sessions/search", response_model=AppResponse)
async def search_sessions(
    request: Request,
    app_id: str,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> AppResponse:
    """Search across all sessions — matches title and message content.

    Returns sessions ranked by relevance (title match > recent message match).
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    if not q or not q.strip():
        return AppResponse(success=True, data={"sessions": [], "total": 0, "query": q})

    query = q.strip().lower()
    user_id = getattr(request.state, "user_id", None)

    # Get all sessions (max 200 to bound search cost)
    if user_id:
        all_sessions = await manager.list_sessions(app_id, user_id=user_id, limit=200)
    else:
        all_sessions = await manager.list_sessions(app_id, limit=200)

    matches: list[tuple[int, dict]] = []  # (score, session_summary)
    for s in all_sessions:
        score = 0
        sid = s.get("session_id", "")
        title = s.get("title", "")
        snippets: list[str] = []

        # Title match (highest weight)
        if query in title.lower():
            score += 10
            snippets.append(f"title: {title[:100]}")

        # Search in persisted messages (first 10 messages, 500 chars each)
        try:
            uid = s.get("user_id", user_id or "local")
            messages = await asyncio.to_thread(manager._session_store.load_messages, app_id, sid, user_id=uid)
            if messages:
                for i, msg in enumerate(messages[:10]):
                    content = (msg.get("content", "") or "")[:500].lower()
                    if query in content:
                        score += 5 if i < 2 else 1
                        # Extract snippet around match
                        idx = content.find(query)
                        start = max(0, idx - 40)
                        end = min(len(content), idx + len(query) + 40)
                        snippet = content[start:end].strip()
                        snippets.append(f"message[{i}]: ...{snippet}...")
                        if len(snippets) >= 3:
                            break
        except Exception:
            pass

        if score > 0:
            s["relevance"] = score
            s["snippets"] = snippets[:3]
            matches.append((score, s))

    # Sort by relevance (descending), then by last_active (descending)
    matches.sort(key=lambda x: (-x[0], -x[1].get("last_active", 0)))
    total = len(matches)
    page = [m[1] for m in matches[offset:offset + limit]]

    return AppResponse(success=True, data={
        "sessions": page,
        "total": total,
        "query": q,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/sessions/{session_id}", response_model=AppResponse)
async def get_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get session status — metadata, live metrics, context pressure.

    Single endpoint for clients to get the full session state on load.
    All numbers come from the SessionMetrics counters (populated by
    agent_loop during execution) — no estimation or speculation.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    user_id = getattr(request.state, "user_id", None)
    session = await manager.get_session(app_id, session_id, user_id=user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    data = session.summary()

    # is_active: True if an agent turn is in progress right now
    data["is_active"] = manager.is_session_active(app_id, session_id)

    # Live metrics from SessionMetrics (populated by agent_loop).
    # The agent_loop may store metrics under app_id="default" if ctx.app_id
    # is not set, so we try both the real app_id and "default".
    try:
        from digitorn.core.runtime.session_metrics import get_session_metrics, _sessions
        # Find the existing entry. Keys are "{app_id}:{session_id}:{agent_id}"
        # — we don't know the agent_id a priori (builder, main, chatbot, …),
        # so we scan every prefix match and pick the one with real tokens.
        # Falls back to "default:" prefix for legacy entries created before
        # ctx.app_id was wired (bootstrap pre-2026-04-19).
        sm = None
        best_total = -1
        for _try_app in (app_id, "default"):
            prefix = f"{_try_app}:{session_id}:"
            for _key, _entry in _sessions.items():
                if _key.startswith(prefix) and _entry.total_tokens > best_total:
                    sm = _entry
                    best_total = _entry.total_tokens
            if sm is not None and best_total > 0:
                break
        if sm is None:
            # Fallback: get_or_create (may be empty for new sessions)
            sm = get_session_metrics(app_id, session_id)
        data["turn_number"] = sm.turn
        data["tokens"] = {
            "prompt": sm.prompt_tokens,
            "completion": sm.completion_tokens,
            "total": sm.total_tokens,
        }
        ctx_snapshot = sm.context.snapshot()

        # If context is empty (no turn yet), compute initial estimate from the deployed app
        if ctx_snapshot.get("total_estimated_tokens", 0) == 0:
            deployed = _get_deployed(request, app_id)
            if deployed:
                entry_ctx = deployed.entry_context
                system_prompt = entry_ctx.system_prompt or ""
                tools = entry_ctx.tools or []
                cc = entry_ctx.context_config

                sys_tokens = len(system_prompt) // 4
                tools_tokens = len(tools) * 90
                msg_tokens = sum(
                    len(m.get("content", "")) // 4
                    for m in session.messages
                    if isinstance(m.get("content"), str)
                )
                total = sys_tokens + tools_tokens + msg_tokens
                max_tok = cc.max_tokens if cc else 200000
                out_reserved = cc.output_reserved if cc else 4096
                effective = max_tok - out_reserved

                ctx_snapshot = {
                    "max_tokens": max_tok,
                    "output_reserved": out_reserved,
                    "effective_max": effective,
                    "system_prompt_tokens": sys_tokens,
                    "system_prompt_pct": round(sys_tokens / max(effective, 1) * 100, 2),
                    "tools_schema_tokens": tools_tokens,
                    "tools_schema_pct": round(tools_tokens / max(effective, 1) * 100, 2),
                    "message_history_tokens": msg_tokens,
                    "message_history_pct": round(msg_tokens / max(effective, 1) * 100, 2),
                    "total_estimated_tokens": total,
                    "pressure": round(total / max(effective, 1), 4),
                    "available_tokens": max(0, effective - total),
                    "compactions": 0,
                }

        data["context"] = ctx_snapshot
        data["tools"] = {
            "total_calls": sm.tool_calls_total,
            "success": sm.tool_calls_success,
            "failed": sm.tool_calls_failed,
        }
        data["errors"] = {
            "total": sm.errors,
            "last": sm.last_error,
        }
        data["model"] = sm.model or (
            getattr(deployed.entry_context.provider, "model", "")
            if _get_deployed(request, app_id) else ""
        )
    except Exception:
        pass  # Metrics not available (session never had a chat turn)

    return AppResponse(success=True, data=data)


@router.get("/{app_id}/sessions/{session_id}/preview", response_model=AppResponse)
async def get_session_preview(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get the current preview snapshot for a session.

    Returns the full preview state: scalar state map, all resource
    channels, and the event ring buffer. Clients call this on connect
    (or reconnect) to hydrate the UI without replaying every event.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    preview_mod = deployed.modules.get("preview")
    if preview_mod is None:
        return AppResponse(success=True, data={"state": {}, "resources": {}})

    user_id = getattr(request.state, "user_id", None)
    snapshot = preview_mod.snapshot_for(session_id, user_id=user_id)
    return AppResponse(success=True, data=snapshot)


@router.get("/{app_id}/sessions/{session_id}/history", response_model=AppResponse)
async def get_session_history(
    request: Request, app_id: str, session_id: str,
    include_system: bool = False,
) -> AppResponse:
    """Get full conversation history for a session.

    Returns all messages (system, user, assistant) in order.
    Useful for SDK clients to restore context or display history.

    Args:
        include_system: If True, include raw system messages (behavior directives,
            violations, etc.). Default False — only user/assistant/tool.
            Used by the dev testing SDK to inspect behavior enforcement.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    user_id = getattr(request.state, "user_id", None)
    session = await manager.get_session(app_id, session_id, user_id=user_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    # Build structured turns from raw LLM messages
    if include_system:
        # Return raw messages including system (for dev SDK)
        turns = session.messages
    else:
        turns = _build_history_turns(session.messages)
    # Load persisted event log for full UI reconstruction
    events = await manager.load_session_events(app_id, session.session_id, user_id=user_id or "local")
    # Surface in-progress-turn state so a reopened client knows
    # immediately that a turn is still running (and which queued
    # messages are waiting behind it).
    from digitorn.core.app import message_queue as _mq
    turn_active = await manager.is_turn_running(app_id, session.session_id)
    try:
        pending_entries = await _mq.list_for_session(
            session.session_id, include_finished=False,
        )
    except Exception:
        pending_entries = []
    data: dict[str, Any] = {
        **session.summary(),
        "messages": turns,
        "events": events or [],
        "turn_active": turn_active,
        "pending_queue": [e.to_dict() for e in pending_entries],
    }
    # Include snapshots for workspace/memory/preview state restoration
    if session.memory_snapshot:
        data["memory_snapshot"] = session.memory_snapshot
    if session.preview_snapshot:
        data["preview_snapshot"] = session.preview_snapshot
    return AppResponse(success=True, data=data)


@router.get("/{app_id}/sessions/{session_id}/images/{image_id}")
async def get_session_image(
    request: Request, app_id: str, session_id: str, image_id: str,
):
    """Serve a session image by ID. Returns raw image bytes."""
    from fastapi.responses import Response
    from digitorn.core.image_store import get_image_store

    store = get_image_store()
    ref = store.get_ref(image_id, session_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="Image not found")

    data = await store.get(image_id, session_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Image data not found")

    return Response(
        content=data,
        media_type=ref.mime,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{app_id}/sessions/{session_id}/export", response_model=AppResponse)
async def export_session(
    request: Request,
    app_id: str,
    session_id: str,
    format: str = "markdown",
) -> AppResponse:
    """Export a session as Markdown (or other formats).

    Produces a clean document with all conversation turns, tool calls,
    and results. Suitable for sharing, archiving, or documentation.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # BUG-075: reject anonymous or cross-user export (data exfil).
    session = await _require_session_access(request, app_id, session_id)

    if format != "markdown":
        return AppResponse(success=False, error=f"Unsupported format: {format}. Use 'markdown'.")

    import datetime

    lines: list[str] = []
    title = session.title or "Untitled Session"
    created = datetime.datetime.fromtimestamp(session.created_at).strftime("%Y-%m-%d %H:%M")
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"**App:** {app_id} | **Session:** `{session_id[:8]}...` | **Created:** {created}")
    lines.append("")
    lines.append("---")
    lines.append("")

    turn_num = 0
    for msg in session.messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "system":
            continue  # Skip system prompts in export

        if role == "user":
            turn_num += 1
            lines.append(f"## Turn {turn_num}")
            lines.append("")
            lines.append(f"**User:**")
            lines.append("")
            lines.append(content.strip())
            lines.append("")

        elif role == "assistant":
            if content.strip():
                lines.append(f"**Assistant:**")
                lines.append("")
                lines.append(content.strip())
                lines.append("")

            # Tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", "")
                    if isinstance(args, str) and len(args) > 200:
                        args = args[:200] + "..."
                    lines.append(f"> **Tool:** `{name}`")
                    if args:
                        lines.append(f"> ```json")
                        lines.append(f"> {args}")
                        lines.append(f"> ```")
                    lines.append("")

        elif role == "tool":
            # Tool result
            result = content
            if len(result) > 500:
                result = result[:500] + "\n... (truncated)"
            lines.append(f"> **Result:**")
            lines.append(f"> ```")
            for rline in result.split("\n")[:20]:
                lines.append(f"> {rline}")
            lines.append(f"> ```")
            lines.append("")

    # Footer with stats
    lines.append("---")
    lines.append("")
    lines.append(f"*Exported from Digitorn · {turn_num} turns · {len(session.messages)} messages*")

    md = "\n".join(lines)
    filename = f"{app_id}_{session_id[:8]}_{created.replace(' ', '_').replace(':', '')}.md"

    return AppResponse(success=True, data={
        "content": md,
        "format": "markdown",
        "filename": filename,
        "turns": turn_num,
        "messages": len(session.messages),
    })


@router.delete("/{app_id}/sessions/{session_id}", response_model=AppResponse)
async def delete_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Delete a session and its history."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    deleted = await manager.end_session(app_id, session_id, user_id=_uid)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return AppResponse(success=True, data={
        "session_id": session_id,
        "deleted": True,
    })


# ── Session-level actions (compact, undo, fork) ──────────────────────────

@router.post("/{app_id}/sessions/{session_id}/compact", response_model=AppResponse)
async def compact_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Compact the context window for a session (truncate old messages)."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    deployed = manager._deployed.get(app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")

    messages = session.messages
    before = len(messages)
    if before < 4:
        return AppResponse(success=True, data={
            "before": before, "after": before, "freed": 0,
            "note": "Too few messages to compact",
        })

    try:
        from digitorn.core.runtime.compaction import emergency_compact
        ctx = deployed.entry_context
        await emergency_compact(ctx, messages)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Compaction failed: {exc}")

    after = len(messages)
    return AppResponse(success=True, data={
        "before": before,
        "after": after,
        "freed": before - after,
    })


@router.post("/{app_id}/sessions/{session_id}/undo", response_model=AppResponse)
async def undo_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Undo the last file edit via filesystem checkpoints."""
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = manager._deployed.get(app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")

    fs_module = deployed.modules.get("filesystem")
    if fs_module is None or not hasattr(fs_module, "_checkpoints"):
        return AppResponse(success=False, error="Filesystem undo not available")

    if not fs_module._checkpoints:
        return AppResponse(success=False, error="No checkpoints to undo")

    # Find the most recently checkpointed file
    from pathlib import Path as _Path
    latest_path = None
    latest_ts = 0.0
    for fpath, stack in fs_module._checkpoints.items():
        if stack and stack[-1][0] > latest_ts:
            latest_ts = stack[-1][0]
            latest_path = fpath
    if latest_path is None:
        return AppResponse(success=False, error="No checkpoints found")

    stack = fs_module._checkpoints[latest_path]
    _ts, content = stack.pop()
    _Path(latest_path).write_bytes(content)
    remaining = sum(len(s) for s in fs_module._checkpoints.values())

    return AppResponse(success=True, data={
        "path": latest_path,
        "restored_bytes": len(content),
        "remaining": remaining,
    })


@router.post("/{app_id}/sessions/{session_id}/fork", response_model=AppResponse)
async def fork_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Fork a session — create a new session with the same message history."""
    import uuid as _uuid
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    # BUG-074: reject cross-user fork (session theft).
    source = await _require_session_access(request, app_id, session_id)
    _uid = getattr(request.state, "user_id", None) or "local"

    new_id = str(_uuid.uuid4())
    from digitorn.core.app.sessions import ConversationSession
    new_session = ConversationSession(
        session_id=new_id,
        app_id=app_id,
        user_id=_uid,
        messages=[m.copy() for m in source.messages],
        title=f"Fork of {source.title or session_id[:8]}",
        memory_snapshot=dict(source.memory_snapshot),
    )
    await asyncio.to_thread(manager._session_store.put, new_session)

    return AppResponse(success=True, data={
        "forked_from": session_id,
        "new_session_id": new_id,
        "message_count": len(new_session.messages),
    })


@router.post("/{app_id}/sessions/{session_id}/abort", response_model=AppResponse)
async def abort_session_turn(
    request: Request, app_id: str, session_id: str,
    purge_queue: bool = False,
) -> AppResponse:
    """Abort the currently running agent turn for a session.

    **Default behavior**: cancels only the currently running turn. The
    rest of the message queue is **preserved** and the dispatcher
    picks up the next message automatically — the user gets
    ``message_started`` for ``next_correlation_id`` within seconds.

    ``?purge_queue=true`` drops every queued message along with the
    abort — use when the user clicks "Stop everything" rather than
    "Skip this message".

    The session state (messages, memory, tool calls) is preserved up
    to the interruption point. Orphaned tool_calls get synthetic error
    results on the next message so the LLM resumes cleanly.

    Safe to call even if no turn is running (returns success with
    was_active=false).
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    # BUG-071: reject cross-user aborts (destructive remote DoS).
    await _require_session_access(request, app_id, session_id)

    _uid = getattr(request.state, "user_id", None) or "local"

    was_active = manager.is_session_active(app_id, session_id)

    # Cancel the running asyncio task — this triggers CancelledError
    # inside _chat_locked which saves state + marks session.interrupted
    active_key = f"{app_id}:{session_id}"
    task = manager._session_tasks.get(active_key)
    if task is not None and not task.done():
        task.cancel()

    # Kill background shell tasks for this session so they don't orphan.
    # Each killed task sends a 'cancelled' notification to the agent queue
    # so the agent knows on resume.
    bg_killed = 0
    agents_killed = 0
    deployed = manager._deployed.get(app_id)
    if deployed:
        shell_mod = deployed.modules.get("shell")
        if shell_mod is not None and hasattr(shell_mod, "cleanup_session"):
            try:
                await shell_mod.cleanup_session(session_id)
                bg_killed = 1  # flag that cleanup ran
            except Exception:
                logger.debug("abort: shell cleanup_session failed", exc_info=True)

        # Kill running sub-agents for this session.
        # They can't survive without the coordinator, and their asyncio tasks
        # would leak if left running after the parent turn is cancelled.
        spawn_mod = deployed.modules.get("agent_spawn")
        if spawn_mod is not None and hasattr(spawn_mod, "cleanup_session"):
            try:
                await spawn_mod.cleanup_session(session_id)
                agents_killed = 1
            except Exception:
                logger.debug("abort: agent_spawn cleanup_session failed", exc_info=True)

        # Kill background tasks from context_builder (watchers, background_run)
        cb_mod = deployed.entry_context.context_builder if hasattr(deployed, "entry_context") else None
        if cb_mod is None:
            cb_mod = deployed.modules.get("context_builder")
        if cb_mod is not None and hasattr(cb_mod, "cleanup_session_bg_tasks"):
            try:
                await cb_mod.cleanup_session_bg_tasks(session_id)
            except Exception:
                logger.debug("abort: context_builder cleanup_session_bg_tasks failed", exc_info=True)

    # Queue handling — default is "keep the rest". Explicit opt-in
    # ``?purge_queue=true`` drops everything. The currently-running row
    # is ALSO cleaned up here (the drain in _run_turn's finally will
    # mark it done, but we want abort semantics: status=cancelled).
    queue_purged = 0
    from digitorn.core.app import message_queue as _mq
    try:
        # Mark the currently running row as cancelled (if any). The
        # drain chain will still kick in after _run_turn's finally
        # returns and pick the next queued message.
        entries = await _mq.list_for_session(session_id)
        for e in entries:
            if e.status == "running":
                try:
                    await _mq.mark_cancelled(e.id)
                    _mq.fail_awaiter(
                        e.correlation_id,
                        RuntimeError("aborted by user"),
                    )
                except Exception:
                    pass
                break
        if purge_queue:
            queue_purged = await _mq.clear(session_id)
    except Exception:
        logger.debug("abort: queue cleanup failed", exc_info=True)

    # Signal abort via the event bus (Socket.IO clients see it immediately)
    try:
        bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
        await manager.event_bus.publish(bus_key, {
            "type": "abort",
            "session_id": session_id,
            "queue_purged": queue_purged,
            "queue_preserved": not purge_queue,
        })
    except Exception:
        pass

    # If the queue was preserved AND there's a next message queued,
    # kick off the drain NOW so the frontend sees message_started for
    # the next entry without having to wait for _run_turn's finally
    # (which might be blocked on the cancellation).
    if not purge_queue:
        try:
            # Small delay so the cancelled task fully winds down before
            # we dispatch the next. Prevents a race where the drain
            # tries to acquire the session_lock before the aborted
            # task has released it.
            async def _resume() -> None:
                await asyncio.sleep(0.2)
                try:
                    await _drain_queue_next(
                        request, app_id, session_id, _uid,
                    )
                except Exception as exc:
                    logger.warning("abort_resume_failed: %s", exc)
            asyncio.create_task(_resume())
        except Exception:
            pass

    return AppResponse(success=True, data={
        "session_id": session_id,
        "was_active": was_active,
        "aborted": True,
        "bg_tasks_cleaned": bg_killed > 0,
        "agents_cleaned": agents_killed > 0,
        "queue_purged": queue_purged,
        "queue_preserved": not purge_queue,
    })


@router.get("/{app_id}/sessions/{session_id}/tasks", response_model=AppResponse)
async def list_background_tasks(request: Request, app_id: str, session_id: str) -> AppResponse:
    """List all background shell tasks for a session.

    Returns running and recently finished tasks with their status,
    command, pid, uptime, and output line counts.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = manager._deployed.get(app_id)
    shell_mod = deployed.modules.get("shell") if deployed else None
    if shell_mod is None or not hasattr(shell_mod, "_tasks"):
        return AppResponse(success=True, data={"tasks": []})

    # Filter tasks belonging to this session
    session_task_ids = set()
    if hasattr(shell_mod, "_session_tasks"):
        session_task_ids = shell_mod._session_tasks.get(session_id, set())

    tasks = []
    for tid in session_task_ids:
        task = shell_mod._tasks.get(tid)
        if task is None:
            continue
        tasks.append({
            "task_id": tid,
            "command": task.command[:200],
            "status": "running" if task.is_running else "finished",
            "exit_code": task.exit_code,
            "pid": task.pid,
            "uptime_seconds": round(task.uptime_seconds, 1),
            "stdout_lines": len(task.stdout_lines),
            "stderr_lines": len(task.stderr_lines),
            "started_at": task.started_at,
            "finished_at": task.finished_at,
        })

    return AppResponse(success=True, data={"tasks": tasks})


@router.post("/{app_id}/sessions/{session_id}/tasks/{task_id}/cancel", response_model=AppResponse)
async def cancel_background_task(
    request: Request, app_id: str, session_id: str, task_id: str,
) -> AppResponse:
    """Cancel a specific background shell task (user-initiated).

    Terminates the process, sends a 'cancelled' notification to the agent
    so it knows the user stopped the task on next turn.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = manager._deployed.get(app_id)
    shell_mod = deployed.modules.get("shell") if deployed else None
    if shell_mod is None or not hasattr(shell_mod, "cancel_task"):
        raise HTTPException(status_code=404, detail="Shell module not available")

    result = await shell_mod.cancel_task(session_id, task_id)
    return AppResponse(success=result.get("success", False), data=result)


@router.post("/{app_id}/sessions/{session_id}/resume", response_model=AppResponse)
async def resume_session(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Resume an interrupted session automatically.

    When a session was interrupted (daemon crash, network loss, etc.),
    the client calls this to trigger recovery WITHOUT the user typing
    a message. The daemon:

    1. Detects orphaned tool_calls and injects synthetic error results
    2. Sends a system message telling the LLM to continue
    3. Triggers a new agent turn (async, events via Socket.IO)

    If the session is NOT interrupted or is currently active, returns
    a no-op success response.

    The client flow on reconnect:
        GET  /sessions/{sid}                   → check is_active + interrupted
        Socket.IO join_session with since=N    → reconnect + replay missed events
        POST /sessions/{sid}/resume            → auto-continue if interrupted
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # If a turn is already running, nothing to resume
    if manager.is_session_active(app_id, session_id):
        return AppResponse(success=True, data={
            "resumed": False,
            "reason": "Turn already in progress",
        })

    # If session is not interrupted, nothing to do
    if not session.interrupted:
        return AppResponse(success=True, data={
            "resumed": False,
            "reason": "Session is not interrupted",
        })

    # Trigger an auto-continue turn via /messages
    # The manager will detect interrupted=True and run _recover_interrupted_session
    async def _resume_turn():
        try:
            await manager.chat(
                app_id, session_id,
                "[auto-resume] Continue from where you left off.",
                user_id=_uid,
            )
        except Exception as exc:
            bus_key = manager.event_bus.session_key(app_id, session_id, _uid)
            await manager.event_bus.publish(bus_key, {
                "type": "error",
                "data": {"error": f"Resume failed: {exc}"},
            })

    async def _guarded_resume():
        async with _turn_semaphore:
            await _resume_turn()

    task = asyncio.create_task(_guarded_resume())
    _active_turn_tasks.add(task)
    task.add_done_callback(_active_turn_tasks.discard)

    return AppResponse(success=True, data={
        "resumed": True,
        "session_id": session_id,
    })


@router.get("/{app_id}/sessions/{session_id}/memory", response_model=AppResponse)
async def get_session_memory(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Get the current memory state for a session (goal, todos, facts).

    Lighter than full history — only the working memory snapshot.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # Memory snapshot from session
    memory = dict(session.memory_snapshot)

    # Also try to get live memory from the memory module
    deployed = manager._deployed.get(app_id)
    if deployed:
        mem_module = deployed.modules.get("memory")
        if mem_module and hasattr(mem_module, "store"):
            store = mem_module.store
            working = getattr(store, "working", None)
            if working:
                memory["goal"] = getattr(working, "goal", "") or ""
                memory["todos"] = [
                    t.to_dict() if hasattr(t, "to_dict") else str(t)
                    for t in getattr(working, "todos", [])
                ]
                memory["facts"] = [
                    {"content": f.content if hasattr(f, "content") else str(f)}
                    for f in getattr(working, "key_facts", [])
                ]

    return AppResponse(success=True, data=memory)


@router.get("/{app_id}/sessions/{session_id}/workspace", response_model=AppResponse)
async def get_session_workspace(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Full workspace snapshot for a session — durable + in-memory state merged.

    Returns everything the client needs to re-render the session view
    identically on reopen:

    - ``workspace`` / ``workspace_mode`` — physical workspace dir (if any)
    - ``render_mode`` / ``entry_file`` — from the top-level ``workspace:`` YAML
    - ``snapshot`` — the live preview state tree (``state`` map, ``resources``
      channels, last ``seq``). Hydrated from DB on first read after a
      daemon restart, then live-updated by every tool call.
    - ``git`` — git status of the physical workspace (if present)
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # BUG-076: reject anonymous / cross-user workspace peeking.
    session = await _require_session_access(request, app_id, session_id)
    _uid = session.user_id if session is not None else (
        getattr(request.state, "user_id", None) or "local"
    )

    # Post-scoping refactor: _deployed is keyed by (scope, owner, app_id).
    # Use the scope-aware manager.get() to resolve.
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")

    import os
    ws_mode = getattr(deployed.compiled.execution, "workspace_mode", "auto")
    if ws_mode == "fixed":
        workspace = getattr(deployed.compiled.execution, "workspace", "") or ""
    elif ws_mode == "none":
        workspace = ""
    else:
        workspace = os.getcwd()

    result: dict[str, Any] = {
        "session_id": session_id,
        "app_id": app_id,
        "workspace": workspace,
        "workspace_mode": ws_mode,
    }

    # Top-level workspace: block (render_mode, entry_file, title).
    ws_block = getattr(deployed.compiled, "workspace", None)
    if ws_block is not None:
        result["render_mode"] = getattr(ws_block, "render_mode", "auto")
        result["entry_file"] = getattr(ws_block, "entry_file", None)
        result["title"] = getattr(ws_block, "title", None)

    # Hydrated preview snapshot (state + resources + seq). Pulls from
    # in-memory store first; if empty (e.g. after restart and before a
    # turn), fetches directly from the DB snapshot table so reopening
    # a session from a cold client still renders the last state.
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    snapshot: dict[str, Any] = {
        "state": {}, "resources": {}, "seq": 0, "hydrated": False,
    }
    if preview_module is not None:
        try:
            state = await _activate_preview_session(
                request, app_id, session_id, preview_module, user_id=_uid,
            )
            if state is not None:
                snapshot = state.snapshot()
                snapshot["hydrated"] = True
            elif hasattr(preview_module, "snapshot_for"):
                snapshot = preview_module.snapshot_for(session_id, user_id=_uid)
                snapshot["hydrated"] = True
        except Exception as exc:
            logger.warning(
                "get_workspace_snapshot_failed sid=%s: %s",
                session_id, exc, exc_info=True,
            )
    result["snapshot"] = snapshot

    if workspace:
        result["git"] = _get_workspace_status(workspace)

    return AppResponse(success=True, data=result)


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


@router.get("/{app_id}/sessions/{session_id}/workspace/export", response_model=AppResponse)
async def export_session_workspace(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Export the full workspace snapshot as a portable JSON object.

    The returned payload can be POSTed to ``/workspace/import`` on any
    session of the same app (or ``/workspace/fork`` to create a new one).
    Suitable for "Save a copy", cross-user hand-off, or project
    templates.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # BUG-066 + cross-user guard: use the shared helper so the 404 we
    # return is indistinguishable from every other session route and
    # the owner lookup uses the same uid resolution path as GET /sessions.
    session = await _require_session_access(request, app_id, session_id)
    _uid = session.user_id

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module — nothing to export")

    try:
        state = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if state is None:
            raise RuntimeError("preview_module.activate_session returned None")
        snap = state.snapshot()
    except Exception as exc:
        logger.warning("export_snapshot_failed sid=%s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Snapshot export failed: {exc}")

    from datetime import datetime, timezone
    payload = {
        "format": "digitorn.workspace.snapshot",
        "version": 1,
        "app_id": app_id,
        "source_session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "state": snap.get("state", {}),
        "resources": snap.get("resources", {}),
        "seq": snap.get("seq", 0),
    }
    return AppResponse(success=True, data=payload)


@router.post("/{app_id}/sessions/{session_id}/workspace/import", response_model=AppResponse)
async def import_session_workspace(
    request: Request, app_id: str, session_id: str,
    body: WorkspaceImportRequest,
) -> AppResponse:
    """Import a snapshot into an existing session.

    Overwrites (``replace=True``) or merges (``replace=False``) the
    current in-memory state and force-flushes to DB so reopening the
    session yields the imported view.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or access denied")

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module — cannot import")

    snap = body.snapshot or {}
    snap_state = snap.get("state") or {}
    snap_resources = snap.get("resources") or {}
    snap_seq = int(snap.get("seq") or 0)

    try:
        dest = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if dest is None:
            raise RuntimeError("preview_module.activate_session returned None")
        if body.replace:
            dest.clear()
        dest.restore_from_dict({
            "state": {**(dest.state if not body.replace else {}), **snap_state},
            "resources": _merge_resources(dest.resources if not body.replace else {}, snap_resources),
            "seq": max(dest._seq, snap_seq) + 1,
            "user_id": _uid,
        })
        await preview_module._flush_now(session_id)
    except Exception as exc:
        logger.warning("import_snapshot_failed sid=%s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Snapshot import failed: {exc}")

    return AppResponse(success=True, data={
        "session_id": session_id,
        "imported": True,
        "replaced": body.replace,
        "files": len(snap_resources.get("files") or {}),
        "state_keys": len(snap_state),
        "seq": dest._seq,
    })


@router.post("/{app_id}/sessions/{session_id}/workspace/fork", response_model=AppResponse)
async def fork_session_workspace(
    request: Request, app_id: str, session_id: str,
    body: WorkspaceForkRequest,
) -> AppResponse:
    """Fork a session's workspace into a brand new session.

    Creates a fresh session (new id, fresh chat history) and copies the
    source workspace snapshot wholesale — so the user can keep editing
    the same React app / slide deck / workspace without polluting the
    conversation history.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    _uid = getattr(request.state, "user_id", None) or "local"
    src = await manager.get_session(app_id, session_id, user_id=_uid)
    if src is None:
        raise HTTPException(status_code=404, detail="Source session not found or access denied")

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module — cannot fork")

    try:
        src_state = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if src_state is None:
            raise RuntimeError("preview_module.activate_session returned None")
        src_snap = src_state.snapshot()
    except Exception as exc:
        logger.warning("fork_source_snapshot_failed sid=%s: %s", session_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Fork failed at source: {exc}")

    import uuid as _uuid
    from digitorn.core.app.sessions import ConversationSession
    new_sid = body.target_session_id or str(_uuid.uuid4())
    src_title = getattr(src, "title", "") or ""
    new_session = ConversationSession(
        session_id=new_sid,
        app_id=app_id,
        user_id=_uid,
        workspace=getattr(src, "workspace", "") or "",
    )
    new_session.title = body.title or (src_title + " (fork)" if src_title else "")
    effective_prompt = deployed.entry_context.system_prompt or ""
    if effective_prompt:
        new_session.add_system(effective_prompt)
    await asyncio.to_thread(manager._session_store.put, new_session)

    try:
        dest = await _activate_preview_session(
            request, app_id, new_sid, preview_module, user_id=_uid,
        )
        if dest is None:
            raise RuntimeError("preview_module.activate_session returned None")
        dest.clear()
        dest.restore_from_dict({
            "state": dict(src_snap.get("state") or {}),
            "resources": {
                ch: {rid: dict(payload) for rid, payload in items.items()}
                for ch, items in (src_snap.get("resources") or {}).items()
            },
            "seq": int(src_snap.get("seq") or 0) + 1,
            "user_id": _uid,
        })
        await preview_module._flush_now(new_sid)
    except Exception as exc:
        logger.warning("fork_apply_snapshot_failed sid=%s: %s", new_sid, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Fork failed at destination: {exc}")

    return AppResponse(success=True, data={
        "source_session_id": session_id,
        "session_id": new_sid,
        "forked": True,
        "files": len((src_snap.get("resources") or {}).get("files") or {}),
        "seq": dest._seq,
    })


def _merge_resources(
    base: dict[str, dict[str, Any]],
    incoming: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Deep-merge snapshot resources — incoming wins on per-id conflicts."""
    out: dict[str, dict[str, Any]] = {
        ch: {rid: dict(payload) for rid, payload in items.items()}
        for ch, items in base.items()
    }
    for ch, items in (incoming or {}).items():
        bucket = out.setdefault(ch, {})
        for rid, payload in items.items():
            bucket[rid] = dict(payload)
    return out


# ── Split snapshot endpoints (preview / code / file content) ─────────


async def _resolve_deployed_preview(
    request: Request, app_id: str,
) -> tuple[Any, Any]:
    """Common path: validate deploy, resolve deployed + preview_module."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module")
    return deployed, preview_module


def _strip_content_from_files(resources: dict[str, Any]) -> dict[str, Any]:
    """Return resources with file `content` stripped but everything else kept.

    For the lightweight code-snapshot endpoint — Flutter's explorer + SCM
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


@router.get("/{app_id}/sessions/{session_id}/workspace/preview-snapshot",
            response_model=AppResponse)
async def get_preview_snapshot(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Lightweight snapshot for the preview pane — state + non-files channels.

    Returns: ``{state, resources: {<channel>: {...}} (without "files"), seq}``.
    Use this to render the live preview canvas without pulling file content.
    """
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"

    state = await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    if state is None:
        raise HTTPException(status_code=500, detail="preview activate failed")
    snap = state.snapshot()
    resources = {
        ch: items for ch, items in (snap.get("resources") or {}).items()
        if ch != "files"
    }
    return AppResponse(success=True, data={
        "session_id": session_id,
        "state": snap.get("state", {}),
        "resources": resources,
        "seq": snap.get("seq", 0),
    })


@router.get("/{app_id}/sessions/{session_id}/workspace/code-snapshot",
            response_model=AppResponse)
async def get_code_snapshot(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """File tree + metadata for the code editor — NO content.

    Preview module is preferred (it carries live validation / pending-diff
    metadata). For apps without preview we fall back to listing files on
    disk in the session workspace so the explorer still renders.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")
    _uid = getattr(request.state, "user_id", None) or "local"
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None

    files_meta: dict[str, Any] = {}
    seq = 0

    if preview_module is not None:
        state = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if state is not None:
            snap = state.snapshot()
            files_raw = (snap.get("resources") or {}).get("files", {}) or {}
            files_meta = _strip_content_from_files({"files": files_raw}).get("files", {})
            seq = snap.get("seq", 0)

    if not files_meta:
        import os as _os
        from pathlib import Path as _Path
        sess = await manager.get_session(app_id, session_id, user_id=_uid)
        ws = getattr(sess, "workspace", "") if sess else ""
        if ws and _os.path.isdir(ws):
            _SKIP = {
                "node_modules", ".git", "__pycache__", ".venv", "venv",
                "dist", "build", ".next", ".vite", ".cache", ".turbo",
                ".output", ".svelte-kit", "target", ".pytest_cache",
                ".mypy_cache", ".digitorn",
            }
            root = _Path(ws)
            count = 0
            for p in root.rglob("*"):
                if count >= 2000:
                    break
                try:
                    if not p.is_file():
                        continue
                    rel_parts = p.relative_to(root).parts
                except (ValueError, OSError):
                    continue
                if any(part in _SKIP for part in rel_parts):
                    continue
                rel = "/".join(rel_parts)
                try:
                    stat = p.stat()
                except OSError:
                    continue
                files_meta[rel] = {
                    "path": rel,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "source": "disk",
                }
                count += 1

    return AppResponse(success=True, data={
        "session_id": session_id,
        "files": files_meta,
        "seq": seq,
    })


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}/history",
            response_model=AppResponse)
async def file_history_endpoint(
    request: Request, app_id: str, session_id: str, file_path: str,
) -> AppResponse:
    """History of baseline revisions for a file — latest first.

    Must be declared BEFORE the ``/files/{file_path:path}`` catch-all,
    otherwise FastAPI's first-match routing consumes the ``/history``
    suffix as part of file_path and the request returns 404.
    """
    _validate_id(session_id, "session_id")
    _uid = getattr(request.state, "user_id", None) or "local"
    manager = _get_manager(request)
    sess = await manager.get_session(app_id, session_id, user_id=_uid)
    ws = getattr(sess, "workspace", "") if sess else ""
    if not ws:
        return AppResponse(success=True, data={"path": file_path, "revisions": []})
    import json as _json
    from pathlib import Path as _Path
    hist_dir = _Path(ws) / ".digitorn" / "sessions" / session_id / "baselines" / (file_path + ".history")
    idx_path = hist_dir / "_index.json"
    revisions: list[dict[str, Any]] = []
    if idx_path.is_file():
        try:
            revisions = _json.loads(idx_path.read_text(encoding="utf-8")) or []
        except Exception:
            revisions = []
    return AppResponse(success=True, data={"path": file_path, "revisions": revisions})


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
            response_model=AppResponse)
async def get_file_content(
    request: Request, app_id: str, session_id: str, file_path: str,
    include_baseline: bool = False,
) -> AppResponse:
    """Fetch the full content of a single workspace file (lazy-loaded).

    Works for apps with or without the ``preview`` module — if preview is
    loaded, we serve from its in-memory resources (current live state).
    Otherwise we fall back to reading the file directly from the session
    workspace on disk (apps that only use ``filesystem``/``workspace``
    without streaming preview events still need to serve their files).

    With ``include_baseline=true`` the response also includes the
    last-approved baseline content + a pending unified diff — used by the
    diff viewer when the user clicks "review changes".
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        raise HTTPException(status_code=404, detail="App not deployed")
    _uid = getattr(request.state, "user_id", None) or "local"
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None

    payload: dict[str, Any] | None = None
    resolved_path = file_path

    # Preview-based path (current live resources).
    if preview_module is not None:
        state = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if state is not None:
            files = (state.resources.get("files") or {})
            payload = files.get(file_path)
            if payload is None:
                # Try resolving with leading ./ or normalising.
                for k in files:
                    if k.endswith(file_path) or k.lstrip("./") == file_path:
                        payload = files[k]
                        resolved_path = k
                        break

    # Disk fallback — works for apps without preview module, OR when the
    # file was written outside the preview pipeline (filesystem module,
    # shell output, etc.).
    if payload is None:
        import os as _os
        sess = await manager.get_session(app_id, session_id, user_id=_uid)
        ws = getattr(sess, "workspace", "") if sess else ""
        if ws:
            # Guard against path escape — resolve target and verify it
            # still lives under the workspace root.
            ws_abs = _os.path.abspath(ws)
            target = _os.path.abspath(_os.path.join(ws_abs, file_path))
            if not target.startswith(ws_abs + _os.sep) and target != ws_abs:
                raise HTTPException(status_code=400, detail="path escapes workspace")
            if _os.path.isfile(target):
                try:
                    with open(target, "r", encoding="utf-8", errors="replace") as _fh:
                        content = _fh.read()
                    stat = _os.stat(target)
                    payload = {
                        "content": content,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "source": "disk",
                    }
                    resolved_path = file_path
                except (OSError, PermissionError) as exc:
                    raise HTTPException(status_code=500, detail=f"read failed: {exc}")

    if payload is None:
        raise HTTPException(status_code=404, detail=f"file not found: {file_path}")

    out: dict[str, Any] = {
        "path": resolved_path,
        "payload": dict(payload),
    }
    if include_baseline:
        try:
            sess = await manager.get_session(app_id, session_id, user_id=_uid)
            ws = getattr(sess, "workspace", "") if sess else ""
            if ws:
                from digitorn.modules.preview.fs_backend import read_baseline
                baseline = read_baseline(ws, session_id, resolved_path)
                if baseline is not None:
                    out["baseline"] = baseline
                    from digitorn.modules.workspace.module import _safe_unified_diff
                    out["unified_diff_pending"] = _safe_unified_diff(
                        baseline, payload.get("content") or "", resolved_path,
                    )
        except Exception as exc:
            logger.warning("get_file_content_baseline_failed: %s", exc)

    return AppResponse(success=True, data=out)


class FileActionRequest(BaseModel):
    """Body for file validation actions."""
    path: str = Field(..., description="Workspace-relative file path.")


@router.post("/{app_id}/sessions/{session_id}/workspace/files/approve",
             response_model=AppResponse)
async def approve_file_endpoint(
    request: Request, app_id: str, session_id: str, body: FileActionRequest,
) -> AppResponse:
    """Mark a file as approved — snapshot its current content as baseline."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import ApproveFileParams
    result = await ws_module.approve_file(ApproveFileParams(path=body.path))
    if not result.success:
        # BUG-065: returning 200 + success:false is contradictory —
        # the HTTP status said OK while the body said "this operation
        # failed". Surface the failure as an HTTP error so clients that
        # branch on status_code get the right signal.
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "approve_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/sessions/{session_id}/workspace/files/reject",
             response_model=AppResponse)
async def reject_file_endpoint(
    request: Request, app_id: str, session_id: str, body: FileActionRequest,
) -> AppResponse:
    """Reject the pending changes — revert file to baseline or delete."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import RejectFileParams
    result = await ws_module.reject_file(RejectFileParams(path=body.path))
    if not result.success:
        # BUG-065: returning 200 + success:false is contradictory.
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "reject_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


class HunksActionRequest(BaseModel):
    path: str = Field(..., description="Workspace-relative file path.")
    hunks: list = Field(
        default_factory=list,
        description="Hunk indices (int) or hashes (12-char str).",
    )


@router.post("/{app_id}/sessions/{session_id}/workspace/files/approve-hunks",
             response_model=AppResponse)
async def approve_file_hunks_endpoint(
    request: Request, app_id: str, session_id: str, body: HunksActionRequest,
) -> AppResponse:
    """Partial approve — stage only selected hunks, leave the rest pending."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import HunksActionParams
    result = await ws_module.approve_file_hunks(
        HunksActionParams(path=body.path, hunks=body.hunks),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "approve_hunks_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/sessions/{session_id}/workspace/files/reject-hunks",
             response_model=AppResponse)
async def reject_file_hunks_endpoint(
    request: Request, app_id: str, session_id: str, body: HunksActionRequest,
) -> AppResponse:
    """Partial revert — undo only selected hunks, keep the rest pending."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import HunksActionParams
    result = await ws_module.reject_file_hunks(
        HunksActionParams(path=body.path, hunks=body.hunks),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "reject_hunks_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


class WritebackRequest(BaseModel):
    content: str = Field(..., description="New file content.")
    auto_approve: bool = Field(default=False, description="Snapshot as baseline immediately.")
    source: str = Field(default="user", description="Attribution — 'user' / 'import' / 'script'.")


@router.put("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
            response_model=AppResponse)
async def writeback_file_endpoint(
    request: Request, app_id: str, session_id: str,
    file_path: str, body: WritebackRequest,
) -> AppResponse:
    """User-side write — manual edit, conflict resolution, drag-drop import."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import WritebackParams
    result = await ws_module.writeback_file(
        WritebackParams(path=file_path, content=body.content, auto_approve=body.auto_approve),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "writeback_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


class CommitRequest(BaseModel):
    message: str = Field(..., description="Commit message.")
    files: list[str] | None = Field(
        default=None, description="Explicit paths (null = all approved).",
    )
    push: bool = Field(default=False, description="git push after commit.")


@router.post("/{app_id}/sessions/{session_id}/workspace/commit",
             response_model=AppResponse)
async def commit_session_endpoint(
    request: Request, app_id: str, session_id: str, body: CommitRequest,
) -> AppResponse:
    """Commit approved files to git — one-shot ship to the session's repo."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import CommitParams
    result = await ws_module.commit_session(
        CommitParams(message=body.message, files=body.files, push=body.push),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "commit_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)




@router.post("/{app_id}/sessions/{session_id}/workspace/git-status",
             response_model=AppResponse)
async def refresh_git_status(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Trigger a git status refresh — emits `resource_patched` for every file."""
    _validate_id(session_id, "session_id")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import GitStatusParams
    result = await ws_module.git_status(GitStatusParams())
    if not result.success:
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


# ── LSP RPC (Phase 2: hover, goto, references, completion, rename) ────


class LspRpcRequest(BaseModel):
    """Body for ``POST /lsp/request``.

    ``method`` + ``params`` follow the Language Server Protocol spec
    (textDocument/hover, textDocument/definition, textDocument/references,
    textDocument/completion, textDocument/rename, textDocument/signatureHelp,
    textDocument/documentSymbol, …).

    **Phase 3 additions** — abort + debounce semantics:

    - ``request_id`` (optional client uuid) — correlation id for the
      companion ``POST /lsp/cancel`` endpoint. When omitted, the daemon
      mints one and returns it in the response.
    - ``supersede_previous`` (default ``true``) — auto-cancel any
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
            "Client correlation id — use it to cancel this specific "
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
    """Body for ``POST /lsp/cancel`` — cancel an in-flight LSP request."""
    request_id: str = Field(
        ..., description="Correlation id returned by /lsp/request.",
    )


@router.post(
    "/{app_id}/sessions/{session_id}/lsp/request",
    response_model=AppResponse,
)
async def lsp_rpc_request(
    request: Request, app_id: str, session_id: str, body: LspRpcRequest,
) -> AppResponse:
    """Forward an LSP RPC to the language server backing the given file.

    This is the sole entry point clients (Monaco, ``useLspRequest`` Flutter
    hook, custom tooling) use for hover / goto / references / completion /
    rename / signature help. The daemon doesn't reshape payloads — LSP
    spec semantics are the contract.

    Returns::

        {"success": true, "data": {"server": "pyright", "method": "...",
                                    "result": <lsp response>}}

    Error responses map cleanly to HTTP semantics:

    - 404 — app not deployed or has no LSP module
    - 400 — file extension has no registered server, or server not
             installed, or method unsupported (protocol=compiler/linter)
    - 504 — server responded with None (timeout)
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    lsp_module = deployed.modules.get("lsp") if hasattr(deployed, "modules") else None
    if lsp_module is None:
        raise HTTPException(status_code=404, detail="App has no LSP module")

    _uid = getattr(request.state, "user_id", None) or "local"
    sess = await _get_manager(request).get_session(app_id, session_id, user_id=_uid)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found")

    from digitorn.modules.lsp.params import LspRequestParams
    lsp_params = LspRequestParams(
        path=body.path,
        method=body.method,
        params=body.params,
        timeout_seconds=body.timeout_seconds,
        request_id=body.request_id,
        session_id=session_id,
        supersede_previous=body.supersede_previous,
    )

    # Fire LSP request as a monitored task so we can react to HTTP
    # client disconnects (Phase 3: real server-side abort when the
    # client fetch is aborted). We poll `request.is_disconnected()`
    # in parallel and cancel the underlying LSP task if the fetch dies.
    lsp_task = asyncio.create_task(lsp_module.request(lsp_params))

    async def _watch_disconnect() -> None:
        while not lsp_task.done():
            try:
                if await request.is_disconnected():
                    logger.debug(
                        "lsp_rpc: client disconnected, cancelling LSP task",
                    )
                    lsp_task.cancel()
                    return
            except Exception:
                return
            await asyncio.sleep(0.1)

    watch_task = asyncio.create_task(_watch_disconnect())
    try:
        result = await lsp_task
    except asyncio.CancelledError:
        return AppResponse(
            success=False, error="request cancelled",
            data={"cancelled": True, "request_id": body.request_id},
        )
    finally:
        if not watch_task.done():
            watch_task.cancel()

    if not result.success:
        err = result.error or "LSP request failed"
        # Cancellation comes back with `cancelled: true` in data.
        if (result.data or {}).get("cancelled"):
            return AppResponse(
                success=False, error=err, data=result.data,
            )
        if "timeout" in err.lower() or "no result" in err.lower():
            return AppResponse(
                success=False, error=err,
                data=result.data or {"timeout": True},
            )
        raise HTTPException(status_code=400, detail=err)
    return AppResponse(success=True, data=result.data)


@router.post(
    "/{app_id}/sessions/{session_id}/lsp/cancel",
    response_model=AppResponse,
)
async def lsp_rpc_cancel(
    request: Request, app_id: str, session_id: str, body: LspCancelRequest,
) -> AppResponse:
    """Cancel an in-flight LSP request by ``request_id``.

    Safe to call on already-completed requests (returns ``cancelled:
    false, already_done: true``). Returns ``request not found`` if the
    id never existed or was already cleaned up.

    Typical UX: Monaco fires an abort token on a completion request,
    the client catches it and hits this endpoint with the correlation
    id it sent in the original /lsp/request call.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    lsp_module = deployed.modules.get("lsp") if hasattr(deployed, "modules") else None
    if lsp_module is None:
        raise HTTPException(status_code=404, detail="App has no LSP module")

    from digitorn.modules.lsp.params import LspCancelParams
    result = await lsp_module.cancel_request(LspCancelParams(
        request_id=body.request_id,
        session_id=session_id,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error, data=result.data)
    return AppResponse(success=True, data=result.data)


@router.get("/{app_id}/triggers", response_model=AppResponse)
async def app_triggers(request: Request, app_id: str) -> AppResponse:
    """Get the status of all triggers and channels for a background app.

    Shows configured triggers, active listeners, last activation times,
    and any errors. Useful for monitoring background apps.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    compiled = deployed.compiled
    result: dict[str, Any] = {
        "app_id": app_id,
        "mode": compiled.execution.mode,
        "is_background": compiled.execution.mode == "background",
    }

    # Legacy triggers from execution.triggers
    triggers = compiled.execution.triggers or []
    result["triggers"] = [
        {
            "id": t.id,
            "type": t.type,
            "schedule": t.schedule if hasattr(t, "schedule") else "",
            "paths": t.paths if hasattr(t, "paths") else [],
            "path": t.path if hasattr(t, "path") else "",
            "method": t.method if hasattr(t, "method") else "POST",
        }
        for t in triggers
    ]

    # Channels module providers (modern trigger system)
    channels_mod = deployed.modules.get("channels")
    if channels_mod is not None:
        providers = []
        for name, provider in getattr(channels_mod, "_providers", {}).items():
            adapter = getattr(provider, "adapter", None)
            # Channel type resolution — `channel_type` is rarely set on
            # the provider wrapper; the actual kind is on the adapter's
            # class (ADAPTER_TYPE) or falls back to the provider's own
            # `type` attr. Previously this returned "?" for every
            # channel in the diagnostics response.
            # BUG-099: prefer the class-level ``CHANNEL_ID`` (which is
            # the authoritative registry key — ``file_watcher``,
            # ``webhook``, …) over the classname-squish fallback which
            # produced ``filewatcher`` instead of ``file_watcher``.
            ctype = (
                getattr(provider, "channel_type", None)
                or getattr(provider, "type", None)
                or getattr(adapter, "CHANNEL_ID", None)
                or getattr(adapter, "ADAPTER_TYPE", None)
                or getattr(type(adapter), "CHANNEL_ID", None) if adapter else None
            )
            if not ctype and adapter is not None:
                # Last-resort: classname-derived, but insert a snake
                # case boundary so CamelCase → camel_case instead of
                # squishing to ``filewatcher``.
                import re as _re_cls
                stripped = adapter.__class__.__name__.replace("Adapter", "")
                ctype = _re_cls.sub(r"(?<!^)(?=[A-Z])", "_", stripped).lower()
            if not ctype:
                ctype = "unknown"
            providers.append({
                "name": name,
                "type": ctype,
                "inbound": getattr(adapter, "SUPPORTS_INBOUND", False) if adapter else False,
                "outbound": getattr(adapter, "SUPPORTS_OUTBOUND", False) if adapter else False,
                "status": getattr(provider, "status", "unknown"),
                "events_received": getattr(provider, "events_received", 0),
                "last_event_at": getattr(provider, "last_event_at", None),
            })
        result["channels"] = providers
    else:
        result["channels"] = []

    # Scheduler jobs for this app
    try:
        from digitorn.core.app.scheduler import SchedulerService
        scheduler = getattr(manager, "_scheduler", None)
        if scheduler is not None:
            jobs = []
            for job_id, job in getattr(scheduler, "_jobs", {}).items():
                if getattr(job, "app_id", "") == app_id:
                    jobs.append({
                        "job_id": job_id,
                        "schedule_type": getattr(job, "schedule_type", "?"),
                        "schedule": getattr(job, "schedule", ""),
                        "next_run": getattr(job, "next_run_at", None),
                        "runs": getattr(job, "run_count", 0),
                        "status": getattr(job, "status", "active"),
                    })
            result["scheduled_jobs"] = jobs
        else:
            result["scheduled_jobs"] = []
    except Exception:
        result["scheduled_jobs"] = []

    # Active watchers for this app
    try:
        cb = deployed.context_builder
        if cb is not None and hasattr(cb, "_active_watchers"):
            watchers = []
            for wid, w in getattr(cb, "_active_watchers", {}).items():
                watchers.append({
                    "watcher_id": wid,
                    "status": getattr(w, "status", "active"),
                    "events": getattr(w, "event_count", 0),
                })
            result["watchers"] = watchers
        else:
            result["watchers"] = []
    except Exception:
        result["watchers"] = []

    return AppResponse(success=True, data=result)


# ── Background Sessions (multi-user multi-session) ──────────────────


class BackgroundSessionCreateRequest(BaseModel):
    name: str = ""
    params: dict[str, Any] = {}
    routing_keys: dict[str, str] = {}
    workspace: str = ""


@router.post("/{app_id}/background-sessions", response_model=AppResponse)
async def create_background_session(
    request: Request,
    app_id: str,
    body: BackgroundSessionCreateRequest,
) -> AppResponse:
    """Create a new background session for the authenticated user.

    In multi mode, each user can create multiple sessions with custom params
    (e.g. different CVs, different configs). In mono mode, this is a no-op —
    the session is auto-created on first trigger.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    compiled = deployed.compiled
    session_mode = getattr(compiled.execution, "session_mode", "mono")
    user_id = getattr(request.state, "user_id", "anonymous")

    if session_mode == "mono":
        # Mono: get or create the single session
        store = _get_bg_session_store(request)
        session = await store.get_or_create_mono(app_id, user_id)
        return AppResponse(success=True, data=session)

    # Multi: check limit
    store = _get_bg_session_store(request)
    max_per_user = getattr(compiled.execution, "max_sessions_per_user", 10)
    if max_per_user > 0:
        count = await store.count_for_user(app_id, user_id)
        if count >= max_per_user:
            return AppResponse(
                success=False,
                error=f"Max sessions per user reached ({max_per_user})",
            )

    session = await store.create(
        app_id=app_id,
        user_id=user_id,
        name=body.name,
        params=body.params,
        routing_keys=body.routing_keys,
        workspace=body.workspace,
    )
    return AppResponse(success=True, data=session)


@router.get("/{app_id}/background-sessions", response_model=AppResponse)
async def list_background_sessions(
    request: Request,
    app_id: str,
    limit: int = 50,
    offset: int = 0,
) -> AppResponse:
    """List background sessions for the authenticated user."""
    _validate_id(app_id)
    user_id = getattr(request.state, "user_id", None)
    store = _get_bg_session_store(request)

    # Admin sees all, regular user sees only their own
    perms = getattr(request.state, "permissions", [])
    filter_user = user_id if "*" not in perms else None

    sessions = await store.list_for_app(
        app_id, user_id=filter_user, limit=min(limit, 200), offset=offset,
    )
    return AppResponse(success=True, data={
        "sessions": sessions,
        "count": len(sessions),
    })


@router.get("/{app_id}/background-sessions/{bg_session_id}", response_model=AppResponse)
async def get_background_session(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Get a single background session with its params and routing keys."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    session = await store.get(bg_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Background session not found")
    return AppResponse(success=True, data=session)


@router.post("/{app_id}/background-sessions/{bg_session_id}/pause", response_model=AppResponse)
async def pause_background_session(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Pause a background session — triggers will skip it."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    ok = await store.update_status(bg_session_id, "paused")
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return AppResponse(success=True, data={"session_id": bg_session_id, "status": "paused"})


@router.post("/{app_id}/background-sessions/{bg_session_id}/resume", response_model=AppResponse)
async def resume_background_session(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Resume a paused background session."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    ok = await store.update_status(bg_session_id, "active")
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return AppResponse(success=True, data={"session_id": bg_session_id, "status": "active"})


@router.delete("/{app_id}/background-sessions/{bg_session_id}", response_model=AppResponse)
async def delete_background_session(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Delete a background session. Stops receiving triggers."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    # Wipe payload files first (best-effort) so we never leave orphan bytes
    try:
        await store.clear_payload(bg_session_id)
    except Exception:
        pass
    ok = await store.delete(bg_session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return AppResponse(success=True, data={"session_id": bg_session_id, "deleted": True})


# ── Payload schema (declarative form contract) ──────────────────────


@router.get("/{app_id}/payload-schema", response_model=AppResponse)
async def get_app_payload_schema(request: Request, app_id: str) -> AppResponse:
    """Return the declarative payload schema for a background app.

    The Flutter dashboard calls this once per app to render a typed
    form (instead of a generic key/value editor) and to know which
    fields/files are required before a session can be activated.

    Returns ``data: null`` when the app has no schema declared — the
    dashboard should fall back to the free-form editor in that case.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    schema = getattr(deployed.compiled.execution, "payload_schema", None)
    return AppResponse(success=True, data=schema)


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

    # File slots — at least ``max_count`` ≥ 1 file matching the slot's
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


# ── Background session payload ──────────────────────────────────────
#
# The payload is the user's pre-filled input (prompt text + files +
# metadata) that the daemon replays into every scheduled activation
# for a background session. See background_session_store.py for the
# storage model — these routes are a thin HTTP surface over the
# ``set_payload`` / ``get_payload`` / ``add_payload_file`` helpers.


class PayloadSetRequest(BaseModel):
    """Body for PUT /background-sessions/{sid}/payload."""

    prompt: str | None = None
    metadata: dict[str, Any] | None = None


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


@router.get("/{app_id}/background-sessions/{bg_session_id}/payload", response_model=AppResponse)
async def get_background_session_payload(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Return the full payload (prompt + metadata + files) for a session.

    Also returns a ``validation`` block describing whether the payload
    satisfies the app's declared ``payload_schema`` — the dashboard uses
    this to decide whether to enable the "Activate" button.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    store = _get_bg_session_store(request)
    _assert_session_visible(await store.get(bg_session_id), app_id, request)
    payload = await store.get_payload(bg_session_id)
    schema = getattr(deployed.compiled.execution, "payload_schema", None)
    errors = _validate_payload_against_schema(schema, payload)
    return AppResponse(success=True, data={
        **payload,
        "validation": {
            "schema_required": bool(schema and schema.get("required")),
            "valid": len(errors) == 0,
            "errors": errors,
        },
    })


@router.put("/{app_id}/background-sessions/{bg_session_id}/payload", response_model=AppResponse)
async def set_background_session_payload(
    request: Request, app_id: str, bg_session_id: str, body: PayloadSetRequest,
) -> AppResponse:
    """Create or update the prompt + metadata of a session payload."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    _assert_session_visible(await store.get(bg_session_id), app_id, request)
    payload = await store.set_payload(
        bg_session_id, prompt=body.prompt, metadata=body.metadata,
    )
    return AppResponse(success=True, data=payload)


@router.delete("/{app_id}/background-sessions/{bg_session_id}/payload", response_model=AppResponse)
async def clear_background_session_payload(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Remove the entire payload (prompt + metadata + all files) for a session."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    _assert_session_visible(await store.get(bg_session_id), app_id, request)
    await store.clear_payload(bg_session_id)
    return AppResponse(success=True, data={"cleared": True})


@router.post(
    "/{app_id}/background-sessions/{bg_session_id}/payload/files",
    response_model=AppResponse,
)
async def upload_background_session_payload_file(
    request: Request,
    app_id: str,
    bg_session_id: str,
    file: UploadFile = File(...),
) -> AppResponse:
    """Attach a file to a session payload (multipart upload).

    The file bytes are stored on disk under
    ``~/.digitorn/apps/<app_id>/sessions/<sid>/payload/`` and an entry
    is added to the payload's ``files`` list so the injection layer can
    surface its path to the agent at trigger time.
    """
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    _assert_session_visible(await store.get(bg_session_id), app_id, request)

    if not file.filename:
        raise HTTPException(status_code=400, detail="filename required")
    # Cap at 25 MiB by default — dashboard sets smaller per-file limits.
    max_bytes = 25 * 1024 * 1024
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"file exceeds {max_bytes} bytes")

    try:
        payload = await store.add_payload_file(
            bg_session_id,
            filename=file.filename,
            content=content,
            mime_type=file.content_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AppResponse(success=True, data=payload)


@router.delete(
    "/{app_id}/background-sessions/{bg_session_id}/payload/files/{filename}",
    response_model=AppResponse,
)
async def delete_background_session_payload_file(
    request: Request, app_id: str, bg_session_id: str, filename: str,
) -> AppResponse:
    """Remove a single file from a session payload."""
    _validate_id(app_id)
    store = _get_bg_session_store(request)
    _assert_session_visible(await store.get(bg_session_id), app_id, request)
    ok = await store.remove_payload_file(bg_session_id, filename)
    if not ok:
        raise HTTPException(status_code=404, detail="file not found")
    payload = await store.get_payload(bg_session_id)
    return AppResponse(success=True, data=payload)


# ── Activations (background trigger history) ────────────────────────


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


@router.get("/{app_id}/activations", response_model=AppResponse)
async def list_activations(
    request: Request,
    app_id: str,
    limit: int = 20,
    offset: int = 0,
    trigger_id: str | None = None,
    status: str | None = None,
) -> AppResponse:
    """List activation history for a background app.

    Returns a paginated list of trigger activations with timing, result,
    and token usage. Filter by trigger_id or status (running/completed/failed).
    """
    _validate_id(app_id)
    store = _get_activation_store(request)
    activations = await store.list(
        app_id, limit=min(limit, 100), offset=offset,
        trigger_id=trigger_id, status=status,
    )
    total = await store.count(app_id)
    return AppResponse(success=True, data={
        "activations": activations,
        "total": total,
        "limit": limit,
        "offset": offset,
    })


@router.get("/{app_id}/activations/stats", response_model=AppResponse)
async def activation_stats(request: Request, app_id: str) -> AppResponse:
    """Get aggregated activation statistics.

    Returns: total count, success rate, avg duration, total tokens, total cost.
    """
    _validate_id(app_id)
    store = _get_activation_store(request)
    stats = await store.stats(app_id)
    return AppResponse(success=True, data=stats)


@router.get("/{app_id}/channels/health", response_model=AppResponse)
async def channels_health(request: Request, app_id: str) -> AppResponse:
    """Return the live health of every channel registered for an app.

    Walks the ``ChannelRegistry`` of the deployed app, calls
    ``health_check()`` on each instance and returns a structured dict
    the Flutter dashboard can render as a status badge per channel.

    Response::

        {
          "success": true,
          "data": {
            "app_id": "newsletter-digest",
            "channel_count": 3,
            "channels": {
              "email": {
                "status": "ok",
                "latency_ms": 124.5,
                "last_error": null,
                "last_success_at": "2026-04-13T10:35:45Z",
                "deliveries_total": 12,
                "deliveries_failed": 0,
                "details": {"smtp_host": "smtp.sendgrid.net"}
              },
              "slack": {"status": "ok", ...},
              "failing_webhook": {
                "status": "down",
                "last_error": "HTTP 503 Service Unavailable",
                ...
              }
            }
          }
        }

    Status values: ``ok`` (happy path), ``degraded`` (working but
    flaky — retries needed), ``down`` (last attempt failed and the
    channel is considered unreachable). The dashboard maps each to a
    dot color.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # ChannelRegistry is scoped to the manager. We only want instances
    # that belong to this app — the registry indexes them by name, and
    # names are already scoped by app at creation time (see
    # AppManager._build_and_deploy channel loop).
    registry = getattr(manager, "_channel_registry", None)
    if registry is None:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "channel_count": 0,
                "channels": {},
                "note": "No channel registry on this manager.",
            },
        )

    # Resolve the set of channel names the app declares. Apps can declare
    # channels either at the top-level ``channels:`` block (older form,
    # lands in ``deployed.compiled.channels``) or inside the ``channels``
    # module config as ``modules.channels.config.providers`` (newer form
    # used by most builtins). The /triggers endpoint reads the latter —
    # we fall back to it when the top-level block is empty so the two
    # endpoints stay in agreement (BUG-051).
    app_channel_names: set[str] = set((deployed.compiled.channels or {}).keys())
    if not app_channel_names:
        channels_mod = deployed.modules.get("channels") if getattr(deployed, "modules", None) else None
        if channels_mod is not None:
            app_channel_names = set(getattr(channels_mod, "_providers", {}).keys())

    try:
        all_health = await registry.health_all()
    except Exception as exc:
        logger.warning("channels_health_all failed app=%s: %s", app_id, exc)
        return AppResponse(
            success=False,
            error=f"Failed to query channel health: {exc}",
        )

    channels: dict[str, dict[str, Any]] = {}
    for name, health in all_health.items():
        if name not in app_channel_names:
            continue
        channels[name] = {
            "status": health.status,
            "latency_ms": round(health.latency_ms, 1),
            "last_error": health.last_error,
            "last_success_at": health.last_success_at,
            "deliveries_total": health.deliveries_total,
            "deliveries_failed": health.deliveries_failed,
            "details": health.details or {},
        }

    # For channels the app declared but that aren't in the registry
    # (not yet started, or failed at startup), report them as "unknown"
    # so the dashboard shows something actionable instead of hiding them.
    for name in app_channel_names:
        if name not in channels:
            channels[name] = {
                "status": "unknown",
                "latency_ms": 0.0,
                "last_error": "Channel instance not found in registry",
                "last_success_at": None,
                "deliveries_total": 0,
                "deliveries_failed": 0,
                "details": {},
            }

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "channel_count": len(channels),
            "channels": channels,
        },
    )


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
            # ``package_registry.get`` is async — run the coroutine
            # via ``loop.run_until_complete`` in FastAPI handlers is
            # wrong; just return None and let the caller fall back.
            return None
        except Exception:
            pass
    return None


@router.get("/{app_id}/assets/{asset_path:path}")
async def get_app_asset(
    request: Request, app_id: str, asset_path: str, size: int = 0,
):
    """Serve any file from a deployed app's companion directory.

    Covers README.md, CHANGELOG.md, LICENSE, skills/*.md,
    assets/*, workspace defaults — anything the YAML references
    via a relative path. Guarded against path traversal; denies
    ``.digitorn/*`` (daemon-managed area).

    **``?size=N``** — when Pillow is installed and the asset is
    a raster image (PNG/JPG/WebP), serve a resized variant of N
    pixels on the longest side. Results are cached on disk under
    ``.digitorn/resized/`` so repeated requests don't re-encode.
    When Pillow isn't installed or the asset isn't an image,
    ``size`` is ignored and the original is served.

    Use this route over ``/api/packages/{id}/assets/...`` for
    deployed apps — it doesn't require the app to also be
    installed as a package.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    bundle_dir = _resolve_app_bundle_dir(request, app_id, manager)
    if bundle_dir is None:
        # Package registry fallback (async get)
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass
    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    if asset_path.startswith(".digitorn") or "/.digitorn/" in asset_path:
        raise HTTPException(
            status_code=403, detail="Access to daemon-managed files denied",
        )

    # BUG-079: the raw ``app.yaml`` / ``meta.json`` / ``package.toml``
    # expose system_prompts, model config, constraints, and private
    # setup_steps that include secrets at runtime. They must not be
    # readable by any authenticated user — restrict to the owner of a
    # user-scope deploy or to admins for system-scope apps. The same
    # rule applies to any other ``.yaml`` / ``.toml`` config file
    # living at the bundle root.
    _norm_asset = asset_path.replace("\\", "/").lower()
    _restricted = (
        "app.yaml", "app.yml", "meta.json", "package.toml",
        "manifest.json", "manifest.yaml",
    )
    if _norm_asset in _restricted:
        perms = getattr(request.state, "permissions", []) or []
        is_admin = "*" in perms
        caller_uid = _caller_user_id(request)
        # Walk the _deployed index to find which scope this app lives
        # under. A system-scope app's sensitive files are admin-only;
        # a user-scope app's sensitive files are owner-only.
        owner_uid: str | None = None
        scope = "system"
        for key, dep in (manager._deployed or {}).items():
            if getattr(dep, "app_id", None) != app_id:
                continue
            if key.startswith("system:"):
                scope = "system"
                owner_uid = None
                break
            if key.startswith("user:"):
                parts = key.split(":", 2)
                if len(parts) >= 2:
                    owner_uid = parts[1]
                    scope = "user"
                break
        if scope == "system" and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Access to a system-scope app's source manifest "
                    "requires admin permission."
                ),
            )
        if scope == "user" and owner_uid and caller_uid != owner_uid and not is_admin:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Access to another user's app manifest is denied."
                ),
            )

    target = (bundle_dir / asset_path).resolve()
    try:
        target.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Asset path escapes app dir",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"Asset not found: {asset_path}",
        )

    # Resize support (Pillow optional).
    if size and size > 0:
        resized = _try_resize_image(bundle_dir, target, size)
        if resized is not None:
            return FileResponse(str(resized))

    return FileResponse(str(target))


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
        # SVG, PDF, GIF, etc. — don't resize
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


@router.get("/{app_id}/files", response_model=AppResponse)
async def list_app_files(
    request: Request, app_id: str, subdir: str = "",
):
    """List files available in a deployed app's companion directory.

    Lets the Flutter client discover what assets / skills / prompts
    ship with an app without guessing filenames. ``subdir`` narrows
    the listing to a subdirectory (e.g. ``?subdir=skills`` or
    ``?subdir=assets``). Empty = root.

    Returns a shallow listing (one directory level) — call again
    with a subdir query to drill down.
    """
    from pathlib import Path
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    bundle_dir = _resolve_app_bundle_dir(request, app_id, manager)
    if bundle_dir is None:
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass
    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    if subdir.startswith(".digitorn") or "/.digitorn/" in subdir:
        raise HTTPException(
            status_code=403, detail="Access to daemon-managed files denied",
        )

    target_dir = (bundle_dir / subdir).resolve() if subdir else bundle_dir
    try:
        target_dir.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="subdir escapes app dir",
        )
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")

    entries: list[dict[str, Any]] = []
    for child in sorted(target_dir.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        name = child.name
        if name.startswith(".digitorn") or name == ".digitorn":
            continue
        rel = child.relative_to(bundle_dir).as_posix()
        entry: dict[str, Any] = {
            "name": name,
            "path": rel,
            "type": "directory" if child.is_dir() else "file",
        }
        if child.is_file():
            try:
                stat = child.stat()
                entry["size"] = stat.st_size
                # Hint the asset URL the client should use
                entry["url"] = f"/api/apps/{app_id}/assets/{rel}"
            except Exception:
                pass
        entries.append(entry)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "subdir": subdir,
            "entries": entries,
            "count": len(entries),
        },
    )


@router.get("/{app_id}/icon")
async def get_app_icon(request: Request, app_id: str):
    """Stream a deployed app's icon file.

    Reads the bundle store where the compiler persists the
    companion files next to the YAML. Returns 404 when the app
    has no icon declared or the file doesn't exist.

    This is the route Flutter should use for app cards in the Hub
    Apps tab, chat headers, and anywhere else the app needs a
    visual identity. Prefer this over ``/api/packages/{id}/icon``
    for deployed apps — they're the same file but this endpoint
    doesn't require the app to also be installed as a package.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )
    meta = deployed.compiled.meta
    icon_rel = getattr(meta, "icon", "") or ""
    if not icon_rel:
        raise HTTPException(
            status_code=404, detail="App has no icon declared",
        )

    # Icon path: the compiler resolves app.yaml next to its companion
    # files. We trust the bundle store's app dir.
    bundle_dir: Path | None = None
    try:
        bs = getattr(manager, "_bundle_store", None)
        if bs is not None:
            _d = bs.app_dir(app_id)
            if _d:
                bundle_dir = Path(_d).resolve()
    except Exception:
        bundle_dir = None

    # Fall back to the package install dir when the app was
    # installed as a package
    if bundle_dir is None or not bundle_dir.is_dir():
        pkg_registry = getattr(request.app.state, "package_registry", None)
        if pkg_registry is not None:
            try:
                pkg = await pkg_registry.get(app_id)
                if pkg and pkg.get("install_dir"):
                    bundle_dir = Path(pkg["install_dir"]).resolve()
            except Exception:
                pass

    if bundle_dir is None or not bundle_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="App bundle dir not found",
        )

    icon_path = (bundle_dir / icon_rel).resolve()
    try:
        icon_path.relative_to(bundle_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Icon path escapes app dir",
        )
    if not icon_path.is_file():
        raise HTTPException(status_code=404, detail="Icon file not found")

    return FileResponse(str(icon_path))


@router.get("/{app_id}/status", response_model=AppResponse)
async def app_status(request: Request, app_id: str) -> AppResponse:
    """Hero-stats endpoint for the Flutter background app dashboard.

    One round-trip returns everything the top of the dashboard needs:

    - ``live``          → current run state (``running`` / ``idle``) +
                          number of activations in status='running'
    - ``stats``         → all-time aggregated stats (same as
                          ``/activations/stats``) so the UI doesn't have
                          to chain two requests on page load
    - ``hourly``        → the 24-hour sparkline bucket list, one row per
                          hour, oldest first, including empty hours
    - ``trend_24h``     → total runs + failed runs in the last 24 h
                          (convenience aggregate on top of ``hourly``)
    - ``triggers_summary`` → light summary of trigger + channel state so
                          the dashboard header can show "2 triggers · 3
                          channels active" without a second call to
                          ``/triggers``

    This is what the dashboard should call at page load and whenever
    the user hits the ↻ refresh button. Everything else (activation
    list, trigger details, channel details) is lazy-loaded on demand.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    store = _get_activation_store(request)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # ── Live state (cheap: one COUNT per status group) ─────────────
    counts = await store.count_by_status(app_id)
    running_count = counts.get("running", 0)
    live_state = "running" if running_count > 0 else "idle"

    # ── All-time stats ─────────────────────────────────────────────
    stats = await store.stats(app_id)

    # ── 24h hourly buckets for the sparkline ───────────────────────
    hourly = await store.hourly_buckets(app_id, hours=24)
    trend_total = sum(b["total"] for b in hourly)
    trend_failed = sum(b["failed"] for b in hourly)
    trend_completed = sum(b["completed"] for b in hourly)

    # ── Triggers + channels summary ────────────────────────────────
    # Apps define triggers / channels in one of two styles:
    #
    #   1. Legacy ``execution.triggers: [...]`` — shown as compiled.execution.triggers
    #   2. Module-based ``modules.channels.config.providers: {...}`` — shown as
    #      live instances on the channels module itself (deployed.modules['channels']._providers)
    #
    # The dashboard needs ONE unified count regardless of which style the
    # user picked. We aggregate both sources here so the frontend sees
    # the same number for both.
    compiled = deployed.compiled
    legacy_triggers = compiled.execution.triggers or []

    # Count providers from the live channels module if it's loaded
    channels_mod = deployed.modules.get("channels")
    channel_providers: dict[str, Any] = {}
    if channels_mod is not None:
        channel_providers = getattr(channels_mod, "_providers", {}) or {}

    # Aggregate types across both styles
    all_types: set[str] = set()
    for t in legacy_triggers:
        if hasattr(t, "type") and t.type:
            all_types.add(t.type)
    for name, prov in channel_providers.items():
        adapter_name = getattr(getattr(prov, "adapter", None), "ADAPTER_ID", None) or (
            getattr(prov, "config", None) and getattr(prov.config, "adapter", None)
        )
        if adapter_name:
            all_types.add(str(adapter_name))

    triggers_summary = {
        "count": len(legacy_triggers) + len(channel_providers),
        "types": sorted(all_types),
    }

    # Channel names include both legacy compiled.channels (top-level
    # channels section) AND active channel provider instances from the
    # channels module (the new style).
    channel_names: set[str] = set((compiled.channels or {}).keys())
    channel_names.update(channel_providers.keys())

    channels_summary = {
        "count": len(channel_names),
        "names": sorted(channel_names),
    }

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "mode": compiled.execution.mode,
            "is_background": compiled.execution.mode == "background",
            "live": {
                "state": live_state,
                "running_count": running_count,
                "status_counts": counts,
            },
            "stats": stats,
            "hourly": hourly,
            "trend_24h": {
                "total": trend_total,
                "completed": trend_completed,
                "failed": trend_failed,
                "success_rate": round(
                    trend_completed / max(trend_total, 1) * 100, 1,
                ),
            },
            "triggers_summary": triggers_summary,
            "channels_summary": channels_summary,
        },
    )


@router.get("/{app_id}/activations/{activation_id}", response_model=AppResponse)
async def get_activation(request: Request, app_id: str, activation_id: str) -> AppResponse:
    """Get full details of a single activation.

    Includes message, response, tool calls, timing, tokens.
    """
    _validate_id(app_id)
    store = _get_activation_store(request)
    activation = await store.get(activation_id)
    if activation is None:
        raise HTTPException(status_code=404, detail="Activation not found")
    return AppResponse(success=True, data=activation)


@router.get(
    "/{app_id}/activations/{activation_id}/events",
    response_model=AppResponse,
)
async def get_activation_events(
    request: Request,
    app_id: str,
    activation_id: str,
    event_type: str | None = None,
) -> AppResponse:
    """Return the full timeline of events for an activation.

    Events are ordered by ``sequence`` (monotonically assigned when
    each event was recorded) so the frontend can render them without
    worrying about wall-clock ties. Every event has the shape::

        {
          "id": "abcd...",
          "sequence": 7,
          "timestamp": "2026-04-13T10:35:44.102Z",
          "event_type": "tool_call",
          "data": {
             "call_id": "call_xyz",
             "name": "filesystem.read",
             "params": {"file_path": "/data/news.json"},
             "success": true,
             "error": "",
             "result_preview": {...}
          }
        }

    Known ``event_type`` values currently emitted by the background
    runtime: ``tool_call``, ``thinking``, ``channel_sent``,
    ``artifact``, ``turn_start``, ``turn_end``.

    Pass ``?event_type=tool_call`` (or any of the above) to filter.
    """
    _validate_id(app_id)
    _validate_id(activation_id, "activation_id")
    store = _get_activation_store(request)

    # Confirm the activation exists first so we return 404 instead of []
    activation = await store.get(activation_id)
    if activation is None:
        raise HTTPException(status_code=404, detail="Activation not found")
    if activation.get("app_id") != app_id:
        raise HTTPException(
            status_code=404,
            detail="Activation does not belong to this app",
        )

    events = await store.list_events(activation_id, event_type=event_type)
    return AppResponse(
        success=True,
        data={
            "activation_id": activation_id,
            "event_count": len(events),
            "events": events,
        },
    )


@router.get(
    "/{app_id}/activations/{activation_id}/artifacts",
    response_model=AppResponse,
)
async def get_activation_artifacts(
    request: Request, app_id: str, activation_id: str,
) -> AppResponse:
    """List every artifact (file) produced by an activation.

    An artifact is a file-writing tool call (``filesystem.write``,
    ``filesystem.edit``, ``notebook.*``, ``spreadsheet.*``, ``pdf.*``,
    ``presentation.*``) that succeeded. The background runtime emits a
    dedicated ``artifact`` event for each of these at activation time,
    so this route just reads the already-persisted rows.

    Response::

        {
          "success": true,
          "data": {
            "activation_id": "...",
            "count": 2,
            "artifacts": [
              {
                "sequence": 7,
                "timestamp": "2026-04-13T10:35:44.783Z",
                "path": "/workspace/out/report.pdf",
                "action": "pdf.create",
                "size_bytes": 124567
              },
              ...
            ]
          }
        }

    The Flutter dashboard drawer uses this endpoint to populate its
    "Artifacts" section. Click on an artifact opens the viewer
    registered for that file type (CSV, PDF, Notebook, …).
    """
    _validate_id(app_id)
    _validate_id(activation_id, "activation_id")
    store = _get_activation_store(request)

    activation = await store.get(activation_id)
    if activation is None:
        raise HTTPException(status_code=404, detail="Activation not found")
    if activation.get("app_id") != app_id:
        raise HTTPException(
            status_code=404,
            detail="Activation does not belong to this app",
        )

    events = await store.list_events(activation_id, event_type="artifact")
    artifacts: list[dict[str, Any]] = []
    for e in events:
        data = e.get("data") or {}
        artifacts.append({
            "sequence": e.get("sequence"),
            "timestamp": e.get("timestamp"),
            "path": data.get("path"),
            "action": data.get("action"),
            "size_bytes": data.get("size_bytes"),
        })

    return AppResponse(
        success=True,
        data={
            "activation_id": activation_id,
            "count": len(artifacts),
            "artifacts": artifacts,
        },
    )


# Max bytes the daemon is willing to stream in a single file request.
# Tuned so the Flutter dashboard drawer can reasonably preview common
# outputs (PDFs, CSVs, small PPTX) but can't be abused to hog the
# server with multi-GB files. Raise if you ever need to serve bigger
# files — the streaming path below is already chunked, the cap is a
# deliberate product decision, not a technical limit.
_MAX_ARTIFACT_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50 MB


@router.get("/{app_id}/artifacts/{event_id}/download")
async def download_artifact(
    request: Request, app_id: str, event_id: str,
):
    """Stream an artifact file to the client.

    The ``event_id`` MUST be the id of an ``ActivationEvent`` row with
    ``event_type='artifact'`` — in other words, a file that was
    previously recorded by the background runtime when a tool wrote it
    to disk. This is enforced strictly to prevent any form of path
    injection: the client never passes a filesystem path, it passes an
    opaque id that the daemon resolves to a path it already knows and
    trusted at recording time.

    Security pipeline:

    1. Look up the event by id.
    2. Verify the event belongs to an activation of this ``app_id``
       (cross-app lookup is blocked).
    3. Verify the event_type is ``artifact``.
    4. Read the path from the event's ``data.path``.
    5. Verify the file exists, is a regular file (not a dir/symlink to
       a dir), and is within the size limit.
    6. Stream it with the right Content-Type.

    A failure at any step returns 404 with a generic message — the
    client should never be able to tell the difference between "path
    doesn't exist", "not an artifact", and "doesn't belong to this app"
    because those distinctions leak info about other users' data.
    """
    _validate_id(app_id)
    _validate_id(event_id, "event_id")
    _require_permission(request, "apps:read")

    store = _get_activation_store(request)
    event = await store.get_event(event_id, app_id=app_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if event.get("event_type") != "artifact":
        raise HTTPException(status_code=404, detail="Artifact not found")

    data = event.get("data") or {}
    raw_path = data.get("path")
    if not raw_path or not isinstance(raw_path, str):
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Resolve symlinks and check that the file exists + is a regular
    # file. We do NOT gate on a workspace allowlist here because the
    # artifact was recorded by the daemon itself during a tool call —
    # the daemon trusted that path enough to execute the write, so it
    # can trust it enough to read it back. Path injection is blocked
    # at step 1-3 (the event_id lookup), not here.
    try:
        file_path = Path(raw_path).resolve()
    except Exception:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact file no longer exists on disk: {file_path.name}",
        )

    try:
        stat = file_path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="Artifact not readable")

    if stat.st_size > _MAX_ARTIFACT_DOWNLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Artifact too large to serve: {stat.st_size} bytes "
                f"(max {_MAX_ARTIFACT_DOWNLOAD_SIZE} bytes). Open it "
                f"from the filesystem directly."
            ),
        )

    # Content type — Python's mimetypes covers the common cases
    # (pdf, csv, json, yaml, md, png, …). Fall back to
    # application/octet-stream so browsers offer a download rather than
    # trying to render a mystery binary.
    import mimetypes
    ctype, _enc = mimetypes.guess_type(str(file_path))
    if not ctype:
        ctype = "application/octet-stream"

    from fastapi.responses import FileResponse
    return FileResponse(
        path=str(file_path),
        media_type=ctype,
        filename=file_path.name,
        headers={
            "Cache-Control": "private, no-store",
            "X-Artifact-Event-Id": event_id,
            "X-Artifact-Action": str(data.get("action") or ""),
            "X-Artifact-Size": str(stat.st_size),
        },
    )


@router.head("/{app_id}/artifacts/{event_id}/download")
async def download_artifact_head(
    request: Request, app_id: str, event_id: str,
):
    """HEAD equivalent of the download endpoint.

    Returns only the response headers — Content-Type, Content-Length,
    X-Artifact-* — so the client can decide whether to proceed with
    the full GET (e.g. to avoid downloading a 49 MB file into memory
    when the user only wanted to see the size in a tooltip). Same
    security pipeline as the GET.
    """
    _validate_id(app_id)
    _validate_id(event_id, "event_id")
    _require_permission(request, "apps:read")

    store = _get_activation_store(request)
    event = await store.get_event(event_id, app_id=app_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if event.get("event_type") != "artifact":
        raise HTTPException(status_code=404, detail="Artifact not found")

    data = event.get("data") or {}
    raw_path = data.get("path")
    if not raw_path or not isinstance(raw_path, str):
        raise HTTPException(status_code=404, detail="Artifact not found")

    try:
        file_path = Path(raw_path).resolve()
    except Exception:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact gone")

    try:
        stat = file_path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="Artifact not readable")

    import mimetypes
    ctype, _enc = mimetypes.guess_type(str(file_path))
    if not ctype:
        ctype = "application/octet-stream"

    from fastapi.responses import Response
    return Response(
        status_code=200,
        headers={
            "Content-Type": ctype,
            "Content-Length": str(stat.st_size),
            "X-Artifact-Event-Id": event_id,
            "X-Artifact-Action": str(data.get("action") or ""),
            "X-Artifact-Size": str(stat.st_size),
            "X-Artifact-Filename": file_path.name,
        },
    )


@router.get("/{app_id}/errors", response_model=AppResponse)
async def app_errors(request: Request, app_id: str, limit: int = 10) -> AppResponse:
    """Get recent failed activations with error details."""
    _validate_id(app_id)
    store = _get_activation_store(request)
    errors = await store.recent_errors(app_id, limit=min(limit, 50))
    return AppResponse(success=True, data={"errors": errors, "count": len(errors)})


# ── Trigger Control (pause, resume, fire, test) ─────────────────────


@router.post("/{app_id}/triggers/{trigger_id}/fire", response_model=AppResponse)
async def fire_trigger(request: Request, app_id: str, trigger_id: str) -> AppResponse:
    """Manually fire a trigger (for testing/debugging).

    Activates the agent as if the trigger had fired naturally.
    The activation is recorded in the history.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    compiled = deployed.compiled
    triggers = compiled.execution.triggers or []
    trigger = None
    for t in triggers:
        if t.id == trigger_id:
            trigger = t
            break

    if trigger is None:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")

    # Resolve target sessions BEFORE launching the activation so the
    # HTTP response tells the caller whether anything will actually
    # happen. Previously fire_trigger returned {"fired": true} for a
    # trigger whose app has zero background_sessions — the activation
    # silently falls back to a global run, and /background-sessions
    # stays empty, making the response look like a lie.
    routing = getattr(trigger, "routing", "broadcast")
    target_sessions: list[dict[str, Any]] = []
    try:
        from digitorn.core.app.background_session_store import BackgroundSessionStore
        from digitorn.core.database import get_session_factory
        bg_store = BackgroundSessionStore(get_session_factory())
        target_sessions = await bg_store.resolve_routing(app_id, routing, "")
    except Exception as exc:
        logger.debug("fire_trigger routing_resolve_failed: %s", exc)

    active_sessions = [
        s for s in target_sessions if s.get("status") == "active"
    ]

    from digitorn.core.runtime.modes.background import _activate
    ctx = deployed.entry_context
    message = trigger.message or f"[manual fire] Trigger {trigger_id} activated."

    asyncio.create_task(_activate(
        ctx, trigger_id, message,
        max_turns=compiled.execution.max_turns,
        timeout=compiled.execution.timeout,
        on_tool_call=None,
        on_activation=None,
        trigger_type=trigger.type,
        trigger_payload={
            "manual": True,
            "_routing": routing,
        },
        app_id=app_id,
        max_concurrent=compiled.execution.max_concurrent_activations,
    ))

    dispatch = (
        "sessions" if active_sessions else "global_fallback"
    )
    return AppResponse(success=True, data={
        "fired": True,
        "trigger_id": trigger_id,
        "trigger_type": trigger.type,
        "message": message[:200],
        "dispatch": dispatch,
        "target_session_count": len(active_sessions),
        "target_session_ids": [s.get("id", "")[:40] for s in active_sessions[:10]],
        "routing": routing,
        "note": (
            "Manual fire does not create a new background_session; it "
            "activates existing ones (or falls back to a global run if "
            "none exist). Track progress via /activations/{activation_id}."
        ),
    })


@router.post("/{app_id}/triggers/{trigger_id}/test", response_model=AppResponse)
async def test_trigger(
    request: Request,
    app_id: str,
    trigger_id: str,
    body: dict[str, Any] | None = None,
) -> AppResponse:
    """Test a trigger with a custom payload (dry-run style).

    Fires the trigger synchronously and returns the agent's full response.
    Useful for debugging webhook payloads.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    compiled = deployed.compiled
    triggers = compiled.execution.triggers or []
    trigger = None
    for t in triggers:
        if t.id == trigger_id:
            trigger = t
            break

    if trigger is None:
        raise HTTPException(status_code=404, detail=f"Trigger '{trigger_id}' not found")

    # Build message from template + test payload
    message = trigger.message or f"Test trigger {trigger_id}"
    test_payload = body or {}
    if test_payload.get("body"):
        message = message.replace("{{event.body}}", str(test_payload["body"])[:10000])
    if test_payload.get("path"):
        message = message.replace("{{event.path}}", str(test_payload["path"]))

    # Run synchronously (wait for result)
    from digitorn.core.runtime.agent_loop import agent_turn
    ctx = deployed.entry_context
    messages = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": message},
    ]

    # Wrap the turn so any exception (timeout, provider error, agent
    # crash) still surfaces as a well-formed AppResponse instead of a
    # raw 500 with empty body — the frontend couldn't show any feedback
    # on failure paths before this.
    try:
        result = await agent_turn(
            ctx, messages,
            max_turns=min(compiled.execution.max_turns, 10),  # Cap for test
            timeout=min(compiled.execution.timeout, 60),      # Cap for test
        )
    except Exception as exc:
        logger.warning("test_trigger_failed app=%s trigger=%s: %s",
                       app_id, trigger_id, exc)
        return AppResponse(success=False, error=f"{type(exc).__name__}: {exc}",
                           data={"trigger_id": trigger_id,
                                 "message": message[:500]})

    return AppResponse(success=True, data={
        "trigger_id": trigger_id,
        "message": message[:500],
        "response": result.content,
        "tool_calls_count": result.tool_calls_count,
        "turns_used": result.turns_used,
        "error": result.error,
    })


@router.get("/{app_id}/diagnostics", response_model=AppResponse)
async def app_diagnostics(request: Request, app_id: str) -> AppResponse:
    """Run diagnostics checks for a deployed app."""
    import platform
    _validate_id(app_id)
    manager = _get_manager(request)

    checks: list[dict[str, Any]] = []

    # Daemon health
    checks.append({"name": "Daemon", "ok": True, "detail": "running"})

    # App deployed — use the manager's scoped `get()` (same resolver
    # the rest of the API uses) instead of a bare dict lookup on
    # `_deployed`. The dict lookup missed user-scoped deploys whose key
    # is `user:<uid>:<app_id>`, so `/api/apps` said "deployed" and
    # `/diagnostics` said "not deployed" for the same app.
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if deployed is None:
        checks.append({"name": "App", "ok": False, "detail": "not deployed"})
        return AppResponse(success=True, data={"checks": checks})

    checks.append({"name": "App", "ok": True, "detail": deployed.compiled.meta.name})

    # Model
    entry = deployed.entry_context
    model = getattr(entry.provider, "model", "?")
    checks.append({"name": "Model", "ok": bool(model and model != "?"), "detail": model})

    # Modules
    mod_names = list(deployed.modules.keys())
    checks.append({"name": "Modules", "ok": len(mod_names) > 0, "detail": f"{len(mod_names)} loaded"})

    # Tools
    total_tools = deployed.index.total_tools if deployed.index else 0
    checks.append({"name": "Tools", "ok": total_tools > 0, "detail": f"{total_tools} available"})

    # Platform
    checks.append({"name": "Platform", "ok": True, "detail": f"{platform.system()} {platform.release()}"})

    # Git Bash (Windows)
    if platform.system() == "Windows":
        try:
            import subprocess
            r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=3)
            checks.append({"name": "Git", "ok": r.returncode == 0, "detail": r.stdout.strip()})
        except Exception as e:
            checks.append({"name": "Git", "ok": False, "detail": str(e)[:50]})

    # MCP servers
    mcp = deployed.modules.get("mcp")
    if mcp and hasattr(mcp, "_connections"):
        connected = sum(1 for c in mcp._connections.values()
                        if getattr(c, "status", "") == "connected")
        total = len(mcp._connections)
        checks.append({"name": "MCP", "ok": connected == total,
                        "detail": f"{connected}/{total} connected"})

    return AppResponse(success=True, data={"checks": checks})


class BackgroundTaskRequest(BaseModel):
    tool: str
    params: dict[str, Any] = {}


class BackgroundTaskActionRequest(BaseModel):
    timeout: float = 60.0


@router.post("/{app_id}/background-tasks", response_model=AppResponse)
async def launch_background_task(
    request: Request, app_id: str, body: BackgroundTaskRequest,
) -> AppResponse:
    """Launch a tool as a background task.

    The tool runs asynchronously. Poll status via GET or subscribe
    to the session via Socket.IO for completion events.

    Returns the task_id for tracking.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=400, detail="App has no context_builder")

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(
        name=body.tool, params=body.params,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.get("/{app_id}/background-tasks", response_model=AppResponse)
async def list_background_tasks_app(request: Request, app_id: str) -> AppResponse:
    """List all background tasks (running + completed)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"tasks": []})

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(list_tasks=True))
    return AppResponse(success=True, data=result.data if result.success else {"tasks": []})


@router.get("/{app_id}/background-tasks/{task_id}", response_model=AppResponse)
async def get_background_task(request: Request, app_id: str, task_id: str) -> AppResponse:
    """Get status and result of a background task."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(task_id=task_id))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


@router.delete("/{app_id}/background-tasks/{task_id}", response_model=AppResponse)
async def cancel_background_task_app(request: Request, app_id: str, task_id: str) -> AppResponse:
    """Cancel a running background task."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(task_id=task_id, cancel=True))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/background-tasks/{task_id}/wait", response_model=AppResponse)
async def wait_background_task(
    request: Request, app_id: str, task_id: str, body: BackgroundTaskActionRequest,
) -> AppResponse:
    """Wait for a background task to complete (with timeout)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(
        task_id=task_id, wait=True, timeout=body.timeout,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


class WatcherCreateRequest(BaseModel):
    """Request body for creating a watcher."""

    tool: str
    params: dict[str, Any] = {}
    interval: float = 30.0
    label: str = ""
    notify_when: str = "on_change"
    notify_config: dict[str, Any] = {}


@router.post("/{app_id}/watchers", response_model=AppResponse)
async def create_watcher(
    request: Request, app_id: str, body: WatcherCreateRequest,
) -> AppResponse:
    """Create a persistent watcher that periodically executes a tool.

    The watcher runs in the background and pushes notifications based
    on the notify_when strategy. Returns the watcher_id for tracking.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=400, detail="App has no context_builder")

    from digitorn.modules.context_builder.params import WatchStartParams
    result = await cb.watch_start(WatchStartParams(
        name=body.tool,
        params=body.params,
        interval=body.interval,
        label=body.label,
        notify_when=body.notify_when,
        notify_config=body.notify_config,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.get("/{app_id}/watchers", response_model=AppResponse)
async def list_watchers(request: Request, app_id: str) -> AppResponse:
    """List all watchers (running + paused)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"watchers": []})

    from digitorn.modules.context_builder.params import WatchListParams
    result = await cb.watch_list(WatchListParams())
    return AppResponse(success=True, data=result.data if result.success else {"watchers": []})


@router.get("/{app_id}/watchers/{watcher_id}", response_model=AppResponse)
async def get_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Get watcher status, metrics, and recent history."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_status(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


@router.delete("/{app_id}/watchers/{watcher_id}", response_model=AppResponse)
async def stop_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Stop and remove a watcher."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_stop(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/watchers/{watcher_id}/pause", response_model=AppResponse)
async def pause_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Pause a running watcher (keeps history, skips checks)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_pause(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/watchers/{watcher_id}/resume", response_model=AppResponse)
async def resume_watcher(request: Request, app_id: str, watcher_id: str) -> AppResponse:
    """Resume a paused watcher."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import WatcherIdParams
    result = await cb.watch_resume(WatcherIdParams(watcher_id=watcher_id))
    if not result.success:
        if "not found" in (result.error or "").lower():
            raise HTTPException(status_code=404, detail=result.error)
        return AppResponse(success=False, error=result.error)
    return AppResponse(success=True, data=result.data)


class ToolExecuteRequest(BaseModel):
    params: dict[str, Any] = {}
    session_id: str | None = None


@router.get("/{app_id}/tools/search", response_model=AppResponse)
async def search_tools(
    request: Request, app_id: str,
    query: str = "", max_results: int = 10,
) -> AppResponse:
    """Semantic + keyword search over all tools available to the app.

    Returns ranked results with scores, descriptions, and parameter schemas.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"results": []})

    if not query or not query.strip():
        return AppResponse(success=True, data={"tools": [], "count": 0, "query": query})

    from digitorn.modules.context_builder.params import SearchToolsParams
    result = await cb.search_tools(SearchToolsParams(
        query=query, max_results=max_results,
    ))
    return AppResponse(success=True, data=result.data if result.success else {"results": []})


@router.get("/{app_id}/tools/categories", response_model=AppResponse)
async def list_tool_categories(request: Request, app_id: str) -> AppResponse:
    """List all tool categories (modules) with tool counts."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"categories": []})

    from digitorn.modules.context_builder.params import ListCategoriesParams
    result = await cb.list_categories(ListCategoriesParams())
    return AppResponse(success=True, data=result.data if result.success else {"categories": []})


@router.get("/{app_id}/tools/categories/{category}", response_model=AppResponse)
async def browse_tool_category(
    request: Request, app_id: str, category: str, page: int = 1,
) -> AppResponse:
    """Browse tools in a category (paginated)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"tools": []})

    from digitorn.modules.context_builder.params import BrowseCategoryParams
    result = await cb.browse_category(BrowseCategoryParams(
        category=category, page=page,
    ))
    return AppResponse(success=True, data=result.data if result.success else {"tools": []})


@router.get("/{app_id}/tools/{tool_name:path}", response_model=AppResponse)
async def get_tool_schema(request: Request, app_id: str, tool_name: str) -> AppResponse:
    """Get full schema for a tool by qualified name (e.g. filesystem.read).

    Returns parameters, description, examples, aliases, side effects.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import GetToolParams
    result = await cb.get_tool(GetToolParams(name=tool_name))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/tools/{tool_name:path}/execute", response_model=AppResponse)
async def execute_tool(
    request: Request, app_id: str, tool_name: str, body: ToolExecuteRequest,
) -> AppResponse:
    """Execute a tool directly by qualified name.

    Bypasses the agent — runs the tool and returns the raw result.
    Security policies (grant/approve/deny) still apply.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    # Activate the preview/workspace session so mutations (set_resource,
    # set_state) bind to the right session and schedule their debounced
    # persist. Without this, tool-execute calls bypass the session wiring
    # the agent loop normally sets up and writes land on a "_default_"
    # session that never gets flushed to the right row.
    sid = body.session_id
    if sid:
        _uid = _caller_user_id(request) or ""
        preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
        # ``set_active=True`` — this endpoint is about to run a mutating
        # tool; the write path reads ``preview._active_session_id`` to
        # decide which session's state to update.
        await _activate_preview_session(
            request, app_id, sid, preview_module,
            user_id=_uid, set_active=True,
        )

    from digitorn.modules.context_builder.params import ExecuteToolParams
    result = await cb.execute_tool(ExecuteToolParams(
        name=tool_name, params=body.params,
    ))
    if not result.success:
        return AppResponse(success=False, error=result.error, data=result.data)
    return AppResponse(success=True, data=result.data)


@router.get("/{app_id}/index", response_model=AppResponse)
async def get_app_index(request: Request, app_id: str) -> AppResponse:
    """Get full tool index structure for the app.

    Returns all categories, tools, aliases, and metadata.
    Useful for SDK clients to build local caches or UI.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    deployed = _get_deployed(request, app_id)
    if deployed is None or deployed.index is None:
        return AppResponse(success=True, data={"categories": [], "total_tools": 0})

    idx = deployed.index
    categories = []
    for cat_name in sorted(idx.categories.keys()):
        cat_info = idx.categories[cat_name]
        tools_in_cat = []
        for tool_entry in idx.tools.values():
            if tool_entry.module_id == cat_name:
                tools_in_cat.append({
                    "name": tool_entry.fqn,
                    "description": tool_entry.description,
                    "aliases": tool_entry.aliases,
                    "tags": tool_entry.tags,
                    "side_effects": tool_entry.side_effects,
                    "risk_level": tool_entry.risk_level,
                    "params_schema": tool_entry.params_schema,
                })
        categories.append({
            "name": cat_name,
            "description": cat_info.description if hasattr(cat_info, "description") else "",
            "tool_count": len(tools_in_cat),
            "tools": tools_in_cat,
        })

    return AppResponse(success=True, data={
        "total_tools": idx.total_tools,
        "total_categories": idx.total_categories,
        "tool_injection_mode": deployed.entry_context.tool_injection,
        "categories": categories,
    })


@router.get("/{app_id}/preview/{buffer_key:path}")
async def preview_buffer(request: Request, app_id: str, buffer_key: str):
    """Serve the app's static preview (web/dist/).

    When ``buffer_key`` is empty (e.g. ``/preview/?session_id=...``), serves
    the static dist bundle so the Flutter client can load the app's web UI
    in an iframe at ``/api/apps/{app_id}/preview/``.

    Query params:
        session_id: Forwarded to the static dist's index.html.
    """
    from fastapi.responses import Response

    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # Try serving static dist first (same as preview-server/proxy).
    # This lets Flutter load /api/apps/{app_id}/preview/?session_id=xxx
    # and also serves sub-paths like /preview/assets/index-xxx.js.
    static = await _try_serve_static_dist(request, app_id, buffer_key or "")
    if static is not None:
        return static

    raise HTTPException(status_code=404, detail="No static preview available for this app")


@router.get("/{app_id}/preview-server/status")
async def preview_server_status(request: Request, app_id: str):
    """Return the current state of an app's preview dev server.

    Response shape matches :meth:`PreviewManager.status().as_dict()` with
    the tailing log buffer. Flutter polls (or opens an SSE stream) to
    reflect the status in the admin panel.

    When the app ships a pre-built ``web/dist/`` (production mode) the
    endpoint reports ``enabled: true`` + ``state: "running"`` even if
    no Vite dev server is alive — the proxy will serve the static
    bundle directly. This makes the Flutter client display the iframe
    instead of "no preview block declared".
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    pm = getattr(deployed, "preview_manager", None)
    if pm is not None:
        return {"data": {"enabled": True, "mode": "dev_server", **pm.status().as_dict()}}

    has_static = await _has_static_dist(request, app_id)
    if has_static:
        return {
            "data": {
                "enabled": True,
                "mode": "static",
                "state": "running",
                "pid": None,
                "port": None,
                "version": None,
                "started_at": None,
                "last_exit_code": None,
                "restart_count": 0,
                "last_error": None,
                "logs_tail": [],
            }
        }
    return {"data": {"enabled": False, "state": "disabled"}}


async def _has_static_dist(request: Request, app_id: str) -> bool:
    """True when ``web/dist/index.html`` exists for this app's deploy."""
    from pathlib import Path as _Path
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        return False
    candidates: list[_Path] = []
    source_path = getattr(deployed.compiled, "source_path", None) if hasattr(deployed, "compiled") else None
    if source_path is not None:
        candidates.append(_Path(source_path).parent / "web" / "dist" / "index.html")
    pkg_registry = getattr(request.app.state, "package_registry", None)
    if pkg_registry is not None:
        try:
            row = await pkg_registry.get(app_id)
            if row is not None:
                install_dir = row.get("install_dir") if isinstance(row, dict) else getattr(row, "install_dir", None)
                if install_dir:
                    candidates.append(_Path(install_dir) / "web" / "dist" / "index.html")
        except Exception:
            pass
    return any(c.is_file() for c in candidates)


@router.get("/{app_id}/preview-server/logs")
async def preview_server_logs(request: Request, app_id: str, limit: int = 200):
    """Return the last ``limit`` log lines captured from the dev server."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    pm = getattr(deployed, "preview_manager", None)
    if pm is None:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' has no preview dev server",
        )
    return {"data": {"lines": pm.get_logs(limit=limit)}}


@router.post("/{app_id}/preview-server/restart")
async def preview_server_restart(request: Request, app_id: str):
    """Restart the dev server (stop + start). Resets the crash budget."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    pm = getattr(deployed, "preview_manager", None)
    if pm is None:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' has no preview dev server",
        )
    try:
        await pm.restart()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Restart failed: {exc}",
        )
    return {"data": pm.status().as_dict()}


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

    candidate_roots: list[_Path] = []
    source_path = getattr(deployed.compiled, "source_path", None) if hasattr(deployed, "compiled") else None
    if source_path is not None:
        candidate_roots.append(_Path(source_path).parent / "web" / "dist")

    pkg_registry = getattr(request.app.state, "package_registry", None)
    if pkg_registry is not None:
        try:
            row = await pkg_registry.get(app_id)
            if row is not None:
                install_dir = row.get("install_dir") if isinstance(row, dict) else getattr(row, "install_dir", None)
                if install_dir:
                    candidate_roots.append(_Path(install_dir) / "web" / "dist")
        except Exception as exc:
            logger.debug("static_dist: package_registry lookup failed for %s: %s", app_id, exc)

    dist_root: _Path | None = None
    for root in candidate_roots:
        if (root / "index.html").is_file():
            dist_root = root
            break
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
    return FileResponse(str(target))


async def _proxy_preview_http(
    request: Request,
    app_id: str,
    path: str,
):
    """Serve a preview asset.

    Resolution order:
      1. Pre-built ``web/dist/`` (production mode, zero Node process)
      2. Live Vite dev server via reverse-proxy (development mode)
    """
    import httpx

    static_resp = await _try_serve_static_dist(request, app_id, path)
    if static_resp is not None:
        return static_resp

    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    pm = getattr(deployed, "preview_manager", None)
    if pm is None or not pm.enabled:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' has no preview dev server")

    upstream_base = f"http://127.0.0.1:{pm.port}"
    upstream_path = f"/api/apps/{app_id}/preview-server/proxy/{path or ''}"
    query = request.url.query
    if query:
        query_no_token = "&".join(
            kv for kv in query.split("&")
            if kv and not kv.startswith("token=")
        )
        upstream_url = f"{upstream_base}{upstream_path}"
        if query_no_token:
            upstream_url = f"{upstream_url}?{query_no_token}"
    else:
        upstream_url = f"{upstream_base}{upstream_path}"

    # Strip hop-by-hop headers
    _hop_by_hop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host",
    }
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _hop_by_hop
    }
    # Hint the dev server about the original host for HMR link generation
    fwd_headers.setdefault("X-Forwarded-For", request.client.host if request.client else "")
    fwd_headers.setdefault("X-Forwarded-Proto", request.url.scheme)

    body = await request.body()

    # One-shot request: simpler than streaming, and dev-server payloads
    # (HTML, JS chunks, CSS) are small enough. HMR websockets go
    # through a separate WebSocket upgrade route.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                headers=fwd_headers,
                content=body,
            )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail="Preview dev server is not accepting connections",
        )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Preview dev server error: {exc}",
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _hop_by_hop
    }
    from starlette.responses import Response as _Resp
    return _Resp(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


@router.api_route(
    "/{app_id}/preview-server/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def preview_server_proxy(request: Request, app_id: str, path: str = ""):
    """Reverse-proxy HTTP traffic to the app's dev server.

    Flutter points an iframe at ``/api/apps/{app_id}/preview-server/proxy/``
    to render the dev UI inside the Digitorn admin panel. HMR websockets
    use the matching ``/preview-server/ws/{path:path}`` upgrade route.
    """
    _validate_id(app_id)
    return await _proxy_preview_http(request, app_id, path)


@router.websocket("/{app_id}/preview-server/ws/{path:path}")
async def preview_server_ws(websocket: Any, app_id: str, path: str = ""):
    """Upgrade + bridge a WebSocket to the app's dev server for HMR.

    Dev servers (Vite, Next.js, Remix) push hot-reload messages over
    a WebSocket. This handler accepts the client upgrade, connects a
    matching upstream WebSocket to ``ws://127.0.0.1:{port}/{path}``, and
    pumps frames bidirectionally until either side closes.
    """
    try:
        import websockets
    except ImportError:
        await websocket.close(code=1011, reason="websockets library unavailable")
        return

    _validate_id(app_id)

    # Resolve deployed app via the same helper as the HTTP routes.
    # We need an explicit request-like object to call _get_deployed; the
    # websocket scope doesn't expose request.state directly in all
    # FastAPI versions, so we read user_id + manager off the app state.
    try:
        manager = websocket.app.state.app_manager
    except AttributeError:
        await websocket.close(code=1011, reason="daemon not ready")
        return

    # Per-user scoping: the websocket upgrade carries the bearer token
    # in query params (browsers cannot set Authorization on WS upgrades
    # — the standard workaround is ``?token=``). Fall back to
    # anonymous/local in dev mode.
    user_id = "local"
    try:
        token = websocket.query_params.get("token") if hasattr(websocket, "query_params") else None
        auth = getattr(websocket.app.state, "auth_service", None)
        if token and auth is not None:
            payload = auth.verify_access_token(token)
            user_id = payload.user_id
    except Exception as exc:
        logger.debug("preview_ws_auth_skipped: %s", exc)

    deployed = manager.get(app_id, owner_user_id=user_id) if hasattr(manager, "get") else None
    if deployed is None:
        await websocket.close(code=1008, reason="app not deployed")
        return
    pm = getattr(deployed, "preview_manager", None)
    if pm is None or not pm.enabled:
        await websocket.close(code=1008, reason="no preview dev server")
        return

    upstream_url = f"ws://127.0.0.1:{pm.port}/{path}"
    if hasattr(websocket, "query_params"):
        query_pairs = [
            f"{k}={v}" for k, v in websocket.query_params.items() if k != "token"
        ]
        if query_pairs:
            upstream_url = f"{upstream_url}?{'&'.join(query_pairs)}"

    await websocket.accept()

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=list(websocket.scope.get("subprotocols", []) or []) or None,
            open_timeout=5.0,
            ping_interval=None,
        ) as upstream:
            async def _client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        if "text" in msg and msg["text"] is not None:
                            await upstream.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream.send(msg["bytes"])
                except Exception:
                    return

            async def _upstream_to_client():
                try:
                    async for frame in upstream:
                        if isinstance(frame, bytes):
                            await websocket.send_bytes(frame)
                        else:
                            await websocket.send_text(frame)
                except Exception:
                    return

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(_client_to_upstream()),
                    asyncio.create_task(_upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as exc:
        logger.debug("preview_ws_bridge_error app=%s: %s", app_id, exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════
# Widgets — declarative UI runtime served by the daemon
# ═════════════════════════════════════════════════════════════════
#
# Routes:
#   GET    /{app_id}/widgets                    full compiled tree
#   GET    /{app_id}/widgets/validate           lint mode (no deploy)
#   POST   /{app_id}/widgets/action             dispatch a user action
#   GET    /{app_id}/widgets/state              global widget state
#   POST   /{app_id}/widgets/state              persist global state
#
# Per-user scoping: every route resolves the deployed app via
# ``_get_deployed(request, app_id)`` so private user installs
# shadow system installs.


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


@router.get("/{app_id}/widgets")
async def get_widgets(request: Request, app_id: str):
    """Return the app's compiled widgets tree.

    Flutter calls this once on app open to render Z2 (chat_side) and
    Z3 (workspace_tabs). Z1 (inline) and Z4 (modals) are addressable
    by name via SSE / open_modal actions.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    widgets = getattr(deployed.compiled, "widgets", None)
    return {"data": _serialise_widgets(widgets)}


@router.get("/{app_id}/widgets/data/{binding}")
async def get_widget_data(
    request: Request,
    app_id: str,
    binding: str,
):
    """Resolve and return one named data binding from the app's widgets.

    The widget tree references data via ``items: '{{sources}}'`` etc.
    Each name maps to an entry under the zone's ``data:`` block:

    .. code-block:: yaml

        widgets:
          chat_side:
            data:
              sources:
                type: http
                url: /rag/sources
                poll: 10s

    The client calls ``GET /api/apps/{id}/widgets/data/sources`` to
    hydrate the binding. Supported source types:

    - ``http``    — HTTP request (relative to daemon, app-scoped)
    - ``tool``    — invoke a module action with args
    - ``static``  — return the value verbatim
    - ``stream``  — opens an SSE stream (delegated to the SSE route;
                    this endpoint just returns a placeholder)
    - ``local``   — client-side only, returns the default value

    Query params (any) are forwarded to HTTP source requests as the
    request query string, and to tool source as additional args.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    widgets = getattr(deployed.compiled, "widgets", None)
    if widgets is None:
        raise HTTPException(status_code=404, detail="App has no widgets block")

    # Walk all zones to find the binding under any data: map.
    data_spec: dict[str, Any] | None = None

    def _scan(d: dict | None) -> dict | None:
        if isinstance(d, dict) and binding in d:
            return d[binding]
        return None

    if widgets.chat_side and widgets.chat_side.data:
        data_spec = _scan(widgets.chat_side.data)
    if data_spec is None:
        for tab in widgets.workspace_tabs or []:
            data_spec = _scan(tab.data)
            if data_spec is not None:
                break
    if data_spec is None:
        for modal in (widgets.modals or {}).values():
            data_spec = _scan(modal.data)
            if data_spec is not None:
                break
    if data_spec is None:
        for inline in (widgets.inline or {}).values():
            data_spec = _scan(inline.data)
            if data_spec is not None:
                break

    if data_spec is None:
        raise HTTPException(
            status_code=404,
            detail=f"binding {binding!r} not found in any widgets zone",
        )

    source_type = (data_spec.get("type") or "static").lower()
    extra_query = dict(request.query_params)

    if source_type == "static":
        return {"data": {"value": data_spec.get("value")}}

    if source_type == "local":
        # Local sources are client-side; we return the declared default
        # so the client can hydrate offline if it has no cache yet.
        return {"data": {"value": data_spec.get("default")}}

    if source_type == "http":
        import httpx
        method = (data_spec.get("method") or "GET").upper()
        url = data_spec.get("url") or ""
        headers = data_spec.get("headers") or {}
        query = {**(data_spec.get("query") or {}), **extra_query}
        body_data = data_spec.get("body") if method != "GET" else None
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method, url,
                    headers=headers,
                    params=query,
                    json=body_data,
                )
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type:
                    payload = resp.json()
                else:
                    payload = {"text": resp.text}
                return {"data": {"value": payload, "status": resp.status_code}}
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"data binding {binding!r} http error: {exc}",
            )

    if source_type == "tool":
        tool = data_spec.get("tool")
        args = {**(data_spec.get("args") or {}), **extra_query}
        if not tool:
            raise HTTPException(
                status_code=400,
                detail=f"data binding {binding!r}: tool source missing 'tool' field",
            )
        try:
            session_id = request.query_params.get("session_id")
            result = await _execute_widget_tool(deployed, tool, args, session_id=session_id)
            return {"data": {"value": result}}
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"data binding {binding!r} tool error: {exc}",
            )

    if source_type == "stream":
        # Initial snapshot: try to fetch once via HTTP if a URL is
        # provided so the client has data to render before the SSE
        # stream warms up. The actual stream lives at
        # /widgets/data/{binding}/stream below.
        url = data_spec.get("url") or ""
        snapshot: Any = None
        if url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                    if "application/json" in resp.headers.get("content-type", ""):
                        snapshot = resp.json()
                    else:
                        snapshot = {"text": resp.text}
            except Exception:
                snapshot = None
        return {"data": {
            "value": snapshot,
            "stream_url": f"/api/apps/{app_id}/widgets/data/{binding}/stream",
            "reducer": data_spec.get("reducer", "replace"),
            "limit": data_spec.get("limit"),
        }}

    raise HTTPException(
        status_code=400,
        detail=f"data binding {binding!r}: unknown source type {source_type!r}",
    )


@router.post("/{app_id}/widgets/upload")
async def widgets_upload(
    request: Request,
    app_id: str,
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    binding: str | None = Form(default=None),
):
    """Generic multipart upload endpoint for ``file_upload`` widgets.

    Stores the file under
    ``~/.local/share/digitorn/uploads/{user_id}/{session_id}/{file_id}/{filename}``
    and returns a ``{file_id, url, size, content_type}`` payload the
    client can echo into the form value (so the next form submission
    references the uploaded file by id, not by content).

    Apps that need custom upload handling (validation, virus scan,
    indexing) can provide their own ``upload_to.url`` in the
    ``file_upload`` primitive — the daemon only handles the generic
    case when no custom URL is set.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    user_id = getattr(request.state, "user_id", None) or "local"
    sid = session_id or "_default_"

    import uuid as _uuid
    from platformdirs import user_data_dir
    file_id = _uuid.uuid4().hex[:16]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "upload.bin")
    base = Path(user_data_dir("digitorn")) / "uploads" / user_id / sid / file_id
    base.mkdir(parents=True, exist_ok=True)
    target = base / safe_name

    size = 0
    with target.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            out.write(chunk)

    # Promote into widget state so the agent / next form submission
    # can reference the uploaded file by id without a round-trip.
    if hasattr(deployed, "modules"):
        widget_mod = deployed.modules.get("widget")
        if widget_mod is not None:
            widget_mod.set_active_session(sid)
            sess = widget_mod._store.get_or_create(sid)
            sess.state.setdefault("uploads", {})
            sess.state["uploads"][file_id] = {
                "filename": safe_name,
                "size": size,
                "content_type": file.content_type,
                "binding": binding,
                "path": str(target),
            }

    return {"data": {
        "file_id": file_id,
        "filename": safe_name,
        "size": size,
        "content_type": file.content_type,
        "url": f"/api/apps/{app_id}/widgets/upload/{user_id}/{sid}/{file_id}/{safe_name}",
    }}


@router.get("/{app_id}/widgets/upload/{user_id}/{sid}/{file_id}/{filename}")
async def widgets_download(
    request: Request,
    app_id: str,
    user_id: str,
    sid: str,
    file_id: str,
    filename: str,
):
    """Serve a previously uploaded file back to the client.

    Per-user scoped: only the owning user (or admin) can read it.
    """
    _validate_id(app_id)
    caller = getattr(request.state, "user_id", None) or "local"
    perms = list(getattr(request.state, "permissions", []) or [])
    is_admin = "*" in perms
    if caller != user_id and not is_admin:
        raise HTTPException(status_code=403, detail="not your upload")

    from platformdirs import user_data_dir
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    base = Path(user_data_dir("digitorn")) / "uploads" / user_id / sid / file_id
    target = base / safe_filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="upload not found")

    from fastapi.responses import FileResponse
    return FileResponse(target, filename=safe_filename)


@router.get("/{app_id}/widgets/data/{binding}/stream")
async def stream_widget_data(
    request: Request,
    app_id: str,
    binding: str,
):
    """SSE bridge for ``type: stream`` data sources.

    The widget's ``data: { live_metrics: { type: stream, url: ... } }``
    block declares an upstream URL serving SSE. The daemon proxies
    each frame to the client so the dashboard updates live without
    a full re-fetch.

    Two upstream contracts are supported:

    1. **SSE upstream** — the URL serves ``text/event-stream``. The
       daemon parses ``data:`` lines and forwards them.
    2. **HTTP poll upstream** — the URL serves JSON. The daemon
       polls every ``poll`` seconds (from the data spec) and emits
       a frame per response.

    Reducer hint (``replace`` / ``append`` / ``merge``) is sent in
    the first frame so the client knows how to integrate updates.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    widgets = getattr(deployed.compiled, "widgets", None)
    if widgets is None:
        raise HTTPException(status_code=404, detail="App has no widgets block")

    # Resolve the data spec across all zones
    spec: dict[str, Any] | None = None

    def _scan(d: dict | None) -> dict | None:
        if isinstance(d, dict) and binding in d:
            return d[binding]
        return None

    if widgets.chat_side and widgets.chat_side.data:
        spec = _scan(widgets.chat_side.data)
    if spec is None:
        for tab in widgets.workspace_tabs or []:
            spec = _scan(tab.data)
            if spec is not None:
                break
    if spec is None:
        for modal in (widgets.modals or {}).values():
            spec = _scan(modal.data)
            if spec is not None:
                break

    if spec is None or (spec.get("type") or "").lower() != "stream":
        raise HTTPException(
            status_code=404,
            detail=f"binding {binding!r} is not a stream source",
        )

    upstream_url = spec.get("url") or ""
    if not upstream_url:
        raise HTTPException(
            status_code=400,
            detail=f"stream binding {binding!r} missing 'url'",
        )

    poll_raw = spec.get("poll") or "5s"
    try:
        poll_sec = float(str(poll_raw).rstrip("smsh"))
        if str(poll_raw).endswith("ms"):
            poll_sec = poll_sec / 1000.0
        elif str(poll_raw).endswith("h"):
            poll_sec *= 3600
    except ValueError:
        poll_sec = 5.0

    async def _gen():
        import httpx
        # Initial frame with reducer + limit metadata
        meta = {
            "reducer": spec.get("reducer", "replace"),
            "limit": spec.get("limit"),
        }
        yield f"event: meta\ndata: {_json.dumps(meta)}\n\n"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                # Probe the upstream — if it returns text/event-stream, bridge it.
                head = await client.get(
                    upstream_url, headers={"Accept": "text/event-stream"},
                )
                if "text/event-stream" in head.headers.get("content-type", ""):
                    # SSE bridge
                    async with client.stream("GET", upstream_url) as upstream:
                        buffer = ""
                        async for chunk in upstream.aiter_text():
                            buffer += chunk
                            while "\n\n" in buffer:
                                event, _, buffer = buffer.partition("\n\n")
                                # Forward verbatim
                                yield event + "\n\n"
                else:
                    # Poll mode: re-fetch every poll_sec
                    while True:
                        try:
                            resp = await client.get(upstream_url)
                            if "application/json" in resp.headers.get("content-type", ""):
                                payload = resp.json()
                            else:
                                payload = {"text": resp.text}
                            yield f"event: data\ndata: {_json.dumps(payload, default=str)}\n\n"
                        except Exception as exc:
                            yield f"event: error\ndata: {_json.dumps({'error': str(exc)})}\n\n"
                        await asyncio.sleep(poll_sec)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            yield f"event: error\ndata: {_json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{app_id}/widgets/validate")
async def validate_widgets(request: Request, app_id: str):
    """Lint endpoint — recompiles the widgets block and returns errors.

    Used by the builder UI for live validation. Read-only — does not
    redeploy the app.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")
    widgets = getattr(deployed.compiled, "widgets", None)
    return {"data": {
        "ok": True,
        "version": getattr(widgets, "version", 1) if widgets else None,
        "has_chat_side": getattr(widgets, "chat_side", None) is not None,
        "workspace_tab_count": len(getattr(widgets, "workspace_tabs", []) or []),
        "modal_count": len(getattr(widgets, "modals", {}) or {}),
        "inline_count": len(getattr(widgets, "inline", {}) or {}),
    }}


class WidgetActionRequest(BaseModel):
    widget_id: str
    action_id: str | None = None
    type: str  # tool | http | chat | set_state | refresh | sequence | open_modal | close | …
    payload: dict[str, Any] = {}
    form: dict[str, Any] = {}
    state: dict[str, Any] = {}
    session_id: str | None = None


@router.post("/{app_id}/widgets/action")
async def widgets_action(
    request: Request,
    app_id: str,
    body: WidgetActionRequest,
):
    """Dispatch a user widget action.

    Action handling matrix:

    - ``tool``        → run the named tool through the agent loop
    - ``http``        → execute an app-scoped HTTP call
    - ``chat``        → inject the message into the session as a user turn
    - ``set_state``   → mutate the per-session widget state map
    - ``refresh``     → re-fetch the listed data bindings
    - ``sequence``    → run multiple steps inside a single dispatch
    - ``close`` / ``open_modal`` / ``open_workspace`` → effect echo
      (the client handles the UI side; this just ACKs)

    The route returns ``{ok, effect}`` so the client can chain
    follow-up actions (toast, refresh, navigation, …).
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    action_type = body.type
    payload = body.payload or {}
    effect: dict[str, Any] | None = None

    # ── Server-side form re-validation ────────────────────────────
    # The Flutter client validates locally before letting the user
    # submit, but a malicious / buggy client can bypass that. We
    # re-run the same rules (required, regex, min, max, type_hint)
    # against the submitted body.form values and reject 400 with
    # structured field-level errors if any fail. This is the bare
    # minimum needed for production multi-tenant safety.
    if body.form:
        from digitorn.modules.widget.validate import (
            collect_form_inputs, validate_form_values,
        )
        widgets_cfg = getattr(deployed.compiled, "widgets", None)
        if widgets_cfg is not None:
            inputs = collect_form_inputs(widgets_cfg)
            ok, val_errors = validate_form_values(inputs, body.form)
            if not ok:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "form_validation_failed",
                        "fields": val_errors,
                    },
                )

    # ── Auto-promote form values to per-session widget state ──────
    # Whatever the user submits via a widget form is persisted in
    # the widget module's session state so the agent can read it
    # back on the next turn (via {{widget.state.X}} or via the
    # WIDGET section in the system prompt). This makes form fields
    # behave as first-class session variables — exactly what the
    # spec expects ("see each widget value as a variable").
    if body.form and hasattr(deployed, "modules"):
        widget_mod = deployed.modules.get("widget")
        if widget_mod is not None:
            sid = body.session_id or "_default_"
            widget_mod.set_active_session(sid)
            sess = widget_mod._store.get_or_create(sid)
            sess.state.setdefault("form", {})
            sess.state["form"].update(body.form)
            # Also record the latest form snapshot under a stable
            # key so {{widget.state.last_form}} always works.
            sess.state["last_form"] = dict(body.form)

    if action_type == "tool":
        # Route through the deployed app's agent loop. We don't run a
        # full chat turn — just execute the tool directly with the
        # provided args. This mirrors the existing /interact pattern.
        tool = payload.get("tool")
        args = dict(payload.get("args") or {})

        # ── Form auto-merge into args ─────────────────────────
        # When a button/submit inside a ``form`` widget triggers a
        # ``tool`` action, the spec lets the YAML reference form
        # values via ``args: { topic: "{{form.topic}}" }``. The
        # Flutter client substitutes ``{{form.X}}`` into the args
        # before POSTing, so they normally arrive already populated.
        #
        # However, for the common case where the YAML omits the
        # explicit args mapping (``submit: { action: { action: tool,
        # tool: create_meeting } }``), we automatically fold the
        # form fields into args here so the tool sees them. This
        # makes the simplest YAML "just work" without forcing users
        # to template every field by hand.
        if body.form:
            for k, v in body.form.items():
                args.setdefault(k, v)
        if not tool:
            raise HTTPException(
                status_code=400,
                detail="action.tool requires payload.tool",
            )
        try:
            result = await _execute_widget_tool(
                deployed, tool, args,
                session_id=body.session_id,
            )
            effect = {"action": "tool_result", "tool": tool, "result": result}

            # ── Auto-store tool results in widget state ──────
            # The next agent turn can read this via
            # {{widget.state.results.<tool>}} or just
            # {{widget.state.last_result}}. Without this, every
            # widget-triggered tool call would be invisible to
            # the conversation that follows.
            if hasattr(deployed, "modules"):
                widget_mod = deployed.modules.get("widget")
                if widget_mod is not None:
                    sid = body.session_id or "_default_"
                    sess = widget_mod._store.get_or_create(sid)
                    sess.state.setdefault("results", {})
                    sess.state["results"][tool] = result
                    sess.state["last_result"] = {
                        "tool": tool, "value": result,
                    }
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"tool {tool!r} failed: {exc}",
            )

    elif action_type == "http":
        # Scoped HTTP call relative to the daemon. The client could
        # do this directly, but routing through the daemon means the
        # call inherits the user's auth + the app's network grants.
        method = (payload.get("method") or "GET").upper()
        url = payload.get("url") or ""
        body_data = payload.get("body")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    method, url, json=body_data,
                )
                effect = {
                    "action": "http_result",
                    "status": resp.status_code,
                    "body": resp.text[:64_000],
                }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"http error: {exc}")

    elif action_type == "chat":
        # The client should normally inject locally, but if they
        # POST it to us we echo it back so they know we received it.
        effect = {
            "action": "chat",
            "template": payload.get("template", ""),
            "silent": payload.get("silent", False),
        }

    elif action_type == "set_state":
        sess_id = body.session_id or "_default_"
        widget_mod = deployed.modules.get("widget") if hasattr(deployed, "modules") else None
        if widget_mod is None:
            raise HTTPException(
                status_code=404,
                detail=f"App '{app_id}' does not load the widget module",
            )
        widget_mod.set_active_session(sess_id)
        sess = widget_mod._store.get_or_create(sess_id)
        sess.state.update(payload.get("set", {}))
        widget_mod._publish(
            sess, "widget:state", {"state": dict(sess.state)},
        )
        effect = {"action": "set_state_ok", "state": dict(sess.state)}

    elif action_type == "refresh":
        # We acknowledge — the client re-fetches the bindings on its
        # own via /widgets/data/<binding>. (Server-side caches don't
        # exist yet for v1.)
        effect = {
            "action": "refresh",
            "bindings": payload.get("bindings", []),
        }

    elif action_type == "sequence":
        steps = payload.get("steps") or []
        results: list[Any] = []
        for step in steps:
            try:
                # Recurse via the same handler by spoofing a request.
                # In practice a sequence is rare and small; we just
                # echo the steps so the client can run them.
                results.append({"action": step.get("action"), "ack": True})
            except Exception as exc:
                results.append({"error": str(exc)})
                if payload.get("stop_on_error", True):
                    break
        effect = {"action": "sequence_result", "steps": results}

    elif action_type == "open_workspace":
        # Ephemeral workspace tabs are stored server-side as mounted
        # widgets in the per-session store, so the snapshot returned
        # by /widgets and Socket.IO widget events includes them. The client
        # then renders the new tab next to its declared workspace_tabs.
        effect = {"action": "open_workspace", **payload}
        if isinstance(payload.get("ephemeral"), dict):
            sess_id = body.session_id or "_default_"
            widget_mod = deployed.modules.get("widget") if hasattr(deployed, "modules") else None
            if widget_mod is not None:
                from digitorn.modules.widget.module import RenderParams
                widget_mod.set_active_session(sess_id)
                eph = payload["ephemeral"]
                tab_id = eph.get("id") or f"tab_{int(time.time() * 1000)}"
                tree = eph.get("tree")
                # Apply the same server-side substitution we use in render
                from digitorn.modules.widget.expr import substitute_tree
                sess = widget_mod._store.get_or_create(sess_id)
                scopes = widget_mod._build_scopes(sess, ctx=eph.get("ctx") or {})
                rendered_tree = substitute_tree(tree, scopes) if tree else None
                await widget_mod.render(RenderParams(
                    zone="workspace",
                    target=tab_id,
                    widget_id=f"workspace_{tab_id}",
                    tree=rendered_tree,
                    ctx=eph.get("ctx") or {},
                ))
                effect["mounted_tab_id"] = tab_id

    elif action_type in ("close", "open_modal", "open_url",
                          "navigate", "copy", "download", "alert", "confirm"):
        # Pure client-side effects — we just ACK.
        effect = {"action": action_type, **payload}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown widget action type {action_type!r}",
        )

    return {"data": {"ok": True, "effect": effect}}


async def _execute_widget_tool(
    deployed: Any,
    tool: str,
    args: dict[str, Any],
    session_id: str | None = None,
) -> Any:
    """Resolve a tool by name and execute it through the app's modules.

    Walks the deployed app's modules until one accepts the action.
    Tool naming convention: ``module.action`` (e.g. ``filesystem.read``)
    or short PascalCase (e.g. ``Read``) — both routed via the runtime
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


class InteractRequest(BaseModel):
    """User interaction with a workspace widget."""
    module_id: str
    widget: str
    action: str
    state: dict[str, Any] = {}


@router.post("/{app_id}/interact", response_model=AppResponse)
async def interact_widget(request: Request, app_id: str, body: InteractRequest) -> AppResponse:
    """Handle a bidirectional widget interaction from the frontend.

    Routes the action to the module that owns the widget via the service bus.
    The module processes the action and can update state, which triggers
    SSE events back to the frontend for live UI updates.

    The module must implement a `widget_interact(widget, action, state)` method.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    # Find the module via context_builder's service bus
    cb = getattr(deployed.entry_context, "context_builder", None)
    if cb is None:
        raise HTTPException(status_code=500, detail="No context_builder")

    service_bus = getattr(cb, "_service_bus", None)
    if service_bus is None:
        raise HTTPException(status_code=500, detail="No service bus available")

    # Look up the target module
    module = service_bus.get_provider(body.module_id)
    if module is None:
        raise HTTPException(status_code=404, detail=f"Module '{body.module_id}' not found")

    if not hasattr(module, "widget_interact"):
        raise HTTPException(
            status_code=400,
            detail=f"Module '{body.module_id}' does not support widget interactions",
        )

    try:
        result = await module.widget_interact(
            widget=body.widget,
            action=body.action,
            state=body.state,
        )
    except Exception as exc:
        logger.error("widget_interact_failed app=%s module=%s: %s", app_id, body.module_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Widget interaction failed.")

    # Return the result for immediate use by the frontend.
    return AppResponse(success=True, data={
        "module_id": body.module_id,
        "widget": body.widget,
        "action": body.action,
        "result": result if isinstance(result, dict) else {"ok": True},
    })


@router.delete("/{app_id}", response_model=AppResponse)
async def delete_app(
    request: Request,
    app_id: str,
    undeploy_only: bool = False,
    delete_history: bool = True,
    scope: str | None = None,
) -> AppResponse:
    """Delete a scoped app install — scope-aware removal.

    **Multi-tenant scoping**:

    - Default: the caller's JWT ``user_id`` is used. If the caller has
      a user-scoped install of ``app_id`` it is deleted; otherwise the
      system install is the target. Bob's install is never touched
      when Alice deletes.
    - ``?scope=system`` (admin): force removal of the system install
      even when a user install exists.
    - ``?scope=user`` (admin w/ impersonation): target the caller's
      user install explicitly.

    **Destructiveness**:

    - ``?undeploy_only=true``: stops in memory only; data preserved.
      Prefer ``POST /disable`` for user-facing pause.
    - ``?delete_history=false``: wipes app definition + bundles + disk
      for this scope but keeps sessions / messages / activations for
      audit. Not reversible (bundle gone).
    - Default (``delete_history=true``): total removal of this scope.

    Built-in apps cannot be deleted.
    """
    _require_permission(request, "apps:undeploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    # The app might be deployed in memory (common case) OR only in the
    # DB (e.g. a failed deploy we want to clean up). For a full delete
    # we accept BOTH states — otherwise only deployed apps.
    is_in_memory = _is_deployed(request, app_id)
    if undeploy_only and not is_in_memory:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not deployed",
        )

    # Guard: built-in apps are off-limits. Return immediately, before
    # touching anything persistent.
    deployed = _get_deployed(request, app_id)
    if deployed is not None and getattr(deployed, "builtin", False):
        return AppResponse(
            success=False,
            error=f"Cannot remove built-in app '{app_id}'.",
        )

    caller_user_id = _caller_user_id(request) or None
    perms = list(getattr(request.state, "permissions", []) or [])
    is_admin = "*" in perms

    # Honor explicit ?scope=system from admins (and loopback self-calls
    # which get admin perms). Non-admins can't override scope.
    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope explicitly.",
        )

    if undeploy_only:
        async def _undeploy_bg():
            try:
                await manager.undeploy(app_id, user_id=caller_user_id)
                logger.info("undeploy_complete app=%s", app_id)
            except Exception as exc:
                logger.error("undeploy_failed app=%s: %s", app_id, exc)

        asyncio.create_task(_undeploy_bg())
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "undeployed": True,
                "deleted": False,
                "message": "App stopped. Data preserved — will reload at next daemon restart.",
            },
        )

    # Full delete: synchronous so the caller knows the outcome.
    try:
        result = await manager.delete_app(
            app_id,
            user_id=caller_user_id if scope != "system" else None,
            scope=scope,
            delete_history=delete_history,
        )
    except RuntimeError as exc:
        # Built-in apps raise here — map to a clean 403.
        return AppResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.error("delete_app_failed app=%s: %s", app_id, exc, exc_info=True)
        return AppResponse(
            success=False,
            error=f"Delete failed: {exc}",
        )

    msg_tail = " (history preserved)" if not delete_history else ""
    actually = bool(result.get("actually_deleted", True))
    if not actually:
        # Honest no-op response. The user asked to delete something they
        # don't own at this scope (e.g. a builtin system app with no
        # user-scoped override). Previously the API lied and reported
        # `deleted: true, disk_removed: true, secrets_deleted: 1` — fiction
        # flagged as BUG-048. Tell the truth instead.
        return AppResponse(
            success=False,
            data={
                "app_id": app_id,
                "scope": result.get("scope", "system"),
                "deleted": False,
                "deployed": False,
                "bundles_deleted": 0,
                "disk_removed": False,
                "secrets_deleted": 0,
                "db_removed": False,
                "message": (
                    f"Nothing to delete for '{app_id}' at scope "
                    f"'{result.get('scope', 'system')}'. The app may "
                    f"be a built-in, installed under a different scope, "
                    f"or already removed."
                ),
            },
            error="nothing_to_delete",
        )

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "scope": result.get("scope", "system"),
            "owner_user_id": result.get("owner_user_id", ""),
            "deleted": True,
            "deployed": result.get("deployed", False),
            "bundles_deleted": result.get("bundles_deleted", 0),
            "disk_removed": result.get("disk_removed", False),
            "secrets_deleted": result.get("secrets_deleted", 0),
            "db_removed": result.get("db_removed", False),
            "history_preserved": result.get("history_preserved", False),
            "message": (
                f"App '{app_id}' permanently deleted "
                f"({result.get('bundles_deleted', 0)} bundle(s), "
                f"{result.get('secrets_deleted', 0)} secret(s))" + msg_tail + "."
            ),
        },
    )


# ── Disable / Enable ───────────────────────────────────────────────


class DisableRequest(BaseModel):
    """Optional body for POST /api/apps/{id}/disable.

    A free-text ``reason`` is persisted on the DB row so other admins
    can see why the app was taken down (e.g. "security incident
    2026-04-17", "migrating to new API key", "user request via
    support ticket #1234").
    """
    reason: str | None = None


@router.post("/{app_id}/disable", response_model=AppResponse)
async def disable_app(
    request: Request,
    app_id: str,
    body: DisableRequest | None = None,
    scope: str | None = None,
) -> AppResponse:
    """Disable a scoped app install — hide it + refuse interaction.

    Scope resolution mirrors DELETE: the caller's JWT targets their
    own user install by default. Admins can pass ``?scope=system`` to
    disable the system install instead.

    Built-in apps cannot be disabled.
    """
    _require_permission(request, "apps:undeploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    perms = list(getattr(request.state, "permissions", []) or [])
    is_admin = "*" in perms
    if scope == "system" and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can target the system scope.",
        )

    caller_user_id = _caller_user_id(request) or None
    reason = (body.reason if body is not None else None) or None

    try:
        result = await manager.disable_app(
            app_id,
            user_id=caller_user_id if scope != "system" else None,
            scope=scope,
            reason=reason,
        )
    except RuntimeError as exc:
        return AppResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.error("disable_app_failed app=%s: %s", app_id, exc, exc_info=True)
        return AppResponse(success=False, error=f"Disable failed: {exc}")

    return AppResponse(success=True, data={
        **result,
        "message": f"App '{app_id}' disabled. Admin must re-enable via POST /api/apps/{app_id}/enable.",
    })


@router.post("/{app_id}/enable", response_model=AppResponse)
async def enable_app(
    request: Request,
    app_id: str,
    scope: str | None = None,
    user_id: str | None = None,
) -> AppResponse:
    """Re-enable a disabled app (ADMIN ONLY) and redeploy it.

    Scope-aware: when ``?scope=user&user_id=<uid>`` is supplied the
    admin re-enables that user's install. Otherwise the system
    install is targeted. Fails if the bundle was wiped.
    """
    _validate_id(app_id)
    perms = list(getattr(request.state, "permissions", []) or [])
    is_admin = "*" in perms
    if not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only administrators can re-enable a disabled app.",
        )

    manager = _get_manager(request)
    try:
        result = await manager.enable_app(
            app_id,
            user_id=user_id,
            scope=scope,
        )
    except RuntimeError as exc:
        return AppResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.error("enable_app_failed app=%s: %s", app_id, exc, exc_info=True)
        return AppResponse(success=False, error=f"Enable failed: {exc}")

    return AppResponse(success=True, data={
        **result,
        "message": f"App '{app_id}' re-enabled.",
    })


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


@router.get("/{app_id}/approvals", response_model=AppResponse)
async def list_approvals(request: Request, app_id: str) -> AppResponse:
    """List pending approval requests for an app."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    user_id = getattr(request.state, "user_id", None) or "local"
    deployed = _get_deployed(request, app_id)
    aq = getattr(deployed, "approval_queue", None) if deployed else None
    if aq is None:
        return AppResponse(success=True, data={"pending": []})
    return AppResponse(success=True, data={"pending": aq.list_pending_for_user(user_id)})


@router.post("/{app_id}/approve", response_model=AppResponse)
async def resolve_approval(
    request: Request, app_id: str, body: ApprovalResolveRequest,
) -> AppResponse:
    """Approve or deny a pending approval request."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not deployed")

    user_id = getattr(request.state, "user_id", None) or "local"
    deployed = _get_deployed(request, app_id)
    aq = getattr(deployed, "approval_queue", None) if deployed else None
    if aq is None:
        return AppResponse(success=False, error="No approval queue for this app")

    payload = body.resolved_payload()
    logger.info(
        "approve_request: id=%s approved=%s payload_len=%d payload_preview=%r",
        body.request_id, body.approved, len(payload), payload[:120],
    )
    resolved = aq.resolve(
        body.request_id, body.approved, message=payload, user_id=user_id,
    )
    if not resolved:
        return AppResponse(
            success=False,
            error="Request not found, already resolved, or not authorized",
        )
    return AppResponse(
        success=True,
        data={
            "request_id": body.request_id,
            "approved": body.approved,
            "payload_received": bool(payload),
        },
    )


class QuotaRequest(BaseModel):
    """Request body for setting a per-app rate limit."""

    rpm: int


@router.get("/{app_id}/quota", response_model=AppResponse)
async def get_app_quota(request: Request, app_id: str) -> AppResponse:
    """Get current rate limit usage for an app."""
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    return AppResponse(success=True, data=limiter.get_usage(app_id))


@router.put("/{app_id}/quota", response_model=AppResponse)
async def set_app_quota(
    request: Request, app_id: str, body: QuotaRequest
) -> AppResponse:
    """Set a custom rate limit for an app (requests per minute)."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    limiter.set_quota(app_id, body.rpm)
    return AppResponse(success=True, data={"app_id": app_id, "rpm": body.rpm})


@router.delete("/{app_id}/quota", response_model=AppResponse)
async def remove_app_quota(request: Request, app_id: str) -> AppResponse:
    """Remove custom rate limit (reverts to global default)."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    limiter.remove_quota(app_id)
    return AppResponse(success=True, data={"app_id": app_id, "quota": "default"})


@router.get("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def get_user_quota(
    request: Request, app_id: str, user_id: str,
) -> AppResponse:
    """Get rate limit usage for a specific user on an app."""
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    return AppResponse(success=True, data=limiter.get_usage(app_id, user_id=user_id))


@router.put("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def set_user_quota(
    request: Request, app_id: str, user_id: str, body: QuotaRequest,
) -> AppResponse:
    """Set a custom rate limit for a user on an app."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    limiter.set_user_quota(app_id, user_id, body.rpm)
    return AppResponse(
        success=True,
        data={"app_id": app_id, "user_id": user_id, "rpm": body.rpm},
    )


@router.delete("/{app_id}/quota/user/{user_id}", response_model=AppResponse)
async def remove_user_quota(
    request: Request, app_id: str, user_id: str,
) -> AppResponse:
    """Remove custom quota for a user (reverts to app default)."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    limiter = _get_rate_limiter(request)
    limiter.remove_user_quota(app_id, user_id)
    return AppResponse(
        success=True,
        data={"app_id": app_id, "user_id": user_id, "quota": "default"},
    )


class SecretSetRequest(BaseModel):
    """Request body for setting a secret."""

    value: str


@router.post("/{app_id}/reload", response_model=AppResponse)
async def reload_app(request: Request, app_id: str) -> AppResponse:
    """Hot-reload a deployed app from its current bundle.

    Use this after updating a secret (e.g. rotating an API key) or when
    you want to force the in-memory instance to pick up persistent
    changes without restarting the whole daemon.

    What it does:
      - Re-reads the app's frozen bundle from disk
      - Re-reads the current secrets from the secret store
      - Stops the running in-memory instance (drops active sessions)
      - Rebuilds the app with the fresh secrets and puts it back in
        the pool of deployed apps

    What it does NOT do:
      - Does not modify any DB row (Application, AppBundle, AppProfile)
      - Does not change the bundle on disk
      - Does not bump the version

    Permission: ``apps:deploy`` (same as deploy, because reload is
    functionally a redeploy of the same content).
    """
    _require_permission(request, "apps:deploy")
    _validate_id(app_id)
    manager = _get_manager(request)

    # Guard: built-in apps are rebuilt by _deploy_builtin_apps at boot.
    deployed = _get_deployed(request, app_id)
    if deployed is not None and getattr(deployed, "builtin", False):
        return AppResponse(
            success=False,
            error=(
                f"Cannot hot-reload built-in app '{app_id}'. "
                f"Restart the daemon to pick up changes."
            ),
        )

    try:
        result = await manager.reload_app(app_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")
    except FileNotFoundError as exc:
        return AppResponse(
            success=False,
            error=str(exc),
        )
    except RuntimeError as exc:
        return AppResponse(success=False, error=str(exc))
    except Exception as exc:
        logger.error("reload_app_failed app=%s: %s", app_id, exc, exc_info=True)
        return AppResponse(
            success=False,
            error=f"Reload failed: {exc}",
        )

    return AppResponse(
        success=True,
        data={
            **result,
            "message": (
                f"App '{app_id}' reloaded with "
                f"{result.get('secrets_applied', 0)} secret(s) applied."
            ),
        },
    )


@router.get("/{app_id}/secrets", response_model=AppResponse)
async def list_secrets(request: Request, app_id: str) -> AppResponse:
    """List secret key names for an app (values are never returned)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    keys = await manager.list_secrets(app_id)
    return AppResponse(success=True, data={"app_id": app_id, "keys": keys})


# Pattern matching every `{{env.KEY_NAME}}` or `{{secret.KEY_NAME}}`
# reference in the raw YAML. We don't go through the compiler's
# resolver because the resolver replaces references with values — we
# need the NAMES, not the values.
_SECRET_REF_RE = re.compile(
    r"\{\{\s*(env|secret)\.([A-Za-z_][A-Za-z0-9_]*)\s*(?:\?\?[^}]*)?\}\}",
)


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

    The inference is best-effort and conservative — when the walker
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

            # ── agents[i] — inline brains ────────────────────────
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


@router.get("/{app_id}/required-secrets", response_model=AppResponse)
async def required_secrets(request: Request, app_id: str) -> AppResponse:
    """List the secrets the app's YAML REQUIRES and their current status.

    Returns everything the UI needs to render a "manage credentials"
    screen for an app with multiple providers:

    - ``key``       : the secret name (e.g. ``ANTHROPIC_API_KEY``)
    - ``used_by``   : list of dotted YAML paths where the secret is
                      referenced — so the UI can group by agent/module
                      or at least show "this key is used in 2 places"
    - ``is_set``    : ``true`` if the secret is already defined in
                      ``SecretStore`` or matches an env var on the daemon
    - ``reference_type``: ``"env"`` or ``"secret"`` depending on the
                      template style (``{{env.X}}`` vs ``{{secret.X}}``)

    The response also includes:

    - ``missing_count`` : how many required secrets have no value yet
    - ``unused_keys``   : secrets stored in SecretStore that are NOT
                         referenced by the current YAML (orphans from
                         an old version of the app — the UI can offer
                         to clean them up)

    This route reads from the app's **current bundle** on disk — so the
    list reflects the deployed version, not the live memory state (they
    should match, but if someone fiddled with secrets manually the
    ``is_set`` column will reveal the drift).
    """
    _validate_id(app_id)
    _require_permission(request, "apps:read")
    manager = _get_manager(request)

    # Resolve the raw YAML the app was deployed with. Prefer the bundle
    # (authoritative) and fall back to Application.yaml_content for
    # legacy rows that predate the bundle refactor.
    raw_yaml: str | None = None
    try:
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload as _selectinload

        from digitorn.core.database import get_session_factory
        from digitorn.core.models import Application

        _sf = get_session_factory()
        async with _sf() as session:
            result = await session.execute(
                _select(Application)
                .options(_selectinload(Application.current_bundle))
                .where(Application.app_id == app_id)
            )
            app_row = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("required_secrets db error app=%s: %s", app_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Database error: {exc}",
        )

    if app_row is None:
        raise HTTPException(status_code=404, detail=f"App '{app_id}' not found")

    if app_row.current_bundle is not None:
        descriptor = manager._bundle_store.get_by_path(
            app_id, app_row.current_bundle.bundle_path,
        )
        if descriptor is not None:
            try:
                raw_yaml = manager._bundle_store.load_yaml(descriptor)
            except Exception as exc:
                logger.warning(
                    "required_secrets: bundle YAML unreadable for %s: %s",
                    app_id, exc,
                )

    if raw_yaml is None:
        raw_yaml = app_row.yaml_content

    if not raw_yaml:
        raise HTTPException(
            status_code=404,
            detail=(
                f"App '{app_id}' has no readable YAML (no bundle and no "
                f"yaml_content). Re-deploy it to enable secret introspection."
            ),
        )

    # Parse YAML and walk it for secret references. Even if the YAML is
    # malformed we still want to run the regex over the raw text as a
    # fallback — the user NEEDS to know what keys the app expects.
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(raw_yaml)
    except Exception:
        parsed = None

    hits: dict[str, dict[str, Any]] = {}
    ref_type: dict[str, str] = {}

    if isinstance(parsed, dict):
        _walk_yaml_for_secrets(parsed, [], hits)
        # Annotate each hit with the reference type (env vs secret) by
        # re-scanning the raw text.
        for key in hits:
            # Default to "env" since it's the common case; upgrade to
            # "secret" if we find a {{secret.KEY}} reference in the text.
            ref_type[key] = "env"
        for m in _SECRET_REF_RE.finditer(raw_yaml):
            ref_type[m.group(2)] = m.group(1)
    else:
        # Fallback: regex over raw text only (no used_by paths / no
        # provider or agent inference).
        for m in _SECRET_REF_RE.finditer(raw_yaml):
            key = m.group(2)
            hits.setdefault(
                key, {"locations": [], "providers": set(), "agents": set()},
            )
            ref_type[key] = m.group(1)

    # Cross-reference with what's actually in the store.
    stored_keys = set(await manager.list_secrets(app_id))

    # Also consult the (new) credentials DB for each inferred provider:
    # a secret is considered "set" if the calling user has a granted
    # credential for the same provider this app references. Without
    # this check the pre-session gate would keep reporting a secret
    # as missing even after the user created and granted it, because
    # ``list_secrets`` only sees the legacy per-app ``SecretStore``.
    _uid = getattr(request.state, "user_id", None) or "local"
    _cred_store = getattr(request.app.state, "credential_store", None)

    async def _credential_is_set(provider_name: str | None) -> bool:
        if not provider_name or _cred_store is None:
            return False
        try:
            row = await _cred_store.resolve_for_app(
                provider_name=provider_name,
                user_id=_uid,
                app_id=app_id,
                decrypt=False,
            )
        except Exception as exc:
            logger.warning(
                "required_secrets: resolve_for_app failed provider=%s: %s",
                provider_name, exc,
            )
            return False
        return row is not None

    required: list[dict[str, Any]] = []
    missing_count = 0
    for key in sorted(hits.keys()):
        is_set_in_store = key in stored_keys
        is_set_in_env = key in os.environ
        entry = hits[key]
        providers_list = sorted(entry.get("providers", set()))
        agents_list = sorted(entry.get("agents", set()))
        # Primary provider: the single inferred name when unambiguous,
        # None when the key is used in zero or multiple providers.
        primary_provider = providers_list[0] if len(providers_list) == 1 else None
        primary_agent = agents_list[0] if len(agents_list) == 1 else None
        is_set_in_creds = await _credential_is_set(primary_provider)
        is_set = is_set_in_store or is_set_in_env or is_set_in_creds
        if not is_set:
            missing_count += 1
        required.append({
            "key": key,
            "reference_type": ref_type.get(key, "env"),
            "used_by": sorted(set(entry.get("locations", []))),
            "is_set": is_set,
            "source": (
                "credentials" if is_set_in_creds
                else ("secret_store" if is_set_in_store
                      else ("daemon_env" if is_set_in_env else None))
            ),
            # Canonical provider name the credential should be stored
            # under — matches what ``session_resolver`` will look up
            # at turn time. Never the internal ``{agent}_brain`` id.
            "provider": primary_provider,
            "providers": providers_list,
            # Owning agent id(s), for UX grouping — the same secret
            # can legitimately be used by several agents sharing one
            # provider.
            "agent_id": primary_agent,
            "agent_ids": agents_list,
        })

    # Secrets stored but NOT referenced — orphans from an older version.
    referenced = set(hits.keys())
    unused = sorted(k for k in stored_keys if k not in referenced)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "required": required,
            "missing_count": missing_count,
            "total_required": len(required),
            "unused_keys": unused,
        },
    )


@router.get("/{app_id}/secrets/{key}", response_model=AppResponse)
async def check_secret(request: Request, app_id: str, key: str) -> AppResponse:
    """Check if a secret exists (value is never returned)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    value = await manager.get_secret(app_id, key)
    return AppResponse(
        success=True,
        data={"app_id": app_id, "key": key, "exists": value is not None},
    )


@router.put("/{app_id}/secrets/{key}", response_model=AppResponse)
async def set_secret(
    request: Request,
    app_id: str,
    key: str,
    body: SecretSetRequest,
    reload: bool = True,
) -> AppResponse:
    """Set (or update) an encrypted secret for an app.

    By default the running app is **hot-reloaded** immediately so the
    new value takes effect without a daemon restart — the typical use
    case is rotating an API key and wanting the next request to use it.
    Pass ``?reload=false`` when you want to stage multiple secret
    updates and trigger a single reload at the end via
    ``POST /api/apps/{app_id}/reload`` or the bulk ``PUT /secrets``.
    """
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)
    try:
        await manager.set_secret(app_id, key, body.value)
    except Exception as exc:
        return AppResponse(success=False, error=f"Failed to set secret: {exc}")

    reloaded = False
    reload_error: str | None = None
    if reload and _is_deployed(request, app_id):
        try:
            await manager.reload_app(app_id)
            reloaded = True
        except Exception as exc:
            logger.warning(
                "secret_set_reload_failed app=%s key=%s: %s",
                app_id, key, exc,
            )
            reload_error = str(exc)

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "key": key,
            "status": "set",
            "reloaded": reloaded,
            "reload_error": reload_error,
        },
    )


class SecretsBulkSetRequest(BaseModel):
    """Body for PUT /{app_id}/secrets — set multiple secrets at once."""

    secrets: dict[str, str] = Field(
        ...,
        description=(
            "Map of secret name → value. Empty values are rejected. "
            "Existing secrets with the same name are overwritten."
        ),
    )


@router.put("/{app_id}/secrets", response_model=AppResponse)
async def set_secrets_bulk(
    request: Request,
    app_id: str,
    body: SecretsBulkSetRequest,
    reload: bool = True,
) -> AppResponse:
    """Set many secrets in one request, then optionally hot-reload.

    This is the convenience endpoint to use when rotating several keys
    at the same time. It writes each secret through ``SecretStore``
    (same code path as the per-key PUT) and then, if ``reload=true``
    (the default), triggers a single ``reload_app`` so every new value
    takes effect together. Without bulk, rotating N keys would cost N
    reloads which is wasteful and can momentarily break the app between
    two updates.
    """
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)

    if not body.secrets:
        return AppResponse(
            success=False,
            error="'secrets' map is empty — nothing to set.",
        )

    failed: dict[str, str] = {}
    set_count = 0
    for key, value in body.secrets.items():
        if not isinstance(key, str) or not key:
            failed[str(key)] = "invalid key"
            continue
        if not isinstance(value, str):
            failed[key] = "value must be a string"
            continue
        try:
            await manager.set_secret(app_id, key, value)
            set_count += 1
        except Exception as exc:
            failed[key] = str(exc)

    reloaded = False
    reload_error: str | None = None
    if reload and set_count > 0 and _is_deployed(request, app_id):
        try:
            await manager.reload_app(app_id)
            reloaded = True
        except Exception as exc:
            logger.warning(
                "bulk_secret_reload_failed app=%s: %s", app_id, exc,
            )
            reload_error = str(exc)

    return AppResponse(
        success=len(failed) == 0,
        data={
            "app_id": app_id,
            "set_count": set_count,
            "failed": failed,
            "reloaded": reloaded,
            "reload_error": reload_error,
        },
        error=(
            f"{len(failed)} secret(s) failed to set: {list(failed.keys())}"
            if failed else None
        ),
    )


@router.delete("/{app_id}/secrets/{key}", response_model=AppResponse)
async def delete_secret(request: Request, app_id: str, key: str) -> AppResponse:
    """Delete a secret."""
    _require_permission(request, "apps:write")
    _validate_id(app_id)
    manager = _get_manager(request)
    deleted = await manager.delete_secret(app_id, key)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")
    return AppResponse(
        success=True, data={"app_id": app_id, "key": key, "status": "deleted"}
    )


class OAuthCallbackParams(BaseModel):
    """Query params from OAuth callback."""

    code: str
    state: str


@router.get("/{app_id}/oauth/authorize", response_model=AppResponse)
async def oauth_authorize(
    request: Request,
    app_id: str,
    server_id: str,
    session_id: str,
) -> AppResponse:
    """Start an OAuth2 authorization flow for an MCP server.

    Returns the authorization URL the user should open in their browser.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    entry = mcp_module._pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not connected: {server_id}")
    if entry.auth_config is None:
        raise HTTPException(400, f"MCP server '{server_id}' has no OAuth config")

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is None:
        raise HTTPException(500, "UserStore not available")

    user = await user_store.resolve_user_for_session(session_id)
    if user is None:
        raise HTTPException(404, f"No user found for session: {session_id}")

    auth_url, state_key = mcp_module._oauth.build_authorize_url(
        entry.auth_config, server_id, user.id,
    )

    return AppResponse(
        success=True,
        data={
            "auth_url": auth_url,
            "state": state_key,
            "provider": entry.auth_config.provider,
            "server_id": server_id,
        },
    )


@router.get("/{app_id}/oauth/callback")
async def oauth_callback(
    request: Request,
    app_id: str,
    code: str,
    state: str,
) -> AppResponse:
    """OAuth2 callback endpoint — exchanges authorization code for tokens.

    This is the redirect_uri that the OAuth provider redirects to after
    the user authorizes. Exchanges the code for tokens and stores them.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    oauth_state = mcp_module._oauth.get_pending_state(state)
    if oauth_state is None:
        raise HTTPException(400, "Invalid or expired OAuth state")

    entry = mcp_module._pool.get_server(oauth_state.server_id)
    if entry is None or entry.auth_config is None:
        raise HTTPException(400, "MCP server or auth config not found")

    try:
        token_data = await mcp_module._oauth.exchange_code(
            entry.auth_config, oauth_state, code,
        )
    except Exception as exc:
        logger.error("oauth_exchange_failed: %s", exc)
        raise HTTPException(400, f"Token exchange failed: {exc}")

    from digitorn.modules.mcp.oauth import parse_token_response

    access_token, refresh_token, expires_at, scope = parse_token_response(token_data)

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is None:
        raise HTTPException(500, "UserStore not available")

    await user_store.store_token(
        oauth_state.user_id,
        entry.auth_config.provider,
        access_token,
        refresh_token,
        scope=scope,
        expires_at=expires_at,
    )

    await mcp_module._inject_oauth_token(
        oauth_state.server_id, entry, entry.auth_config, access_token,
        token_type=token_data.get("token_type"),
    )

    if deployed.context_builder is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = deployed.context_builder.build_and_set_index(
            deployed.modules, security_profile,
        )
        _refresh_deployed_agent_tools(deployed, new_index)

    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        from starlette.responses import HTMLResponse

        return HTMLResponse(
            "<html><body style='font-family:system-ui;text-align:center;padding:60px'>"
            f"<h1>&#10004; {entry.auth_config.provider.title()} autorisé !</h1>"
            "<p>Tu peux fermer cet onglet.</p></body></html>"
        )

    return AppResponse(
        success=True,
        data={
            "message": f"Authorization successful for {entry.auth_config.provider}",
            "provider": entry.auth_config.provider,
            "server_id": oauth_state.server_id,
            "user_id": oauth_state.user_id,
        },
    )


@router.get("/{app_id}/mcp/pending-oauth", response_model=AppResponse)
async def list_pending_oauth(request: Request, app_id: str) -> AppResponse:
    """List MCP servers that need OAuth authorization (no valid token yet)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        return AppResponse(success=True, data={"pending": []})

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        return AppResponse(success=True, data={"pending": []})

    pending = []
    for server_id, entry in pool._servers.items():
        if entry.auth_config is None:
            continue
        has_token = False
        if entry.transport_type == "stdio" and entry.auth_config.env_token_var:
            current_env = getattr(entry, "_connect_kwargs", {}).get("env", {})
            has_token = bool(current_env.get(entry.auth_config.env_token_var))
        elif entry.status == "connected" and entry.tools:
            has_token = True

        if not has_token:
            pending.append({
                "server_id": server_id,
                "provider": entry.auth_config.provider,
                "client_id": entry.auth_config.client_id,
                "client_secret": entry.auth_config.client_secret,
                "scopes": entry.auth_config.scopes,
                "redirect_uri": entry.auth_config.redirect_uri,
                "env_token_var": entry.auth_config.env_token_var,
            })

    return AppResponse(success=True, data={"pending": pending})


class InjectOAuthTokenRequest(BaseModel):
    """Request body for injecting an OAuth token into an MCP server."""
    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None
    expires_in: int | None = None
    scope: str | None = None


@router.post("/{app_id}/mcp/{server_id}/oauth-token", response_model=AppResponse)
async def inject_oauth_token(
    request: Request, app_id: str, server_id: str, body: InjectOAuthTokenRequest,
) -> AppResponse:
    """Inject an OAuth token into an MCP server and persist it in DB.

    Used by the CLI after completing a local OAuth flow.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(400, "App has no MCP module")

    entry = mcp_module._pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")
    if entry.auth_config is None:
        raise HTTPException(400, f"MCP server '{server_id}' has no OAuth config")

    await mcp_module._inject_oauth_token(
        server_id, entry, entry.auth_config, body.access_token,
        token_type=body.token_type,
    )

    cb = deployed.context_builder
    if cb is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = cb.build_and_set_index(deployed.modules, security_profile)
        _refresh_deployed_agent_tools(deployed, new_index)
        logger.info(
            "tool_index_rebuilt after oauth_inject app=%s tools=%d",
            app_id, new_index.total_tools,
        )

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is not None:
        from datetime import datetime, timedelta, timezone

        user, _ = await user_store.get_or_create_user(
            app_id, "local", "cli-user", display_name="CLI User",
        )
        expires_at = None
        if body.expires_in:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.expires_in)
        await user_store.store_token(
            user.id, entry.auth_config.provider, body.access_token,
            refresh_token=body.refresh_token,
            scope=body.scope or "",
            expires_at=expires_at,
        )

    return AppResponse(
        success=True,
        data={
            "server_id": server_id,
            "provider": entry.auth_config.provider,
            "status": "injected",
        },
    )


@router.delete("/{app_id}/mcp/{server_id}/oauth-token", response_model=AppResponse)
async def revoke_mcp_oauth(request: Request, app_id: str, server_id: str) -> AppResponse:
    """Revoke an MCP server's OAuth token — disconnect and delete from DB.

    The server entry is kept in the pool (with status reset) so /connect
    can re-authorize later.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(404, f"App not deployed: {app_id}")

    mcp_module = deployed.modules.get("mcp")
    if mcp_module is None:
        raise HTTPException(404, "No MCP module in this app")

    pool = getattr(mcp_module, "_pool", None)
    if pool is None:
        raise HTTPException(404, "No MCP connection pool")

    entry = pool.get_server(server_id)
    if entry is None:
        raise HTTPException(404, f"MCP server not found: {server_id}")

    if entry.auth_config is None:
        raise HTTPException(400, f"Server '{server_id}' has no OAuth config")

    provider = entry.auth_config.provider

    user_store = getattr(mcp_module, "_user_store", None)
    if user_store is not None:
        try:
            from digitorn.core.database import get_session
            from digitorn.core.models import UserOAuthToken
            from sqlalchemy import select, delete as sa_delete

            async for session in get_session():
                await session.execute(
                    sa_delete(UserOAuthToken).where(
                        UserOAuthToken.provider == provider,
                    )
                )
                await session.commit()
                break
        except Exception as exc:
            logger.warning("revoke_token_db_error provider=%s: %s", provider, exc)

    try:
        await pool.disconnect(server_id)
    except Exception as exc:
        logger.warning("mcp_disconnect_error server=%s: %s", server_id, exc)

    from digitorn.modules.mcp.connections import MCPServerEntry

    stub = MCPServerEntry(
        server_id=server_id,
        transport_type=entry.transport_type,
        transport=entry.transport,
        status="disconnected",
        auth_config=entry.auth_config,
        _connect_kwargs={
            k: v for k, v in entry._connect_kwargs.items()
            if k != "env" or not entry.auth_config.env_token_var
        },
    )
    if entry.auth_config.env_token_var and "env" in entry._connect_kwargs:
        clean_env = dict(entry._connect_kwargs.get("env", {}))
        clean_env.pop(entry.auth_config.env_token_var, None)
        stub._connect_kwargs["env"] = clean_env

    pool._servers[server_id] = stub

    cb = deployed.context_builder
    if cb is not None:
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = cb.build_and_set_index(deployed.modules, security_profile)
        _refresh_deployed_agent_tools(deployed, new_index)
        logger.info(
            "tool_index_rebuilt after oauth_revoke app=%s server=%s tools=%d",
            app_id, server_id, new_index.total_tools,
        )

    return AppResponse(
        success=True,
        data={
            "server_id": server_id,
            "provider": provider,
            "status": "disconnected",
        },
    )


def _get_manager(request: Request):
    """Get the AppManager from app state."""
    manager = getattr(request.app.state, "app_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="AppManager not available — daemon may still be starting",
        )
    return manager


def _get_rate_limiter(request: Request):
    """Get the RateLimiter from app state."""
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise HTTPException(
            status_code=503,
            detail="RateLimiter not available — daemon may still be starting",
        )
    return limiter
