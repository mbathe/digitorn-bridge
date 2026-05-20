"""Pre-dispatch handlers for slash commands declared in"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


SLASH_CMD_RE = re.compile(
    r"^\s*/([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+([\s\S]*))?$",
    re.IGNORECASE,
)


class DispatchResult:
    """Outcome of a slash-command dispatch."""

    __slots__ = ("message", "handled")

    def __init__(self, message: str, *, handled: bool = True) -> None:
        self.message = message
        self.handled = handled


SlashHandler = Callable[[dict[str, Any]], Awaitable[DispatchResult]]


async def _builtin_help(ctx: dict[str, Any]) -> DispatchResult:
    """List every command available in this app's palette."""
    deployed = ctx["deployed"]
    user_id = ctx.get("user_id")
    app_id = ctx["app_id"]
    compiled = deployed.compiled

    lines: list[str] = ["# Available commands", ""]

    slash_commands = list(getattr(compiled, "slash_commands", []) or [])
    if slash_commands:
        lines.append("## Slash commands")
        for s in slash_commands:
            cmd = (s.get("command") or "").strip()
            desc = (s.get("description") or "").strip()
            if not cmd:
                continue
            lines.append(f"- **{cmd}** - {desc}" if desc else f"- **{cmd}**")
        lines.append("")

    app_skills = list(getattr(compiled, "skills", []) or [])
    if app_skills:
        lines.append("## Skills (`/use_skill <name>`)")
        for s in app_skills:
            cmd = (s.get("command") or "").lstrip("/").strip()
            desc = (s.get("description") or "").strip()
            if not cmd:
                continue
            lines.append(f"- **{cmd}** - {desc}" if desc else f"- **{cmd}**")
        lines.append("")

    allow_user = bool(getattr(compiled, "allow_user_skills", False))
    if allow_user and user_id:
        try:
            from digitorn.core.database import get_session_factory
            from digitorn.core.models import UserSkill
            from sqlalchemy import select

            factory = get_session_factory()
            async with factory() as db:
                rows = (
                    await db.execute(
                        select(UserSkill.name, UserSkill.description)
                        .where(UserSkill.user_id == user_id)
                        .where(UserSkill.app_id == app_id)
                        .order_by(UserSkill.updated_at.desc())
                    )
                ).all()
            if rows:
                lines.append("## Your skills (`/use_skill <name>`)")
                for name, desc in rows:
                    label = f"- **{name}**"
                    if desc:
                        label += f" - {desc}"
                    lines.append(label)
                lines.append("")
        except Exception as exc:
            logger.warning("slash_dispatch help: user_skills lookup failed: %s", exc)

    if len(lines) <= 2:
        lines = ["No commands declared for this app."]

    return DispatchResult("\n".join(lines))


