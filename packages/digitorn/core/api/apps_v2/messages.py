"""Routes for the messages group, extracted from the legacy ``apps.py``.

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
    _get_workspace_status,
    _validate_id,
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



@router.post("/{app_id}/sessions/{session_id}/messages")
async def session_send_message(
    request: Request,
    app_id: str,
    session_id: str,
    body: SessionMessageRequest,
) -> AppResponse:
    """Send a message to a session. Events arrive via Socket.IO.

    **Queueing (Phase 3 - per-session FIFO queue)**

    When a turn is already running on this session, the message is
    enqueued instead of failing with ``session_busy``. The dispatcher
    picks the head of the queue as soon as the running turn finishes.
    The queue is persisted across daemon restarts.

    ``queue_mode`` controls the response:

    - ``async`` (default, recommended) - returns 202 immediately with
      ``{correlation_id, position, queue_depth}``. The client tracks the
      message via SSE events ``message_queued``, ``message_started``,
      ``message_done`` / ``message_cancelled``.
    - ``wait`` - legacy: block until the turn finishes, return the
      message data. Equivalent to the pre-queue behaviour for simple
      clients.

    Over-capacity (``session.queue.max_depth``) returns 429 + a
    ``queue_full`` event.
    """
    _validate_id(app_id)
    _validate_id(session_id, "session_id")
    manager = _get_manager(request)
    # Strong deploy check - not only does the manager know the app,
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
                f"App '{app_id}' is in a degraded state - deployed but "
                f"not fully initialized. Re-deploy to recover."
            ),
        )
    # BUG-072: reject cross-user message injection. New sessions still
    # pass (the handler creates them bound to the caller).
    _existing_session = await _require_session_create_or_owner(request, app_id, session_id)

    _user_id = getattr(request.state, "user_id", None)
    _workspace = body.workspace
    _active_mode = body.mode  # NOTE: received only - merge layer pending
    if _active_mode:
        logger.info(
            "messages_mode_received app=%s sid=%s mode=%s",
            app_id, session_id, _active_mode,
        )

    # Pre-create the session row when the caller is sending the first
    # message on a brand-new sid. The contract is "POST /messages on a
    # fresh sid creates the session on the fly" - but the in-line create
    # in ``manager.chat()`` only fires when ``chat()`` is actually
    # invoked. Pre-chat PAUSED paths (credential_required gate, quota
    # rejected, exception thrown before ``manager.chat`` runs) skip
    # ``manager.chat`` entirely, leaving a phantom session_id: the POST
    # returned 200, the message was accepted, but a follow-up GET
    # returned 404 forever. Pre-creating here closes that gap.
    if _existing_session is None:
        try:
            from digitorn.core.app.sessions import ConversationSession
            # ``body.workspace`` is the legacy "agent's working dir"
            # field that pre-dates the split. Treat it as the workdir
            # so the agent's tools see the right path; the daemon-
            # private workspace stays empty (legacy paths share one
            # tree, ``apply_workspace_override`` will paper over it).
            _new_session = ConversationSession(
                session_id=session_id,
                app_id=app_id,
                user_id=_user_id or "local",
                workspace=_workspace or "",
                workdir=_workspace or "",
            )
            _deployed_for_prompt = _get_deployed(request, app_id)
            if _deployed_for_prompt is not None:
                _sys_prompt = (
                    getattr(_deployed_for_prompt.entry_context, "system_prompt", "")
                    or ""
                )
                if _sys_prompt:
                    _new_session.add_system(_sys_prompt)
            await asyncio.to_thread(manager._session_store.put, _new_session)
        except Exception as _create_exc:
            logger.warning(
                "messages_pre_create_session_failed app=%s sid=%s: %s",
                app_id, session_id, _create_exc,
            )

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
    #   1. Always persist the message to the queue - gives us FIFO,
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
        # future - `is_turn_running` returns False (the coroutine is
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
        # AND nothing's running AND no approval is holding - the drain
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
                "queue_orphan_detected app=%s session=%s depth=%d - "
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

    # Authoritative seq attached to the user_message event when (and ONLY
    # when) the message is dispatched, NOT when it is queued. The chat
    # bubble on every client renders against this number; clients refuse
    # to add a user bubble to the chat without it (see the Flutter +
    # web composer logic). Stays ``0`` for queued (pending) messages -
    # those live only in the queue panel until the daemon dispatches
    # them, at which point a follow-up user_message event over the SSE
    # stream carries the canonical seq.
    _active_user_seq: int = 0

    if _qcfg.enabled and not _skip_queue:
        # Three enqueue strategies - the mode picks which helper runs.
        #
        # replace_last: if the tail of the queue is still queued,
        #   overwrite it with this new message in place. Client UX:
        #   "oops wrong message, use this one instead".
        #
        # auto_merge (config-driven): if a recent queued message from
        #   the same user is < auto_merge_window_s old, fold the new
        #   content into it - saves an LLM call when the user fires
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
                # a correlation_id - so the event is keyed by a fresh
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

        # Stash the inbound JWT alongside the row so the drain worker
        # can re-publish it before a queued/replayed turn calls the
        # gateway. Without this, queued turns lose ``Authorization``.
        try:
            from digitorn.core.runtime.request_context import get_inbound_user_jwt
            _mq.attach_jwt(entry.id, get_inbound_user_jwt())
        except Exception:
            pass

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

            # ── Final drain-kick safety (UNCONDITIONAL). The
            #    turn-active check at line 359 may return a stale
            #    True even after the previous turn completed (the
            #    flag isn't always cleared synchronously, especially
            #    with the persist-in-bg change). _drain_queue_next is
            #    idempotent: it pops the head atomically and returns
            #    immediately if the queue is empty or another task
            #    already grabbed it. Calling it after every enqueue
            #    closes the warm-path gap where messages could sit
            #    queued forever.
            try:
                await _drain_queue_next(request, app_id, session_id, _uid)
            except Exception as exc:
                logger.debug("post_enqueue_drain_kick failed: %s", exc)

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
        #    fall through to ``_run_turn`` - emit RUNNING events so the
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
                _active_user_seq = await manager.event_bus.emit(_turn_event(
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
                )) or 0
            except Exception:
                pass

        # `message_started` is now emitted exclusively by
        # `dispatch_turn` (single source of truth). The position is
        # surfaced via the TurnEntry so the event payload still carries
        # the queue head's index when relevant.
        _active_position = _head.position
    else:
        import uuid as _uuid
        _active_correlation_id = f"fp-{_uuid.uuid4().hex[:12]}"
        _active_queue_row_id = ""
        _active_position = 0

        try:
            from digitorn.core.events.envelope import OpState as _OS
            _active_user_seq = await manager.event_bus.emit(_turn_event(
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
            )) or 0
        except Exception:
            pass

    async def _run_turn():
        # Single source of truth for "run a chat turn end-to-end":
        # cred check, heartbeat, manager.chat(), event emission, error
        # classification all live inside `dispatch_turn`. We just hand
        # it the entry, await the outcome, then handle queue terminal
        # bookkeeping (chain to next, mark row done/failed/paused).
        from ._dispatch import (
            dispatch_turn, TurnEntry, TurnSource, TurnStatus,
        )
        outcome = await dispatch_turn(
            request, app_id, session_id,
            entry=TurnEntry(
                correlation_id=_active_correlation_id or "",
                message=body.message,
                workspace=_workspace,
                image_refs=_image_refs if _image_refs else None,
                client_message_id=body.client_message_id or "",
                queue_row_id=_active_queue_row_id or "",
                position=_active_position,
            ),
            user_id=_user_id or "local",
            source=TurnSource.FAST,
        )
        # PAUSED: credential gate hit. Mark the row terminal with
        # `credential_required` so `is_turn_running` returns False;
        # otherwise the user's RETRY queues behind a stuck row. The
        # client already sees `credential_required` on the bubble; the
        # next dispatch happens when they click RETRY (fresh POST).
        if outcome.status == TurnStatus.PAUSED:
            if _active_queue_row_id:
                try:
                    await _mq.mark_failed(
                        _active_queue_row_id,
                        error_code="credential_required",
                    )
                except Exception:
                    pass
            # The pre-chat credential gate raised before ``manager.chat()``
            # ran, so its ``finally:`` never discarded the active key
            # we set via ``reserve_session``. Without this explicit
            # release, ``is_session_active`` keeps returning True forever
            # and ``abort`` cannot unstick the session (its ``task.cancel``
            # is a no-op when no task was ever registered).
            if _reserved:
                try:
                    manager.release_session(app_id, session_id)
                except Exception:
                    logger.debug(
                        "paused_release_session_failed", exc_info=True,
                    )
            return
        # COMPLETED / FAILED / CANCELLED: dispatch_turn already flipped
        # the queue row + scheduled the next chain dispatch internally
        # (Step 6 ordering: flip BEFORE emitting message_done so the
        # next POST sees a clean daemon). Nothing more for us to do.

    # ── Dispatch agent turn to a worker thread ────────────────────────
    # The turn runs in its own event loop inside a thread from the worker
    # pool. The main event loop stays free for HTTP/SSE at all times.
    # A semaphore caps concurrency - beyond _MAX_CONCURRENT_TURNS the
    # endpoint returns 503 immediately instead of starving the daemon.
    if _turn_semaphore.locked() and _turn_semaphore._value == 0:
        if _reserved:
            manager.release_session(app_id, session_id)
        return AppResponse(
            success=False,
            data={"error": "Server busy - too many concurrent agent turns", "retry": True},
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
            # Authoritative chat seq for the user_message just emitted.
            # Non-zero ONLY when the message was dispatched immediately
            # (fast-path or queue-head fresh-enqueue). For pure-queued
            # messages it stays ``null``: clients must NOT add a chat
            # bubble in that case - the seq arrives later via the SSE
            # ``user_message`` event when the daemon dispatches the
            # message from the queue. Single source of truth for
            # ordering and persistence.
            "seq": _active_user_seq if _active_user_seq > 0 else None,
            "state": state_envelope,
        },
    )

