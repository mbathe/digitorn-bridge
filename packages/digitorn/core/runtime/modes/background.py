"""Background mode - daemon waiting for triggers.

If the ``channels`` module is loaded, it handles all trigger types
(cron, watch, http, email, rss, queue) via its adapter system.
Otherwise, falls back to the legacy trigger loops for backward
compatibility.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import logging
import time
from pathlib import Path
from typing import Any

from digitorn.core.runtime.agent_loop import agent_turn
from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)


# Context-local reference to the activation event recorder for the
# CURRENT running activation. Code anywhere in the runtime (channel
# registry, modules, hooks) can read it via ``get_current_recorder()``
# and push ``channel_sent`` / custom events into the timeline without
# having to thread the recorder through every function signature.
#
# The background runtime sets this at the start of each activation and
# resets it at the end - the contextvar scope is per-task so concurrent
# activations never leak into each other.
_CURRENT_ACTIVATION_RECORDER: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "digitorn_activation_recorder", default=None,
)


def get_current_activation_recorder() -> Any:
    """Return the recorder for the currently running background activation.

    Returns ``None`` in any non-background context, so call sites MUST
    check for None before emitting events::

        rec = get_current_activation_recorder()
        if rec is not None:
            await rec.record_channel_sent("email", target, success=True)
    """
    return _CURRENT_ACTIVATION_RECORDER.get()


# ── Activation event recorder ───────────────────────────────────────
#
# Wraps the caller-supplied on_tool_call / on_thinking callbacks so
# that every event an agent_turn emits during a background activation
# is ALSO persisted to the activation_events table, keyed by the
# current activation_id. The dashboard drawer reads this table to
# render the step-by-step timeline of a run.
#
# The recorder is a thin, failure-tolerant layer: if the DB is down or
# the INSERT fails, the live activation is NOT interrupted. Telemetry
# is a "nice to have", not a correctness requirement.

# Tool actions that produce files - normalised into "artifact" events
# on top of the raw tool_call record, so the UI can show a dedicated
# "Artifacts" section without having to parse tool_call params itself.
#
# IMPORTANT: the ``on_tool_call`` callback receives the tool name in
# whatever form the LLM emitted it - usually the Claude-Code-style
# short name ("Write", "Edit", "NotebookAdd") rather than the FQN
# ("filesystem.write"). We normalise to FQN via ``to_fqn()`` before
# doing the membership check, and we keep the allowlist in FQN form
# because it's the canonical, stable identifier.
_FILE_WRITE_ACTIONS = frozenset({
    "filesystem.write",
    "filesystem.edit",
    "filesystem.create",
    "notebook.write",
    "notebook.edit_cell",
    "notebook.add_cell",
    "spreadsheet.create",
    "spreadsheet.edit",
    "spreadsheet.write",
    "pdf.create",
    "pdf.write",
    "presentation.create",
})


# Per-file cap for daemon-side payload loading. Bigger files are
# recorded as a note but their bytes are NOT shipped to the LLM -
# otherwise a 200 MB video in a session would blow up every activation.
_PAYLOAD_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

# MIME types we treat as plain text and inline verbatim. Anything else
# that decodes as UTF-8 is also treated as text as a fallback.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset({
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/toml",
    "application/javascript",
    "application/x-javascript",
    "application/x-sh",
    "application/x-python",
})


def _mime_is_text(mime: str) -> bool:
    mime = (mime or "").lower()
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    return mime in _TEXT_MIME_EXACT


def _decode_as_text(data: bytes) -> str | None:
    """Return the UTF-8 decoding of ``data`` iff it looks like real text.

    We refuse to inline bytes that decode but contain a lot of NUL /
    control characters (usually garbled binary) - those go through the
    binary-note path instead.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Crude but effective: reject if >1% of characters are C0 controls
    # outside the usual \t\n\r set.
    if text:
        bad = sum(1 for c in text if ord(c) < 32 and c not in "\t\n\r")
        if bad / len(text) > 0.01:
            return None
    return text


def _get_credential_store_for_activation(ctx: AgentContext) -> Any:
    """Best-effort lookup of the daemon's ``CredentialStore`` from an AgentContext.

    The credential store is attached to the FastAPI app state at
    daemon startup (``app.state.credential_store``) and a reference
    is stashed on the ``AppManager`` instance. Agent contexts are
    built from the manager, so we can follow the chain:

        ctx.runtime_app  →  manager  →  credential_store

    Returns ``None`` if the chain doesn't resolve - for instance in
    standalone CLI mode or in unit tests where no daemon is wired.
    In that case the runtime resolver becomes a no-op, which is the
    right behaviour: no store means no per-user secrets, fall back
    to whatever the compile-time dict already had.
    """
    # Direct reference on the context (set by some callers)
    store = getattr(ctx, "credential_store", None)
    if store is not None:
        return store

    # Walk via runtime_app → manager → credential_store
    runtime_app = (
        getattr(ctx, "runtime_app", None) or getattr(ctx, "_runtime_app", None)
    )
    if runtime_app is not None:
        manager = getattr(runtime_app, "manager", None) or getattr(
            runtime_app, "_manager", None,
        )
        if manager is not None:
            store = getattr(manager, "_credential_store", None)
            if store is not None:
                return store

    # Last-resort: look for a singleton stashed by the daemon's
    # lifespan. Some codepaths (tests, standalone CLI) never set this.
    try:
        from digitorn.core.app.manager_v2 import _manager_holder  # type: ignore
        mgr = _manager_holder.get("manager") if _manager_holder else None
        if mgr is not None:
            return getattr(mgr, "_credential_store", None)
    except Exception:
        pass
    return None


def _get_app_payload_schema(ctx: AgentContext, app_id: str) -> dict[str, Any] | None:
    """Look up the compiled ``payload_schema`` for the app currently running.

    The compiled schema lives on ``deployed.compiled.execution.payload_schema``
    in the AppManager. We try a few attribute paths because background
    mode is reached from both the legacy entry point (raw AgentContext)
    and the new app manager entry point (deployed app duck-typed onto
    ctx.runtime_app). Returns ``None`` when no schema can be resolved
    rather than raising - schema enforcement is a soft gate.
    """
    try:
        runtime_app = getattr(ctx, "runtime_app", None) or getattr(
            ctx, "_runtime_app", None,
        )
        if runtime_app is not None:
            compiled = getattr(runtime_app, "compiled", None)
            if compiled is not None:
                return getattr(compiled.execution, "payload_schema", None)
    except Exception:
        pass

    return None


