"""Single source of truth for dispatching a chat turn.

Three call sites previously each owned their own copy of the
"run a turn through manager.chat() + classify errors + emit
events + handle credentials" logic:

* `messages.py::_run_turn` (fast-path, POST /messages)
* `_shared.py::_drain_queue_next::_run_next` (chained drain)
* `manager_v2/_queue.py::drain_session_queue` (Socket.IO resume)

Each had drifted: divergent log levels, missing event-type
promotion for credential errors, no heartbeat in the drain paths,
inconsistent cancellation detection. This module collapses all
three to one async function so the contract is honoured uniformly.

The redesign rationale lives in
`.logs/QUEUE_DISPATCH_REDESIGN.md`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ._shared import _classify_error, _inc_agent_turns, _turn_event

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


_BYOK_CACHE_TTL_S = 60.0
_byok_cache: dict[tuple[str, str], tuple[float, bool, dict[str, Any], bool]] = {}


def invalidate_byok_cache(user_id: str | None = None, app_id: str | None = None) -> None:
    """Drop cached BYOK state. Called after credential grant/revoke."""
    if user_id is None and app_id is None:
        _byok_cache.clear()
        return
    keys = [
        k for k in _byok_cache
        if (user_id is None or k[0] == user_id)
        and (app_id is None or k[1] == app_id)
    ]
    for k in keys:
        _byok_cache.pop(k, None)


async def _maybe_inject_rag_context(
    deployed: Any | None,
    session_id: str,
    user_message: str,
) -> str:
    """Enrich the user message with the content of files attached to
    this session.

    Two modes (from `compiled.meta.attachments_mode`):

      * `direct` (default) - full extracted text is prepended
        verbatim. Agent answers immediately, no tool call needed.
      * `tool`   - text NOT prepended. The agent is told the
        files live under `attachments/` in the workspace and
        must call `WsRead` to inspect them. Falls back to
        `direct` if the workspace module isn't loaded (the agent
        would have no way to read otherwise).

    Returns the original message unchanged when no attachment is
    visible.

    Name kept as `_maybe_inject_rag_context` for call-site
    compatibility - the body no longer touches the rag module.
    """
    if deployed is None:
        return user_message
    modules = getattr(deployed, "modules", {}) or {}
    ws_mod = modules.get("workspace")

    try:
        from digitorn.core.file_store import get_file_store
        file_store = get_file_store()
        refs = file_store.list_refs(session_id)
    except Exception:
        refs = []
    if not refs:
        return user_message

    new_refs = [r for r in refs if not getattr(r, "announced", False)]
    if not new_refs:
        return user_message

    cached_texts: list[tuple[Any, str]] = [
        (ref, getattr(ref, "extracted_text", "") or "")
        for ref in new_refs
    ]
    total_chars = sum(len(t) for _, t in cached_texts)

    mode = _resolve_attachments_mode(deployed)

    new_ids = [getattr(r, "file_id", "") for r in new_refs]

    if mode == "tool" and ws_mod is not None:
        block = _format_tool_mode_block(cached_texts)
        file_store.mark_announced(new_ids, session_id)
        return block + user_message

    if total_chars > 0:
        block = _format_full_inject_block(cached_texts)
        file_store.mark_announced(new_ids, session_id)
        return block + user_message

    # No extracted text on any ref - surface the bare manifest so
    # the agent at least knows files are attached.
    block = _format_rag_context_block(new_refs, [])
    file_store.mark_announced(new_ids, session_id)
    return block + user_message


def _resolve_attachments_mode(deployed: Any) -> str:
    """Read `attachments_mode` from the deployed app's meta.

    Defaults to `"direct"` when the field is missing (older
    bundles) or the value is unrecognised. The legacy values
    `inject` / `hybrid` / `auto` all map to `direct` so old
    YAMLs keep working without a migration.
    """
    try:
        meta = getattr(getattr(deployed, "compiled", None), "meta", None)
        mode = (getattr(meta, "attachments_mode", "") or "").lower()
        if mode == "tool":
            return "tool"
        # `direct` and every legacy alias collapse to direct.
        return "direct"
    except Exception:
        return "direct"


TRANSIENT_OPEN = "<<<DIGITORN_TRANSIENT_BLOCK>>>"
TRANSIENT_CLOSE = "<<<END_DIGITORN_TRANSIENT_BLOCK>>>"


def _wrap_transient(body: str) -> str:
    return f"{TRANSIENT_OPEN}\n{body}{TRANSIENT_CLOSE}\n"


def _format_tool_mode_block(cached_texts: list[tuple[Any, str]]) -> str:
    """Render the tool-mode manifest + STRONG instructions.

    The block is written assuming the LLM might mistake WsRead for a
    user-facing command. We hammer the fact that `WsRead` is a
    tool the assistant ITSELF must invoke - never instruct the user
    to call it - because models routinely default to "ask the user
    to do it" when in doubt.
    """
    lines: list[str] = []
    file_rows: list[str] = []
    first_path: str | None = None
    first_line_count = 0
    for ref, text in cached_texts:
        name = getattr(ref, "original_name", "") or getattr(ref, "file_id", "")
        mime = getattr(ref, "mime", "")
        size = getattr(ref, "size", 0)
        fmt = getattr(ref, "format", "") or ""
        line_count = text.count("\n") + 1 if text else 0
        meta_bits = [
            _format_size(size),
            f"{line_count} lines" if line_count else "",
            f"format={fmt.lstrip('.')}" if fmt else "",
        ]
        meta = ", ".join(b for b in meta_bits if b)
        safe = _sanitise_attachment_name(name)
        path = f"attachments/{safe}"
        file_rows.append(f"- {path}  ({mime}; {meta})")
        if first_path is None:
            first_path = path
            first_line_count = line_count

    persistent: list[str] = []
    persistent.append("[Attached files]")
    persistent.append(
        "The following files are attached to this chat session. "
        "You can load any of them with `WsRead(path)`, using "
        "the exact paths below."
    )
    persistent.extend(file_rows)
    persistent.append("[/Attached files]")
    persistent.append("")

    transient: list[str] = []
    transient.append("[Attached files - how to read]")
    transient.append(
        f"{len(cached_texts)} new file(s) attached. Their content "
        f"is not yet in your context. Call `WsRead(path)` ONCE "
        f"per file you actually need to answer the user's "
        f"question, then answer from that content. Never instruct "
        f"the user to call WsRead - they cannot."
    )
    transient.append(
        "If you already called `WsRead` on a file earlier in "
        "this conversation, REUSE the prior tool result from your "
        "message history. Only re-call WsRead when the user "
        "explicitly asks for a different slice (`offset` / "
        "`limit`) of the same file."
    )
    transient.append("")
    transient.append("How `WsRead` works:")
    transient.append("  - Signature: `WsRead(path, offset?, limit?)`.")
    transient.append(
        "  - `path` MUST start with `attachments/` and match "
        "one of the paths in the [Attached files] block above. "
        "No leading slash, no `./`, no absolute disk path."
    )
    transient.append(
        "  - `offset` (0-indexed line number) and `limit` "
        "(line count) are OPTIONAL. Omit both to receive the "
        "WHOLE file. Add them only when paginating a huge doc."
    )
    transient.append(
        "  - PDFs / DOCX / PPTX / ODT / ODS / RTF were already "
        "parsed to text - you receive readable text, never raw "
        "bytes."
    )
    transient.append("")
    if first_path is not None:
        transient.append("Worked example:")
        if first_line_count and first_line_count > 400:
            transient.append(
                f"  user: \"que dit ce fichier ?\"\n"
                f"  -> emit tool call: WsRead("
                f"path=\"{first_path}\", offset=0, limit=400)\n"
                f"  -> read result.content, paginate if needed.\n"
                f"  -> answer with [{first_path.rsplit('/', 1)[-1]} "
                f"· lines 0-400]"
            )
        else:
            transient.append(
                f"  user: \"que dit ce fichier ?\"\n"
                f"  -> emit tool call: WsRead("
                f"path=\"{first_path}\")\n"
                f"  -> answer with [{first_path.rsplit('/', 1)[-1]}]"
            )
        transient.append("")
    transient.append("Avoid:")
    transient.append(
        "  - Telling the user to run WsRead(...) themselves."
    )
    transient.append(
        "  - Guessing the file's content from its name."
    )
    transient.append(
        "  - Calling WsRead with a path NOT in the list above."
    )
    transient.append("[/Attached files - how to read]")
    transient.append("")

    return "\n".join(persistent) + "\n" + _wrap_transient(
        "\n".join(transient) + "\n"
    )


def _sanitise_attachment_name(name: str) -> str:
    """Mirror of `WorkspaceModule.register_attachment`'s filter so
    the dispatch's manifest paths match what the workspace actually
    registered. Keeps the agent's WsRead calls deterministic."""
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9._\-() ]+", "_", name or "").strip("._ ")
    return safe or "file"