async def _builtin_compact_session(ctx: dict[str, Any]) -> DispatchResult:
    """Trim the agent's in-flight message list + mirror the cut to"""
    manager = ctx.get("manager")
    deployed = ctx["deployed"]
    app_id = ctx["app_id"]
    session_id = ctx["session_id"]
    user_id = ctx.get("user_id") or "local"
    if manager is None:
        return DispatchResult("Compaction unavailable (manager missing).")

    session = await manager.get_session(app_id, session_id, user_id=user_id)
    if session is None:
        return DispatchResult("Session not found.")

    messages = session.messages
    before = len(messages)
    if before < 4:
        return DispatchResult(
            f"Too few messages to compact ({before} ≤ 4). Skipped."
        )

    try:
        from digitorn.core.runtime.compaction import emergency_compact
        ctx_agent = deployed.entry_context
        result = await asyncio.wait_for(
            emergency_compact(
                ctx_agent, messages,
                reason="manual_slash",
                event_bus=manager.event_bus,
                app_id=app_id,
                session_id=session_id,
                user_id=user_id,
            ),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return DispatchResult(
            "Compaction timed out after 60s. Check daemon logs."
        )
    except Exception as exc:
        logger.exception("slash compact_session failed: %s", exc)
        return DispatchResult(f"Compaction failed: {exc}")

    # Mirror to the SessionStore so /history survives daemon restart.
    if result.get("compacted") and result.get("to_compact_count", 0) > 0:
        try:
            from digitorn.core.runtime.session_store.bridge import (
                get_default_bridge,
            )
            bridge = get_default_bridge()
            if bridge is not None:
                store_state = bridge.store.state(session_id)
                if store_state is not None and store_state.messages:
                    keep_count = int(result["to_keep_count"])
                    state_msgs = store_state.messages
                    cutoff_idx = max(0, len(state_msgs) - keep_count)
                    if cutoff_idx > 0:
                        cutoff_seq = int(state_msgs[cutoff_idx - 1].seq)
                        await bridge.store.compact_session(
                            session_id,
                            cutoff_seq=cutoff_seq,
                            summary=(
                                f"[Context compacted: "
                                f"{result['to_compact_count']} older "
                                f"messages removed, "
                                f"{int(result.get('tokens_before', 0))} -> "
                                f"{int(result.get('tokens_after', 0))} tokens]"
                            ),
                            strategy="truncate",
                            tokens_estimate=int(result.get("tokens_after", 0)),
                            model=str(getattr(ctx_agent, "model", "") or ""),
                        )
        except Exception as exc:
            logger.warning(
                "slash compact durable write failed sid=%s err=%s",
                session_id, exc,
            )

    after = len(messages)
    freed = before - after
    tokens_before = int(result.get("tokens_before", 0))
    tokens_after = int(result.get("tokens_after", 0))
    return DispatchResult(
        f"**Context compacted.**\n\n"
        f"- Messages: {before} → {after} (freed {freed})\n"
        f"- Tokens: ~{tokens_before:,} → ~{tokens_after:,}"
    )


async def _builtin_undo_session(ctx: dict[str, Any]) -> DispatchResult:
    """Restore the most recently checkpointed file via the"""
    deployed = ctx["deployed"]
    fs_module = deployed.modules.get("filesystem")
    if fs_module is None or not hasattr(fs_module, "_checkpoints"):
        return DispatchResult("Filesystem undo not available for this app.")
    if not fs_module._checkpoints:
        return DispatchResult("No checkpoints to undo.")

    latest_path = None
    latest_ts = 0.0
    for fpath, stack in fs_module._checkpoints.items():
        if stack and stack[-1][0] > latest_ts:
            latest_ts = stack[-1][0]
            latest_path = fpath
    if latest_path is None:
        return DispatchResult("No checkpoints found.")

    stack = fs_module._checkpoints[latest_path]
    _ts, content = stack.pop()
    try:
        await asyncio.to_thread(Path(latest_path).write_bytes, content)
    except Exception as exc:
        logger.exception("slash undo write failed: %s", exc)
        return DispatchResult(f"Undo failed writing `{latest_path}`: {exc}")

    remaining = sum(len(s) for s in fs_module._checkpoints.values())
    return DispatchResult(
        f"**Undone.** Restored `{latest_path}` "
        f"({len(content):,} bytes). "
        f"{remaining} checkpoint(s) remaining across all files."
    )


BUILTIN_HANDLERS: dict[str, SlashHandler] = {
    "help": _builtin_help,
    "compact_session": _builtin_compact_session,
    "undo_session": _builtin_undo_session,
}


def lookup_slash_action(
    compiled: Any, command_name: str,
) -> dict[str, Any] | None:
    """Find the SlashCommand entry whose `command` matches and"""
    slash_commands = list(getattr(compiled, "slash_commands", []) or [])
    needle = command_name.strip().lower()
    for entry in slash_commands:
        cmd = (entry.get("command") or "").strip().lower().lstrip("/")
        if cmd != needle:
            continue
        action = entry.get("action")
        if not isinstance(action, dict):
            continue
        if (action.get("type") or "").lower() != "builtin":
            continue
        name = (action.get("name") or "").strip().lower()
        if not name:
            continue
        if name not in BUILTIN_HANDLERS:
            logger.warning(
                "slash_dispatch: app declared builtin '%s' but no "
                "handler registered (known: %s)",
                name, list(BUILTIN_HANDLERS),
            )
            continue
        return action
    return None


async def dispatch(
    action: dict[str, Any],
    *,
    deployed: Any,
    app_id: str,
    session_id: str,
    user_id: str | None,
    args: str,
    manager: Any | None = None,
) -> DispatchResult:
    """Execute a builtin slash handler. Caller MUST have validated"""
    name = (action.get("name") or "").strip().lower()
    handler = BUILTIN_HANDLERS[name]
    try:
        return await handler({
            "deployed": deployed,
            "app_id": app_id,
            "session_id": session_id,
            "user_id": user_id,
            "args": args,
            "action_params": action.get("params") or {},
            "manager": manager,
        })
    except Exception as exc:
        logger.exception(
            "slash_dispatch '%s' raised: %s", name, exc,
        )
        return DispatchResult(
            f"Command `/{name}` failed: {exc}",
            handled=True,
        )