def _build_payload_message_content(
    base_text: str, payload: dict[str, Any],
) -> str | list[dict[str, Any]]:
    """Fold a background session payload into the activation user message.

    Reads every attached file from disk **on the daemon**, classifies
    it, and returns either:

    - a plain string (no files, or only inlinable text) - cheap path,
      fully backwards compatible with legacy activations;
    - a multimodal Anthropic-style content list ``[{type:text}, {type:image}, {type:document}, ...]``
      when images or PDFs need to ride along.

    Classification
    ~~~~~~~~~~~~~~
    - ``image/*`` → base64 image block
    - ``application/pdf`` → base64 document block (Claude's native PDF path)
    - text MIME types or any file that cleanly decodes as UTF-8 → inlined
      verbatim in the text portion between ``--- name ---`` fences
    - anything else → a short "[skipped: name (mime, size)]" note so the
      agent is aware the file exists but its bytes can't be inlined

    The agent never runs ``filesystem.read`` on these paths - that
    keeps the system self-contained even when the daemon is remote
    (the client uploaded the files over HTTP, they only live on the
    daemon's disk, and the agent just consumes what we inject).
    """
    if not isinstance(payload, dict) or not payload:
        return base_text

    prompt = str(payload.get("prompt") or "").strip()
    files = payload.get("files") or []
    metadata = payload.get("metadata") or {}

    if not prompt and not files and not metadata:
        return base_text

    text_parts: list[str] = [base_text.rstrip(), "", "---"]

    if prompt:
        text_parts.append("User request:")
        text_parts.append(prompt)
        text_parts.append("")

    if metadata:
        text_parts.append("User preferences:")
        for k, v in metadata.items():
            text_parts.append(f"[{k}]: {v}")
        text_parts.append("")

    image_blocks: list[dict[str, Any]] = []
    document_blocks: list[dict[str, Any]] = []
    inlined_text_files: list[tuple[str, str]] = []
    notes: list[str] = []

    for f in files:
        if not isinstance(f, dict):
            continue
        name = f.get("name") or "file"
        path_str = f.get("path") or ""
        if not path_str:
            notes.append(f"{name}: no path")
            continue
        path = Path(path_str)
        if not path.is_file():
            notes.append(f"{name}: missing on disk")
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            notes.append(f"{name}: stat failed ({exc})")
            continue
        if size > _PAYLOAD_MAX_FILE_BYTES:
            notes.append(f"{name}: too large ({size} bytes, cap {_PAYLOAD_MAX_FILE_BYTES})")
            continue

        try:
            data = path.read_bytes()
        except OSError as exc:
            notes.append(f"{name}: unreadable ({exc})")
            continue

        mime = (f.get("mime_type") or "").lower()

        if mime.startswith("image/"):
            image_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
            continue

        if mime == "application/pdf":
            document_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
            continue

        # Text-like: inline verbatim
        text_content: str | None = None
        if _mime_is_text(mime):
            text_content = _decode_as_text(data)
        if text_content is None:
            # Fallback: if no MIME was provided, let UTF-8 decide.
            text_content = _decode_as_text(data)
        if text_content is not None:
            inlined_text_files.append((name, text_content))
            continue

        notes.append(f"{name}: binary ({mime or 'unknown'}, {size} bytes) not inlined")

    if inlined_text_files:
        text_parts.append("Attached files:")
        for name, body in inlined_text_files:
            text_parts.append(f"--- {name} ---")
            text_parts.append(body)
            text_parts.append(f"--- end {name} ---")
        text_parts.append("")

    if image_blocks or document_blocks:
        # Summary line so the agent knows non-text attachments exist.
        count_bits = []
        if image_blocks:
            count_bits.append(f"{len(image_blocks)} image(s)")
        if document_blocks:
            count_bits.append(f"{len(document_blocks)} PDF(s)")
        text_parts.append("Attached (below this message): " + ", ".join(count_bits))
        text_parts.append("")

    if notes:
        text_parts.append("Notes:")
        for n in notes:
            text_parts.append(f"- {n}")
        text_parts.append("")

    full_text = "\n".join(text_parts).rstrip() + "\n"

    if not image_blocks and not document_blocks:
        return full_text

    return [
        {"type": "text", "text": full_text},
        *image_blocks,
        *document_blocks,
    ]


def _resolve_tool_fqn(name: str) -> str:
    """Resolve any tool name form to its FQN (module.action).

    Short names ("Write"), double-underscored ("filesystem__write")
    and FQN ("filesystem.write") all become the FQN form. Falls back
    to the raw input on any resolver failure so a partial match is
    still possible.
    """
    try:
        from digitorn.core.runtime.tool_names import to_fqn
        return to_fqn(name)
    except Exception:
        return name