def _format_full_inject_block(
    cached_texts: list[tuple[Any, str]],
) -> str:
    """Render the WHOLE extracted text of every attached file.

    Each file is fenced with `=== BEGIN/END <name> ===` markers
    so the LLM can emit precise per-file citations. This is the
    block used in `direct` mode (default).
    """
    lines: list[str] = []
    lines.append("[Attached files context]")
    lines.append(f"Session files ({len(cached_texts)}):")
    for ref, _ in cached_texts:
        name = getattr(ref, "original_name", "") or getattr(ref, "file_id", "")
        mime = getattr(ref, "mime", "")
        size = getattr(ref, "size", 0)
        lines.append(f"- {name} ({mime}, {_format_size(size)})")

    lines.append("")
    lines.append("Full document content:")
    for ref, text in cached_texts:
        name = getattr(ref, "original_name", "") or getattr(ref, "file_id", "")
        if not text:
            continue
        lines.append("")
        lines.append(f"=== BEGIN {name} ===")
        lines.append(text.rstrip())
        lines.append(f"=== END {name} ===")

    lines.append("")
    lines.append(
        "Citation rules:"
        " quote facts from the documents above and cite the source"
        " filename in square brackets, e.g. [contract.pdf]."
        " Never invent quotes that aren't in the documents."
        " If something isn't in the documents, say so plainly."
    )
    lines.append("[/Attached files context]")
    lines.append("")
    return _wrap_transient("\n".join(lines) + "\n")


