"""Internal hooks - condition/action pairs evaluated during the agent loop."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from digitorn.core.runtime.system_directive import inject_system_directive
from digitorn.core.runtime.internal_call import internal_call_scope

logger = logging.getLogger(__name__)


@dataclass
class TurnState:
    messages: list[dict[str, Any]]
    turn: int
    max_turns: int
    tool_calls_count: int
    agent_id: str
    mode: str = "one_shot"
    tool_injection: str = "discovery"

    max_context_tokens: int = 128_000
    output_reserved: int = 4096

    _estimated_tokens: int | None = field(default=None, repr=False)

    _last_compact_turn: int = field(default=-10, repr=False)
    _agent_context: Any = field(default=None, repr=False)

    @property
    def estimated_tokens(self) -> int:
        if self._estimated_tokens is None:
            text_chars = 0
            n_tool_calls = 0
            for msg in self.messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    text_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text_chars += len(part.get("text", ""))
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    n_tool_calls += len(tool_calls)
                    for tc in tool_calls:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "")
                        text_chars += len(args) if isinstance(args, str) else len(str(args))
            self._estimated_tokens = (text_chars + 3) // 4 + n_tool_calls * 150
        return self._estimated_tokens

    @property
    def effective_max_tokens(self) -> int:
        return max(self.max_context_tokens - self.output_reserved, 1)

    @property
    def token_pressure(self) -> float:
        return self.estimated_tokens / self.effective_max_tokens

    @property
    def message_count(self) -> int:
        return len(self.messages)


@dataclass
class HookCondition:
    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookAction:
    type: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hook:
    id: str
    on: str = "turn_end"
    condition: HookCondition = field(default_factory=lambda: HookCondition(type="always"))
    action: HookAction = field(default_factory=lambda: HookAction(type="inject_message"))
    cooldown: float = 0.0
    max_fires: int = 0
    priority: int = 100
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    agent_id: str | None = None
    timeout: float = 30.0


@dataclass
class RuntimeHook:
    hook: Hook
    last_fired: float = 0.0
    fire_count: int = 0

    @property
    def can_fire(self) -> bool:
        if not self.hook.enabled:
            return False
        if self.hook.max_fires and self.fire_count >= self.hook.max_fires:
            return False
        if self.hook.cooldown <= 0:
            return True
        return (time.monotonic() - self.last_fired) >= self.hook.cooldown

    def mark_fired(self) -> None:
        self.last_fired = time.monotonic()
        self.fire_count += 1


ConditionFn = Callable[[TurnState, dict[str, Any]], bool]

_CONDITION_REGISTRY: dict[str, ConditionFn] = {}
_CONDITION_PARAMS: dict[str, dict[str, str]] = {}


def register_condition(
    name: str,
    params: dict[str, str] | None = None,
) -> Callable[[ConditionFn], ConditionFn]:
    def decorator(fn: ConditionFn) -> ConditionFn:
        _CONDITION_REGISTRY[name] = fn
        if params is not None:
            _CONDITION_PARAMS[name] = params
        return fn

    return decorator


def get_condition(name: str) -> ConditionFn | None:
    return _CONDITION_REGISTRY.get(name)


def get_condition_params(name: str) -> dict[str, str] | None:
    return _CONDITION_PARAMS.get(name)


def all_condition_names() -> list[str]:
    return sorted(_CONDITION_REGISTRY.keys())


@register_condition("context_pressure", params={"threshold": "optional"})
def _eval_context_pressure(state: TurnState, params: dict[str, Any]) -> bool:
    threshold = params.get("threshold", 0.75)
    return state.token_pressure >= threshold


@register_condition("turn_count", params={"threshold": "required", "every": "optional"})
def _eval_turn_count(state: TurnState, params: dict[str, Any]) -> bool:
    threshold = params.get("threshold", 10)
    every = params.get("every", 0)

    if every > 0:
        return state.turn > 0 and state.turn % every == 0

    return state.turn >= threshold


@register_condition("tool_calls", params={"threshold": "required"})
def _eval_tool_calls(state: TurnState, params: dict[str, Any]) -> bool:
    threshold = params.get("threshold", 20)
    return state.tool_calls_count >= threshold


@register_condition("message_count", params={"threshold": "required"})
def _eval_message_count(state: TurnState, params: dict[str, Any]) -> bool:
    threshold = params.get("threshold", 50)
    return state.message_count >= threshold


@register_condition("always", params={})
def _eval_always(state: TurnState, params: dict[str, Any]) -> bool:
    return True


@register_condition("never", params={})
def _eval_never(state: TurnState, params: dict[str, Any]) -> bool:
    return False


def _eval_nested(state: TurnState, cond: dict[str, Any]) -> bool:
    t = cond.get("type")
    if not t:
        return False
    fn = get_condition(t)
    if fn is None:
        logger.warning("composite_condition: unknown inner type '%s'", t)
        return False
    params = {k: v for k, v in cond.items() if k != "type"}
    try:
        return bool(fn(state, params))
    except Exception as exc:
        logger.warning("composite_condition: '%s' error: %s", t, exc)
        return False


@register_condition("all_of", params={"conditions": "required"})
def _eval_all_of(state: TurnState, params: dict[str, Any]) -> bool:
    inner = params.get("conditions") or []
    if not isinstance(inner, list):
        return False
    for cond in inner:
        if not isinstance(cond, dict) or not _eval_nested(state, cond):
            return False
    return True


@register_condition("any_of", params={"conditions": "required"})
def _eval_any_of(state: TurnState, params: dict[str, Any]) -> bool:
    inner = params.get("conditions") or []
    if not isinstance(inner, list):
        return False
    for cond in inner:
        if isinstance(cond, dict) and _eval_nested(state, cond):
            return True
    return False


@register_condition("not", params={"condition": "required"})
def _eval_not(state: TurnState, params: dict[str, Any]) -> bool:
    cond = params.get("condition")
    if not isinstance(cond, dict):
        return False
    return not _eval_nested(state, cond)


ActionFn = Callable[..., Awaitable[None]]

_ACTION_REGISTRY: dict[str, ActionFn] = {}
_ACTION_PARAMS: dict[str, dict[str, str]] = {}


def register_action(
    name: str,
    params: dict[str, str] | None = None,
) -> Callable[[ActionFn], ActionFn]:
    def decorator(fn: ActionFn) -> ActionFn:
        _ACTION_REGISTRY[name] = fn
        if params is not None:
            _ACTION_PARAMS[name] = params
        return fn

    return decorator


def get_action(name: str) -> ActionFn | None:
    return _ACTION_REGISTRY.get(name)


def get_action_params(name: str) -> dict[str, str] | None:
    return _ACTION_PARAMS.get(name)


def all_action_names() -> list[str]:
    return sorted(_ACTION_REGISTRY.keys())


@register_action("compact_context", params={
    "strategy": "optional",
    "keep_last": "optional",
    "keep_recent": "optional",
    "summary_max_tokens": "optional",
    "summary_prompt": "optional",
    "target_pressure": "optional",
    "cooldown_turns": "optional",
})
async def _exec_compact_context(
    state: TurnState,
    params: dict[str, Any],
    *,
    provider: Any = None,
    context_builder: Any = None,
) -> None:
    strategy = params.get("strategy", "summarize")
    keep_recent = params.get("keep_recent", params.get("keep_last", 10))
    summary_max_tokens = params.get("summary_max_tokens", 1024)
    target_pressure = params.get("target_pressure", 0.5)

    messages = state.messages

    cooldown = params.get("cooldown_turns", 3)
    if state.turn - state._last_compact_turn < cooldown:
        logger.debug(
            "hook_compact: skipping - cooldown (%d turns since last compact)",
            state.turn - state._last_compact_turn,
        )
        return

    if len(messages) <= keep_recent + 1:
        return

    try:
        agent_ctx = getattr(state, "_agent_context", None)
        cb = getattr(agent_ctx, "context_builder", None) if agent_ctx else None
        runner = getattr(cb, "hook_runner", None)
        if runner is not None:
            await runner.run("pre_compact", state)
    except Exception as exc:
        logger.debug("pre_compact hook failed: %s", exc)

    system_msg = messages[0] if messages and messages[0].get("role") == "system" else None
    conversation = messages[1:] if system_msg else messages[:]

    if len(conversation) <= keep_recent:
        return

    safe_keep = _find_safe_split_point(conversation, keep_recent)

    to_compact = conversation[:-safe_keep] if safe_keep < len(conversation) else []
    to_keep = conversation[-safe_keep:] if safe_keep < len(conversation) else conversation

    if not to_compact:
        return

    _mem = getattr(state._agent_context, "memory_module", None) if state._agent_context else None
    context_reminder = _build_context_reminder(
        context_builder, state.tool_injection,
        memory_module=_mem,
        agent_context=state._agent_context,
        recent_messages=to_keep,
    )

    tokens_before = state.estimated_tokens
    recent_messages_before = list(to_keep)

    summary_text: str = ""
    full_injected_note: str = ""
    if strategy == "truncate":
        summary_text, full_injected_note = _do_truncate(
            messages, system_msg, to_compact, to_keep, context_reminder,
        )
    elif strategy == "summarize":
        summary_text, full_injected_note = await _do_summarize(
            state, messages, system_msg, to_compact, to_keep,
            provider=provider,
            context_builder=context_builder,
            summary_max_tokens=summary_max_tokens,
            summary_prompt=params.get("summary_prompt"),
            context_reminder=context_reminder,
        )

    if full_injected_note:
        await inject_system_directive(
            state._agent_context,
            content=full_injected_note,
            source="compaction_summary",
            metadata={"strategy": strategy, "compacted": len(to_compact)},
        )

    max_msg_chars = max((state.max_context_tokens // 10) * 4, 4000)
    for msg in messages[1:]:
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > max_msg_chars:
            role = msg.get("role", "")
            cut = max_msg_chars - 300
            msg["content"] = (
                content[:cut]
                + f"\n\n[TRUNCATED: this {role} message was {len(content):,} chars, "
                f"only first {cut:,} shown. The full content was too large for the "
                f"context window. Use start_line/end_line to read specific sections "
                f"of large files instead of reading the entire file.]"
            )

    state._estimated_tokens = None

    state._last_compact_turn = state.turn
    if state._agent_context is not None:
        state._agent_context.last_compact_turn = state.turn

    tokens_after = state.estimated_tokens
    new_pressure = state.token_pressure
    logger.info(
        "hook_compact: %s - %d msgs compacted, %d kept, pressure: %.1f%%",
        strategy, len(to_compact), len(to_keep), new_pressure * 100,
    )

    # mirror the trim into SessionStore - otherwise the projection keeps the full history and we re-compact every turn.
    agent_ctx = state._agent_context
    if agent_ctx is not None:
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            from digitorn.core.runtime.session_metrics import (
                get_session_metrics,
            )
            bridge = get_default_bridge()
            sid_for_compact = getattr(agent_ctx, "session_id", None)
            if bridge is not None and sid_for_compact:
                store_state = bridge.store.state(sid_for_compact)
                if store_state is not None and store_state.messages:
                    keep_count = len(to_keep)
                    state_msgs = store_state.messages
                    cutoff_idx = max(0, len(state_msgs) - keep_count)
                    if cutoff_idx > 0:
                        cutoff_seq = int(state_msgs[cutoff_idx - 1].seq)
                        await bridge.store.compact_session(
                            sid_for_compact,
                            cutoff_seq=cutoff_seq,
                            summary=(
                                f"[Proactive hook {strategy}: "
                                f"{len(to_compact)} older messages removed, "
                                f"{int(tokens_before)} -> {int(tokens_after)} tokens]"
                            ),
                            strategy=strategy,
                            tokens_estimate=int(tokens_after),
                            model=str(getattr(agent_ctx, "model", "") or ""),
                        )
            try:
                _app_id = getattr(agent_ctx, "app_id", "") or "default"
                _agent_id = getattr(agent_ctx, "agent_id", "") or "main"
                _sm = get_session_metrics(
                    _app_id, sid_for_compact or "default", _agent_id,
                )
                _sm.context.compactions += 1
            except Exception as exc:
                logger.debug("hook_compact: metrics update failed: %s", exc)
        except Exception as exc:
            logger.warning(
                "hook_compact: durable persist failed (%s); LLM call "
                "still benefits from the in-memory trim but next turn "
                "will reload the full history from the store",
                exc,
            )

    if state._agent_context is not None:
        from digitorn.core.runtime.compaction_persistence import (
            emit_compaction_event,
        )
        await emit_compaction_event(
            state._agent_context,
            reason=params.get("_reason", "scheduled"),
            strategy=strategy,
            summary_text=summary_text,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            to_keep_count=len(to_keep),
            recent_messages_before=recent_messages_before,
        )


def _build_context_reminder(
    context_builder: Any | None,
    tool_injection: str = "discovery",
    memory_module: Any | None = None,
    agent_context: Any | None = None,
    recent_messages: list[dict[str, Any]] | None = None,
) -> str:
    if context_builder is None:
        return ""

    index = getattr(context_builder, "_index", None)
    if index is None or not index.categories:
        return ""

    from digitorn.core.runtime.system_directives import (
        SYS_CONTEXT_RELOAD_HEADER,
    )
    parts: list[str] = [SYS_CONTEXT_RELOAD_HEADER, ""]

    if tool_injection in ("direct", "compact_direct"):
        parts.append(f"You have {index.total_tools} tools available. "
                      "Call them directly by name - no discovery step needed.")
        parts.append("")
        for fqn, tool in index.tools.items():
            short_desc = tool.description.split(".")[0].strip() if tool.description else ""
            parts.append(f"- {fqn}: {short_desc}")
    else:
        parts.append(
            f"You have {index.total_tools} tools across "
            f"{index.total_categories} categories:"
        )
        for cat in index.categories.values():
            tool_names = ", ".join(cat.tool_names[:5])
            suffix = f" (+{cat.tool_count - 5} more)" if cat.tool_count > 5 else ""
            parts.append(
                f"- {cat.module_id} ({cat.tool_count} tools): {tool_names}{suffix}"
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

    if agent_context is not None:
        setup = agent_context.setup_summary if agent_context else None
        if setup:
            parts.append("")
            parts.append("Pre-configured resources (still active):")
            for entry in setup:
                parts.append(f"- {entry}")

    if recent_messages and index:
        tool_usage: dict[str, int] = {}
        for msg in recent_messages:
            for tc in msg.get("tool_calls", []):
                fn_name = tc.get("function", {}).get("name", "")
                if fn_name:
                    tool_usage[fn_name] = tool_usage.get(fn_name, 0) + 1
        top_tools = sorted(tool_usage.items(), key=lambda x: -x[1])[:5]
        example_lines: list[str] = []
        for fn_name, _count in top_tools:
            sep = fn_name.rfind("__")
            fqn = f"{fn_name[:sep]}.{fn_name[sep + 2:]}" if sep > 0 else fn_name
            indexed = index.tools.get(fqn)
            if indexed and indexed.examples:
                import json as _json
                ex = indexed.examples[0]
                ex_val = ex.get("value", ex) if isinstance(ex, dict) else ex
                example_lines.append(
                    f"- {fqn}: `{_json.dumps(ex_val, ensure_ascii=False)}`"
                )
        if example_lines:
            parts.append("")
            parts.append("Quick reference - examples for your most-used tools:")
            parts.extend(example_lines)

    parts.append("")
    parts.append(
        "Primitive capabilities (always available): "
        "run_parallel (execute multiple actions simultaneously), "
        "background_run (1 tool, 5 modes: launch/status/cancel/wait/list background tasks), "
        "watch_start/watch_stop/watch_pause/watch_resume/watch_status/"
        "watch_list/watch_history (persistent periodic monitoring). "
        "Background tasks and watchers auto-notify you - no polling needed."
    )

    if memory_module is not None:
        from digitorn.modules.memory.hooks import on_compaction
        memory_block = on_compaction(memory_module, [])
        if memory_block:
            parts.append("")
            parts.append(memory_block)

    _spawn = getattr(context_builder, "_spawn_specialists", None)
    if _spawn is None:
        _spawn_mod = getattr(context_builder, "_spawn_module_ref", None)
        if _spawn_mod is not None:
            _spawn = getattr(_spawn_mod, "_specialists", None)
    if not _spawn:
        _has_spawn = any("spawn_agent" in str(t) for t in getattr(index, "tools", {}).keys())
        if _has_spawn:
            parts.append("")
            parts.append(
                "You have sub-agents available (spawn_agent). "
                "Delegate large file reads and parallel tasks to protect your context."
            )
    else:
        parts.append("")
        parts.append("Sub-agents available:")
        for sid, spec in _spawn.items():
            parts.append(f"  - {sid}: {spec.get('specialty', 'general')}")
        parts.append(
            "Delegate large reads and parallel work to them. "
            "Your context has been compacted -- protect the remaining space."
        )

    _skills = getattr(context_builder, "_skills", [])
    if _skills:
        skill_names = ", ".join(s.get("command", "") for s in _skills)
        parts.append("")
        parts.append(f"Available skills: {skill_names}")

    from digitorn.core.runtime.system_directives import (
        SYS_CONTEXT_RELOAD_FOOTER,
    )
    parts.append("")
    parts.append(SYS_CONTEXT_RELOAD_FOOTER)

    return "\n".join(parts)


def _find_safe_split_point(conversation: list[dict[str, Any]], keep_recent: int) -> int:
    if keep_recent >= len(conversation):
        return len(conversation)

    split_idx = len(conversation) - keep_recent
    while split_idx > 0:
        msg = conversation[split_idx]
        if msg.get("role") == "tool":
            split_idx -= 1
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            split_idx -= 1
            continue
        break

    return len(conversation) - split_idx


def _do_truncate(
    messages: list[dict[str, Any]],
    system_msg: dict[str, Any] | None,
    to_compact: list[dict[str, Any]],
    to_keep: list[dict[str, Any]],
    context_reminder: str = "",
) -> tuple[str, str]:
    new_messages = []
    if system_msg:
        new_messages.append(system_msg)

    from digitorn.core.runtime.system_directives import SYS_CONTEXT_TRUNCATED
    summary_note = SYS_CONTEXT_TRUNCATED.format(n=len(to_compact))
    note_parts = [summary_note]
    if context_reminder:
        note_parts.append("")
        note_parts.append(context_reminder)

    full_injected_note = "\n".join(note_parts)
    new_messages.append({
        "role": "system",
        "content": full_injected_note,
    })
    new_messages.extend(to_keep)

    messages.clear()
    messages.extend(new_messages)
    return summary_note, full_injected_note


async def _do_summarize(
    state: TurnState,
    messages: list[dict[str, Any]],
    system_msg: dict[str, Any] | None,
    to_compact: list[dict[str, Any]],
    to_keep: list[dict[str, Any]],
    *,
    provider: Any = None,
    context_builder: Any = None,
    summary_max_tokens: int = 1024,
    summary_prompt: str | None = None,
    context_reminder: str = "",
) -> tuple[str, str]:
    if provider is None:
        logger.warning("hook_compact: no provider for summarize, falling back to truncate")
        return _do_truncate(messages, system_msg, to_compact, to_keep, context_reminder)

    if summary_prompt is None:
        summary_prompt = (
            "Summarize the following conversation concisely. "
            "Preserve:\n"
            "- The user's ORIGINAL REQUEST (what they asked for)\n"
            "- Key facts, decisions, and conclusions discovered so far\n"
            "- Tool calls made and their results (which tools were used, what they returned)\n"
            "- Important data discovered (file contents, query results, etc.)\n"
            "- Any errors encountered and how they were resolved\n"
            "- Configuration context: file paths, database connections, API endpoints, "
            "credentials references, environment variables, and workspace settings\n"
            "- WHAT REMAINS TO DO - list the unfinished work clearly\n"
            "\n"
            "End the summary with a clear 'NEXT STEPS:' section listing "
            "what the agent should do next to complete the task.\n"
            "\n"
            "Respond with only the summary, no preamble."
        )

    formatted = []
    for msg in to_compact:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if role == "tool" and len(content) > 500:
            content = content[:500] + "... [truncated tool output]"
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            call_names = [
                tc.get("function", {}).get("name", "?") for tc in tool_calls
            ]
            formatted.append(f"[{role}]: {content} [called: {', '.join(call_names)}]")
        elif content:
            formatted.append(f"[{role}]: {content}")

    conversation_text = "\n".join(formatted)

    from digitorn.modules.llm_provider.providers.base import ChatMessage

    try:
        summary_response = await provider.chat(
            [
                ChatMessage(role="system", content=summary_prompt),
                ChatMessage(role="user", content=conversation_text),
            ],
            tools=None,
            max_tokens=summary_max_tokens,
        )

        summary_content = (
            summary_response.content
            if hasattr(summary_response, "content")
            else summary_response.get("content", "")
        )
    except Exception as exc:
        logger.warning("hook_compact: summarize failed (%s), falling back to truncate", exc)
        return _do_truncate(messages, system_msg, to_compact, to_keep, context_reminder)

    from digitorn.core.runtime.system_directives import SYS_CONTEXT_SUMMARISED
    summary_text = (
        f"{SYS_CONTEXT_SUMMARISED.format(n=len(to_compact))}\n\n"
        f"[Conversation summary - {len(to_compact)} messages compacted]:\n"
        f"{summary_content}"
    )
    summary_parts = [summary_text]
    if context_reminder:
        summary_parts.append("")
        summary_parts.append(context_reminder)

    full_injected_note = "\n".join(summary_parts)
    new_messages = []
    if system_msg:
        new_messages.append(system_msg)
    new_messages.append({
        "role": "system",
        "content": full_injected_note,
    })
    new_messages.extend(to_keep)

    messages.clear()
    messages.extend(new_messages)
    return summary_text, full_injected_note


@register_action("inject_message", params={"content": "required", "role": "optional", "placeholder": "optional", "strategy": "optional", "position": "optional"})
async def _exec_inject_message(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    content = params.get("content")
    strategy = params.get("strategy", "auto")
    role = params.get("role", "user")
    position = params.get("position", "before_last")

    if not content:
        logger.warning("hook_inject: no content specified")
        return

    ctx = getattr(state, "_agent_context", None)

    if strategy in ("auto", "user"):
        for i in range(len(state.messages) - 1, -1, -1):
            msg = state.messages[i]
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"] + f"\n\n[System note: {content}]"
                logger.debug("hook_inject: appended to last user message")
                return
        strategy = "system"

    if strategy == "system":
        for msg in state.messages:
            if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"] + f"\n\n{content}"
                await inject_system_directive(
                    ctx,
                    content=content,
                    source="hook_inject_message",
                    metadata={"strategy": "system", "merged_inflight": True},
                )
                logger.debug("hook_inject: appended to system prompt")
                return
        await inject_system_directive(
            ctx,
            content=content,
            source="hook_inject_message",
            messages=state.messages,
            position="prepend",
            metadata={"strategy": "system", "created_new": True},
        )
        logger.debug("hook_inject: created new system message")
        return

    if role == "system":
        position_kind = "append" if position == "end" else "insert_before_last"
        await inject_system_directive(
            ctx,
            content=content,
            source="hook_inject_message",
            messages=state.messages,
            position=position_kind,
            metadata={"strategy": "new_message"},
        )
    else:
        new_msg = {"role": role, "content": content}
        if position == "end":
            state.messages.append(new_msg)
        else:
            idx = max(len(state.messages) - 1, 0)
            state.messages.insert(idx, new_msg)
    logger.debug("hook_inject: inserted %s message at %s", role, position)


def _walk_path(obj: Any, path: str) -> Any:
    cur = obj
    for segment in path.split("."):
        if cur is None:
            return None
        if segment.isdigit() and isinstance(cur, (list, tuple)):
            idx = int(segment)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            continue
        if isinstance(cur, dict):
            cur = cur.get(segment)
            continue
        cur = getattr(cur, segment, None)
    return cur


def _render_tool_templates(value: Any, state: "TurnState") -> Any:
    if isinstance(value, list):
        return [_render_tool_templates(v, state) for v in value]
    if isinstance(value, dict):
        return {k: _render_tool_templates(v, state) for k, v in value.items()}
    if not isinstance(value, str) or "{{" not in value:
        return value

    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None:
        return value

    tool_name = getattr(tool_ctx, "tool_name", "") or ""
    tool_params = getattr(tool_ctx, "tool_params", {}) or {}
    tool_result = getattr(tool_ctx, "tool_result", None)
    tool_error = getattr(tool_ctx, "tool_error", None)

    import re as _re
    pattern = _re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

    def _resolve(match: "_re.Match[str]") -> str:
        ref = match.group(1).strip()
        if ref == "tool.name":
            return str(tool_name)
        if ref == "tool.fqn":
            return str(tool_name)
        if ref == "tool.error":
            return "" if tool_error is None else str(tool_error)
        if ref == "tool.result":
            try:
                import json as _json
                return _json.dumps(tool_result, default=str, ensure_ascii=False)
            except Exception:
                return str(tool_result) if tool_result is not None else ""
        if ref.startswith("tool.params."):
            path = ref[len("tool.params."):]
            val = _walk_path(tool_params, path)
            return "" if val is None else str(val)
        if ref.startswith("tool.result."):
            path = ref[len("tool.result."):]
            val = _walk_path(tool_result, path) if tool_result is not None else None
            return "" if val is None else str(val)
        return match.group(0)

    return pattern.sub(_resolve, value)


@register_action("module_action", params={"module": "required", "action": "required", "params": "optional", "action_params": "optional"})
async def _exec_module_action(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    name = params.get("name")
    if not name:
        mod = params.get("module")
        act = params.get("action")
        if mod and act:
            name = f"{mod}.{act}"
    action_params = dict(
        params.get("action_params")
        or params.get("params")
        or {}
    )

    if not name:
        logger.warning(
            "hook_module_action: no tool name specified "
            "(need either `name: module.action` or `module: + action:`)",
        )
        return

    if context_builder is None:
        logger.warning("hook_module_action: no context_builder available")
        return

    action_params = _render_tool_templates(action_params, state)

    try:
        result = await context_builder.execute(
            "execute_tool", {"name": name, "params": action_params}
        )
        logger.info("hook_module_action: %s → %s", name, "ok" if result.success else result.error)
    except Exception as exc:
        logger.warning("hook_module_action: %s failed: %s", name, exc)


@register_action("module_action_inject", params={"module": "required", "action": "required", "params": "optional", "action_params": "optional", "role": "optional"})
async def _exec_module_action_inject(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    name = params.get("name")
    if not name:
        mod = params.get("module")
        act = params.get("action")
        if mod and act:
            name = f"{mod}.{act}"
    action_params = dict(
        params.get("action_params")
        or params.get("params")
        or {}
    )
    fmt = params.get("format", "auto")
    prefix = params.get("prefix", "")

    if not name:
        logger.warning("hook_module_action_inject: no tool name specified")
        return

    if context_builder is None:
        logger.warning("hook_module_action_inject: no context_builder available")
        return

    action_params = _render_tool_templates(action_params, state)

    try:
        result = await context_builder.execute(
            "execute_tool", {"name": name, "params": action_params}
        )

        if not result or not result.success:
            logger.debug("hook_module_action_inject: %s returned no result or failed", name)
            return

        data = result.data if hasattr(result, "data") else result.output
        if data is None:
            return

        message_text = _format_action_result_for_injection(name, data, fmt, prefix)
        if message_text is None:
            return

        await inject_system_directive(
            getattr(state, "_agent_context", None),
            content=message_text,
            source="hook_inject_message",
            messages=state.messages,
            position="insert_before_last",
            metadata={"action": name, "via": "module_action_inject"},
        )

        logger.info("hook_module_action_inject: %s → injected diagnostics", name)

    except Exception as exc:
        logger.warning("hook_module_action_inject: %s failed: %s", name, exc)


def _format_action_result_for_injection(
    name: str, data: Any, fmt: str, prefix: str
) -> str | None:
    if isinstance(data, dict):
        errors = data.get("errors", 0)
        warnings = data.get("warnings", 0)
        diagnostics = data.get("diagnostics", [])
        path = data.get("path", "")

        if fmt == "auto" and errors == 0 and warnings == 0:
            return None

        if diagnostics:
            lines = []
            if prefix:
                lines.append(prefix)
            lines.append(f"[LSP] {path}: {errors} error(s), {warnings} warning(s)")
            for d in diagnostics[:10]:
                sev = d.get("severity", "info")
                msg = d.get("message", "")
                line = d.get("line", "?")
                col = d.get("column", "")
                loc = f":{line}" + (f":{col}" if col else "")
                lines.append(f"  {sev.upper()} {path}{loc}: {msg}")
            if len(diagnostics) > 10:
                lines.append(f"  ... and {len(diagnostics) - 10} more")
            return "\n".join(lines)

        if errors > 0 or warnings > 0:
            return f"[LSP] {path}: {errors} error(s), {warnings} warning(s)"

        if fmt == "always":
            return f"[LSP] {path}: clean (no errors)"

        return None

    text = str(data)
    if fmt == "auto" and len(text) < 5:
        return None
    return f"{prefix}{text}" if prefix else text


@register_action("log", params={"message": "required", "level": "optional"})
async def _exec_log(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    template = params.get("message", "Hook fired: turn={turn} tokens={tokens}")
    level = params.get("level", "info")

    message = template.format(
        turn=state.turn,
        tokens=state.estimated_tokens,
        tools=state.tool_calls_count,
        messages=state.message_count,
        max_turns=state.max_turns,
    )

    getattr(logger, level, logger.info)("hook_log: %s", message)


@register_condition("tool_name", params={"match": "required"})
def _eval_tool_name(state: TurnState, params: dict[str, Any]) -> bool:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None:
        return False
    tool_name = getattr(tool_ctx, "tool_name", "")
    match = params.get("match", "")

    if isinstance(match, list):
        patterns = match
    elif "|" in match:
        patterns = [p.strip() for p in match.split("|")]
    else:
        patterns = [match]

    from fnmatch import fnmatch
    try:
        from digitorn.core.runtime.tool_names import to_fqn, to_short
        fqn_name = to_fqn(tool_name)
        short_name = to_short(tool_name)
    except Exception:
        fqn_name = tool_name
        short_name = tool_name
    candidates = {tool_name, fqn_name, short_name, tool_name.split(".")[-1]}

    for pattern in patterns:
        try:
            fqn_pattern = to_fqn(pattern)
            short_pattern = to_short(pattern)
        except Exception:
            fqn_pattern = pattern
            short_pattern = pattern
        pat_variants = {pattern, fqn_pattern, short_pattern}
        for cand in candidates:
            if any(fnmatch(cand, p) for p in pat_variants):
                return True
    return False


@register_condition("tool_failed", params={})
def _eval_tool_failed(state: TurnState, params: dict[str, Any]) -> bool:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None:
        return False
    ok = getattr(tool_ctx, "tool_ok", None)
    if ok is None:
        ok = getattr(tool_ctx, "tool_success", True)
    return not ok


@register_condition("content_contains", params={"keyword": "required"})
def _eval_content_contains(state: TurnState, params: dict[str, Any]) -> bool:
    keyword = params.get("keyword", "").lower()
    if not keyword:
        return False
    for msg in reversed(state.messages[-5:]):
        content = msg.get("content", "")
        if isinstance(content, str) and keyword in content.lower():
            return True
    return False


@register_condition("error_type", params={"match": "required"})
def _eval_error_type(state: TurnState, params: dict[str, Any]) -> bool:
    error_code = getattr(state, "_error_code", "") or ""
    if not error_code:
        error_ctx = getattr(state, "error_context", None)
        if error_ctx is not None:
            error_code = getattr(error_ctx, "code", "") or ""
    if not error_code:
        return False
    match = params.get("match", "")
    from fnmatch import fnmatch
    return fnmatch(error_code, match)


@register_condition("expression", params={"expr": "required"})
def _eval_expression(state: TurnState, params: dict[str, Any]) -> bool:
    expr = params.get("expr", "")
    if not expr:
        return False
    try:
        return bool(eval(expr, {"__builtins__": {}}, {
            "turn": state.turn,
            "tools": state.tool_calls_count,
            "messages": state.message_count,
            "pressure": state.token_pressure,
            "tokens": state.estimated_tokens,
            "max_turns": state.max_turns,
        }))
    except Exception:
        return False


@register_action("shell", params={"command": "required", "cwd": "optional", "timeout": "optional", "on_error": "optional"})
async def _exec_shell(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    command = params.get("command", "")
    timeout = params.get("timeout", 30.0)
    inject = params.get("inject_result", False)
    on_error = params.get("on_error", "ignore")

    if not command:
        return

    command = _render_tool_templates(command, state)
    command = command.replace("{{turn}}", str(state.turn))
    command = command.replace("{{tokens}}", str(state.estimated_tokens))

    logger.info("hook_shell: %s", command[:100])

    if context_builder is None:
        logger.warning("hook_shell refused: no context_builder available")
        return

    try:
        result = await context_builder.execute(
            "execute_tool",
            {"name": "shell.bash", "params": {
                "command": command,
                "timeout": int(timeout),
            }},
        )
    except Exception as exc:
        logger.warning("hook_shell failed to dispatch via shell.bash: %s", exc)
        return

    if not result.success:
        logger.info("hook_shell blocked: %s", result.error)
        if on_error == "inject":
            await inject_system_directive(
                getattr(state, "_agent_context", None),
                content=(
                    f'<digitorn-directive type="hook_shell_blocked" severity="warn">\n'
                    f"[Hook shell blocked] {str(result.error)[:500]}\n"
                    f"</digitorn-directive>"
                ),
                source="hook_shell_blocked",
                messages=state.messages,
                metadata={"command": command, "error": str(result.error or "")},
            )
        return

    data = result.data if isinstance(result.data, dict) else {}
    stdout_str = str(data.get("stdout", "")).strip()
    stderr_str = str(data.get("stderr", "")).strip()
    exit_code = int(data.get("exit_code", 0) or 0)

    if exit_code != 0 and on_error == "inject":
        await inject_system_directive(
            getattr(state, "_agent_context", None),
            content=(
                f'<digitorn-directive type="hook_shell_error" exit="{exit_code}" severity="warn">\n'
                f"[Hook shell error] Command: {command}\n"
                f"Exit: {exit_code}\n{stderr_str[:500]}\n"
                f"</digitorn-directive>"
            ),
            source="hook_shell_error",
            messages=state.messages,
            metadata={"command": command, "exit_code": exit_code},
        )
    elif inject and stdout_str:
        await inject_system_directive(
            getattr(state, "_agent_context", None),
            content=(
                f'<digitorn-directive type="hook_shell_stdout">\n'
                f"[Hook shell] {stdout_str[:2000]}\n"
                f"</digitorn-directive>"
            ),
            source="hook_shell_stdout",
            messages=state.messages,
            metadata={"command": command, "stdout_len": len(stdout_str)},
        )
    logger.info(
        "hook_shell: exit=%d stdout=%d stderr=%d",
        exit_code, len(stdout_str), len(stderr_str),
    )


@register_action("gate", params={"reason": "optional", "allow": "optional"})
async def _exec_gate(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    reason = params.get("reason", "Blocked by hook policy")
    state._gate_blocked = True
    state._gate_reason = reason
    logger.info("hook_gate: blocked tool - %s", reason)


@register_action("transform_params", params={"transformation": "required"})
async def _exec_transform_params(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None:
        return
    tool_params = getattr(tool_ctx, "tool_params", None)
    if not isinstance(tool_params, dict):
        return

    nested = params.get("transformation") or {}
    if not isinstance(nested, dict):
        nested = {}
    set_params = nested.get("set") or params.get("set") or {}
    remove_keys = nested.get("remove") or params.get("remove") or []

    if not isinstance(set_params, dict):
        set_params = {}
    if not isinstance(remove_keys, list):
        remove_keys = []

    for k, v in set_params.items():
        tool_params[k] = v
    for k in remove_keys:
        tool_params.pop(k, None)

    logger.info("hook_transform: set=%s remove=%s", list(set_params.keys()), remove_keys)


@register_action("transform_result", params={"transformation": "required"})
async def _exec_transform_result(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    nested = params.get("transformation") or {}
    if not isinstance(nested, dict):
        nested = {}
    append = nested.get("append_to_result") or params.get("append_to_result") or ""
    note = nested.get("inject_note") or params.get("inject_note") or ""

    if append:
        tool_ctx = getattr(state, "tool_context", None)
        if tool_ctx is not None and hasattr(tool_ctx, "tool_result"):
            tool_ctx.tool_result = str(tool_ctx.tool_result) + "\n" + append

    if note:
        await inject_system_directive(
            getattr(state, "_agent_context", None),
            content=note,
            source="hook_summary",
            messages=state.messages,
            metadata={"action": "transform_result"},
        )


@register_action("chain", params={"actions": "required"})
async def _exec_chain(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    actions = params.get("actions", [])
    stop_on_failure = bool(params.get("stop_on_failure", False))
    failed_actions: list[str] = []
    for action_def in actions:
        action_type = action_def.get("type")
        if "params" in action_def and isinstance(action_def["params"], dict):
            action_params = action_def["params"]
        else:
            action_params = {k: v for k, v in action_def.items() if k != "type"}
        action_fn = get_action(action_type)
        if action_fn is None:
            logger.warning("hook_chain: unknown action '%s'", action_type)
            failed_actions.append(action_type or "unknown")
            if stop_on_failure:
                logger.warning(
                    "hook_chain: stopping after %d failed action(s) due to stop_on_failure",
                    len(failed_actions),
                )
                break
            continue
        try:
            await action_fn(state, action_params, **kwargs)
        except Exception as exc:
            logger.warning(
                "hook_chain: action '%s' failed: %s",
                action_type, exc, exc_info=True,
            )
            failed_actions.append(action_type)
            try:
                if not hasattr(state, "metadata") or state.metadata is None:
                    state.metadata = {}
                state.metadata.setdefault("hook_failures", []).append({
                    "action": action_type,
                    "error": str(exc),
                })
            except Exception as meta_exc:
                logger.debug("hook_chain: metadata failure record failed: %s", meta_exc)
            if stop_on_failure:
                logger.warning(
                    "hook_chain: stopping after %d failed action(s) due to stop_on_failure",
                    len(failed_actions),
                )
                break


@register_action("notify", params={"title": "optional", "message": "optional", "level": "optional", "tag": "optional"})
async def _exec_notify(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    title = params.get("title", "Hook notification")
    message = params.get("message", "")
    level = params.get("level", "info")

    agent_ctx = getattr(state, "_agent_context", None)
    event_bus = getattr(agent_ctx, "_event_bus", None) if agent_ctx else None
    bus_key = getattr(agent_ctx, "_bus_key", None) if agent_ctx else None

    if event_bus is not None and bus_key is not None:
        try:
            from digitorn.core.events.envelope import (
                SessionEvent as _SE, OpType as _OT, OpState as _OS,
                gen_op_id,
            )
            _op_state = _OS.FAILED if level in ("error", "warning") else _OS.COMPLETED
            _app_id = getattr(agent_ctx, "app_id", "") or ""
            _session_id = getattr(agent_ctx, "session_id", "") or ""
            _user_id = getattr(agent_ctx, "user_id", "") or "local"
            if _app_id and _session_id:
                await event_bus.emit(_SE.build(
                    type="hook_notification",
                    app_id=_app_id, session_id=_session_id, user_id=_user_id,
                    op_id=gen_op_id("hooknotif"),
                    op_type=_OT.SYSTEM, op_state=_op_state,
                    payload={"title": title, "message": message, "level": level},
                ))
        except Exception as exc:
            logger.debug("hook_notify: event_bus emit failed: %s", exc)

    logger.info("hook_notify: [%s] %s - %s", level, title, message[:100])


@register_action("pipe", params={"to": "required", "map": "optional", "extra": "optional", "on_error": "optional"})
async def _exec_pipe(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    if context_builder is None:
        return
    to_tool = params.get("to")
    if not to_tool:
        logger.warning("hook_pipe: missing 'to' parameter")
        return

    mapping = dict(params.get("map") or {})
    extra = dict(params.get("extra") or {})
    on_error = (params.get("on_error") or "ignore").lower()

    resolved = _render_tool_templates(mapping, state)
    call_params = {**extra, **resolved}

    try:
        result = await context_builder.execute(
            "execute_tool", {"name": to_tool, "params": call_params},
        )
        if not getattr(result, "success", False):
            msg = getattr(result, "error", "unknown")
            if on_error == "raise":
                raise RuntimeError(f"pipe target {to_tool} failed: {msg}")
            if on_error == "log":
                logger.warning("hook_pipe: %s → %s", to_tool, msg)
        else:
            logger.info("hook_pipe: %s → ok", to_tool)
    except Exception as exc:
        if on_error == "raise":
            raise
        logger.warning("hook_pipe: %s failed: %s", to_tool, exc)


@register_action("lsp_diagnose", params={"path_field": "optional", "content_field": "optional", "publish": "optional", "inject_result": "optional", "read_from_disk": "optional"})
async def _exec_lsp_diagnose(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None or context_builder is None:
        return

    tool_params = getattr(tool_ctx, "tool_params", {}) or {}
    path_fields = params.get("path_field") or [
        "file_path", "path", "filepath", "filename", "file",
    ]
    if isinstance(path_fields, str):
        path_fields = [path_fields]
    content_fields = params.get("content_field") or [
        "content", "contents", "body", "text", "data",
    ]
    if isinstance(content_fields, str):
        content_fields = [content_fields]
    publish = params.get("publish", True)
    inject_result = params.get("inject_result", False)
    read_from_disk = params.get("read_from_disk", True)

    path: str | None = None
    for f in path_fields:
        v = tool_params.get(f)
        if v:
            path = str(v)
            break
    content: str | None = None
    for f in content_fields:
        v = tool_params.get(f)
        if v is not None:
            content = str(v)
            break

    if not path:
        return

    if content is None:
        tool_result = getattr(tool_ctx, "tool_result", None)
        if isinstance(tool_result, dict):
            content = tool_result.get("content")
    if content is None and read_from_disk:
        agent_ctx_early = getattr(state, "_agent_context", None)
        ws = getattr(agent_ctx_early, "workspace", "") if agent_ctx_early else ""
        import os as _os
        try_path = path
        if not _os.path.isabs(try_path) and ws:
            try_path = _os.path.join(ws, path)
        def _read_disk(p: str) -> str | None:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                return None
        content = await asyncio.to_thread(_read_disk, try_path)

    try:
        lsp_result = await context_builder.execute(
            "execute_tool",
            {
                "name": "lsp.notify_change",
                "params": {
                    "path": path,
                    **({"content": content} if content is not None else {}),
                },
            },
        )
    except Exception as exc:
        logger.debug("hook_lsp_diagnose: notify_change failed: %s", exc)
        return

    agent_ctx_for_sync = getattr(state, "_agent_context", None)
    ws_mod = (
        getattr(agent_ctx_for_sync, "workspace_module", None)
        if agent_ctx_for_sync else None
    )
    if ws_mod is not None and hasattr(ws_mod, "_sync_from_disk"):
        try:
            await ws_mod._sync_from_disk()
        except Exception as exc:
            logger.debug("hook_lsp_diagnose: workspace sync_from_disk failed: %s", exc)

    flat_items: list[dict[str, Any]] = []
    if getattr(lsp_result, "success", False) and getattr(lsp_result, "data", None):
        flat_items = lsp_result.data.get("diagnostics") or []

    if inject_result and flat_items:
        errors = [d for d in flat_items if d.get("severity") == "error"]
        warnings = [d for d in flat_items if d.get("severity") not in (None, "error")]
        summary = {
            "lint": flat_items,
            "errors": len(errors),
            "warnings": len(warnings),
        }
        try:
            current = tool_ctx.tool_result
            if isinstance(current, dict):
                merged = dict(current)
                merged.setdefault("lint", flat_items)
                merged.setdefault("errors", summary["errors"])
                merged.setdefault("warnings", summary["warnings"])
                tool_ctx.tool_result = merged
            else:
                import json as _json
                suffix = (
                    f"\n\n[lsp_diagnose] {summary['errors']} error(s), "
                    f"{summary['warnings']} warning(s):\n"
                    + _json.dumps(flat_items[:10], ensure_ascii=False, indent=2)
                )
                tool_ctx.tool_result = str(current or "") + suffix
        except Exception as exc:
            logger.debug("hook_lsp_diagnose: inject_result failed: %s", exc)

    if not publish:
        return

    items = flat_items
    severity_max = None
    if flat_items:
        try:
            from digitorn.modules.lsp.parsers import Diagnostic as _Diag
            lsp_items: list[dict[str, Any]] = []
            severities: set[str] = set()
            for it in items:
                sev = (it.get("severity") or "info").lower()
                severities.add(sev)
                d = _Diag(
                    file=it.get("file", path),
                    line=int(it.get("line") or 1),
                    column=int(it.get("column") or 1),
                    severity=sev,
                    message=it.get("message", ""),
                    code=str(it.get("code") or ""),
                    source=str(it.get("source") or ""),
                )
                lsp_items.append(d.to_lsp_dict())
            order = ("error", "warning", "info", "hint")
            severity_max = next((s for s in order if s in severities), None)
            items = lsp_items
        except Exception as exc:
            logger.debug("lsp_diagnose: items conversion failed: %s", exc)

    agent_ctx = getattr(state, "_agent_context", None)
    preview_module = getattr(agent_ctx, "preview_module", None) if agent_ctx else None
    if preview_module is None:
        return
    try:
        import time as _time
        from digitorn.modules.preview.module import SetResourceParams
        await preview_module.set_resource(SetResourceParams(
            channel="diagnostics",
            id=path,
            payload={
                "file_path": path,
                "items": items,
                "severity_max": severity_max,
                "updated_at": _time.time(),
                "source_module": "hook",
            },
        ))
    except Exception as exc:
        logger.debug("hook_lsp_diagnose: publish failed: %s", exc)


def _yaml_pre_validate(content: str) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    try:
        from digitorn.core.app.yaml_loader import safe_load_strict
        parsed = safe_load_strict(content)
    except Exception as exc:
        return [{
            "path": "<root>",
            "message": f"YAML parse error: {exc}",
            "hint": "Fix YAML syntax - most common: unquoted 'on:' (YAML 1.1 "
                    "boolean trap - use '\"on\":' instead).",
        }]

    if not isinstance(parsed, dict):
        return [{
            "path": "<root>",
            "message": "Top-level must be a mapping.",
            "hint": "Start with 'app:', 'modules:', 'agents:', 'execution:', 'capabilities:'.",
        }]

    app = parsed.get("app")
    if not isinstance(app, dict):
        stray = [k for k in ("name", "description", "version", "icon", "color", "author", "tags", "category") if k in parsed]
        if stray:
            errors.append({
                "path": "app",
                "message": "Missing 'app:' block - metadata is at root instead.",
                "hint": f"Move these under 'app:': {stray}. Required: app.app_id (kebab-case).",
            })
        else:
            errors.append({
                "path": "app",
                "message": "Missing or invalid 'app' block.",
                "hint": "Add: app:\\n  app_id: kebab-case-id\\n  name: ...\\n  version: '1.0.0'",
            })
    else:
        if not app.get("app_id"):
            if app.get("id"):
                errors.append({
                    "path": "app.app_id",
                    "message": "Found 'app.id' - the field is named 'app_id', not 'id'.",
                    "hint": "Rename app.id → app.app_id (kebab-case).",
                })
            else:
                errors.append({"path": "app.app_id", "message": "'app.app_id' is required.", "hint": "kebab-case, unique."})
        stray_in_app = [k for k in ("mode", "max_tokens_per_turn", "max_turns", "settings", "timeout", "entry_agent", "triggers") if k in app]
        if stray_in_app:
            errors.append({
                "path": "app",
                "message": f"Fields {stray_in_app} are not valid under 'app:' - belong under 'execution:'.",
                "hint": "execution:\\n  mode: conversation\\n  entry_agent: <id>\\n  max_turns: 20",
            })
        misnested = {
            "agents": "agents (top-level LIST)",
            "modules": "modules (top-level DICT keyed by module_id)",
            "capabilities": "capabilities (top-level)",
            "execution": "execution (top-level)",
            "workspace": "workspace (top-level block for render_mode / entry_file)",
            "preview": "preview (top-level block, or under modules.preview)",
            "hooks": "execution.hooks (LIST under execution)",
        }
        misplaced = [k for k in misnested if k in app]
        for k in misplaced:
            errors.append({
                "path": f"app.{k}",
                "message": f"'app.{k}' is not valid - move to root as {misnested[k]}.",
                "hint": f"Remove '{k}:' from under 'app:' and put it at the document root.",
            })

    modules = parsed.get("modules")
    if modules is not None and not isinstance(modules, dict):
        errors.append({
            "path": "modules",
            "message": "'modules' must be a mapping keyed by module_id.",
            "hint": "modules:\\n  memory:\\n    config: {...}",
        })
    elif isinstance(modules, dict):
        for mid, mcfg in modules.items():
            if mcfg is None:
                continue
            if not isinstance(mcfg, dict):
                errors.append({
                    "path": f"modules.{mid}",
                    "message": "Module config must be a mapping or null.",
                    "hint": "Use '{}' for defaults.",
                })
                continue
            if "type" in mcfg:
                errors.append({
                    "path": f"modules.{mid}.type",
                    "message": "'type' is not a valid module field.",
                    "hint": "Modules are keyed by id - delete the 'type:' line.",
                })
            allowed = {"config", "setup", "constraints", "middleware"}
            misplaced_in_module = {
                "capabilities": (
                    "capabilities belong at the ROOT of the YAML, not inside a module. "
                    "Use top-level `capabilities.grant: [{module: %s, actions: [...]}]` "
                    "to grant this module to an agent." % mid
                ),
                "grants": "See 'capabilities' - same location hint.",
                "actions": (
                    "Actions aren't declared here. They're discovered from the "
                    "module's manifest. To grant them, list them under "
                    "capabilities.grant[].actions at the document root."
                ),
            }
            for key in mcfg.keys():
                if key in allowed:
                    continue
                if key in {"type"}:
                    continue
                if key in misplaced_in_module:
                    errors.append({
                        "path": f"modules.{mid}.{key}",
                        "message": f"'{key}' inside a module is invalid.",
                        "hint": misplaced_in_module[key],
                    })
                    continue
                errors.append({
                    "path": f"modules.{mid}.{key}",
                    "message": f"'{key}' is not a recognised field on a module.",
                    "hint": f"Wrap it under 'config:' - e.g. modules.{mid}.config.{key}.",
                })

    agents = parsed.get("agents")
    if agents is not None:
        if isinstance(agents, dict):
            errors.append({
                "path": "agents",
                "message": "'agents' must be a LIST, not a mapping.",
                "hint": "agents:\\n  - id: my-agent\\n    brain: {...}",
            })
        elif isinstance(agents, list):
            for idx, ag in enumerate(agents):
                if not isinstance(ag, dict):
                    errors.append({
                        "path": f"agents[{idx}]",
                        "message": "Agent entry must be a mapping.",
                        "hint": "agents: - id: ...",
                    })
                    continue
                if not ag.get("id"):
                    if ag.get("name"):
                        errors.append({
                            "path": f"agents[{idx}].id",
                            "message": f"Agent uses 'name' instead of 'id' (found name='{ag.get('name')}').",
                            "hint": "Rename 'name' → 'id'. Each agent needs a unique id.",
                        })
                    else:
                        errors.append({
                            "path": f"agents[{idx}].id",
                            "message": "Agent is missing 'id'.",
                            "hint": "Each agent needs a unique id.",
                        })
                if "model" in ag and "brain" not in ag:
                    errors.append({
                        "path": f"agents[{idx}].model",
                        "message": "'model' at agent top-level - belongs in brain.model.",
                        "hint": "brain:\\n  provider: ...\\n  model: ...",
                    })
                if "llm" in ag and "brain" not in ag:
                    errors.append({
                        "path": f"agents[{idx}].llm",
                        "message": "Agent uses 'llm:' - the field is named 'brain:' in Digitorn.",
                        "hint": "Rename llm → brain. brain:\\n  provider: <id>\\n  model: <name>\\n  backend: openai_compat\\n  config: {api_key: '{{env.PROVIDER_KEY}}'}",
                    })
                if "prompt" in ag and "system_prompt" not in ag:
                    errors.append({
                        "path": f"agents[{idx}].prompt",
                        "message": "'prompt' is not a valid agent field - use 'system_prompt'.",
                        "hint": "system_prompt: |\\n  You are ...",
                    })
                if "mode" in ag:
                    errors.append({
                        "path": f"agents[{idx}].mode",
                        "message": "'mode' is not a valid agent field - execution.mode sets it globally.",
                        "hint": "Remove 'mode' from the agent; set execution.mode at the root.",
                    })
                brain = ag.get("brain")
                if isinstance(brain, dict):
                    provider = (brain.get("provider") or "").lower()
                    cfg = brain.get("config") or {}
                    api_key = cfg.get("api_key", "")
                    if provider == "anthropic":
                        if api_key and "DEEPSEEK" in str(api_key).upper():
                            errors.append({
                                "path": f"agents[{idx}].brain.config.api_key",
                                "message": "Anthropic brain configured with a DeepSeek key.",
                                "hint": "Use 'claude-code' or '{{env.ANTHROPIC_API_KEY}}'.",
                            })
                    elif provider and api_key == "claude-code":
                        errors.append({
                            "path": f"agents[{idx}].brain.config.api_key",
                            "message": f"'claude-code' is only valid for provider: anthropic (got '{provider}').",
                            "hint": f"Use '{{{{env.{provider.upper()}_API_KEY}}}}' instead.",
                        })
                    mt = brain.get("max_tokens")
                    model = str(brain.get("model") or "")
                    if provider == "deepseek" and isinstance(mt, int) and mt > 8192:
                        errors.append({
                            "path": f"agents[{idx}].brain.max_tokens",
                            "message": f"DeepSeek caps max_tokens at 8192 (got {mt}).",
                            "hint": "Set max_tokens: 8192.",
                        })
                    if provider and model and provider not in model.lower() and provider == "deepseek" and "deepseek" not in model.lower():
                        errors.append({
                            "path": f"agents[{idx}].brain.model",
                            "message": f"Provider 'deepseek' with non-deepseek model '{model}'.",
                            "hint": "Use deepseek-chat or deepseek-reasoner.",
                        })

    execution = parsed.get("execution")
    if not isinstance(execution, dict):
        errors.append({
            "path": "execution",
            "message": "Missing 'execution' block.",
            "hint": "execution:\\n  mode: conversation\\n  entry_agent: <agent-id>",
        })
    else:
        mode = execution.get("mode")
        if mode not in (None, "conversation", "one_shot", "background"):
            errors.append({
                "path": "execution.mode",
                "message": f"Unknown execution.mode: '{mode}'.",
                "hint": "Valid: conversation | one_shot | background.",
            })
        triggers = execution.get("triggers") or []
        if mode == "background" and not triggers:
            errors.append({
                "path": "execution.triggers",
                "message": "mode: background requires at least one trigger.",
                "hint": "triggers:\\n  - type: cron\\n    expression: '*/5 * * * *'",
            })
        if isinstance(triggers, list):
            for tidx, tr in enumerate(triggers):
                if isinstance(tr, dict) and "pattern" in tr and "type" not in tr:
                    errors.append({
                        "path": f"execution.triggers[{tidx}]",
                        "message": "Trigger has 'pattern' but no 'type'.",
                        "hint": "Triggers use 'type:' (cron|http|file_watcher|webhook).",
                    })

    caps = parsed.get("capabilities")
    if isinstance(caps, dict):
        grant = caps.get("grant")
        if isinstance(grant, list):
            for gidx, g in enumerate(grant):
                if isinstance(g, str):
                    errors.append({
                        "path": f"capabilities.grant[{gidx}]",
                        "message": "grant entries must be objects, not strings.",
                        "hint": "- module: memory\\n  actions: [remember, recall]",
                    })
                elif isinstance(g, dict):
                    if "module" not in g:
                        errors.append({
                            "path": f"capabilities.grant[{gidx}].module",
                            "message": "grant entry missing 'module'.",
                            "hint": "- module: <id>\\n  actions: [...]",
                        })
                    if "actions" not in g:
                        errors.append({
                            "path": f"capabilities.grant[{gidx}].actions",
                            "message": "grant entry missing 'actions'.",
                            "hint": "actions: [list, of, action, names]",
                        })

    for forbidden in ("workflows", "ui", "deploy", "implementation", "personality", "credentials", "settings", "llm", "tools"):
        if forbidden in parsed:
            if forbidden == "credentials":
                hint = "Use {{env.VAR_NAME}} or {{secret.KEY}} placeholders inside brain.config.api_key - no top-level credentials block."
            elif forbidden == "llm":
                hint = "Per-agent brain config - move under agents[i].brain."
            elif forbidden == "tools":
                hint = "Tool access is controlled via capabilities.grant[i].actions, not a top-level 'tools' block."
            elif forbidden == "settings":
                hint = "Put runtime knobs under execution: (timeout, max_turns) or per-agent brain.max_tokens/temperature."
            else:
                hint = "Valid roots: app, modules, agents, execution, capabilities, workspace, preview, hooks, skills, channels."
            errors.append({
                "path": forbidden,
                "message": f"'{forbidden}' is not a valid root key.",
                "hint": hint,
            })

    if "mode" in parsed and not isinstance(parsed.get("execution"), dict):
        errors.append({
            "path": "mode",
            "message": "'mode' at root - belongs under 'execution.mode'.",
            "hint": "execution:\\n  mode: conversation  # or one_shot | background",
        })

    return errors


@register_action(
    "compile_yaml",
    params={
        "path_field": "optional",
        "content_field": "optional",
        "only_path": "optional",
        "inject_result": "optional",
    },
)
async def _exec_compile_yaml(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None or context_builder is None:
        return

    tool_params = getattr(tool_ctx, "tool_params", {}) or {}
    path_fields = params.get("path_field") or ["path", "file_path"]
    if isinstance(path_fields, str):
        path_fields = [path_fields]
    content_fields = params.get("content_field") or ["content"]
    if isinstance(content_fields, str):
        content_fields = [content_fields]
    only_path = params.get("only_path")
    inject_result = params.get("inject_result", True)

    path: str | None = None
    for f in path_fields:
        v = tool_params.get(f)
        if v:
            path = str(v)
            break
    if not path:
        return

    if only_path:
        import os as _os
        base = _os.path.basename(path)
        if only_path not in (path, base):
            return

    content: str | None = None
    for f in content_fields:
        v = tool_params.get(f)
        if v is not None:
            content = str(v)
            break

    if content is None:
        tool_result = getattr(tool_ctx, "tool_result", None)
        if isinstance(tool_result, dict):
            content = tool_result.get("content")
    if content is None:
        agent_ctx = getattr(state, "_agent_context", None)
        ws = getattr(agent_ctx, "workspace", "") if agent_ctx else ""
        import os as _os
        try_path = path
        if not _os.path.isabs(try_path) and ws:
            try_path = _os.path.join(ws, path)

        def _read_disk(p: str) -> str | None:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                return None
        content = await asyncio.to_thread(_read_disk, try_path)

    if not content:
        return

    pre_errors = _yaml_pre_validate(content)

    try:
        compile_res = await context_builder.execute(
            "execute_tool",
            {
                "name": "dev_tools.app",
                "params": {
                    "yaml_content": content,
                    "compile_yaml": True,
                },
            },
        )
    except Exception as exc:
        logger.debug("hook_compile_yaml: exec failed: %s", exc)
        return

    ok = bool(getattr(compile_res, "success", False))
    data = getattr(compile_res, "data", None) or {}
    errors: list[Any] = []
    if isinstance(data, dict):
        raw_errs = data.get("errors") or []
        if isinstance(raw_errs, list):
            errors = raw_errs
        single = data.get("error")
        if single and not errors:
            errors = [str(single)]
    verdict = {
        "success": ok and len(errors) == 0 and len(pre_errors) == 0,
        "errors": errors,
        "error_count": len(errors),
        "pre_errors": pre_errors,
        "pre_error_count": len(pre_errors),
    }

    if inject_result:
        try:
            current = getattr(tool_ctx, "tool_result", None)
            if isinstance(current, dict):
                merged = dict(current)
                merged.setdefault("compile", verdict)
                if not verdict["success"]:
                    merged["compile_failed"] = True
                    merged["next_step_hint"] = (
                        "Fix the errors listed under `compile.errors` "
                        "with WsEdit, then WsWrite the corrected YAML - "
                        "the post-write hook will recompile automatically."
                    )
                tool_ctx.tool_result = merged
            else:
                import json as _json
                suffix = (
                    f"\n\n[compile_yaml] success={verdict['success']} "
                    f"errors={verdict['error_count']}\n"
                    + (_json.dumps(errors[:10], ensure_ascii=False, indent=2) if errors else "")
                )
                tool_ctx.tool_result = str(current or "") + suffix
        except Exception as exc:
            logger.debug("hook_compile_yaml: inject failed: %s", exc)

    agent_ctx = getattr(state, "_agent_context", None)
    ws_mod = getattr(agent_ctx, "workspace_module", None) if agent_ctx else None
    if ws_mod is None:
        return
    try:
        import json as _json
        from digitorn.modules.workspace.module import WriteParams
        payload = _json.dumps(
            {
                "status": "success" if verdict["success"] else "error",
                "errors": errors,
                "error_count": verdict["error_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
        await ws_mod.write(WriteParams(path="_state/compile.json", content=payload))
    except Exception as exc:
        logger.debug("hook_compile_yaml: state write failed: %s", exc)


@register_action(
    "auto_test_deploy",
    params={
        "smoke_message": "optional",
        "timeout": "optional",
        "inject_result": "optional",
        "only_on_deploy": "optional",
    },
)
async def _exec_auto_test_deploy(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    tool_ctx = getattr(state, "tool_context", None)
    if tool_ctx is None or context_builder is None:
        return

    tool_params = getattr(tool_ctx, "tool_params", {}) or {}
    tool_result = getattr(tool_ctx, "tool_result", None)
    tool_name = getattr(tool_ctx, "tool_name", "") or ""

    only_on_deploy = params.get("only_on_deploy", True)

    try:
        from digitorn.core.runtime.tool_names import to_fqn
        tool_fqn = to_fqn(tool_name)
    except Exception:
        tool_fqn = tool_name

    is_deploy = False
    if tool_params.get("deploy_draft_id"):
        is_deploy = True
    elif tool_fqn == "dev_tools.app":
        validate_only = bool(tool_params.get("validate_only"))
        compile_only = bool(tool_params.get("compile_yaml"))
        prompt_only = bool(tool_params.get("prompt_preview"))
        gen_manifest = bool(tool_params.get("generate_manifest"))
        has_yaml = bool(tool_params.get("yaml_path")) or bool(tool_params.get("yaml_content"))
        if has_yaml and not (validate_only or compile_only or prompt_only or gen_manifest):
            is_deploy = True
    else:
        url = str(tool_params.get("url") or tool_params.get("path") or "")
        method = str(tool_params.get("method") or "").upper()
        if "/api/apps/deploy" in url or url.rstrip("/").endswith("/apps/deploy"):
            if tool_fqn.startswith("http.") and method in ("", "POST"):
                is_deploy = True

    if only_on_deploy and not is_deploy:
        return

    app_id: str | None = None
    if isinstance(tool_result, dict):
        candidates: list[Any] = [tool_result]
        for key in ("data", "body"):
            v = tool_result.get(key)
            if isinstance(v, dict):
                candidates.append(v)
                nested = v.get("data")
                if isinstance(nested, dict):
                    candidates.append(nested)
        for c in candidates:
            aid = c.get("app_id") if isinstance(c, dict) else None
            if aid:
                app_id = str(aid)
                break
    if not app_id:
        return

    smoke_msg = params.get("smoke_message") or "Hello - are you up?"
    timeout = float(params.get("timeout") or 60)
    inject_result = params.get("inject_result", True)

    agent_ctx = getattr(state, "_agent_context", None)
    ws_mod = getattr(agent_ctx, "workspace_module", None) if agent_ctx else None

    import json as _json
    import time as _time
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    if ws_mod is not None:
        try:
            from digitorn.modules.workspace.module import WriteParams
            await ws_mod.write(WriteParams(
                path="_state/deploy.json",
                content=_json.dumps(
                    {"status": "success", "app_id": app_id, "deployed_at": now_iso},
                    ensure_ascii=False, indent=2,
                ),
            ))
        except Exception as exc:
            logger.debug("hook_auto_test_deploy: deploy.json write failed: %s", exc)

    t0 = _time.monotonic()
    try:
        chat_res = await context_builder.execute(
            "execute_tool",
            {
                "name": "dev_tools.chat",
                "params": {
                    "app_id": app_id,
                    "message": smoke_msg,
                    "watch": True,
                    "timeout": timeout,
                },
            },
        )
    except Exception as exc:
        logger.debug("hook_auto_test_deploy: chat exec failed: %s", exc)
        chat_res = None

    ok = bool(getattr(chat_res, "success", False))
    data = getattr(chat_res, "data", None) or {}
    response_text = ""
    tools_used: list[str] = []
    if isinstance(data, dict):
        response_text = str(data.get("text") or "")[:600]
        for tc in data.get("tool_calls") or []:
            if isinstance(tc, dict):
                n = tc.get("name")
                if n:
                    tools_used.append(str(n))
    duration = round(_time.monotonic() - t0, 2)

    entry = {
        "message": smoke_msg,
        "response": response_text,
        "success": ok,
        "duration_s": duration,
        "tools_used": tools_used,
        "at": now_iso,
    }
    if not ok:
        err = getattr(chat_res, "error", None) or (data.get("status") if isinstance(data, dict) else None)
        if err:
            entry["error"] = str(err)[:400]

    if ws_mod is not None:
        try:
            from digitorn.modules.workspace.module import ReadParams, WriteParams
            existing: dict[str, Any] = {"tests": [], "last_run_at": None}
            try:
                read_res = await ws_mod.read(ReadParams(path="_state/tests.json"))
                if getattr(read_res, "success", False):
                    raw = (getattr(read_res, "data", None) or {}).get("content") or ""
                    stripped = "\n".join(
                        line.split("\t", 1)[1] if "\t" in line else line
                        for line in raw.split("\n")
                    )
                    if stripped.strip():
                        parsed = _json.loads(stripped)
                        if isinstance(parsed, dict):
                            existing = parsed
            except Exception as exc:
                logger.debug("test history read parse failed: %s", exc)
            tests = existing.get("tests") or []
            if not isinstance(tests, list):
                tests = []
            tests.append(entry)
            payload = _json.dumps(
                {"tests": tests, "last_run_at": now_iso},
                ensure_ascii=False, indent=2,
            )
            await ws_mod.write(WriteParams(path="_state/tests.json", content=payload))
        except Exception as exc:
            logger.debug("hook_auto_test_deploy: tests.json write failed: %s", exc)

    if inject_result:
        try:
            current = tool_result
            if isinstance(current, dict):
                merged = dict(current)
                merged["auto_test"] = {"success": ok, "duration_s": duration}
                if not ok:
                    merged["auto_test_failed"] = True
                    merged["next_step_hint"] = (
                        "The post-deploy smoke test failed. Read "
                        "`_state/tests.json` for details, fix the app "
                        "YAML or UI files, recompile and redeploy."
                    )
                tool_ctx.tool_result = merged
        except Exception as exc:
            logger.debug("hook_auto_test_deploy: inject failed: %s", exc)


@register_action(
    "prefetch_ground_truth",
    params={
        "include_modules": "optional",
        "include_triggers": "optional",
        "include_templates": "optional",
        "include_examples": "optional",
        "examples_query": "optional",
        "examples_k": "optional",
    },
)
async def _exec_prefetch_ground_truth(
    state: TurnState,
    params: dict[str, Any],
    *,
    context_builder: Any = None,
    **kwargs: Any,
) -> None:
    if context_builder is None:
        return
    if state.turn != 0:
        return

    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return
    if getattr(runtime, "_ground_truth_prefetched", False):
        return

    build_keywords = (
        "build", "create", "make", "generate", "deploy", "compile",
        "agent", "chatbot", "app", "tool", "automation", "workflow",
        "trigger", "schedule", "cron", "webhook", "rag", "pipeline",
        "construis", "crée", "créer", "fais", "déploie", "bâtis",
        "assistant",
    )
    last_user = ""
    for msg in reversed(state.messages):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            last_user = msg["content"].lower()
            break
    has_intent = any(kw in last_user for kw in build_keywords)
    if not has_intent and len(last_user.strip()) < 40:
        logger.info(
            "prefetch_ground_truth: skipped - message is a short non-build turn (%d chars)",
            len(last_user.strip()),
        )
        return

    runtime._ground_truth_prefetched = True

    inc_modules = params.get("include_modules", True)
    inc_triggers = params.get("include_triggers", True)
    inc_templates = params.get("include_templates", True)
    inc_examples = params.get("include_examples", True)
    ex_query = params.get("examples_query") or "canonical digitorn app example"
    ex_k = int(params.get("examples_k") or 3)

    blocks: list[str] = []

    async def _call(name: str, tool_params: dict[str, Any]) -> Any:
        try:
            r = await context_builder.execute(
                "execute_tool", {"name": name, "params": tool_params}
            )
            if getattr(r, "success", False):
                return getattr(r, "data", None)
        except Exception as exc:
            logger.debug("prefetch_ground_truth: %s failed: %s", name, exc)
        return None

    import json as _json

    if inc_modules:
        data = await _call("dev_tools.app", {"list_modules": True})
        if data:
            blocks.append(
                "## Available modules (ground truth)\n"
                + _json.dumps(data, ensure_ascii=False, indent=2)[:4000]
            )

    if inc_triggers:
        data = await _call("dev_tools.app", {"list_triggers": True})
        if data:
            blocks.append(
                "## Available trigger types\n"
                + _json.dumps(data, ensure_ascii=False, indent=2)[:2000]
            )

    if inc_templates:
        data = await _call("dev_tools.app", {"list_templates": True})
        if data:
            blocks.append(
                "## Available templates\n"
                + _json.dumps(data, ensure_ascii=False, indent=2)[:2000]
            )

    if inc_examples:
        data = await _call(
            "rag.query",
            {"knowledge_base": "digitorn_examples", "query": ex_query, "k": ex_k},
        )
        if data:
            blocks.append(
                "## Canonical examples (RAG)\n"
                + _json.dumps(data, ensure_ascii=False, indent=2)[:6000]
            )

    if not blocks:
        return

    prefix = (
        "=== PHASE 0 GROUND TRUTH (pre-fetched, do not re-run) ===\n\n"
        + "\n\n".join(blocks)
        + "\n\n=== END GROUND TRUTH ==="
    )

    for i in range(len(state.messages) - 1, -1, -1):
        msg = state.messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            msg["content"] = prefix + "\n\n" + msg["content"]
            return

    for msg in state.messages:
        if msg.get("role") == "system" and isinstance(msg.get("content"), str):
            msg["content"] = msg["content"] + "\n\n" + prefix
            return


@register_action(
    "enforce_phase6",
    params={"max_reminders": "optional"},
)
async def _exec_enforce_phase6(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    agent_ctx = getattr(state, "_agent_context", None)
    ws_mod = getattr(agent_ctx, "workspace_module", None) if agent_ctx else None
    if ws_mod is None:
        return

    import json as _json
    from digitorn.modules.workspace.module import ReadParams

    async def _read_json(path: str) -> dict | None:
        try:
            r = await ws_mod.read(ReadParams(path=path))
            if not getattr(r, "success", False):
                return None
            raw = (getattr(r, "data", None) or {}).get("content") or ""
            stripped = "\n".join(
                line.split("\t", 1)[1] if "\t" in line else line
                for line in raw.split("\n")
            )
            if not stripped.strip():
                return None
            parsed = _json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    deploy = await _read_json("_state/deploy.json")
    if not deploy or deploy.get("status") != "success":
        return

    tests = await _read_json("_state/tests.json")
    has_success = False
    app_id_tested: str | None = None
    if isinstance(tests, dict):
        for t in tests.get("tests") or []:
            if isinstance(t, dict) and t.get("success"):
                has_success = True
                break
    if has_success:
        return

    max_reminders = int(params.get("max_reminders") or 3)
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return
    store = getattr(runtime, "_smoke_test_reminders", None)
    if store is None:
        runtime._smoke_test_reminders = 0
        store = 0
    if store >= max_reminders:
        return
    runtime._smoke_test_reminders = store + 1

    app_id_tested = deploy.get("app_id") or "<app_id>"
    reminder = (
        f"Smoke test not complete: you deployed '{app_id_tested}' but "
        f"_state/tests.json has no entry with success=true. "
        f"Immediately run Chat(app_id='{app_id_tested}', "
        f"message='<smoke prompt>', watch=true) and stop only when the "
        f"response matches the brief."
    )
    for i in range(len(state.messages) - 1, -1, -1):
        msg = state.messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            msg["content"] = msg["content"] + f"\n\n[System note: {reminder}]"
            return


@register_action(
    "enforce_compile_fix",
    params={"max_reminders": "optional"},
)
async def _exec_enforce_compile_fix(
    state: TurnState,
    params: dict[str, Any],
    **kwargs: Any,
) -> None:
    agent_ctx = getattr(state, "_agent_context", None)
    ws_mod = getattr(agent_ctx, "workspace_module", None) if agent_ctx else None
    if ws_mod is None:
        return

    import json as _json
    from digitorn.modules.workspace.module import ReadParams

    try:
        r = await ws_mod.read(ReadParams(path="_state/compile.json"))
        if not getattr(r, "success", False):
            return
        raw = (getattr(r, "data", None) or {}).get("content") or ""
        stripped = "\n".join(
            line.split("\t", 1)[1] if "\t" in line else line
            for line in raw.split("\n")
        )
        if not stripped.strip():
            return
        parsed = _json.loads(stripped)
    except Exception:
        return
    if not isinstance(parsed, dict):
        return
    if parsed.get("status") == "success":
        return
    errors = parsed.get("errors") or []
    pre_errors = parsed.get("pre_errors") or []
    if not errors and not pre_errors:
        return

    max_reminders = int(params.get("max_reminders") or 5)
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return
    store = getattr(runtime, "_compile_fix_reminders", 0)
    if store >= max_reminders:
        return
    runtime._compile_fix_reminders = store + 1

    preview_list = [str(e) for e in (pre_errors[:3] + errors[:3])]
    reminder = (
        "Compile still failing - _state/compile.json has "
        f"{len(errors)} schema error(s) and {len(pre_errors)} pre-validator "
        f"hint(s). Do NOT stop the turn - immediately fix app.yaml. "
        f"Top issues: {preview_list}. "
        f"Use WsEdit for surgical fixes, then WsWrite the full corrected "
        f"YAML so the compile hook re-validates."
    )

    for i in range(len(state.messages) - 1, -1, -1):
        msg = state.messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            msg["content"] = msg["content"] + f"\n\n[System note: {reminder}]"
            return


class HookRunner:
    def __init__(
        self,
        hooks: list[Hook],
        *,
        provider: Any = None,
        summary_provider: Any = None,
        context_builder: Any = None,
        on_hook_event: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self._hooks = [
            RuntimeHook(hook=h)
            for h in sorted(hooks, key=lambda x: x.priority)
        ]
        self._provider = provider
        self._summary_provider = summary_provider
        self._context_builder = context_builder
        self._on_hook_event = on_hook_event

    @property
    def hook_count(self) -> int:
        return len(self._hooks)

    @property
    def on_hook_event(self) -> Callable[..., Awaitable[None]] | None:
        return self._on_hook_event

    @on_hook_event.setter
    def on_hook_event(self, value: Callable[..., Awaitable[None]] | None) -> None:
        self._on_hook_event = value

    async def _emit(self, hook_id: str, action_type: str, phase: str, details: dict[str, Any] | None = None) -> None:
        if self._on_hook_event is None:
            return
        from digitorn.core.runtime.types import HookEvent
        event = HookEvent(
            hook_id=hook_id,
            action_type=action_type,
            phase=phase,
            details=details or {},
        )
        try:
            await self._on_hook_event(event)
        except Exception as exc:
            logger.warning("hook_event_emit_error: %s", exc)

    _EVENT_ALIASES = {
        "pre_tool_use": "tool_start",
        "post_tool_use": "tool_end",
        "user_prompt": "turn_start",
    }

    async def run(self, event: str, state: TurnState) -> list[str]:
        fired: list[str] = []

        current_agent = getattr(state, "agent_id", None)

        for rh in self._hooks:
            declared_on = rh.hook.on
            canonical = self._EVENT_ALIASES.get(declared_on, declared_on)
            if canonical != event and declared_on != event:
                continue

            scope_agent = rh.hook.agent_id
            if scope_agent is not None and scope_agent != current_agent:
                continue

            if not rh.can_fire:
                continue

            condition_fn = get_condition(rh.hook.condition.type)
            if condition_fn is None:
                logger.warning(
                    "hook_eval: unknown condition '%s' for hook '%s'",
                    rh.hook.condition.type, rh.hook.id,
                )
                continue

            try:
                should_fire = condition_fn(state, rh.hook.condition.params)
            except Exception as exc:
                logger.warning(
                    "hook_eval: condition '%s' error for hook '%s': %s",
                    rh.hook.condition.type, rh.hook.id, exc,
                )
                continue

            if not should_fire:
                continue

            action_fn = get_action(rh.hook.action.type)
            if action_fn is None:
                logger.warning(
                    "hook_exec: unknown action '%s' for hook '%s'",
                    rh.hook.action.type, rh.hook.id,
                )
                continue

            try:
                state._estimated_tokens = None

                _tokens_before = state.estimated_tokens
                await self._emit(
                    rh.hook.id, rh.hook.action.type, "start",
                    {"condition": rh.hook.condition.type, "params": rh.hook.action.params},
                )

                action_provider = self._provider
                if rh.hook.action.type == "compact_context" and self._summary_provider is not None:
                    action_provider = self._summary_provider

                _hook_timeout = rh.hook.timeout if rh.hook.timeout > 0 else 30.0

                _is_compact = rh.hook.action.type == "compact_context"
                _msg_snapshot: list[dict[str, Any]] | None = None
                if _is_compact:
                    try:
                        _msg_snapshot = list(state.messages)
                    except Exception:
                        _msg_snapshot = None

                try:
                    with internal_call_scope():
                        await asyncio.wait_for(
                            action_fn(
                                state,
                                rh.hook.action.params,
                                provider=action_provider,
                                context_builder=self._context_builder,
                            ),
                            timeout=_hook_timeout,
                        )
                except asyncio.TimeoutError:
                    logger.warning(
                        "hook_timeout: hook '%s' (action=%s) exceeded "
                        "%.1fs and was cancelled. Adjust the hook's "
                        "`timeout` field if this is expected.",
                        rh.hook.id, rh.hook.action.type, _hook_timeout,
                    )
                    if _is_compact and _msg_snapshot is not None:
                        try:
                            state.messages.clear()
                            state.messages.extend(_msg_snapshot)
                            state._estimated_tokens = None  # type: ignore[attr-defined]
                            logger.warning(
                                "hook_timeout: restored %d messages from "
                                "pre-compaction snapshot for hook '%s'",
                                len(_msg_snapshot), rh.hook.id,
                            )
                        except Exception as restore_exc:
                            logger.exception(
                                "hook_timeout: failed to restore "
                                "messages snapshot for '%s': %s",
                                rh.hook.id, restore_exc,
                            )
                    await self._emit(
                        rh.hook.id, rh.hook.action.type, "error",
                        {"error": f"timeout after {_hook_timeout}s"},
                    )
                    continue

                rh.mark_fired()
                fired.append(rh.hook.id)

                end_details: dict[str, Any] = {"fire_count": rh.fire_count}
                if rh.hook.action.type == "compact_context":
                    end_details["strategy"] = rh.hook.action.params.get("strategy", "truncate")
                    end_details["tokens_before"] = _tokens_before
                    end_details["tokens_after"] = state.estimated_tokens
                    reduced = _tokens_before - state.estimated_tokens
                    end_details["tokens_reduced"] = reduced
                if rh.hook.action.type == "log":
                    template = rh.hook.action.params.get("message", "")
                    end_details["message"] = template.format(
                        turn=state.turn,
                        tokens=state.estimated_tokens,
                        tools=state.tool_calls_count,
                        messages=state.message_count,
                        max_turns=state.max_turns,
                    )
                await self._emit(
                    rh.hook.id, rh.hook.action.type, "end",
                    end_details,
                )

                logger.debug(
                    "hook_fired: '%s' (condition=%s, action=%s, count=%d)",
                    rh.hook.id, rh.hook.condition.type,
                    rh.hook.action.type, rh.fire_count,
                )
            except Exception as exc:
                await self._emit(
                    rh.hook.id, rh.hook.action.type, "error",
                    {"error": str(exc)},
                )
                logger.warning(
                    "hook_exec: action '%s' error for hook '%s': %s",
                    rh.hook.action.type, rh.hook.id, exc,
                )

        try:
            await self._emit(
                "_system", "context_status", "update",
                {
                    "pressure": round(state.token_pressure, 4),
                    "estimated_tokens": state.estimated_tokens,
                    "max_tokens": state.effective_max_tokens,
                    "threshold": state._agent_context.context_config.compression_trigger
                    if state._agent_context else 0.75,
                    "messages": state.message_count,
                },
            )
        except Exception as exc:
            logger.debug("context_pressure event emit failed: %s", exc)

        return fired


def compile_hooks(compiled_hooks: list[Any]) -> list[Hook]:
    hooks: list[Hook] = []

    for ch in compiled_hooks:
        hooks.append(Hook(
            id=ch.id,
            on=ch.on,
            condition=HookCondition(
                type=ch.condition_type,
                params=ch.condition_params,
            ),
            action=HookAction(
                type=ch.action_type,
                params=ch.action_params,
            ),
            cooldown=ch.cooldown,
            max_fires=getattr(ch, "max_fires", 0),
            priority=getattr(ch, "priority", 100),
            enabled=getattr(ch, "enabled", True),
            tags=list(getattr(ch, "tags", []) or []),
            agent_id=getattr(ch, "agent_id", None),
            timeout=float(getattr(ch, "timeout", 30.0) or 30.0),
        ))

    return hooks


def build_hook_runner(
    compiled_hooks: list[Any],
    *,
    provider: Any = None,
    summary_provider: Any = None,
    context_builder: Any = None,
    on_hook_event: Callable[..., Awaitable[None]] | None = None,
) -> HookRunner | None:
    if not compiled_hooks:
        return None

    hooks = compile_hooks(compiled_hooks)
    return HookRunner(
        hooks,
        provider=provider,
        summary_provider=summary_provider,
        context_builder=context_builder,
        on_hook_event=on_hook_event,
    )