class _ActivationEventRecorder:
    """Wraps callbacks to also persist events into ActivationEvent.

    Usage::

        rec = _ActivationEventRecorder(store, activation_id)
        wrapped_on_tool_call = rec.wrap_on_tool_call(user_on_tool_call)
        wrapped_on_thinking = rec.wrap_on_thinking(user_on_thinking)
        await agent_turn(..., on_tool_call=wrapped_on_tool_call, on_thinking=wrapped_on_thinking)
        await rec.record_channel_sent("email", "alice@x.com", success=True)

    The recorder assigns a monotonically increasing sequence number to
    every event so the frontend can reliably sort them even when two
    events share the same wall-clock timestamp.
    """

    def __init__(self, activation_store: Any, activation_id: str) -> None:
        # ``activation_store`` parameter is kept for source-compat but
        # the recorder no longer uses it. Each ``_emit`` now constructs
        # a fresh ActivationStore on the persist_worker loop, where
        # ``get_session_factory()`` returns the worker's own factory
        # (via the contextvar override installed by ``run_async``).
        # This keeps every record_event() asyncpg INSERT off the
        # daemon's main loop -- critical on the hot path where one
        # background activation can emit dozens of events per turn
        # (tool_call, artifact, turn_boundary, channel_sent, ...).
        self._activation_id = activation_id
        self._sequence = 0
        self._lock = asyncio.Lock()
        # Retained for any external code that introspects the field;
        # not used internally any more.
        self._store = activation_store

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._sequence += 1
            seq = self._sequence
        activation_id = self._activation_id

        async def _do_record() -> None:
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
            store = ActivationStore(get_session_factory())
            await store.record_event(
                activation_id=activation_id,
                sequence=seq,
                event_type=event_type,
                data=data,
            )

        try:
            from digitorn.core.runtime.persist_worker import (
                get_default_worker,
            )
            await get_default_worker().run_async(_do_record)
        except Exception as exc:
            logger.debug(
                "activation_event_emit_failed type=%s: %s",
                event_type, exc,
            )

    async def record_turn_boundary(self, kind: str) -> None:
        """kind = 'turn_start' or 'turn_end' - emitted once per turn."""
        await self._emit(kind, {})

    async def record_channel_sent(
        self,
        channel_name: str,
        target: str,
        *,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        await self._emit("channel_sent", {
            "channel_name": channel_name,
            "target": target,
            "success": success,
            "error": error,
        })

    def wrap_on_tool_call(self, user_cb: Any | None) -> Any:
        """Return an async callback that records AND forwards to user_cb.

        Every tool call becomes a ``tool_call`` event. If the tool is in
        ``_FILE_WRITE_ACTIONS`` and succeeded, a duplicate ``artifact``
        event is ALSO written so the dashboard's Artifacts tab can show
        the file without having to re-parse tool_call params.
        """
        recorder = self

        async def _wrapped(name: str, params: dict, result: Any, call_id: str = "") -> None:
            # Resolve the tool name to its canonical FQN form. The agent
            # loop invokes on_tool_call with whatever name the LLM
            # emitted, which is usually the short Claude-Code-style
            # name ("Write") rather than the FQN ("filesystem.write").
            # Without this resolution, the artifact detection below
            # would NEVER fire.
            fqn_name = _resolve_tool_fqn(name)

            # Extract success / error from the tool result envelope.
            ok = True
            err = ""
            result_preview: Any = None
            if isinstance(result, dict):
                ok = result.get("success", True)
                err = str(result.get("error", "")) or ""
                # Keep a short preview so the drawer can show something
                # without dragging the full tool result payload around.
                if "content" in result:
                    result_preview = str(result.get("content"))[:500]
                elif "data" in result:
                    _d = result.get("data")
                    if isinstance(_d, (str, int, float, bool)):
                        result_preview = _d
                    elif isinstance(_d, dict):
                        result_preview = {
                            k: (str(v)[:200] if isinstance(v, str) else v)
                            for k, v in list(_d.items())[:5]
                        }
            elif hasattr(result, "success"):
                ok = result.success
                err = getattr(result, "error", "") or ""

            await recorder._emit("tool_call", {
                "call_id": call_id,
                "name": fqn_name,
                "short_name": name if name != fqn_name else None,
                "params": {
                    # Trim big string params so we don't blow up the DB
                    # with massive SQL queries or file contents.
                    k: (v[:500] + "…" if isinstance(v, str) and len(v) > 500 else v)
                    for k, v in (params or {}).items()
                },
                "success": ok,
                "error": err,
                "result_preview": result_preview,
            })

            # Artifact tracking - normalise file-producing tool calls.
            # Check against the FQN form so "Write", "filesystem.write"
            # and "filesystem__write" all resolve to the same allowlist
            # entry.
            if ok and fqn_name in _FILE_WRITE_ACTIONS:
                path = None
                if isinstance(params, dict):
                    path = (
                        params.get("path")
                        or params.get("file_path")
                        or params.get("filename")
                    )
                if path:
                    size_bytes = None
                    if isinstance(result, dict):
                        _meta = result.get("data") or {}
                        if isinstance(_meta, dict):
                            size_bytes = _meta.get("size") or _meta.get("bytes_written")
                    await recorder._emit("artifact", {
                        "path": str(path),
                        "action": fqn_name,
                        "size_bytes": size_bytes,
                    })

            if user_cb is not None:
                try:
                    await user_cb(name, params, result, call_id)
                except TypeError:
                    # Legacy callback with 3-arg signature
                    try:
                        await user_cb(name, params, result)
                    except Exception as exc:
                        logger.debug("user on_tool_call error: %s", exc)
                except Exception as exc:
                    logger.debug("user on_tool_call error: %s", exc)

        return _wrapped

    def wrap_on_thinking(self, user_cb: Any | None) -> Any:
        """Return a thinking callback that records AND forwards.

        The agent_loop calls on_thinking with the full reasoning text
        at the end of each turn. We persist a truncated version (2 KB
        max) - the full reasoning can be multi-MB and isn't useful for
        a dashboard drawer.
        """
        recorder = self
        _MAX_THINKING_LEN = 2048

        def _wrapped(text: str) -> None:
            try:
                truncated = text[:_MAX_THINKING_LEN]
                asyncio.create_task(recorder._emit("thinking", {
                    "text": truncated,
                    "truncated": len(text) > _MAX_THINKING_LEN,
                    "original_length": len(text),
                }))
            except Exception as exc:
                logger.debug("on_thinking record error: %s", exc)
            if user_cb is not None:
                try:
                    user_cb(text)
                except Exception as exc:
                    logger.debug("user on_thinking error: %s", exc)

        return _wrapped



# ── Trigger Circuit Breaker ───────────────────────────────────────
# Prevents infinite retry loops when triggers fail repeatedly. Pauses
# the trigger temporarily with exponential backoff. Auto-resets when
# it works again. Tripped by:
#   - Fatal provider errors (billing, auth, quota) → trips fast
#   - Repeated transient failures (timeouts, network, code crashes) → trips slower

# Fatal: trip after 1-2 failures (these won't recover without intervention)
_FATAL_KEYWORDS = {
    "402", "insufficient", "balance", "billing", "quota",
    "401", "unauthorized", "invalid api key", "forbidden",
    "permission denied", "not authorized",
}

# Transient: trip after MORE failures (these may recover on their own)
_TRANSIENT_KEYWORDS = {
    "timeout", "timed out", "connection", "network", "temporarily",
    "rate limit", "429", "503", "502", "504", "unavailable",
}


class _TriggerCircuitBreaker:
    """Per-trigger circuit breaker for fatal AND transient errors.

    Trip thresholds:
    - Fatal errors (billing/auth): trip after 2 consecutive failures
    - Transient errors (network/timeout): trip after 5 consecutive failures
    - Unknown errors (code crash, etc.): trip after 3 consecutive failures
    """

    __slots__ = ("trigger_id", "_consecutive_failures", "_pause_until", "_backoff")

    def __init__(self, trigger_id: str) -> None:
        self.trigger_id = trigger_id
        self._consecutive_failures = 0
        self._pause_until = 0.0
        self._backoff = 300.0  # Start: 5 min

    def is_paused(self) -> bool:
        if self._pause_until <= 0:
            return False
        if time.monotonic() >= self._pause_until:
            # Pause expired - allow one retry
            return False
        return True

    def pause_remaining(self) -> float:
        if self._pause_until <= 0:
            return 0
        return max(0, self._pause_until - time.monotonic())

    def record_success(self) -> None:
        if self._consecutive_failures > 0:
            logger.info(
                "trigger_circuit_breaker_reset trigger=%s (was paused after %d failures)",
                self.trigger_id, self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._pause_until = 0.0
        self._backoff = 300.0

    def _classify(self, error: str) -> str:
        """Classify an error as fatal, transient, or unknown."""
        error_lower = error.lower()
        if any(kw in error_lower for kw in _FATAL_KEYWORDS):
            return "fatal"
        if any(kw in error_lower for kw in _TRANSIENT_KEYWORDS):
            return "transient"
        return "unknown"

    def record_failure(self, error: str) -> None:
        """Record a failure and trip the breaker if threshold reached.

        Trip thresholds depend on error class - fatal errors trip fast
        (2 failures), transient slower (5), unknown in between (3).
        """
        category = self._classify(error)
        self._consecutive_failures += 1

        thresholds = {"fatal": 2, "unknown": 3, "transient": 5}
        threshold = thresholds.get(category, 3)

        if self._consecutive_failures >= threshold:
            self._pause_until = time.monotonic() + self._backoff
            logger.warning(
                "trigger_circuit_breaker_open trigger=%s category=%s failures=%d pausing=%.0fs error=%s",
                self.trigger_id, category, self._consecutive_failures,
                self._backoff, error[:100],
            )
            # Exponential backoff: 5min → 10min → 20min → 40min → max 1 hour
            self._backoff = min(self._backoff * 2, 3600.0)


# Global registry of circuit breakers per trigger
_breakers: dict[str, _TriggerCircuitBreaker] = {}


def _get_breaker(trigger_id: str) -> _TriggerCircuitBreaker:
    if trigger_id not in _breakers:
        _breakers[trigger_id] = _TriggerCircuitBreaker(trigger_id)
    return _breakers[trigger_id]


async def run_background(
    ctx: AgentContext,
    *,
    triggers: list[Any],
    max_turns: int = 30,
    timeout: float = 120.0,
    on_tool_call: Any | None = None,
    on_activation: Any | None = None,
    hook_runner: Any | None = None,
    runtime_app: Any | None = None,
    app_id: str = "",
    max_concurrent_activations: int = 20,
) -> None:
    """Run the agent in background mode.

    If the channels module is loaded (via ``runtime_app.modules``),
    it takes over all trigger handling. Otherwise, falls back to
    the legacy cron/watch loops.

    Args:
        ctx: Agent context.
        triggers: List of CompiledTrigger objects.
        max_turns: Max turns per activation.
        timeout: Timeout per activation.
        on_tool_call: Callback for tool calls.
        on_activation: Optional callback(trigger_id, message, result).
        runtime_app: The RuntimeApp instance (used for channels module delegation).
    """
    # ── Try channels module first ────────────────────────────────
    if runtime_app is not None:
        channels_mod = runtime_app.modules.get("channels")
        if channels_mod is not None:
            # RuntimeApp.__post_init__ has already wired _runtime_app
            # and _hook_runner. NOW we start the inbound listeners -
            # this is the "run" phase (vs deploy which only prepares).
            await channels_mod.start_listeners()

            logger.info(
                "background_channels_running providers=%d",
                len(getattr(channels_mod, "_providers", {})),
            )
            try:
                await asyncio.Event().wait()  # Run forever
            except asyncio.CancelledError:
                logger.info("background_channels_stopped")
            return

    # ── Legacy trigger loops ─────────────────────────────────────
    if not triggers:
        logger.error("Background mode requires at least one trigger")
        return

    _mc = max_concurrent_activations
    tasks = []
    for trigger in triggers:
        if trigger.type == "cron":
            tasks.append(
                _cron_loop(ctx, trigger, max_turns, timeout, on_tool_call, on_activation, hook_runner, app_id=app_id, max_concurrent=_mc)
            )
        elif trigger.type == "watch":
            tasks.append(
                _watch_loop(ctx, trigger, max_turns, timeout, on_tool_call, on_activation, hook_runner, app_id=app_id, max_concurrent=_mc)
            )
        elif trigger.type == "http":
            tasks.append(
                _http_loop(ctx, trigger, max_turns, timeout, on_tool_call, on_activation, hook_runner, app_id=app_id, max_concurrent=_mc)
            )
        else:
            logger.warning("Unknown trigger type: %s", trigger.type)

    if not tasks:
        logger.error("No valid triggers to run")
        return

    await asyncio.gather(*tasks)


async def _activate(
    ctx: AgentContext,
    trigger_id: str,
    message: str,
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None = None,
    trigger_type: str = "unknown",
    trigger_payload: dict[str, Any] | None = None,
    app_id: str = "",
    max_concurrent: int = 20,
) -> None:
    """Dispatch a trigger activation to the correct sessions.

    Resolves routing (broadcast/user/session), then calls
    _run_single_activation for each target session.

    If no background sessions exist, falls back to global context activation.
    """
    # Circuit breaker - skip if paused after fatal errors
    breaker = _get_breaker(trigger_id)
    if breaker.is_paused():
        remaining = breaker.pause_remaining()
        logger.debug(
            "Trigger '%s' skipped: circuit breaker open (retry in %.0fs)",
            trigger_id, remaining,
        )
        return

    # Guard: verify provider is initialized
    provider = getattr(ctx, "provider", None)
    if provider is None:
        logger.error("Trigger '%s' skipped: no LLM provider configured", trigger_id)
        return
    client = getattr(provider, "_client", "EXISTS")
    if client is None:
        logger.error(
            "Trigger '%s' skipped: LLM provider client not initialized "
            "(check API key - 'claude-code' OAuth may not be available in background mode)",
            trigger_id,
        )
        return

    # Extract routing metadata from payload
    routing = "broadcast"
    routing_key_value = ""
    if isinstance(trigger_payload, dict):
        routing = trigger_payload.pop("_routing", "broadcast")
        trigger_payload.pop("_routing_key_template", None)
        routing_key_value = trigger_payload.pop("_routing_key_value", "")

    if not app_id:
        app_id = getattr(ctx, "app_id", "") or "default"

    # ── Routing dispatch - resolve target sessions from DB ──────
    target_sessions: list[dict[str, Any]] = []
    bg_store = None
    try:
        from digitorn.core.app.background_session_store import BackgroundSessionStore
        from digitorn.core.database import get_session_factory
        bg_store = BackgroundSessionStore(get_session_factory())
        target_sessions = await bg_store.resolve_routing(app_id, routing, routing_key_value)
    except Exception as exc:
        logger.debug("Routing resolve failed: %s - falling back to global", exc)

    if target_sessions:
        active_sessions = [s for s in target_sessions if s.get("status") == "active"]
        skipped = len(target_sessions) - len(active_sessions)
        if skipped:
            logger.debug("Skipping %d paused session(s)", skipped)

        logger.info(
            "Trigger '%s' (%s) routing=%s → %d session(s) (max_concurrent=%d)",
            trigger_id, trigger_type, routing, len(active_sessions), max_concurrent,
        )

        # Throttled activation - semaphore limits concurrent LLM calls
        # to prevent rate limit storms with thousands of sessions.
        semaphore = asyncio.Semaphore(max_concurrent)
        completed = 0
        failed = 0

        async def _throttled_activation(sess: dict[str, Any]) -> None:
            """Run a single activation in isolation.

            CRITICAL: this MUST never raise. Any exception is caught and logged
            so that one user's crash never impacts other users in the broadcast.
            """
            nonlocal completed, failed
            try:
                async with semaphore:
                    session_text: str = message
                    params = sess.get("params", {}) or {}
                    # Payload is a reserved sub-dict of params. We surface
                    # it separately so it shows up to the agent as the
                    # user's scheduled input - NOT mixed into the
                    # ``Session context:`` params block.
                    payload = params.get("_payload") or {}

                    # Enforce ``payload_schema.required`` - when the app
                    # declares a required schema and this session's
                    # payload doesn't satisfy it, skip the activation
                    # entirely. The dashboard already blocks the user
                    # from activating an invalid session, but a stale
                    # session that was valid then drifted (e.g. user
                    # deleted a required file) must not silently fire.
                    schema = _get_app_payload_schema(ctx, app_id)
                    if schema and schema.get("required"):
                        try:
                            from digitorn.core.api.apps_v2 import (
                                _validate_payload_against_schema,
                            )
                            schema_errors = _validate_payload_against_schema(
                                schema, payload,
                            )
                        except Exception:
                            schema_errors = []
                        if schema_errors:
                            failed += 1
                            logger.warning(
                                "Session %s skipped: payload schema invalid: %s",
                                sess.get("id", "?")[:10], "; ".join(schema_errors),
                            )
                            return
                    visible_params = {
                        k: v for k, v in params.items() if k != "_payload"
                    }
                    if visible_params:
                        param_lines = "\n".join(
                            f"[{k}]: {v}" for k, v in visible_params.items()
                        )
                        session_text = f"{message}\n\nSession context:\n{param_lines}"
                    # Read payload files from disk on the daemon and
                    # build the final user message - either a plain
                    # string (no images/PDFs) or a multimodal content
                    # block list. The agent never touches the disk for
                    # these files.
                    session_message: str | list[dict[str, Any]] = (
                        _build_payload_message_content(session_text, payload)
                    )
                    try:
                        await _run_single_activation(
                            ctx, trigger_id, session_message,
                            max_turns, timeout, on_tool_call, on_activation, hook_runner,
                            trigger_type, trigger_payload,
                            session_id=sess.get("id", ""),
                            user_id=sess.get("user_id", ""),
                            app_id=app_id,
                        )
                        completed += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        logger.warning(
                            "Activation failed for session %s: %s",
                            sess.get("id", "?")[:10], exc,
                        )

                    # Update session last_active - separately wrapped so a touch
                    # failure does not affect counters or other sessions.
                    if bg_store:
                        try:
                            await bg_store.touch(sess["id"])
                        except asyncio.CancelledError:
                            raise
                        except Exception as touch_exc:
                            logger.debug(
                                "bg_store_touch_failed session=%s: %s",
                                sess.get("id", "?")[:10], touch_exc,
                            )
            except asyncio.CancelledError:
                raise
            except BaseException as base_exc:
                # Last-resort safety net - even SystemExit/KeyboardInterrupt
                # in this code path should not crash the trigger loop.
                failed += 1
                logger.error(
                    "Activation hard-crash for session %s: %s: %s",
                    sess.get("id", "?")[:10], type(base_exc).__name__, base_exc,
                )

        # Launch all - semaphore ensures only max_concurrent run at once
        await asyncio.gather(
            *[_throttled_activation(s) for s in active_sessions],
            return_exceptions=True,
        )

        logger.info(
            "Trigger '%s' broadcast done: %d completed, %d failed, %d skipped",
            trigger_id, completed, failed, skipped,
        )
        return

    # ── No sessions found - fall back to global context activation ──
    logger.info("Trigger '%s' (%s) → global activation (no sessions)", trigger_id, trigger_type)
    await _run_single_activation(
        ctx, trigger_id, message,
        max_turns, timeout, on_tool_call, on_activation, hook_runner,
        trigger_type, trigger_payload,
        app_id=app_id,
    )


async def _run_single_activation(
    ctx: AgentContext,
    trigger_id: str,
    message: str | list[dict[str, Any]],
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None = None,
    trigger_type: str = "unknown",
    trigger_payload: dict[str, Any] | None = None,
    session_id: str = "",
    user_id: str = "",
    app_id: str = "",
) -> None:
    """Run a single agent turn for one activation.

    Creates an Activation record in DB, runs agent_turn, persists the result.
    """
    logger.info("Activation: trigger=%s session=%s", trigger_id, session_id[:10] or "global")

    if not app_id:
        app_id = getattr(ctx, "app_id", "") or "default"

    # ── Runtime per-user secret resolution ──────────────────────────
    # The compiler left per_user / per_app_per_user secrets as
    # ``{{secret.X}}`` passthroughs because it didn't know which user
    # would trigger this activation. Now that we DO know (user_id is
    # on the BackgroundSession), walk the message payload and
    # substitute those templates via the CredentialStore.
    #
    # ``resolve_runtime_secrets_in_value`` walks str/dict/list
    # recursively so both plain text messages and multimodal content
    # block lists are covered in one call.
    try:
        credential_store = _get_credential_store_for_activation(ctx)
        if credential_store is not None:
            from digitorn.core.credentials import (
                resolve_runtime_secrets_in_value,
            )
            message = await resolve_runtime_secrets_in_value(
                message,
                store=credential_store,
                user_id=user_id or "anonymous",
                app_id=app_id,
            )
    except Exception as exc:
        logger.warning(
            "runtime secret resolution failed for activation "
            "(trigger=%s user=%s app=%s): %s - message may contain "
            "unresolved {{secret.X}} placeholders",
            trigger_id, user_id, app_id, exc,
        )

    # The activation table + on_activation callback want a plain text
    # message for display. When the user message is a multimodal
    # content list (text + image/document blocks), extract the text
    # portion plus a short "[+ N attachment(s)]" hint so the dashboard
    # row stays readable and we don't try to JSON-encode raw bytes.
    if isinstance(message, list):
        text_chunks = [
            b.get("text", "") for b in message
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        attachment_count = sum(
            1 for b in message
            if isinstance(b, dict) and b.get("type") in ("image", "document")
        )
        message_for_store = "\n".join(t for t in text_chunks if t)
        if attachment_count:
            message_for_store = (
                f"{message_for_store}\n\n[+ {attachment_count} attachment(s)]"
            )
    else:
        message_for_store = message

    # Create activation record in DB
    activation_id = None
    # Route activation persistence through persist_worker so asyncpg
    # never runs on the main loop. The store is instantiated INSIDE
    # the worker coroutine -- ``get_session_factory()`` then returns
    # the worker's own session factory (via contextvar override set
    # by ``run_async``). Without this, every background trigger
    # blocks the main loop on an asyncpg connect / TLS handshake
    # against Neon -- 200ms-25s stalls on a slow link.
    activation_store_available = False
    try:
        from digitorn.core.app.activation_store import ActivationStore  # noqa: F401
        from digitorn.core.database import get_session_factory  # noqa: F401
        from digitorn.core.runtime.persist_worker import get_default_worker
        activation_store_available = True
    except Exception:
        pass

    if activation_store_available:
        async def _create_activation() -> str:
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
            store = ActivationStore(get_session_factory())
            return await store.create(
                app_id=app_id,
                trigger_id=trigger_id,
                trigger_type=trigger_type,
                message=message_for_store,
                trigger_payload=trigger_payload,
            )
        try:
            activation_id = await get_default_worker().run_async(
                _create_activation,
            )
        except Exception as exc:
            logger.debug("Failed to create activation record: %s", exc)

    # Wire an event recorder so every tool call + thinking block + any
    # channel send during this activation is persisted into the
    # activation_events table. The dashboard drawer uses that table to
    # render the step-by-step timeline.
    recorder: _ActivationEventRecorder | None = None
    wrapped_on_tool_call = on_tool_call
    wrapped_on_thinking = None
    _recorder_token = None  # type: ignore[assignment]
    if activation_id is not None and activation_store_available:
        # Pass ``None`` for the legacy ``activation_store`` arg -- the
        # recorder constructs a fresh store on the worker loop on each
        # ``_emit`` (see ``_ActivationEventRecorder.__init__`` docstring).
        recorder = _ActivationEventRecorder(None, activation_id)
        wrapped_on_tool_call = recorder.wrap_on_tool_call(on_tool_call)
        wrapped_on_thinking = recorder.wrap_on_thinking(None)
        # Make the recorder accessible via the AgentContext so modules
        # (particularly channels) can emit their own channel_sent
        # events without having to thread the recorder through every
        # callback layer.
        try:
            ctx.activation_recorder = recorder  # type: ignore[attr-defined]
        except Exception:
            pass
        # Also bind via a task-local contextvar so code that does NOT
        # have access to the AgentContext (e.g. the global
        # ChannelRegistry used at delivery time) can still retrieve the
        # recorder. The scope is strictly this coroutine and its
        # descendants - concurrent activations on other tasks see their
        # own recorder or None.
        _recorder_token = _CURRENT_ACTIVATION_RECORDER.set(recorder)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ctx.system_prompt},
        {"role": "user", "content": message},
    ]

    result = None
    crash_error: str | None = None
    try:
        result = await agent_turn(
            ctx, messages,
            max_turns=max_turns,
            timeout=timeout,
            on_tool_call=wrapped_on_tool_call,
            on_thinking=wrapped_on_thinking,
            hook_runner=hook_runner,
        )
    except asyncio.CancelledError:
        crash_error = "cancelled"
        raise
    except Exception as exc:
        # Without this catch, any crash in agent_turn (provider 402,
        # timeout, context overflow, tool schema violation, …) would
        # leave the activation row pinned in `status=running` forever
        # - that is the root of BUG-054's zombie rows. Capture the
        # failure, log it, and let the completion block below mark
        # the row as `failed` so the dashboard keeps an honest count.
        crash_error = f"{type(exc).__name__}: {exc}"[:500]
        logger.exception(
            "Activation crashed before agent_turn returned: trigger=%s", trigger_id,
        )
    finally:
        # Clean up the recorder reference so subsequent activations on
        # the same context don't accidentally write into this one's
        # table, and the task-local contextvar is reset even if the
        # agent turn raised.
        if recorder is not None:
            try:
                ctx.activation_recorder = None  # type: ignore[attr-defined]
            except Exception:
                pass
        if _recorder_token is not None:
            try:
                _CURRENT_ACTIVATION_RECORDER.reset(_recorder_token)
            except Exception:
                pass

    # Persist activation result - this MUST run whether agent_turn
    # returned cleanly or crashed, otherwise the row is a zombie.
    # Routed through persist_worker for the same reason as ``_create``
    # above: keep asyncpg off the main loop on a slow / unreachable
    # Neon link.
    if activation_id is not None and activation_store_available:
        async def _complete_activation() -> None:
            from digitorn.core.app.activation_store import ActivationStore
            from digitorn.core.database import get_session_factory
            store = ActivationStore(get_session_factory())
            if result is not None:
                await store.complete(
                    activation_id,
                    response=result.content,
                    tool_calls_count=result.tool_calls_count,
                    turns_used=result.turns_used,
                    prompt_tokens=getattr(result, "prompt_tokens", 0),
                    completion_tokens=getattr(result, "completion_tokens", 0),
                    error=result.error,
                )
            else:
                await store.complete(
                    activation_id,
                    response="",
                    tool_calls_count=0,
                    turns_used=0,
                    error=crash_error or (
                        "activation crashed before completion"
                    ),
                )
        try:
            await get_default_worker().run_async(_complete_activation)
        except Exception as exc:
            logger.debug("Failed to save activation result: %s", exc)

    # Surface failures to the session bus so a frontend attached to this
    # session sees the same error banner it gets for foreground turns.
    # Two failure shapes: (a) agent_turn raised - we captured ``crash_error``
    # and have no ``result``; (b) agent_turn returned with ``result.error``
    # set (e.g. LLM billing 402 wrapped via _handle_llm_error). Both are
    # classified through the same pipeline so the client gets the same
    # structured payload it receives for user-message turns.
    #
    # Gate on a non-empty ``session_id``: activations without a session
    # are "global" (cron hooks, system tasks) - nobody is listening on a
    # specific session bus for them, and emitting untagged error events
    # would leak onto whichever session the client happens to have open
    # (the client-side filter we just tightened drops those anyway).
    _activation_error: str | None = crash_error
    if _activation_error is None and result is not None and result.error and result.error != "aborted":
        _activation_error = result.error
    if _activation_error:
        # Classify once for both the session bus emit AND the inbox
        # fallback - same structured payload whether the user watches
        # live or just sees the bell light up later.
        try:
            from digitorn.core.api.apps_v2 import _classify_error
            error_data = _classify_error(RuntimeError(_activation_error))
        except Exception:
            error_data = {"error": _activation_error[:500], "code": "internal"}
        error_data["trigger_id"] = trigger_id
        if activation_id:
            error_data["activation_id"] = activation_id

        # (a) Session-scoped emit when we have a real session - client
        # sees the error banner + persistent timeline marker in that
        # session's history.
        if session_id:
            try:
                event_bus = getattr(ctx, "event_bus", None) or \
                    getattr(getattr(ctx, "manager", None), "event_bus", None)
                if event_bus is not None:
                    from digitorn.core.events.envelope import (
                        SessionEvent as _SE, OpType as _OT, OpState as _OS,
                    )
                    error_data["session_id"] = session_id
                    await event_bus.emit(_SE.build(
                        type="error",
                        app_id=app_id or getattr(ctx, "app_id", "") or "default",
                        session_id=session_id,
                        user_id=user_id or "local",
                        op_id=f"activation-{activation_id or trigger_id}",
                        op_type=_OT.TURN,
                        op_state=_OS.FAILED,
                        payload=error_data,
                    ))
            except Exception as pub_exc:
                logger.warning(
                    "Failed to publish activation error event (trigger=%s session=%s): %s",
                    trigger_id, session_id[:10], pub_exc,
                )

        # (b) Inbox entry - ALWAYS written (sessioned or not). The
        # daemon's InboxProducer already handles ``error`` events from
        # (a), so we only add a BG_ACTIVATION_FAILED entry directly
        # when there's no session room at all - otherwise we'd double
        # up on the inbox for sessioned runs. Either way the user gets
        # one row in their bell, never zero.
        if not session_id:
            try:
                from digitorn.core.database import get_session_factory
                from digitorn.core.inbox.store import InboxStore
                from digitorn.core.inbox.kinds import InboxKind
                store = InboxStore(get_session_factory())
                pretty_app = (app_id or "background")\
                    .replace("-", " ").replace("_", " ").title()
                await store.create_item(
                    user_id=user_id or "local",
                    kind=InboxKind.BG_ACTIVATION_FAILED,
                    title=f"{pretty_app}: activation failed",
                    subtitle=(error_data.get("error")
                              or _activation_error)[:200],
                    app_id=app_id,
                    activation_id=activation_id,
                    metadata=error_data,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to write BG_ACTIVATION_FAILED inbox entry "
                    "(trigger=%s app=%s): %s",
                    trigger_id, app_id, exc,
                )

    if on_activation and result is not None:
        try:
            await on_activation(trigger_id, message_for_store, result)
        except Exception as exc:
            logger.warning("on_activation callback failed for trigger %s: %s", trigger_id, exc)

    # Update circuit breaker
    breaker = _get_breaker(trigger_id)
    if result is None or (result.error):
        err_reason = (result.error if result is not None else crash_error) or "crashed"
        breaker.record_failure(err_reason)
        logger.warning("Trigger %s completed with error: %s", trigger_id, err_reason)
    else:
        breaker.record_success()
        logger.info(
            "Trigger %s completed: %d tools, %d turns",
            trigger_id, result.tool_calls_count, result.turns_used,
        )


async def _cron_loop(
    ctx: AgentContext,
    trigger: Any,
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None = None,
    app_id: str = "",
    max_concurrent: int = 20,
) -> None:
    """Cron loop - fires the trigger on each cron tick via croniter."""
    from datetime import datetime, timezone
    from croniter import croniter

    logger.info("Cron trigger '%s': schedule '%s'", trigger.id, trigger.schedule)

    try:
        cron = croniter(trigger.schedule, datetime.now(tz=timezone.utc))
    except (ValueError, KeyError) as exc:
        logger.error(
            "Cron trigger '%s': invalid schedule '%s' (%s) - trigger disabled",
            trigger.id, trigger.schedule, exc,
        )
        return

    try:
        while True:
            nxt = cron.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc)
            delay = max(1.0, (nxt - datetime.now(tz=timezone.utc)).total_seconds())
            logger.debug("Cron trigger '%s': sleeping %.0fs", trigger.id, delay)
            await asyncio.sleep(delay)
            await _activate(
                ctx, trigger.id, trigger.message,
                max_turns, timeout, on_tool_call, on_activation, hook_runner,
                trigger_type="cron",
                trigger_payload={
                    "_routing": getattr(trigger, "routing", "broadcast"),
                },
                app_id=app_id,
                max_concurrent=max_concurrent,
            )
    except asyncio.CancelledError:
        logger.info("Cron trigger '%s' stopped", trigger.id)