def _format_rag_context_block(refs: list[Any], hits: list[Any]) -> str:
    """Render a bare-manifest fallback block.

    Used when there's no extracted text to inject (e.g. all files
    failed extraction). Keeps the agent aware that files WERE
    uploaded even though it can't read them. The `hits` parameter
    is kept for call-site compatibility but always empty in the
    current implementation - the historical RAG-retrieval fallback
    has been retired in favour of the binary `direct` / `tool`
    split.
    """
    lines: list[str] = []
    lines.append("[Attached files context]")
    lines.append(f"Session files ({len(refs)}):")
    for ref in refs:
        name = getattr(ref, "original_name", "") or getattr(ref, "file_id", "")
        mime = getattr(ref, "mime", "")
        size = getattr(ref, "size", 0)
        lines.append(f"- {name} ({mime}, {_format_size(size)})")
    lines.append("")
    lines.append(
        "(No extracted content available - the files were uploaded "
        "but the parser did not return any text. Ask the user to "
        "share the content directly if you need it.)"
    )
    lines.append("[/Attached files context]")
    lines.append("")
    return _wrap_transient("\n".join(lines) + "\n")


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


class TurnSource(str, Enum):
    """Where the dispatch was triggered from. Used as a log
    breadcrumb and to gate behaviour: `RESUME` skips the queue
    flip / chain because the resume drain loop manages its own
    state; `FAST` and `DRAIN` let dispatch_turn flip + chain
    internally.
    """

    FAST = "fast"
    DRAIN = "drain"
    RESUME = "resume"


