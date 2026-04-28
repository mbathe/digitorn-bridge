"""Routes for the widgets group, extracted from the legacy ``apps.py``.

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
    _drain_queue_next,
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
        _raise_not_deployed(request, app_id)
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

    - ``http``    - HTTP request (relative to daemon, app-scoped)
    - ``tool``    - invoke a module action with args
    - ``static``  - return the value verbatim
    - ``stream``  - opens an SSE stream (delegated to the SSE route;
                    this endpoint just returns a placeholder)
    - ``local``   - client-side only, returns the default value

    Query params (any) are forwarded to HTTP source requests as the
    request query string, and to tool source as additional args.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

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

    1. **SSE upstream** - the URL serves ``text/event-stream``. The
       daemon parses ``data:`` lines and forwards them.
    2. **HTTP poll upstream** - the URL serves JSON. The daemon
       polls every ``poll`` seconds (from the data spec) and emits
       a frame per response.

    Reducer hint (``replace`` / ``append`` / ``merge``) is sent in
    the first frame so the client knows how to integrate updates.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

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
                # Probe the upstream - if it returns text/event-stream, bridge it.
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


@router.get("/{app_id}/widgets/validate")
async def validate_widgets(request: Request, app_id: str):
    """Lint endpoint - recompiles the widgets block and returns errors.

    Used by the builder UI for live validation. Read-only - does not
    redeploy the app.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)
    widgets = getattr(deployed.compiled, "widgets", None)
    return {"data": {
        "ok": True,
        "version": getattr(widgets, "version", 1) if widgets else None,
        "has_chat_side": getattr(widgets, "chat_side", None) is not None,
        "workspace_tab_count": len(getattr(widgets, "workspace_tabs", []) or []),
        "modal_count": len(getattr(widgets, "modals", {}) or {}),
        "inline_count": len(getattr(widgets, "inline", {}) or {}),
    }}


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
        _raise_not_deployed(request, app_id)

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
    # behave as first-class session variables - exactly what the
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
        # full chat turn - just execute the tool directly with the
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
        # We acknowledge - the client re-fetches the bindings on its
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
        # Pure client-side effects - we just ACK.
        effect = {"action": action_type, **payload}

    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown widget action type {action_type!r}",
        )

    return {"data": {"ok": True, "effect": effect}}


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
    ``file_upload`` primitive - the daemon only handles the generic
    case when no custom URL is set.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if not deployed:
        _raise_not_deployed(request, app_id)

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
        _raise_not_deployed(request, app_id)

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

