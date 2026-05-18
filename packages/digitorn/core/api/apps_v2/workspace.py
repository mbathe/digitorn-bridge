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


# Max bytes accepted by writeback - 10 MB. Higher than typical source
# files but well below daemon OOM territory. Configurable later if a
# real use case needs more.
_WRITEBACK_MAX_BYTES = 10 * 1024 * 1024


def _safe_relative_path(file_path: str) -> str | None:
    """Normalise a workspace-relative path and reject traversals.

    Returns the cleaned forward-slash path, or ``None`` if it tries
    to escape the workspace (``..``, absolute, drive letter, ...).
    Used by every endpoint that accepts a path-as-URL-segment.
    """
    if not file_path:
        return None
    p = file_path.replace("\\", "/").strip("/")
    if not p:
        return None
    # Reject obviously bad segments. We do this on the raw string
    # rather than ``Path.resolve()`` because we want a workspace-
    # relative result without touching the filesystem.
    parts = p.split("/")
    for seg in parts:
        if not seg or seg in (".", ".."):
            return None
        # Windows drive letter or UNC share.
        if len(seg) == 2 and seg.endswith(":"):
            return None
    return "/".join(parts)



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
    # Per-session workdir wins: it's the agent-facing path the user
    # sees in the UI (file tree, editor, status bar). The compiled YAML
    # workspace is only a fallback for legacy ``fixed``/``none`` apps
    # that don't go through the per-session create flow.
    session_workdir = (
        (getattr(session, "workdir", "") or getattr(session, "workspace", ""))
        if session is not None else ""
    )
    if session_workdir:
        workspace = session_workdir
    elif ws_mode == "fixed":
        workspace = getattr(deployed.compiled.execution, "workspace", "") or ""
    elif ws_mode == "none":
        workspace = ""
    else:
        workspace = os.getcwd()

    result: dict[str, Any] = {
        "session_id": session_id,
        "app_id": app_id,
        "workspace": workspace,
        "workdir": workspace,
        "workspace_mode": ws_mode,
    }

    # Top-level workspace: block (render_mode, entry_file, title).
    ws_block = getattr(deployed.compiled, "workspace", None)
    if ws_block is not None:
        result["render_mode"] = getattr(ws_block, "render_mode", "auto")
        result["entry_file"] = getattr(ws_block, "entry_file", None)
        result["title"] = getattr(ws_block, "title", None)

    # Hydrated preview snapshot (state + resources). Pulls from the
    # in-memory store first; the store is itself seeded from
    # ``state.json`` on disk on first ``activate_session`` call, so
    # reopening a session from a cold client still renders the last
    # saved state.
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None
    snapshot: dict[str, Any] = {
        "state": {}, "resources": {}, "hydrated": False,
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
        # Off-loop: 3 ``subprocess.run`` git calls with 3-5s timeouts.
        # Even on the happy path each takes 50-200ms, enough to drop
        # Socket.IO pings under load.
        import asyncio as _asyncio
        result["git"] = await _asyncio.to_thread(_get_workspace_status, workspace)

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

    if preview_module is not None:
        state = await _activate_preview_session(
            request, app_id, session_id, preview_module, user_id=_uid,
        )
        if state is not None:
            snap = state.snapshot()
            files_raw = (snap.get("resources") or {}).get("files", {}) or {}
            files_meta = _strip_content_from_files({"files": files_raw}).get("files", {})

    if not files_meta:
        import os as _os
        from pathlib import Path as _Path
        sess = await manager.get_session(app_id, session_id, user_id=_uid)
        # Walk the AGENT-facing workdir, not the daemon-private
        # workspace - the explorer should show the user's files,
        # never the daemon's internal state (baselines, __sdk__/).
        ws = (
            (getattr(sess, "workdir", "") or getattr(sess, "workspace", ""))
            if sess else ""
        )
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
        "version": 2,
        "app_id": app_id,
        "source_session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "state": snap.get("state", {}),
        "resources": snap.get("resources", {}),
    }
    return AppResponse(success=True, data=payload)


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}/history",
            response_model=AppResponse)
