"""Routes for the background group, extracted from the legacy ``apps.py``.

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
    _has_static_dist,
    _try_serve_static_dist,
    _proxy_preview_http,
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



@router.get("/{app_id}/background-tasks", response_model=AppResponse)
async def list_background_tasks_app(request: Request, app_id: str) -> AppResponse:
    """List all background tasks (running + completed)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        return AppResponse(success=True, data={"tasks": []})

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(list_tasks=True))
    return AppResponse(success=True, data=result.data if result.success else {"tasks": []})


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
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
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


@router.get("/{app_id}/background-tasks/{task_id}", response_model=AppResponse)
async def get_background_task(request: Request, app_id: str, task_id: str) -> AppResponse:
    """Get status and result of a background task."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    cb = deployed.context_builder if deployed else None
    if cb is None:
        raise HTTPException(status_code=404, detail="No context_builder")

    from digitorn.modules.context_builder.params import BackgroundRunParams
    result = await cb.background_run(BackgroundRunParams(task_id=task_id))
    if not result.success:
        raise HTTPException(status_code=404, detail=result.error)
    return AppResponse(success=True, data=result.data)


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
        _raise_not_deployed(request, app_id)

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


@router.post("/{app_id}/background-tasks/{task_id}/wait", response_model=AppResponse)
async def wait_background_task(
    request: Request, app_id: str, task_id: str, body: BackgroundTaskActionRequest,
) -> AppResponse:
    """Wait for a background task to complete (with timeout)."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

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
        _raise_not_deployed(request, app_id)

    deployed = _get_deployed(request, app_id)
    shell_mod = deployed.modules.get("shell") if deployed else None
    if shell_mod is None or not hasattr(shell_mod, "cancel_task"):
        raise HTTPException(status_code=404, detail="Shell module not available")

    result = await shell_mod.cancel_task(session_id, task_id)
    return AppResponse(success=result.get("success", False), data=result)


@router.delete("/{app_id}/background-tasks/{task_id}", response_model=AppResponse)
async def cancel_background_task_app(request: Request, app_id: str, task_id: str) -> AppResponse:
    """Cancel a running background task."""
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

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


@router.get("/{app_id}/notifications/active")
async def has_active_bg_tasks(request: Request, app_id: str) -> AppResponse:
    """Quick check if any background tasks are active for this app.

    Returns ``active: false`` (not 404) when the app is not deployed,
    since this endpoint is polled continuously by the CLI - a 404 would
    spam the server logs with useless error entries.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        return AppResponse(success=True, data={"active": False})
    active = manager.has_active_bg_tasks(app_id)
    return AppResponse(success=True, data={"active": active})


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


@router.get("/{app_id}/background-sessions/{bg_session_id}/payload", response_model=AppResponse)
async def get_background_session_payload(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Return the full payload (prompt + metadata + files) for a session.

    Also returns a ``validation`` block describing whether the payload
    satisfies the app's declared ``payload_schema`` - the dashboard uses
    this to decide whether to enable the "Activate" button.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
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
    # Cap at 25 MiB by default - dashboard sets smaller per-file limits.
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


@router.post("/{app_id}/background-sessions", response_model=AppResponse)
async def create_background_session(
    request: Request,
    app_id: str,
    body: BackgroundSessionCreateRequest,
) -> AppResponse:
    """Create a new background session for the authenticated user.

    In multi mode, each user can create multiple sessions with custom params
    (e.g. different CVs, different configs). In mono mode, this is a no-op -
    the session is auto-created on first trigger.
    """
    _validate_id(app_id)
    manager = _get_manager(request)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

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


@router.post("/{app_id}/background-sessions/{bg_session_id}/pause", response_model=AppResponse)
async def pause_background_session(
    request: Request, app_id: str, bg_session_id: str,
) -> AppResponse:
    """Pause a background session - triggers will skip it."""
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


@router.get("/{app_id}/artifacts/{event_id}/download")
async def download_artifact(
    request: Request, app_id: str, event_id: str,
):
    """Stream an artifact file to the client.

    The ``event_id`` MUST be the id of an ``ActivationEvent`` row with
    ``event_type='artifact'`` - in other words, a file that was
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

    A failure at any step returns 404 with a generic message - the
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
    # artifact was recorded by the daemon itself during a tool call -
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

    # Content type - Python's mimetypes covers the common cases
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

    Returns only the response headers - Content-Type, Content-Length,
    X-Artifact-* - so the client can decide whether to proceed with
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

