"""Routes for the triggers group, extracted from the legacy ``apps.py``.

This module is part of the ``apps_v2`` refactoring - same paths,
same response shapes, same behaviour, just split across multiple files.
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
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from digitorn.core.quota import QuotaPutRequest

from ._shared import (
    _MAX_CONCURRENT_TURNS,
    _turn_semaphore,
    _active_turn_tasks,
    _SAFE_ID_RE,
    _agent_turns_lock,
    _MESSAGE_MAX_BYTES,
    _MAX_ARTIFACT_DOWNLOAD_SIZE,
    _SECRET_REF_RE,
    _validate_app_id,
    _build_history_turns,
    _classify_error,
    _get_workspace_status,
    _validate_id,
    _inc_agent_turns,
    _activate_preview_session,
    _caller_user_id,
    _get_deployed,
    _raise_not_deployed,
    _is_deployed,
    _require_permission,
    _turn_event,
    _require_session_create_or_owner,
    _require_session_access,
    _refresh_deployed_agent_tools,
    _context_advice,
    _merge_resources,
    _resolve_deployed_preview,
    _strip_content_from_files,
    _validate_payload_against_schema,
    _mime_matches,
    _assert_session_visible,
    _get_bg_session_store,
    _get_activation_store,
    _resolve_app_bundle_dir,
    _try_resize_image,
    _serialise_widget_node,
    _serialise_widgets,
    _execute_widget_tool,
    _get_quota_store,
    _require_admin_for_quota,
    _usage_snapshot,
    _walk_yaml_for_secrets,
    _get_manager,
    _get_rate_limiter,
    DeployRequest,
    RunRequest,
    ChatRequest,
    AppSummary,
    AppResponse,
    ValidateRequest,
    PipelineRequest,
    NotificationCheckRequest,
    SessionMessageRequest,
    CreateSessionRequest,
    WorkspaceImportRequest,
    WorkspaceForkRequest,
    FileActionRequest,
    HunksActionRequest,
    WritebackRequest,
    CommitRequest,
    LspRpcRequest,
    LspCancelRequest,
    BackgroundSessionCreateRequest,
    PayloadSetRequest,
    BackgroundTaskRequest,
    BackgroundTaskActionRequest,
    WatcherCreateRequest,
    ToolExecuteRequest,
    WidgetActionRequest,
    InteractRequest,
    DisableRequest,
    ApprovalResolveRequest,
    SecretSetRequest,
    SecretsBulkSetRequest,
    OAuthCallbackParams,
    InjectOAuthTokenRequest
)

router = APIRouter(tags=["apps"])



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
        _raise_not_deployed(request, app_id)

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
            # Channel type resolution - `channel_type` is rarely set on
            # the provider wrapper; the actual kind is on the adapter's
            # class (ADAPTER_TYPE) or falls back to the provider's own
            # `type` attr. Previously this returned "?" for every
            # channel in the diagnostics response.
            # BUG-099: prefer the class-level ``CHANNEL_ID`` (which is
            # the authoritative registry key - ``file_watcher``,
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
        _raise_not_deployed(request, app_id)

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
    # trigger whose app has zero background_sessions - the activation
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
        _raise_not_deployed(request, app_id)

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
    # raw 500 with empty body - the frontend couldn't show any feedback
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

