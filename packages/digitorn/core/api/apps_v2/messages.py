"""Routes for the messages group, extracted from the legacy ``apps.py``.

This module is part of the ``apps_v2`` refactoring — same paths,
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
        _raise_not_deployed(request, app_id)
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
        # Orphan-queue watchdog: when the session has queued messages
        # AND nothing's running AND no approval is holding — the drain
        # chain previously died (daemon crash mid-turn, task cancelled,
        # exception escaping the ``finally: _drain_queue_next``). Left
        # alone the queue sits forever and every new ``send_message``
        # appends to a stuck pile. We kick off a fresh drain task here
        # so the existing queue starts flowing again; the new message
        # this caller is about to enqueue will be picked up by the
        # same drain chain once those older entries finish.
        if (
            _qdepth > 0
            and not _turn_running
            and not _has_pending_approval
        ):
            logger.warning(
                "queue_orphan_detected app=%s session=%s depth=%d — "
                "restarting drain chain",
                app_id, session_id, _qdepth,
            )
            try:
                await _drain_queue_next(request, app_id, session_id, _uid)
            except Exception as exc:
                logger.warning("queue_orphan_drain_failed: %s", exc)
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
                from digitorn.core.events.envelope import OpState as _OS
                # queue_full rejects a NEW message before it ever gets
                # a correlation_id — so the event is keyed by a fresh
                # synthetic op_id (there's no turn to correlate to).
                await manager.event_bus.emit(_turn_event(
                    "queue_full",
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id="",
                    op_state=_OS.FAILED,
                    payload={
                        "depth": exc.depth, "max": exc.max_depth,
                        "session_id": session_id,
                    },
                ))
            except Exception:
                pass
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session queue full ({exc.depth}/{exc.max_depth}). "
                    "Cancel pending messages or wait before sending more."
                ),
            )

        # ── Decide BEFORE emitting any event whether the message will
        #    actually wait in the queue, or whether it'll dispatch in
        #    < 1 ms (because the previous turn finished between our
        #    initial check and now). Re-checking here lets us emit the
        #    right event in either case, instead of always emitting a
        #    "queued" PENDING that the client briefly displays even when
        #    no real wait happens.
        _turn_active = await manager.is_turn_running(app_id, session_id)
        from digitorn.core.events.envelope import OpState as _OS

        if _turn_active:
            # ── True queue: emit PENDING events; client shows the queued
            #    badge until the running turn finishes and the drain
            #    chain emits ``message_started``.
            try:
                current_depth = await _mq.depth_for_session(session_id)
                if merged:
                    _evt = "message_merged"
                elif replaced:
                    _evt = "message_replaced"
                else:
                    _evt = "message_queued"
                await manager.event_bus.emit(_turn_event(
                    _evt,
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=entry.correlation_id,
                    op_state=_OS.PENDING,
                    payload={
                        "correlation_id": entry.correlation_id,
                        "position": entry.position,
                        "queue_depth": current_depth,
                        "message_preview": (entry.message or "")[:200],
                        "merged": merged,
                        "replaced": replaced,
                    },
                ))
            except Exception:
                current_depth = 0

            if not merged and not replaced:
                try:
                    await manager.event_bus.emit(_turn_event(
                        "user_message",
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=entry.correlation_id,
                        op_state=_OS.PENDING,
                        payload={
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
                    ))
                except Exception:
                    pass

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

        # ── Fast-dispatch path: the row was just enqueued, but no turn
        #    is running anymore (the previous one finished between our
        #    initial check and this re-check, or our depth check tripped
        #    on an orphan row that was just drained). Pop the head and
        #    fall through to ``_run_turn`` — emit RUNNING events so the
        #    client UX matches the original fast-path (no queued flash).
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
        if _head.correlation_id != entry.correlation_id:
            body.message = _head.message
            _image_refs = list(_head.image_refs or [])

        # Tell the client about merge/replace mutations even on the
        # fast-dispatch path so the existing message bubble updates in
        # place. RUNNING op_state because the dispatch is starting now.
        if merged or replaced:
            try:
                _evt = "message_merged" if merged else "message_replaced"
                await manager.event_bus.emit(_turn_event(
                    _evt,
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=_active_correlation_id,
                    op_state=_OS.RUNNING,
                    payload={
                        "correlation_id": _active_correlation_id,
                        "position": _head.position,
                        "merged": merged,
                        "replaced": replaced,
                    },
                ))
            except Exception:
                pass

        # Fresh-enqueue (not merged/replaced) → emit user_message RUNNING
        # mirroring the original fast-path branch below.
        if not merged and not replaced:
            try:
                await manager.event_bus.emit(_turn_event(
                    "user_message",
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=_active_correlation_id,
                    op_state=_OS.RUNNING,
                    payload={
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
                ))
            except Exception:
                pass

        # Always emit message_started — closes the asymmetry where the
        # queue-and-immediate path used to skip this event.
        try:
            await manager.event_bus.emit(_turn_event(
                "message_started",
                app_id=app_id, session_id=session_id,
                user_id=_user_id or "local",
                correlation_id=_active_correlation_id,
                op_state=_OS.RUNNING,
                payload={
                    "correlation_id": _active_correlation_id,
                    "session_id": session_id,
                    "position": _head.position,
                    "fast_path": False,
                },
            ))
        except Exception:
            pass
    else:
        import uuid as _uuid
        _active_correlation_id = f"fp-{_uuid.uuid4().hex[:12]}"
        _active_queue_row_id = ""

        try:
            from digitorn.core.events.envelope import OpState as _OS
            await manager.event_bus.emit(_turn_event(
                "user_message",
                app_id=app_id, session_id=session_id,
                user_id=_user_id or "local",
                correlation_id=_active_correlation_id,
                op_state=_OS.RUNNING,
                payload={
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
            ))
        except Exception:
            pass

        try:
            from digitorn.core.events.envelope import OpState as _OS
            await manager.event_bus.emit(_turn_event(
                "message_started",
                app_id=app_id, session_id=session_id,
                user_id=_user_id or "local",
                correlation_id=_active_correlation_id,
                op_state=_OS.RUNNING,
                payload={
                    "correlation_id": _active_correlation_id,
                    "session_id": session_id,
                    "position": 0,
                    "fast_path": True,
                },
            ))
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
                from digitorn.core.events.envelope import OpState as _OS
                await manager.event_bus.emit(_turn_event(
                    _evt_type,
                    app_id=app_id, session_id=session_id,
                    user_id=_user_id or "local",
                    correlation_id=_active_correlation_id or "",
                    op_state=_OS.FAILED,
                    payload=error_data,
                ))
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
                    from digitorn.core.events.envelope import OpState as _OS
                    # Terminal state for the turn cycle: CANCELLED
                    # when the user aborted, COMPLETED otherwise. This
                    # is the state the client uses to close the turn's
                    # spinner permanently.
                    _term_state = _OS.CANCELLED if cancelled else _OS.COMPLETED
                    await manager.event_bus.emit(_turn_event(
                        terminal_type,
                        app_id=app_id, session_id=session_id,
                        user_id=_user_id or "local",
                        correlation_id=_active_correlation_id,
                        op_state=_term_state,
                        payload={
                            "correlation_id": _active_correlation_id,
                            "session_id": session_id,
                            "fast_path": not _active_queue_row_id,
                        },
                    ))
                except Exception:
                    pass
            if _qcfg.enabled:
                # Atomic terminal-flip + drain-next via finish_and_drain
                # (Redis backend). On SQL backend this is the same as
                # the legacy mark_done + next_queued sequence — no
                # behaviour change. Awaiter resolution is handled inside
                # _drain_queue_next so we keep that side-effect unified
                # with the new flow.
                _terminal = "cancelled" if cancelled else "completed"
                try:
                    await _drain_queue_next(
                        request, app_id, session_id, _uid,
                        previous_row_id=(_active_queue_row_id or None),
                        previous_correlation=_active_correlation_id or None,
                        previous_status=_terminal,
                        previous_error_code="turn_cancelled" if cancelled else "",
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

    # Embed the authoritative state envelope in the POST response so
    # the client doesn't have to wait for the first SSE event to know
    # "a turn is running". Eliminates the race where a client misses
    # ``message_started`` and never animates the send button.
    try:
        state_envelope = await manager.build_state_envelope(
            app_id, session_id, _uid,
        )
    except Exception as exc:
        logger.debug("build_state_envelope failed on POST response: %s", exc)
        state_envelope = None

    return AppResponse(
        success=True,
        data={
            "session_id": session_id,
            "status": "accepted",
            "correlation_id": _active_correlation_id or None,
            "client_message_id": body.client_message_id,
            "state": state_envelope,
        },
    )

