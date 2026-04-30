"""Routes for the workspace group, extracted from the legacy ``apps.py``.

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



@router.get("/{app_id}/sessions/{session_id}/workspace", response_model=AppResponse)
async def get_session_workspace(request: Request, app_id: str, session_id: str) -> AppResponse:
    """Full workspace snapshot for a session - durable + in-memory state merged.

    Returns everything the client needs to re-render the session view
    identically on reopen:

    - ``workspace`` / ``workspace_mode`` - physical workspace dir (if any)
    - ``render_mode`` / ``entry_file`` - from the top-level ``workspace:`` YAML
    - ``snapshot`` - the live preview state tree (``state`` map, ``resources``
      channels, last ``seq``). Hydrated from DB on first read after a
      daemon restart, then live-updated by every tool call.
    - ``git`` - git status of the physical workspace (if present)
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    # BUG-076: reject anonymous / cross-user workspace peeking.
    session = await _require_session_access(request, app_id, session_id)
    _uid = session.user_id if session is not None else (
        getattr(request.state, "user_id", None) or "local"
    )

    # Post-scoping refactor: _deployed is keyed by (scope, owner, app_id).
    # Use the scope-aware manager.get() to resolve.
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)

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


@router.get("/{app_id}/sessions/{session_id}/workspace/code-snapshot",
            response_model=AppResponse)
async def get_code_snapshot(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """File tree + metadata for the code editor - NO content.

    Preview module is preferred (it carries live validation / pending-diff
    metadata). For apps without preview we fall back to listing files on
    disk in the session workspace so the explorer still renders.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)
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
        _raise_not_deployed(request, app_id)

    # BUG-066 + cross-user guard: use the shared helper so the 404 we
    # return is indistinguishable from every other session route and
    # the owner lookup uses the same uid resolution path as GET /sessions.
    session = await _require_session_access(request, app_id, session_id)
    _uid = session.user_id

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module - nothing to export")

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


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
            response_model=AppResponse)
async def get_file_content(
    request: Request, app_id: str, session_id: str, file_path: str,
    include_baseline: bool = False,
) -> AppResponse:
    """Fetch the full content of a single workspace file (lazy-loaded).

    Works for apps with or without the ``preview`` module - if preview is
    loaded, we serve from its in-memory resources (current live state).
    Otherwise we fall back to reading the file directly from the session
    workspace on disk (apps that only use ``filesystem``/``workspace``
    without streaming preview events still need to serve their files).

    With ``include_baseline=true`` the response also includes the
    last-approved baseline content + a pending unified diff - used by the
    diff viewer when the user clicks "review changes".
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)
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

    # Disk fallback - works for apps without preview module, OR when the
    # file was written outside the preview pipeline (filesystem module,
    # shell output, etc.).
    if payload is None:
        import os as _os
        sess = await manager.get_session(app_id, session_id, user_id=_uid)
        ws = getattr(sess, "workspace", "") if sess else ""
        if ws:
            # Guard against path escape - resolve target and verify it
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
        payload_diff = payload.get("unified_diff_pending")
        if payload_diff is not None:
            out["unified_diff_pending"] = payload_diff
        try:
            sess = await manager.get_session(app_id, session_id, user_id=_uid)
            ws = getattr(sess, "workspace", "") if sess else ""
            if ws:
                from digitorn.modules.preview.fs_backend import read_baseline
                baseline = read_baseline(ws, session_id, resolved_path)
                if baseline is not None:
                    out["baseline"] = baseline
                    if payload_diff is None:
                        from digitorn.modules.workspace.module import _safe_unified_diff
                        out["unified_diff_pending"] = _safe_unified_diff(
                            baseline, payload.get("content") or "", resolved_path,
                        )
        except Exception as exc:
            logger.warning("get_file_content_baseline_failed: %s", exc)

    return AppResponse(success=True, data=out)


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}/history",
            response_model=AppResponse)
async def file_history_endpoint(
    request: Request, app_id: str, session_id: str, file_path: str,
) -> AppResponse:
    """History of baseline revisions for a file - latest first.

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


@router.get("/{app_id}/sessions/{session_id}/workspace/preview-snapshot",
            response_model=AppResponse)
async def get_preview_snapshot(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Lightweight snapshot for the preview pane - state + non-files channels.

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


@router.put("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
            response_model=AppResponse)
async def writeback_file_endpoint(
    request: Request, app_id: str, session_id: str,
    file_path: str, body: WritebackRequest,
) -> AppResponse:
    """User-side write - manual edit, conflict resolution, drag-drop import."""
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


@router.post("/{app_id}/sessions/{session_id}/workspace/files/approve",
             response_model=AppResponse)
async def approve_file_endpoint(
    request: Request, app_id: str, session_id: str, body: FileActionRequest,
) -> AppResponse:
    """Mark a file as approved - snapshot its current content as baseline."""
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
        # BUG-065: returning 200 + success:false is contradictory -
        # the HTTP status said OK while the body said "this operation
        # failed". Surface the failure as an HTTP error so clients that
        # branch on status_code get the right signal.
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "approve_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/sessions/{session_id}/workspace/files/approve-hunks",
             response_model=AppResponse)
async def approve_file_hunks_endpoint(
    request: Request, app_id: str, session_id: str, body: HunksActionRequest,
) -> AppResponse:
    """Partial approve - stage only selected hunks, leave the rest pending."""
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


@router.post("/{app_id}/sessions/{session_id}/workspace/files/reject",
             response_model=AppResponse)
async def reject_file_endpoint(
    request: Request, app_id: str, session_id: str, body: FileActionRequest,
) -> AppResponse:
    """Reject the pending changes - revert file to baseline or delete."""
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


@router.post("/{app_id}/sessions/{session_id}/workspace/files/reject-hunks",
             response_model=AppResponse)
async def reject_file_hunks_endpoint(
    request: Request, app_id: str, session_id: str, body: HunksActionRequest,
) -> AppResponse:
    """Partial revert - undo only selected hunks, keep the rest pending."""
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


@router.post("/{app_id}/sessions/{session_id}/workspace/commit",
             response_model=AppResponse)
async def commit_session_endpoint(
    request: Request, app_id: str, session_id: str, body: CommitRequest,
) -> AppResponse:
    """Commit approved files to git - one-shot ship to the session's repo."""
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


@router.post("/{app_id}/sessions/{session_id}/workspace/fork", response_model=AppResponse)
async def fork_session_workspace(
    request: Request, app_id: str, session_id: str,
    body: WorkspaceForkRequest,
) -> AppResponse:
    """Fork a session's workspace into a brand new session.

    Creates a fresh session (new id, fresh chat history) and copies the
    source workspace snapshot wholesale - so the user can keep editing
    the same React app / slide deck / workspace without polluting the
    conversation history.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    src = await manager.get_session(app_id, session_id, user_id=_uid)
    if src is None:
        raise HTTPException(status_code=404, detail="Source session not found or access denied")

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module - cannot fork")

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


@router.post("/{app_id}/sessions/{session_id}/workspace/git-status",
             response_model=AppResponse)
async def refresh_git_status(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Trigger a git status refresh - emits `resource_patched` for every file."""
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
        _raise_not_deployed(request, app_id)

    _uid = getattr(request.state, "user_id", None) or "local"
    session = await manager.get_session(app_id, session_id, user_id=_uid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or access denied")

    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)

    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    if preview_module is None:
        raise HTTPException(status_code=400, detail="App has no preview module - cannot import")

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

