"""Durable compaction persistence - snapshot + resume.

Every context-compaction event (hook-triggered, emergency overflow, or
manual API call) emits a ``type='compaction'`` event through the session
bus. The event carries a complete **snapshot payload** - the summary
text, the memory state, the tool catalogue, the setup summary, and the
seq boundaries - so a later ``_rebuild_session_from_db`` can rebuild
``session.messages`` as::

    [system_prompt, build_system_note_from_payload(event.payload),
     *history_log WHERE seq >= kept_range.from_seq]

…without replaying the compacted portion of the history. ``history_log``
stays append-only; the compaction event itself is just another row with
``kind='event'``, ``type='compaction'``.

This module is the single source of truth for both sides of that
contract:

* :func:`emit_compaction_event` - called by the compaction primitives
  after they mutate ``session.messages``. Queries the current max
  ``seq`` to compute the boundaries, snapshots the live context, and
  publishes the event via ``ctx.event_bus.emit`` (the bus stamps seq
  automatically and writes to ``history_log``).

* :func:`build_system_note_from_payload` - called by the rebuild path.
  Produces the exact same reminder text that ``_build_context_reminder``
  would produce in-process, but reads from the frozen JSON payload
  instead of the live runtime objects. Testable unitarily.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Snapshot capture ────────────────────────────────────────────────


def _snapshot_tools(context_builder: Any, tool_injection: str) -> dict[str, Any]:
    """Freeze the tool catalogue shape that will be re-injected on resume.

    ``direct`` / ``compact_direct`` callers see every tool (fqn + short
    description). ``discovery`` callers see the category breakdown only.
    The rebuild helper reproduces the exact wording of the original
    ``_build_context_reminder`` from this snapshot.
    """
    if context_builder is None:
        return {}
    index = getattr(context_builder, "_index", None)
    if index is None or not getattr(index, "categories", None):
        return {}

    snapshot: dict[str, Any] = {
        "tool_injection": tool_injection,
        "total_tools": index.total_tools,
        "total_categories": index.total_categories,
    }

    if tool_injection in ("direct", "compact_direct"):
        snapshot["tools"] = [
            {
                "fqn": fqn,
                "short_desc": (
                    tool.description.split(".")[0].strip()
                    if tool.description else ""
                ),
            }
            for fqn, tool in index.tools.items()
        ]
    else:
        snapshot["categories"] = [
            {
                "module_id": cat.module_id,
                "tool_count": cat.tool_count,
                "sample_names": list(cat.tool_names[:5]),
            }
            for cat in index.categories.values()
        ]

    # Sub-agent specialists (if any)
    _spawn = getattr(context_builder, "_spawn_specialists", None)
    if _spawn is None:
        _spawn_mod = getattr(context_builder, "_spawn_module_ref", None)
        if _spawn_mod is not None:
            _spawn = getattr(_spawn_mod, "_specialists", None)
    if _spawn:
        snapshot["specialists"] = [
            {"id": sid, "specialty": spec.get("specialty", "general")}
            for sid, spec in _spawn.items()
        ]
    elif any("spawn_agent" in str(t) for t in getattr(index, "tools", {}).keys()):
        snapshot["has_spawn_agent"] = True

    skills = getattr(context_builder, "_skills", [])
    if skills:
        snapshot["skill_names"] = [s.get("command", "") for s in skills]

    return snapshot


def _extract_tool_examples(
    recent_messages: list[dict[str, Any]],
    context_builder: Any,
    top_n: int = 5,
) -> list[dict[str, str]]:
    """Mirror ``_build_context_reminder`` example logic - frozen for replay.

    Picks the ``top_n`` most-called tools in ``recent_messages`` and
    freezes their first example into ``[{fqn, example_json}]``.
    """
    if not recent_messages or context_builder is None:
        return []
    index = getattr(context_builder, "_index", None)
    if index is None:
        return []

    tool_usage: dict[str, int] = {}
    for msg in recent_messages:
        for tc in msg.get("tool_calls") or []:
            fn_name = (tc.get("function") or {}).get("name", "")
            if fn_name:
                tool_usage[fn_name] = tool_usage.get(fn_name, 0) + 1

    examples: list[dict[str, str]] = []
    for fn_name, _count in sorted(
        tool_usage.items(), key=lambda x: -x[1]
    )[:top_n]:
        sep = fn_name.rfind("__")
        fqn = f"{fn_name[:sep]}.{fn_name[sep + 2:]}" if sep > 0 else fn_name
        indexed = index.tools.get(fqn)
        if not indexed or not getattr(indexed, "examples", None):
            continue
        ex = indexed.examples[0]
        ex_val = ex.get("value", ex) if isinstance(ex, dict) else ex
        examples.append({
            "fqn": fqn,
            "example_json": json.dumps(ex_val, ensure_ascii=False),
        })
    return examples


def _snapshot_memory(memory_module: Any) -> dict[str, Any]:
    """Freeze memory store to a JSON-serialisable dict.

    Returns an empty dict when memory isn't wired up - the rebuild
    side will then skip memory restoration and fall back to whatever
    state the cache already has.
    """
    if memory_module is None:
        return {}
    store = getattr(memory_module, "store", None)
    if store is None:
        return {}
    try:
        return store.to_dict() or {}
    except Exception as exc:
        logger.debug("snapshot_memory failed: %s", exc)
        return {}


# ── Seq boundary query ──────────────────────────────────────────────


async def _query_max_message_seq(session_id: str, app_id: str) -> int:
    """Return the highest seq among the projected messages for this
    session (user/assistant/system). Returns -1 when no messages have
    been persisted yet (caller treats it as "nothing to compact
    durably - skip persistence").

    Phase 4c: read from the in-memory SessionStore. The projection
    layer (apply_projection) already populates ``state.messages`` from
    user_message / assistant_message / system_message events, with the
    canonical seq stamped on each. So the answer is just the seq of
    the last projected message, or -1 if none.
    """
    try:
        from digitorn.core.runtime.session_store.bridge import (
            get_default_bridge,
        )
    except Exception:
        return -1
    bridge = get_default_bridge()
    if bridge is None:
        return -1
    try:
        state = await bridge.store.open(
            session_id, app_id=app_id, user_id="",
            create_if_missing=False, pin=False,
        )
    except KeyError:
        return -1
    except Exception:
        return -1
    if not state.messages:
        return -1
    return int(state.messages[-1].seq or -1)


# ── Event emission (write path) ─────────────────────────────────────


async def emit_compaction_event(
    ctx: Any,
    *,
    reason: str,
    strategy: str,
    summary_text: str,
    tokens_before: int,
    tokens_after: int,
    to_keep_count: int,
    recent_messages_before: list[dict[str, Any]] | None = None,
    # Explicit overrides for call sites where the shared
    # ``deployed.entry_context`` doesn't carry the live session's
    # identity / bus (e.g. ``POST /compact``). When provided, these
    # win over whatever is set on ``ctx``.
    event_bus: Any | None = None,
    app_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """Persist the compaction to ``history_log`` as a durable event.

    Called by ``_do_truncate`` / ``_do_summarize`` AFTER they mutate
    ``session.messages``. The caller must still hold the session lock
    (which it always does - compaction only runs inside ``_chat_locked``
    for the hook path, inside the manual endpoint handler for
    ``POST /compact``, or inside the agent loop for context-overflow).

    ``to_keep_count`` is ``len(to_keep)`` - used to derive
    ``kept_range.from_seq``. The invariant relied upon is that
    ``to_keep`` is a **contiguous suffix** of the persisted message
    stream, which is true by construction in every existing compaction
    primitive.

    Silently no-ops when:
      - bus / session_id / app_id aren't resolvable (e.g. unit tests
        driving the compaction primitives directly).
      - The session has no persisted messages yet (nothing to summarise
        durably - the in-memory mutation stands alone for this turn).
    """
    bus = (
        event_bus
        or getattr(ctx, "event_bus", None)
        or getattr(ctx, "_event_bus", None)
    )
    session_id = session_id or (getattr(ctx, "session_id", "") or "")
    app_id = app_id or (getattr(ctx, "app_id", "") or "")
    user_id = user_id or (getattr(ctx, "user_id", "") or "local")

    if bus is None or not session_id or not app_id:
        logger.debug(
            "emit_compaction_event: missing bus/ids - skipping "
            "(bus=%s, app=%s, session=%s)", bool(bus), app_id, session_id,
        )
        return

    max_persisted_seq = await _query_max_message_seq(session_id, app_id)
    if max_persisted_seq < 0 or to_keep_count <= 0:
        # Nothing persisted yet (brand-new session) - compaction is
        # purely in-memory for this turn. A subsequent compaction
        # after the first save will produce the first durable event.
        logger.debug(
            "emit_compaction_event: no persisted messages yet "
            "(max_seq=%d, to_keep=%d) - skip", max_persisted_seq, to_keep_count,
        )
        return

    kept_from_seq = max(0, max_persisted_seq - to_keep_count + 1)
    compacted_to_seq = kept_from_seq - 1

    tools_snapshot = _snapshot_tools(
        getattr(ctx, "context_builder", None),
        getattr(ctx, "tool_injection", "discovery"),
    )
    memory_snapshot = _snapshot_memory(getattr(ctx, "memory_module", None))
    tool_examples = _extract_tool_examples(
        recent_messages_before or [],
        getattr(ctx, "context_builder", None),
    )
    setup_summary = list(getattr(ctx, "setup_summary", []) or [])

    payload: dict[str, Any] = {
        "reason": reason,
        "strategy": strategy,
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "kept_range": {
            "from_seq": kept_from_seq,
            "to_seq": max_persisted_seq,
        },
        "compacted_range": {
            "from_seq": 0,
            "to_seq": compacted_to_seq,
        },
        "summary_text": summary_text,
        "memory_snapshot": memory_snapshot,
        "tools_snapshot": tools_snapshot,
        "setup_summary": setup_summary,
        "tool_examples": tool_examples,
        "tool_injection_mode": getattr(ctx, "tool_injection", "discovery"),
    }

    from digitorn.core.events.envelope import (
        SessionEvent, OpType, OpState, gen_op_id,
    )

    try:
        await bus.emit(SessionEvent.build(
            type="compaction",
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
            op_id=gen_op_id("compact"),
            op_type=OpType.COMPACT,
            op_state=OpState.COMPLETED,
            payload=payload,
        ))
        logger.info(
            "compaction_persisted app=%s session=%s reason=%s "
            "strategy=%s kept_from=%d tokens=%d→%d",
            app_id, session_id, reason, strategy,
            kept_from_seq, tokens_before, tokens_after,
        )
    except Exception as exc:
        # Durability failure must NOT mask the in-memory compaction -
        # the turn should continue. On the next restart we'd fall back
        # to full-history rebuild, which is the pre-feature behaviour.
        logger.warning(
            "compaction_persist_failed app=%s session=%s: %s",
            app_id, session_id, exc,
        )


# ── Rebuild path - payload → system note ────────────────────────────


def build_system_note_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the compacted system note for session replay.

    Takes the frozen JSON payload of a persisted ``compaction`` event
    and returns a ``{"role": "system", "content": "..."}`` dict that
    mimics what ``_build_context_reminder`` + summary text produced in
    the original turn.

    Structure (mirrors the order of parts in the live builder):
      1. Summary banner + the summary text
      2. Tool reminder (direct: full list; discovery: category breakdown)
      3. Pre-configured resources (setup summary)
      4. Quick-reference examples for recently-used tools
      5. Primitive capabilities line
      6. Memory state block
      7. Sub-agents note (if any)
      8. Skills line (if any)
      9. Final guardrail paragraph ("context was compacted…")
    """
    payload = payload or {}
    summary_text = payload.get("summary_text", "") or ""
    tools = payload.get("tools_snapshot", {}) or {}
    setup = payload.get("setup_summary", []) or []
    examples = payload.get("tool_examples", []) or []
    memory = payload.get("memory_snapshot", {}) or {}
    mode = tools.get("tool_injection", payload.get("tool_injection_mode", "discovery"))

    parts: list[str] = []
    parts.append(
        f"[Conversation summary - compacted from "
        f"{payload.get('tokens_before', 0)} → "
        f"{payload.get('tokens_after', 0)} tokens]:"
    )
    parts.append(summary_text.strip())
    parts.append("")
    parts.append("[Context reminder - your tools and capabilities are still available]")
    parts.append("")

    total_tools = tools.get("total_tools", 0)
    total_categories = tools.get("total_categories", 0)

    if mode in ("direct", "compact_direct") and tools.get("tools"):
        parts.append(
            f"You have {total_tools} tools available. "
            "Call them directly by name - no discovery step needed."
        )
        parts.append("")
        for t in tools["tools"]:
            parts.append(f"- {t['fqn']}: {t['short_desc']}")
    elif tools.get("categories"):
        parts.append(
            f"You have {total_tools} tools across {total_categories} categories:"
        )
        for cat in tools["categories"]:
            names = ", ".join(cat.get("sample_names", []))
            extra = cat["tool_count"] - len(cat.get("sample_names", []))
            suffix = f" (+{extra} more)" if extra > 0 else ""
            parts.append(
                f"- {cat['module_id']} ({cat['tool_count']} tools): {names}{suffix}"
            )
        parts.append("")
        parts.append(
            "Use search_tools or list_categories to rediscover tools if needed."
        )
        parts.append(
            "CRITICAL: ALL tools MUST be called via execute_tool(name, params). "
            "NEVER call tools directly by name - only search_tools, get_tool, "
            "execute_tool, list_categories, browse_category can be called directly."
        )

    if setup:
        parts.append("")
        parts.append("Pre-configured resources (still active):")
        for entry in setup:
            parts.append(f"- {entry}")

    if examples:
        parts.append("")
        parts.append("Quick reference - examples for your most-used tools:")
        for ex in examples:
            parts.append(f"- {ex['fqn']}: `{ex['example_json']}`")

    parts.append("")
    parts.append(
        "Primitive capabilities (always available): "
        "run_parallel (execute multiple actions simultaneously), "
        "background_run (1 tool, 5 modes: launch/status/cancel/wait/list background tasks), "
        "watch_start/watch_stop/watch_pause/watch_resume/watch_status/"
        "watch_list/watch_history (persistent periodic monitoring). "
        "Background tasks and watchers auto-notify you - no polling needed."
    )

    if memory:
        memory_block = _memory_block_from_snapshot(memory)
        if memory_block:
            parts.append("")
            parts.append(memory_block)

    if tools.get("specialists"):
        parts.append("")
        parts.append("Sub-agents available:")
        for spec in tools["specialists"]:
            parts.append(f"  - {spec['id']}: {spec.get('specialty', 'general')}")
        parts.append(
            "Delegate large reads and parallel work to them. "
            "Your context has been compacted -- protect the remaining space."
        )
    elif tools.get("has_spawn_agent"):
        parts.append("")
        parts.append(
            "You have sub-agents available (spawn_agent). "
            "Delegate large file reads and parallel tasks to protect your context."
        )

    if tools.get("skill_names"):
        parts.append("")
        parts.append(f"Available skills: {', '.join(tools['skill_names'])}")

    parts.append("")
    parts.append(
        "IMPORTANT: The context was compacted to save space. Your memory "
        "above contains your full cognitive state (goal, plan, progress, facts). "
        "The summary above preserves what happened. "
        "Continue working on the task -- check your tasks and keep going. "
        "Do NOT restart or re-read files you already analyzed. "
        "Delegate heavy reads to sub-agents to protect your remaining context."
    )

    return {"role": "system", "content": "\n".join(parts)}


def _memory_block_from_snapshot(memory: dict[str, Any]) -> str:
    """Render a ``[Memory state]`` block from a frozen memory snapshot.

    Keeps rebuild self-contained - doesn't require the live memory
    module. The structure matches what ``MemoryStore.render_full_snapshot``
    produces in-process (goal + todos + facts) so the LLM sees a
    similar shape to a live compaction.
    """
    lines: list[str] = ["[Memory state - restored from compaction snapshot]"]
    goal = memory.get("goal")
    if goal:
        lines.append(f"Goal: {goal}")
    todos = memory.get("todos") or []
    if todos:
        lines.append("Todos:")
        for td in todos[:20]:
            status = td.get("status", "open") if isinstance(td, dict) else ""
            text = td.get("text", td) if isinstance(td, dict) else str(td)
            lines.append(f"  - [{status}] {text}")
    facts = memory.get("facts") or []
    if facts:
        lines.append("Facts:")
        for f in facts[:20]:
            lines.append(f"  - {f}")
    if len(lines) == 1:
        return ""  # nothing to render
    return "\n".join(lines)