async def file_history_endpoint(
    request: Request, app_id: str, session_id: str, file_path: str,
) -> AppResponse:
    """History of baseline revisions for a file - latest first.

    MUST be declared BEFORE the ``/files/{file_path:path}`` catch-all
    that follows. FastAPI route matching is first-match in declaration
    order; with the catch-all earlier, the ``/history`` suffix would be
    consumed as part of ``file_path`` and the request would return 404.
    """
    _validate_id(session_id, "session_id")
    session = await _require_session_access(request, app_id, session_id)
    ws = getattr(session, "workspace", "") or ""
    if not ws:
        return AppResponse(success=True, data={"path": file_path, "revisions": []})
    safe_rel = _safe_relative_path(file_path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    import json as _json
    from pathlib import Path as _Path
    hist_dir = _Path(ws) / ".digitorn" / "sessions" / session_id / "baselines" / (safe_rel + ".history")
    idx_path = hist_dir / "_index.json"
    revisions: list[dict[str, Any]] = []
    if idx_path.is_file():
        try:
            revisions = _json.loads(idx_path.read_text(encoding="utf-8")) or []
        except Exception:
            revisions = []
    return AppResponse(success=True, data={"path": file_path, "revisions": revisions})


@router.get("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}")
async def get_file_content(
    request: Request, app_id: str, session_id: str, file_path: str,
    include_baseline: bool = False,
    raw: bool = False,
):
    """Fetch the full content of a single workspace file (lazy-loaded).

    Works for apps with or without the ``preview`` module - if preview is
    loaded, we serve from its in-memory resources (current live state).
    Otherwise we fall back to reading the file directly from the session
    workspace on disk (apps that only use ``filesystem``/``workspace``
    without streaming preview events still need to serve their files).

    With ``include_baseline=true`` the response also includes the
    last-approved baseline content + a pending unified diff - used by the
    diff viewer when the user clicks "review changes".

    With ``raw=true`` and the workspace payload carries a ``file_id`` (i.e.
    the file was ingested via the chat composer paperclip or the SDK's
    ``ingestFile()`` -> ``register_attachment`` path), this route serves
    the ORIGINAL binary bytes from the file_store directly via
    ``FileResponse`` instead of the JSON envelope. Use case: iframe
    document viewers (PDF.js, docx-preview, image preview) that need the
    pre-extraction bytes, not the text extracted for the agent.

    The ``raw`` mode is a no-op (falls back to JSON) for workspace files
    written by the agent itself (no ``file_id``) - the agent-written
    content IS the file, there's no separate binary.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(file_path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    manager = _get_manager(request)
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    deployed = manager.get(app_id, user_id=_caller_user_id(request))
    if not deployed:
        _raise_not_deployed(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    preview_module = deployed.modules.get("preview") if hasattr(deployed, "modules") else None

    payload: dict[str, Any] | None = None
    resolved_path = safe_rel
    file_path = safe_rel
    # Absolute on-disk path of the file when we fall through to the
    # disk-fallback branch. Used by the ``raw=true`` path to serve
    # binary files (PDF compiled by tectonic, images, etc.) directly
    # from disk via FileResponse, without forcing them through
    # text-mode UTF-8 decode that would corrupt the bytes.
    disk_target: str | None = None

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
    # shell output, etc.). Hidden namespaces (``__sdk__/``, ``.app/``,
    # ``.digitorn/``) live under the daemon-private workspace; regular
    # files live under the agent's workdir. Pick the right root for the
    # path being read.
    if payload is None:
        import os as _os
        sess = await manager.get_session(app_id, session_id, user_id=_uid)
        ws_priv = getattr(sess, "workspace", "") if sess else ""
        ws_user = getattr(sess, "workdir", "") if sess else ""
        ws_user = ws_user or ws_priv
        # Hidden globs - mirror the workspace module's defaults so the
        # SDK can read its own private files via this same route.
        _norm = file_path.replace("\\", "/").lstrip("./")
        _is_hidden = (
            _norm.startswith("__sdk__/") or _norm == "__sdk__"
            or _norm.startswith(".app/") or _norm == ".app"
            or _norm.startswith(".digitorn/") or _norm == ".digitorn"
        )
        ws = ws_priv if _is_hidden else ws_user
        if ws:
            # Guard against path escape - resolve target and verify it
            # still lives under the chosen root.
            ws_abs = _os.path.abspath(ws)
            target = _os.path.abspath(_os.path.join(ws_abs, file_path))
            if not target.startswith(ws_abs + _os.sep) and target != ws_abs:
                raise HTTPException(status_code=400, detail="path escapes workspace")
            if _os.path.isfile(target):
                disk_target = target
                # For raw=true on a disk-only file, we serve binary
                # directly below — do NOT bother opening in text mode
                # (would corrupt PDFs / images / fonts).
                if raw:
                    payload = {
                        # Empty content marker so the raw branch can
                        # short-circuit on `disk_target` without going
                        # through text decode.
                        "content": "",
                        "size": _os.path.getsize(target),
                        "mtime": _os.path.getmtime(target),
                        "source": "disk",
                    }
                    resolved_path = file_path
                else:
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

    # Raw binary mode: when the caller asks for ``raw=true`` AND this
    # file came from the ingest pipeline (paperclip / SDK ingestFile),
    # serve the ORIGINAL bytes from the file_store. The workspace
    # payload only stores the post-extraction TEXT — for an iframe PDF
    # viewer or DOCX renderer we need the pre-extraction binary.
    if raw:
        from starlette.responses import FileResponse
        file_id = payload.get("file_id") if isinstance(payload, dict) else None
        if isinstance(file_id, str) and file_id:
            from digitorn.core.file_store import get_file_store
            store = get_file_store()
            ref = store.get_ref(file_id, session_id)
            if ref is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"raw bytes not in file_store: file_id={file_id}",
                )
            return FileResponse(
                str(ref.path),
                media_type=ref.mime or "application/octet-stream",
                headers={
                    # Inline display in iframe (PDF.js, docx-preview)
                    # rather than triggering a download.
                    "Content-Disposition": "inline",
                    # Modest cache so multi-page navigation in PDF.js
                    # doesn't re-fetch the same blob.
                    "Cache-Control": "private, max-age=300",
                },
            )
        # No file_id, but the file lives on disk (e.g. a PDF compiled
        # by tectonic / latexmk / cargo / pandoc as a side-effect of
        # the lint pipeline -- workspace doesn't track it as a
        # resource but it IS on disk in the session workdir). Serve
        # the bytes directly via FileResponse so the SDK's Blob
        # consumer gets a real binary (was returning corrupted UTF-8
        # JSON before this fallback).
        if disk_target is not None:
            import mimetypes
            mime, _ = mimetypes.guess_type(disk_target)
            return FileResponse(
                disk_target,
                media_type=mime or "application/octet-stream",
                headers={
                    "Content-Disposition": "inline",
                    # Compiled artifacts (tectonic-produced main.pdf,
                    # latexmk output, etc.) update on every save. The
                    # preview iframe's debounced retry hits this route
                    # 0-3 s after the workspace publishes the .tex
                    # update; an in-flight compile may not have
                    # finished yet at the first call, so we must NOT
                    # let the browser cache the stale snapshot.
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                },
            )
        # ``raw=true`` requested but nothing serviceable as binary
        # (agent-written virtual file, no on-disk artifact). Fall
        # through to the JSON envelope; the caller can use the text
        # path or wait for the next compile.

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
    })


@router.put("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
            response_model=AppResponse)
async def writeback_file_endpoint(
    request: Request, app_id: str, session_id: str,
    file_path: str, body: WritebackRequest,
) -> AppResponse:
    """User-side write - manual edit, conflict resolution, drag-drop import."""
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(file_path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    if len(body.content.encode("utf-8")) > _WRITEBACK_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"writeback exceeds {_WRITEBACK_MAX_BYTES} bytes",
        )
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
        WritebackParams(path=safe_rel, content=body.content, auto_approve=body.auto_approve),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "writeback_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.delete("/{app_id}/sessions/{session_id}/workspace/files/{file_path:path}",
               response_model=AppResponse)
async def delete_file_endpoint(
    request: Request, app_id: str, session_id: str, file_path: str,
) -> AppResponse:
    """User-side delete - remove a file from the session workspace.

    Mirror of ``WsDelete`` exposed to agents but invocable by the SDK
    iframe / web client without needing an LLM turn. Requires session
    access auth like all workspace mutations.
    """
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(file_path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import DeleteParams
    result = await ws_module.delete(DeleteParams(path=safe_rel))
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "delete_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.post(
    "/{app_id}/sessions/{session_id}/workspace/ingest-source",
    response_model=AppResponse,
)
async def ingest_source_endpoint(
    request: Request, app_id: str, session_id: str,
    file: UploadFile = File(...),
    target_dir: str = Form("sources"),
):
    """Ingest a binary file as a workspace source / attachment with
    server-side text extraction.

    The chat composer paperclip already does this for attachments — same
    pymupdf / python-docx / pure-text fall-through pipeline used by
    ``POST /messages``. This endpoint exposes the SAME path to the
    preview SDK so iframe apps can offer their own "add source"
    affordance without the user having to round-trip through the chat
    composer. Target directory is parameterised so a single endpoint
    serves both ``sources/`` (default, manual curation) and
    ``attachments/`` (parity with chat composer).

    Effect:
      1. Read the upload's bytes into the file store
      2. Sniff format from bytes + filename hint
      3. ``extract_text(path, format_hint=fmt)`` → plain text
      4. ``workspace.register_attachment(target_dir=<dir>)`` writes
         the extracted text to the files channel under
         ``<dir>/<sanitised_name>`` (idempotent overwrite)

    For pure-text uploads (``text/*``, ``.md``, ``.txt``, ``.csv``, ...)
    the extractor returns the raw content unchanged — no double-encode.

    Returns the workspace-relative path + lines count.
    """
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)

    # Match the same chat-composer cap so the two paths agree on what
    # an "upload" is allowed to be.
    raw = await file.read()
    if len(raw) > _WRITEBACK_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds {_WRITEBACK_MAX_BYTES} bytes",
        )

    if target_dir not in ("sources", "attachments"):
        raise HTTPException(
            status_code=400,
            detail="target_dir must be 'sources' or 'attachments'",
        )

    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = (
        deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    )
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")

    # Persist the upload through the same file_store the chat composer
    # uses. The store decodes base64 already, so we re-encode here.
    # Round-trip cost is acceptable for files we're going to extract
    # text from anyway (orders of magnitude cheaper than pymupdf).
    import base64
    from digitorn.core.file_store import get_file_store
    from digitorn.core.file_extract import extract_text
    from ._attach_helpers import sniff_format

    name = file.filename or "source"
    mime = file.content_type or "application/octet-stream"
    b64 = base64.b64encode(raw).decode("ascii")

    file_store = get_file_store()
    ref = await file_store.store_base64(
        b64, mime,
        session_id=session_id,
        original_name=name,
        message_id="",  # not tied to a chat message
        app_id=app_id,
    )
    fmt = sniff_format(ref.path, filename_hint=name)
    file_store.update_index_status(ref.file_id, session_id, format=fmt)

    extracted = await extract_text(ref.path, format_hint=fmt)
    if not extracted.strip():
        file_store.update_index_status(
            ref.file_id, session_id,
            status="empty",
            error="no extractable text",
        )
        raise HTTPException(
            status_code=422,
            detail={
                "error": "no extractable text from file",
                "format": fmt,
                "mime": mime,
            },
        )

    file_store.update_index_status(
        ref.file_id, session_id,
        status="indexed",
        chunks=0,
        extracted_text=extracted,
    )

    path = await ws_module.register_attachment(
        session_id, name, extracted,
        mime=mime, file_id=ref.file_id, target_dir=target_dir,
    )
    if path is None:
        raise HTTPException(
            status_code=500,
            detail="workspace.register_attachment did not return a path",
        )

    return AppResponse(success=True, data={
        "path": path,
        "lines": extracted.count("\n") + 1,
        "size": len(extracted),
        "mime": mime,
        "format": fmt,
        "target_dir": target_dir,
    })


@router.post("/{app_id}/sessions/{session_id}/workspace/upload/{file_path:path}",
             response_model=AppResponse)
async def upload_file_endpoint(
    request: Request, app_id: str, session_id: str, file_path: str,
    file: UploadFile = File(...),
    auto_approve: str = Form("false"),
):
    """Binary file upload (avatars, images, archives, etc.).

    Multipart-encoded so we don't pay the +33% base64 overhead in
    transit. The file's bytes go straight to the workspace at
    ``file_path``. For text files the existing JSON ``PUT
    /workspace/files/{path}`` route is more idiomatic.

    The workspace module's ``write`` action accepts string content;
    binary blobs are written as latin-1-encoded strings (1:1 byte
    mapping, no normalisation) so the round-trip is loss-less.
    """
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(file_path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    raw = await file.read()
    if len(raw) > _WRITEBACK_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"upload exceeds {_WRITEBACK_MAX_BYTES} bytes",
        )
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import WritebackParams
    # Latin-1 round-trips raw bytes through Python's str type without
    # mangling (each byte maps to one codepoint). The workspace module
    # writes the bytes back with the same encoding when the path's
    # extension hints at binary.
    content = raw.decode("latin-1")
    auto = (auto_approve or "false").lower() in ("true", "1", "yes")
    result = await ws_module.writeback_file(
        WritebackParams(path=safe_rel, content=content, auto_approve=auto),
    )
    if not result.success:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error or "upload_failed", "data": result.data},
        )
    return AppResponse(success=True, data=result.data)


@router.post("/{app_id}/sessions/{session_id}/workspace/files/approve",
             response_model=AppResponse)
async def approve_file_endpoint(
    request: Request, app_id: str, session_id: str, body: FileActionRequest,
) -> AppResponse:
    """Mark a file as approved - snapshot its current content as baseline."""
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(body.path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import ApproveFileParams
    result = await ws_module.approve_file(ApproveFileParams(path=safe_rel))
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
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(body.path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import HunksActionParams
    result = await ws_module.approve_file_hunks(
        HunksActionParams(path=safe_rel, hunks=body.hunks),
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
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(body.path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import RejectFileParams
    result = await ws_module.reject_file(RejectFileParams(path=safe_rel))
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
    await _require_session_access(request, app_id, session_id)
    safe_rel = _safe_relative_path(body.path)
    if safe_rel is None:
        raise HTTPException(status_code=400, detail="invalid file path")
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
    )
    ws_module = deployed.modules.get("workspace") if hasattr(deployed, "modules") else None
    if ws_module is None:
        raise HTTPException(status_code=400, detail="App has no workspace module")
    from digitorn.modules.workspace.module import HunksActionParams
    result = await ws_module.reject_file_hunks(
        HunksActionParams(path=safe_rel, hunks=body.hunks),
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
    await _require_session_access(request, app_id, session_id)
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
    })


@router.get("/{app_id}/sessions/{session_id}/workspace/changes",
            response_model=AppResponse)
async def get_workspace_changes(
    request: Request, app_id: str, session_id: str,
    include_diffs: bool = False,
    only_pending: bool = True,
) -> AppResponse:
    """Aggregated view of workspace files with pending changes.

    Reads the snapshot through ``WorkspaceCacheService`` (so the warm
    path is sub-millisecond, the warm-with-stat path ~5-15 ms, and the
    cold path goes through the full disk hydration once) and returns a
    compact projection of the ``files`` channel:

      - filtered to files with pending insertions / deletions OR
        ``validation == "pending"`` (override with ``only_pending=false``
        to also see approved files);
      - stripped of file content - only metadata that the Changes
        page needs (path, ins, del, totals, status, git_status, ...).
        The pre-existing ``GET /preview`` route shipped the FULL content
        of every workspace file, which made the Changes panel take
        seconds to render on real projects.
      - aggregated totals (count, total_ins_pending, total_del_pending,
        total_ins_lifetime, total_del_lifetime).

    ``include_diffs=true`` opts into the per-file ``unified_diff_pending``
    payload. Off by default because the diff text dominates the payload
    size for sessions with hefty edits - prefer fetching it lazily via
    ``GET /workspace/files/{path}?include_baseline=true`` when the user
    actually expands a hunk.

    Live updates flow as before through ``preview:resource_set`` /
    ``preview:resource_patched`` events on the session socket. This
    route is only the cold-load shortcut.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    if not _is_deployed(request, app_id):
        _raise_not_deployed(request, app_id)
    await _require_session_access(request, app_id, session_id)

    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)

    preview_mod = deployed.modules.get("preview")
    if preview_mod is None:
        return AppResponse(success=True, data={
            "session_id": session_id,
            "files": [],
            "count": 0,
            "total_insertions_pending": 0,
            "total_deletions_pending": 0,
            "total_insertions": 0,
            "total_deletions": 0,
        })

    user_id = getattr(request.state, "user_id", None)

    # Resolve workdir (same lookup as ``GET /preview``). The cache scans
    # this dir for signature changes - we want it pointed at the agent's
    # workdir (where the user's files live) rather than the daemon-
    # private workspace (which only holds baselines + ``__sdk__/``).
    workspace_path = ""
    try:
        manager = _get_manager(request)
        sess = await manager.get_session(app_id, session_id, user_id=user_id)
        if sess:
            workspace_path = (
                getattr(sess, "workdir", "")
                or getattr(sess, "workspace", "")
                or ""
            )
    except Exception:
        workspace_path = ""

    ws_mod = deployed.modules.get("workspace")

    async def _hydrate() -> dict[str, Any]:
        """Cold-path: re-read state.json + workspace files from disk."""
        if hasattr(preview_mod, "hydrate_session"):
            try:
                await preview_mod.hydrate_session(
                    session_id, user_id=user_id,
                    workspace=workspace_path or None,
                )
            except Exception as exc:
                logger.debug("preview_hydrate_session_failed sid=%s: %s", session_id, exc)
        if ws_mod is not None and hasattr(ws_mod, "hydrate_files_from_disk"):
            try:
                await ws_mod.hydrate_files_from_disk(session_id)
            except Exception as exc:
                logger.debug("workspace_hydrate_files_failed sid=%s: %s", session_id, exc)
        return preview_mod.snapshot_for(session_id, user_id=user_id)

    cache = getattr(request.app.state, "workspace_cache", None)
    if cache is not None and workspace_path:
        snapshot = await cache.get_or_hydrate(
            session_id=session_id,
            workspace_path=workspace_path,
            hydrate_fn=_hydrate,
        )
    else:
        snapshot = await _hydrate()

    resources = snapshot.get("resources") or {}
    files_channel = resources.get("files") or {}

    # Project + filter. We do this in pure Python over an in-memory
    # dict; no IO. For 10K files the loop is ~5 ms.
    out_files: list[dict[str, Any]] = []
    total_ins_pending = 0
    total_del_pending = 0
    total_ins_lifetime = 0
    total_del_lifetime = 0
    for path, payload in files_channel.items():
        if not isinstance(payload, dict):
            continue
        ins_pending = int(payload.get("insertions_pending") or 0)
        del_pending = int(payload.get("deletions_pending") or 0)
        validation = payload.get("validation") or "approved"
        # Default filter: only show files with actual pending work.
        # ``only_pending=false`` returns the full set.
        if only_pending and ins_pending == 0 and del_pending == 0 and validation != "pending":
            continue
        entry: dict[str, Any] = {
            "path": path,
            "status": payload.get("status") or "modified",
            "validation": validation,
            "size": int(payload.get("size") or 0),
            "lines": int(payload.get("lines") or 0),
            "language": payload.get("language") or "",
            "insertions": int(payload.get("insertions") or 0),
            "deletions": int(payload.get("deletions") or 0),
            "insertions_pending": ins_pending,
            "deletions_pending": del_pending,
            "total_insertions": int(payload.get("total_insertions") or 0),
            "total_deletions": int(payload.get("total_deletions") or 0),
            "baseline_lines": int(payload.get("baseline_lines") or 0),
            "updated_at": payload.get("updated_at"),
            "operation": payload.get("operation") or "write",
            "git_status": payload.get("git_status"),
        }
        if include_diffs:
            entry["unified_diff_pending"] = payload.get("unified_diff_pending") or ""
        out_files.append(entry)
        total_ins_pending += ins_pending
        total_del_pending += del_pending
        total_ins_lifetime += entry["total_insertions"]
        total_del_lifetime += entry["total_deletions"]

    # Sort by path for deterministic UI rendering. Clients can re-sort
    # client-side if they want (most recent first, by depth, etc.) but
    # alphabetic is the same default both Flutter and web already use.
    out_files.sort(key=lambda e: e["path"])

    return AppResponse(success=True, data={
        "session_id": session_id,
        "files": out_files,
        "count": len(out_files),
        "total_insertions_pending": total_ins_pending,
        "total_deletions_pending": total_del_pending,
        "total_insertions": total_ins_lifetime,
        "total_deletions": total_del_lifetime,
    })


@router.post("/{app_id}/sessions/{session_id}/workspace/git-status",
             response_model=AppResponse)
async def refresh_git_status(
    request: Request, app_id: str, session_id: str,
) -> AppResponse:
    """Trigger a git status refresh - emits `resource_patched` for every file."""
    _validate_id(session_id, "session_id")
    await _require_session_access(request, app_id, session_id)
    deployed, preview_module = await _resolve_deployed_preview(request, app_id)
    _uid = getattr(request.state, "user_id", None) or "local"
    await _activate_preview_session(
        request, app_id, session_id, preview_module, user_id=_uid, set_active=True,
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
    current in-memory state and force-flushes ``state.json`` so
    reopening the session yields the imported view.
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
    })