async def _watch_loop(
    ctx: AgentContext,
    trigger: Any,
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None = None,
    app_id: str = "",
    max_concurrent: int = 20,
) -> None:
    """File watch loop - polls for new files matching glob patterns.

    Simple polling implementation. Will be replaced by inotify/fsevents later.
    """
    import glob as glob_module
    from pathlib import Path

    rt = ctx.runtime_config
    poll_interval = getattr(rt, "watch_poll_interval", 5) if rt else 5
    seen_files: set[str] = set()

    for pattern in trigger.paths:
        for match in glob_module.glob(pattern):
            seen_files.add(str(Path(match).resolve()))

    logger.info("Watch trigger '%s': watching %s", trigger.id, trigger.paths)

    try:
        while True:
            await asyncio.sleep(poll_interval)

            for pattern in trigger.paths:
                for match in glob_module.glob(pattern):
                    resolved = str(Path(match).resolve())
                    if resolved not in seen_files:
                        seen_files.add(resolved)
                        if len(seen_files) > 10_000:
                            oldest = next(iter(seen_files))
                            seen_files.discard(oldest)
                        message = trigger.message.replace("{{event.path}}", match)
                        await _activate(
                            ctx, trigger.id, message,
                            max_turns, timeout, on_tool_call, on_activation, hook_runner,
                            trigger_type="watch",
                            trigger_payload={
                                "path": match, "resolved": resolved,
                                "_routing": getattr(trigger, "routing", "broadcast"),
                            },
                            app_id=app_id,
                            max_concurrent=max_concurrent,
                        )
    except asyncio.CancelledError:
        logger.info("Watch trigger '%s' stopped", trigger.id)