class TurnStatus(str, Enum):
    """Outcome of a single dispatch_turn() call.

    PAUSED is non-terminal: the queue row stays alive, the daemon
    halts the chain, and an external signal (credential grant,
    abort) is required to advance. COMPLETED / FAILED / CANCELLED
    are terminal and the caller writes them to the queue row.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@dataclass(frozen=True)
class TurnEntry:
    """Immutable description of a turn to dispatch. Built by each
    caller from its native input shape (POST body / queue row).
    """

    correlation_id: str
    message: str
    workspace: str | None = None
    image_refs: list[dict[str, Any]] | None = None
    client_message_id: str = ""
    queue_row_id: str = ""
    position: int = 0
    template_system_prompt: str = ""
    mode_id: str = ""


@dataclass(frozen=True)
class TurnOutcome:
    """What dispatch_turn returned. Callers branch on `status` to
    decide whether the queue row needs additional work (PAUSED), or
    just to verify the chain advanced (the chain is now scheduled
    inside dispatch_turn itself - see step 6 of the redesign).
    """

    status: TurnStatus
    error_code: str = ""
    error_data: dict[str, Any] | None = None
    paused_reason: str = ""
    interrupted: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    next_entry: Any = None


_CREDENTIAL_CODES: frozenset[str] = frozenset({
    "credential_required",
    "credential_auth_required",
})

_HEARTBEAT_INTERVAL_S = 30

_CHAIN_TASKS: set[asyncio.Task] = set()


def _log_level_for(code: str) -> int:
    """Pick a log level for a given error code. Credential gates are
    routine UX, not crashes - log at INFO so prod dashboards stay
    clean. Anything else gets the full ERROR + traceback treatment.
    """

    if code in _CREDENTIAL_CODES:
        return logging.INFO
    return logging.ERROR


async def _start_heartbeat(queue_row_id: str) -> asyncio.Task | None:
    """Pulse `_mq.heartbeat(row_id)` every `_HEARTBEAT_INTERVAL_S` to
    keep the lease alive. No-op when there is no queue row (pure
    fast-path with no DB persistence). Returns the task so the caller
    can cancel it in a finally.
    """

    if not queue_row_id:
        return None
    from digitorn.core.app import message_queue as _mq

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
                await _mq.heartbeat(queue_row_id)
            except asyncio.CancelledError:
                return
            except Exception:
                # Heartbeat misses are non-fatal; lease will be reaped
                # by `reap_expired_leases` on the next sweep.
                continue

    return asyncio.create_task(_loop())


def _emit(event_bus: Any, type_: str, *, app_id: str, session_id: str,
          user_id: str, correlation_id: str, op_state: Any,
          payload: dict[str, Any] | None = None) -> Any:
    """Thin wrapper around `event_bus.emit(_turn_event(...))` that
    swallows publish exceptions. The bus's own retry / fallback policy
    is the right layer to handle delivery failure - dispatch_turn
    must not abort a turn because an event couldn't be published.
    """

    return event_bus.emit(_turn_event(
        type_,
        app_id=app_id,
        session_id=session_id,
        user_id=user_id,
        correlation_id=correlation_id,
        op_state=op_state,
        payload=payload,
    ))


async def dispatch_turn(
    request: "Request | None",
    app_id: str,
    session_id: str,
    *,
    entry: TurnEntry,
    user_id: str = "local",
    source: TurnSource = TurnSource.FAST,
    manager: Any | None = None,
    deployed: Any | None = None,
    credential_store: Any | None = None,
) -> TurnOutcome:
    """Run one chat turn end-to-end and return its outcome.

    Two call shapes are supported:

    1. **HTTP-driven** (POST /messages, drain chain) - pass `request`;
       `manager`, `deployed`, `credential_store` are auto-resolved
       from `request.app.state`.
    2. **Background** (Socket.IO resume drain) - pass `request=None`
       and the resolved `manager` (and optionally `deployed` /
       `credential_store`) directly. `_inc_agent_turns` is skipped
       since it's tied to the HTTP request lifecycle.

    Responsibilities (single source of truth for all three call sites):

    1. Emit `message_started` to anchor the turn on the client.
    2. Start a heartbeat for the queue row, if any.
    3. Pre-flight credential check; promote `CredentialMissing` /
       `CredentialAuthRequired` to `credential_required` /
       `credential_auth_required` events. Return `PAUSED` so the
       caller halts the queue chain and waits for a grant.
    4. Invoke `manager.chat()`.
    5. Detect mid-turn cancellation (`session.interrupted`).
    6. On normal exit, emit `message_done`; on failure, classify the
       exception, promote credential codes to the matching event type,
       emit, and return `FAILED` with the structured error payload.

    What this function does NOT own (callers handle):

    * Writing the queue row's terminal status (caller decides whether
      it has a row, and uses `_mq.finish_and_drain` to chain).
    * Re-reserving the session for the next entry in a chain.
    * The decision to chain vs stop on PAUSED.
    * `release_session` / `_active_sessions` bookkeeping (caller
      orchestrates this around the dispatch to close the
      "queued right after turn ends" race - see §11 of the design doc).
    """

    if request is not None:
        from ._shared import _get_manager, _get_deployed
        if manager is None:
            manager = _get_manager(request)
        if deployed is None:
            deployed = _get_deployed(request, app_id)
        if credential_store is None:
            credential_store = getattr(
                request.app.state, "credential_store", None,
            )
    if manager is None:
        raise RuntimeError(
            "dispatch_turn: `manager` is required when `request` is None",
        )
    bus = manager.event_bus

    op_state_module = _import_op_state()
    _OS = op_state_module

    if request is not None:
        await _inc_agent_turns(request)
    heartbeat_task = await _start_heartbeat(entry.queue_row_id)

    interrupted = False
    try:
        try:
            await _emit(
                bus, "message_started",
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.RUNNING,
                payload={
                    "correlation_id": entry.correlation_id,
                    "session_id": session_id,
                    "position": entry.position,
                    "fast_path": source == TurnSource.FAST,
                    "source": source.value,
                },
            )
        except Exception as exc:
            logger.debug("_dispatch best-effort block failed: %s", exc)

        byok_on = False
        byok_overrides: dict[str, dict[str, Any]] = {}
        cred_check_done = False
        _cache_key = (user_id or "local", app_id)
        _cached = _byok_cache.get(_cache_key)
        if _cached is not None and (time.monotonic() - _cached[0]) < _BYOK_CACHE_TTL_S:
            byok_on = _cached[1]
            byok_overrides = _cached[2]
            cred_check_done = _cached[3]
        else:
            try:
                if deployed is not None:
                    from digitorn.core.credentials.byok_store import (
                        build_byok_overrides_for_app,
                        is_byok_enabled,
                    )
                    byok_on = await is_byok_enabled(user_id, app_id)
                    if byok_on:
                        byok_overrides = await build_byok_overrides_for_app(
                            compiled=deployed.compiled, user_id=user_id,
                        )
            except Exception as exc:
                logger.debug(
                    "byok_lookup_failed app=%s user=%s: %s",
                    app_id, user_id, exc,
                )

        try:
            if deployed is not None and byok_on and not cred_check_done:
                from digitorn.core.credentials import (
                    ensure_user_credentials_for_app,
                )
                await ensure_user_credentials_for_app(
                    deployed_app=deployed,
                    user_id=user_id,
                    credential_store=credential_store,
                    byok_overrides=byok_overrides or None,
                )
                cred_check_done = True
            _byok_cache[_cache_key] = (
                time.monotonic(), byok_on, byok_overrides, cred_check_done,
            )
        except asyncio.CancelledError:
            raise
        except Exception as cred_exc:
            outcome = await _on_dispatch_exception(
                cred_exc, bus,
                app_id=app_id, session_id=session_id, user_id=user_id,
                entry=entry, _OS=_OS, pre_chat=True,
            )
            if outcome.status == TurnStatus.PAUSED:
                # No queue flip - caller marks the row failed (Step 5).
                return outcome
            return await _finalize_failed(
                outcome, bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        message_for_llm = await _maybe_inject_rag_context(
            deployed, session_id, entry.message,
        )
        try:
            await manager.chat(
                app_id, session_id, message_for_llm,
                user_id=user_id,
                workspace=entry.workspace,
                image_refs=entry.image_refs or None,
                correlation_id=entry.correlation_id or None,
                client_message_id=entry.client_message_id or None,
                template_system_prompt=entry.template_system_prompt,
                mode_id=entry.mode_id or None,
            )
        except asyncio.CancelledError:
            interrupted = True
            raise
        except Exception as exc:
            outcome = await _on_dispatch_exception(
                exc, bus,
                app_id=app_id, session_id=session_id, user_id=user_id,
                entry=entry, _OS=_OS, pre_chat=False,
            )
            if outcome.status == TurnStatus.PAUSED:
                return outcome
            return await _finalize_failed(
                outcome, bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        # 4. Mid-turn cancellation flag (set by abort handler).
        try:
            sess = await manager.get_session(
                app_id, session_id, user_id=user_id,
            )
            if sess is not None and getattr(sess, "interrupted", False):
                interrupted = True
        except Exception as exc:
            logger.debug("_dispatch best-effort block failed: %s", exc)

        if interrupted:
            return await _finalize_terminal(
                bus, app_id=app_id, session_id=session_id,
                user_id=user_id, entry=entry, _OS=_OS, source=source,
                status=TurnStatus.CANCELLED,
                terminal_event="message_cancelled",
                terminal_op_state=_OS.CANCELLED,
                queue_terminal="cancelled",
                queue_error_code="turn_cancelled",
                interrupted=True,
                request=request, manager=manager,
                deployed=deployed, credential_store=credential_store,
            )

        # 5. Success.
        return await _finalize_terminal(
            bus, app_id=app_id, session_id=session_id,
            user_id=user_id, entry=entry, _OS=_OS, source=source,
            status=TurnStatus.COMPLETED,
            terminal_event="message_done",
            terminal_op_state=_OS.COMPLETED,
            queue_terminal="completed",
            queue_error_code="",
            request=request, manager=manager,
            deployed=deployed, credential_store=credential_store,
        )

    except asyncio.CancelledError:
        try:
            await _emit(
                bus, "message_cancelled",
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.CANCELLED,
                payload={
                    "correlation_id": entry.correlation_id,
                    "session_id": session_id,
                    "reason": "task_cancelled",
                },
            )
        except Exception as exc:
            logger.debug("_dispatch best-effort block failed: %s", exc)
        await _emit_turn_terminal(
            bus, app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id, status="cancelled",
            _OS=_OS,
        )
        raise
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        if request is not None:
            try:
                await _inc_agent_turns(request, -1)
            except Exception as exc:
                logger.debug("_dispatch best-effort block failed: %s", exc)


async def _on_dispatch_exception(
    exc: Exception,
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    pre_chat: bool,
) -> TurnOutcome:
    """Classify an exception thrown during dispatch_turn, log it at
    the appropriate level, emit the matching event (with credential
    promotion), and return the structured outcome.

    `pre_chat=True` means the exception came from the pre-flight
    credential resolver (so we know the chat() never started). That
    information surfaces in the outcome so the caller can decide
    whether to retry the same entry on resume vs treat it as a real
    failure - today the only difference is the `paused_reason`
    string, kept so future call sites can branch on it cleanly.
    """

    error_data = _classify_error(exc)
    code = error_data.get("code") or ""
    log_level = _log_level_for(code)

    if log_level == logging.INFO:
        logger.info(
            "turn_paused app=%s session=%s code=%s pre_chat=%s",
            app_id, session_id, code, pre_chat,
        )
    else:
        logger.log(
            log_level,
            "turn_failed app=%s session=%s code=%s pre_chat=%s: %s",
            app_id, session_id, code, pre_chat, exc,
            exc_info=(log_level >= logging.ERROR),
        )

    if code in _CREDENTIAL_CODES:
        evt_type = code
        try:
            await _emit(
                bus, evt_type,
                app_id=app_id, session_id=session_id, user_id=user_id,
                correlation_id=entry.correlation_id,
                op_state=_OS.WAITING_APPROVAL,
                payload={
                    **error_data,
                    "correlation_id": entry.correlation_id,
                },
            )
        except Exception as exc:
            logger.debug("_dispatch best-effort block failed: %s", exc)
        return TurnOutcome(
            status=TurnStatus.PAUSED,
            error_code=code,
            error_data=error_data,
            paused_reason="credential_missing"
            if code == "credential_required"
            else "credential_auth_required",
        )

    # Generic failure path.
    try:
        await _emit(
            bus, "error",
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=_OS.FAILED,
            payload={
                **error_data,
                "correlation_id": entry.correlation_id,
            },
        )
    except Exception as exc:
        logger.debug("_dispatch best-effort block failed: %s", exc)
    return TurnOutcome(
        status=TurnStatus.FAILED,
        error_code=code or "internal",
        error_data=error_data,
    )


async def _flip_queue_row(
    *,
    session_id: str,
    entry: TurnEntry,
    source: TurnSource,
    terminal_status: str,
    error_code: str,
) -> Any:
    """Atomically transition the row to its terminal state and pop the
    next queued row for the session. Returns the next entry (or None).

    Skipped when:

    * `source == RESUME` - the resume drain loop owns its own queue
      flips via `mark_done` / `mark_failed`; if dispatch_turn also
      flipped, the loop's next `next_queued` would skip every other
      entry.
    * `entry.queue_row_id` is empty - pure fast-path with no DB row
      to flip (no queue infrastructure involved at all).
    """

    if source == TurnSource.RESUME:
        return None
    if not entry.queue_row_id:
        return None
    from digitorn.core.app import message_queue as _mq
    try:
        return await _mq.finish_and_drain(
            session_id, entry.queue_row_id,
            terminal_status=terminal_status,
            error_code=error_code,
        )
    except Exception as exc:
        logger.warning(
            "queue_flip_failed app=%s sid=%s row=%s: %s",
            "<unknown>", session_id, entry.queue_row_id, exc,
        )
        return None


async def _resolve_awaiter(
    correlation_id: str, status: TurnStatus, error_code: str,
) -> None:
    """Unblock any `mode=wait` POST caller that's awaiting this
    correlation_id. Resolved on COMPLETED / CANCELLED, failed on
    FAILED, no-op on PAUSED (caller still has work to do).
    """

    if not correlation_id:
        return
    from digitorn.core.app import message_queue as _mq
    try:
        if status == TurnStatus.COMPLETED:
            _mq.resolve_awaiter(correlation_id, {"status": "completed"})
        elif status == TurnStatus.CANCELLED:
            _mq.resolve_awaiter(correlation_id, {"status": "cancelled"})
        elif status == TurnStatus.FAILED:
            _mq.fail_awaiter(
                correlation_id,
                RuntimeError(error_code or "internal"),
            )
    except Exception as exc:
        logger.debug("_dispatch best-effort block failed: %s", exc)


def _schedule_chain(
    request: "Request | None",
    app_id: str,
    session_id: str,
    *,
    next_entry: Any,
    user_id: str,
    manager: Any,
    deployed: Any,
    credential_store: Any,
) -> None:
    """If a next queued row was popped by the atomic flip, schedule the
    next `dispatch_turn` as a fire-and-forget task so the chain runs
    concurrently with the current turn's caller returning. `source`
    flips to `DRAIN` on the chained call - the new dispatch will own
    its own `message_started` emit, queue flip, and terminal events.
    """

    if next_entry is None:
        return
    next_dispatch_entry = TurnEntry(
        correlation_id=getattr(next_entry, "correlation_id", "") or "",
        message=getattr(next_entry, "message", "") or "",
        image_refs=getattr(next_entry, "image_refs", None) or None,
        queue_row_id=getattr(next_entry, "id", "") or "",
        position=getattr(next_entry, "position", 0) or 0,
        template_system_prompt=getattr(
            next_entry, "template_system_prompt", "",
        ) or "",
    )

    async def _run_chained() -> None:
        try:
            await dispatch_turn(
                request, app_id, session_id,
                entry=next_dispatch_entry,
                user_id=user_id,
                source=TurnSource.DRAIN,
                manager=manager,
                deployed=deployed,
                credential_store=credential_store,
            )
        except Exception as exc:
            logger.warning(
                "chain_dispatch_failed app=%s sid=%s: %s",
                app_id, session_id, exc,
            )

    _chain_task = asyncio.create_task(_run_chained())
    _CHAIN_TASKS.add(_chain_task)
    _chain_task.add_done_callback(_CHAIN_TASKS.discard)


async def _emit_turn_terminal(
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    correlation_id: str,
    status: str,
    _OS: Any,
) -> None:
    """Emit the daemon-resource-protocol consolidated turn terminal.

    Fires AFTER the existing `message_done` / `message_failed` /
    `message_cancelled` emissions, regardless of which path ended
    the turn (success, failure, abort, daemon shutdown). The single
    purpose of this event is to give the client one authoritative
    signal it can use to sweep all of its turn-scoped local state at
    once: spinner, agent animations, background-task overlays - all
    flipped to terminal in a single reducer pass.

    Why a separate event rather than overload `message_done`:
      - Decoupled from the chat bubble lifecycle. A future protocol
        revision can change the message events without affecting the
        turn-state sync layer.
      - Idempotent: late events arriving with seq < this event's seq
        get dropped client-side via `lastTerminalSeq[turn_id]`.
        Putting that semantic on a dedicated event-type makes the
        client guard trivially specific (no need to disambiguate
        across 3 message_* variants).
      - Survives partial cleanup paths. The legacy path emitted
        `message_done` only on success / handled failure. Abort
        and daemon-shutdown paths never reached it. We emit
        `turn_terminal` from EVERY terminal branch (including the
        `CancelledError` re-raise and the watchdog fallbacks) so
        the client never has to wonder "did the turn really end?".

    Status codes: `completed` | `failed` | `cancelled` |
    `interrupted`. `correlation_id` doubles as the turn id (each
    turn gets a fresh one assigned at message accept time).
    """
    try:
        await _emit(
            bus, "turn_terminal",
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=correlation_id,
            op_state=_OS.COMPLETED,
            payload={
                "correlation_id": correlation_id,
                "session_id": session_id,
                "status": status,
            },
        )
    except Exception as exc:
        logger.debug("_dispatch best-effort block failed: %s", exc)


async def _persist_turn_error(
    app_id: str,
    session_id: str,
    error_data: dict[str, Any] | None,
) -> None:
    """Write the classified turn error onto the canonical session so
    poll-based clients (dev CLI, plain REST) surface it via summary().
    SSE clients already receive the live `error` event. Passing None
    clears the slot after a clean turn. Also feeds the session-metrics
    error counter, which nothing else was wiring.
    """
    try:
        from digitorn.core.runtime.session_store import get_default_bridge
        st = get_default_bridge().store.state(session_id)
        if st is not None:
            st.last_error = error_data
    except Exception:
        logger.debug("persist_turn_error: session write failed", exc_info=True)
    if error_data:
        try:
            from digitorn.core.runtime.session_metrics import get_session_metrics
            msg = str(
                error_data.get("error")
                or error_data.get("detail")
                or error_data.get("code")
                or "error"
            )
            get_session_metrics(app_id, session_id).record_error(msg)
        except Exception:
            logger.debug("persist_turn_error: metrics failed", exc_info=True)


async def _finalize_failed(
    outcome: TurnOutcome,
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    source: TurnSource,
    request: "Request | None" = None,
    manager: Any = None,
    deployed: Any = None,
    credential_store: Any = None,
) -> TurnOutcome:
    """Order matters here. The race "queued just after turn ends"
    (Bug B) is closed by doing the queue terminal flip FIRST, then
    emitting the spinner-stop event. Otherwise the client receives
    `message_done`, immediately POSTs a new message, and the daemon's
    `is_turn_running` is still True because the row hasn't flipped
    yet → the new message gets queued and the user sees the QUEUED
    flicker.
    """

    await _persist_turn_error(app_id, session_id, outcome.error_data)

    next_entry = await _flip_queue_row(
        session_id=session_id, entry=entry, source=source,
        terminal_status="failed",
        error_code=outcome.error_code or "internal",
    )
    await _resolve_awaiter(
        entry.correlation_id, TurnStatus.FAILED, outcome.error_code,
    )
    try:
        await _emit(
            bus, "message_done",
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=_OS.COMPLETED,
            payload={
                "correlation_id": entry.correlation_id,
                "session_id": session_id,
            },
        )
    except Exception as exc:
        logger.debug("_dispatch best-effort block failed: %s", exc)
    await _emit_turn_terminal(
        bus, app_id=app_id, session_id=session_id, user_id=user_id,
        correlation_id=entry.correlation_id, status="failed", _OS=_OS,
    )
    _schedule_chain(
        request, app_id, session_id,
        next_entry=next_entry, user_id=user_id,
        manager=manager, deployed=deployed,
        credential_store=credential_store,
    )
    return TurnOutcome(
        status=outcome.status,
        error_code=outcome.error_code,
        error_data=outcome.error_data,
        paused_reason=outcome.paused_reason,
        interrupted=outcome.interrupted,
        extras=outcome.extras,
        next_entry=next_entry,
    )


async def _finalize_terminal(
    bus: Any,
    *,
    app_id: str,
    session_id: str,
    user_id: str,
    entry: TurnEntry,
    _OS: Any,
    source: TurnSource,
    status: TurnStatus,
    terminal_event: str,
    terminal_op_state: Any,
    queue_terminal: str,
    queue_error_code: str,
    interrupted: bool = False,
    request: "Request | None" = None,
    manager: Any = None,
    deployed: Any = None,
    credential_store: Any = None,
) -> TurnOutcome:
    """Same race-fix shape as `_finalize_failed`, generalised for
    COMPLETED and CANCELLED outcomes.
    """

    next_entry = await _flip_queue_row(
        session_id=session_id, entry=entry, source=source,
        terminal_status=queue_terminal,
        error_code=queue_error_code,
    )
    await _resolve_awaiter(entry.correlation_id, status, queue_error_code)
    try:
        payload = {
            "correlation_id": entry.correlation_id,
            "session_id": session_id,
        }
        await _emit(
            bus, terminal_event,
            app_id=app_id, session_id=session_id, user_id=user_id,
            correlation_id=entry.correlation_id,
            op_state=terminal_op_state,
            payload=payload,
        )
    except Exception as exc:
        logger.debug("_dispatch best-effort block failed: %s", exc)
    if interrupted:
        _terminal_status = "interrupted"
    elif terminal_event == "message_cancelled":
        _terminal_status = "cancelled"
    else:
        _terminal_status = "completed"
    await _emit_turn_terminal(
        bus, app_id=app_id, session_id=session_id, user_id=user_id,
        correlation_id=entry.correlation_id, status=_terminal_status,
        _OS=_OS,
    )
    _schedule_chain(
        request, app_id, session_id,
        next_entry=next_entry, user_id=user_id,
        manager=manager, deployed=deployed,
        credential_store=credential_store,
    )
    return TurnOutcome(
        status=status,
        interrupted=interrupted,
        next_entry=next_entry,
    )


def _import_op_state() -> Any:
    """Lazy import of `OpState` so this module's top-level imports
    stay tiny - the events package pulls in the pydantic model graph
    which we don't want to drag in at app boot before this function
    is actually called.
    """

    from digitorn.core.events.envelope import OpState
    return OpState