async def _http_loop(
    ctx: AgentContext,
    trigger: Any,
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None = None,
    app_id: str = "",
    max_concurrent: int = 20,
) -> None:
    """HTTP trigger - starts a lightweight HTTP endpoint that activates the agent.

    Listens on the configured path for incoming requests. When a request
    arrives, the body is injected into the trigger message template and
    the agent is activated.

    Uses aiohttp if available, falls back to raw asyncio TCP server.
    For production webhooks with HMAC auth, use the channels module
    webhook adapter instead.
    """
    path = trigger.path or f"/trigger/{trigger.id}"
    method = (trigger.method or "POST").upper()
    port = getattr(trigger, "port", 0) or 9100

    logger.info("HTTP trigger '%s': listening on :%d%s [%s]", trigger.id, port, path, method)

    try:
        from aiohttp import web

        async def _handler(request: web.Request) -> web.Response:
            if request.method != method:
                return web.Response(status=405, text=f"Expected {method}")
            try:
                body = await request.text()
            except Exception:
                body = ""

            message = trigger.message
            message = message.replace("{{event.body}}", body[:10000])
            message = message.replace("{{event.path}}", request.path)
            message = message.replace("{{event.method}}", request.method)

            for header in ("X-GitHub-Event", "X-Gitlab-Event", "X-Webhook-Event"):
                val = request.headers.get(header, "")
                message = message.replace(f"{{{{event.header.{header}}}}}", val)

            # Resolve routing key from request
            rk_value = ""
            rk_template = getattr(trigger, "routing_key", "")
            if rk_template:
                rk_value = rk_template
                rk_value = rk_value.replace("{{event.body}}", body[:200])
                rk_value = rk_value.replace("{{event.path}}", request.path)
                rk_value = rk_value.replace("{{event.method}}", request.method)
                for hdr_name in ("X-User-Id", "X-Session-Id", "X-GitHub-Event"):
                    rk_value = rk_value.replace(
                        f"{{{{event.header.{hdr_name}}}}}",
                        request.headers.get(hdr_name, ""),
                    )
                # Also try query params
                for qk, qv in request.query.items():
                    rk_value = rk_value.replace(f"{{{{event.query.{qk}}}}}", qv)

            logger.info("HTTP trigger '%s' activated by %s %s (routing_key=%s)", trigger.id, request.method, request.path, rk_value or "none")
            asyncio.create_task(_activate(
                ctx, trigger.id, message,
                max_turns, timeout, on_tool_call, on_activation, hook_runner,
                trigger_type="http",
                trigger_payload={
                    "body": body[:1000], "path": request.path, "method": request.method,
                    "_routing": getattr(trigger, "routing", "broadcast"),
                    "_routing_key_value": rk_value,
                },
                app_id=app_id,
                max_concurrent=max_concurrent,
            ))
            return web.json_response({"triggered": True, "trigger_id": trigger.id})

        app = web.Application()
        app.router.add_route(method, path, _handler)
        app.router.add_route(method, path + "/{sub:.*}", _handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        logger.info("HTTP trigger '%s' ready on http://127.0.0.1:%d%s", trigger.id, port, path)

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    except ImportError:
        logger.info(
            "aiohttp not installed - HTTP trigger '%s' using basic TCP handler.",
            trigger.id,
        )
        await _http_basic_loop(ctx, trigger, max_turns, timeout, on_tool_call, on_activation, hook_runner, port, path, app_id=app_id, max_concurrent=max_concurrent)

    except asyncio.CancelledError:
        logger.info("HTTP trigger '%s' stopped", trigger.id)


async def _http_basic_loop(
    ctx: AgentContext,
    trigger: Any,
    max_turns: int,
    timeout: float,
    on_tool_call: Any | None,
    on_activation: Any | None,
    hook_runner: Any | None,
    port: int,
    path: str,
    app_id: str = "",
    max_concurrent: int = 20,
) -> None:
    """Minimal HTTP trigger using raw asyncio (no aiohttp dependency)."""

    async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            parts = request_line.decode("utf-8", errors="replace").strip().split(" ")
            req_method = parts[0] if parts else "?"
            req_path = parts[1] if len(parts) > 1 else "/"

            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n"):
                    break
                header = line.decode("utf-8", errors="replace").strip().lower()
                if header.startswith("content-length:"):
                    content_length = int(header.split(":")[1].strip())

            body = ""
            if content_length > 0:
                raw = await asyncio.wait_for(reader.read(min(content_length, 100000)), timeout=10)
                body = raw.decode("utf-8", errors="replace")

            if not req_path.startswith(path):
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found")
                await writer.drain()
                writer.close()
                return

            message = trigger.message
            message = message.replace("{{event.body}}", body[:10000])
            message = message.replace("{{event.path}}", req_path)
            message = message.replace("{{event.method}}", req_method)

            logger.info("HTTP trigger '%s' activated by %s %s", trigger.id, req_method, req_path)
            asyncio.create_task(_activate(
                ctx, trigger.id, message,
                max_turns, timeout, on_tool_call, on_activation, hook_runner,
                trigger_type="http",
                trigger_payload={
                    "body": body[:1000], "path": req_path, "method": req_method,
                    "_routing": getattr(trigger, "routing", "broadcast"),
                    "_routing_key_value": "",  # Basic TCP - no header parsing
                },
                app_id=app_id,
                max_concurrent=max_concurrent,
            ))

            resp_body = f'{{"triggered":true,"trigger_id":"{trigger.id}"}}'
            resp = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\n\r\n{resp_body}"
            writer.write(resp.encode())
            await writer.drain()
        except Exception as exc:
            logger.debug("HTTP trigger client error: %s", exc)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(_handle_client, "127.0.0.1", port)
        logger.info("HTTP trigger '%s' ready on http://127.0.0.1:%d%s (basic mode)", trigger.id, port, path)
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        logger.info("HTTP trigger '%s' stopped", trigger.id)
