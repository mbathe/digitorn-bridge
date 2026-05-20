"""Dataclass models extracted from manager.py."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)


def _extract_mode_ids(compiled: CompiledApp) -> list[str]:
    """Return the list of mode IDs declared in `runtime.modes`."""
    runtime = getattr(compiled, "runtime", None) or getattr(
        compiled, "execution", None,
    )
    modes = getattr(runtime, "modes", None) or {}
    ids = list(modes.keys())
    return ids if ids else ["ask"]


_ATTACHMENT_TYPES: tuple[str, ...] = ("image", "document", "audio", "video")


def _expand_attachments(value: object) -> list[str]:
    """Normalise `app.attachments` for the client manifest."""
    if value is None:
        return []
    if isinstance(value, str):
        if value.strip() == "*":
            return list(_ATTACHMENT_TYPES)
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _resolve_default_mode(compiled: CompiledApp) -> str | None:
    runtime = getattr(compiled, "runtime", None) or getattr(
        compiled, "execution", None,
    )
    modes = getattr(runtime, "modes", None) or {}
    if not modes:
        return None
    if "auto" in modes:
        return "auto"
    return next(iter(modes))


@dataclass
class DeployedApp:
    """A fully deployed, ready-to-execute app in the daemon."""

    app_id: str
    compiled: CompiledApp
    contexts: dict[str, AgentContext]
    modules: dict[str, Any]
    context_builder: Any
    bootstrap_result: Any
    hook_runner: Any = None
    approval_queue: Any = None
    deployed_at: float = field(default_factory=time.time)
    sandbox_worker: Any = None  # SandboxWorker (standard level)
    sandbox_pool: Any = None  # WorkerPool (strict/maximum level)
    hot_reloader: Any = None  # BundleHotReloader (dev mode)
    # Scoping - "system" deploys are visible to every user,
    # "user" deploys only to their owner.
    scope: str = "system"
    owner_user_id: str | None = None

    @property
    def mode(self) -> str:
        return self.compiled.execution.mode

    @property
    def entry_context(self) -> AgentContext:
        if not self.contexts:
            raise RuntimeError(
                f"App '{self.app_id}' is registered but has no executable "
                f"agents (ghost state). Re-deploy or check the server "
                f"logs for the original bootstrap failure."
            )
        agent_id = self.compiled.execution.entry_agent
        if agent_id not in self.contexts:
            agent_id = next(iter(self.contexts))
        return self.contexts[agent_id]

    @property
    def index(self) -> Any:
        if self.context_builder is None:
            return None
        return self.context_builder.index

    def summary(self) -> dict[str, Any]:
        meta = self.compiled.meta
        data: dict[str, Any] = {
            "app_id": self.app_id,
            "name": meta.name,
            "short_name": getattr(meta, "short_name", ""),
            "modes": _extract_mode_ids(self.compiled),
            "default_mode": _resolve_default_mode(self.compiled),
            "version": meta.version,
            "description": meta.description,
            "mode": self.mode,
            "agents": list(self.contexts.keys()),
            "modules": self.compiled.module_ids,
            "total_tools": self.index.total_tools if self.index else 0,
            "total_categories": self.index.total_categories if self.index else 0,
            "deployed_at": self.deployed_at,
            "greeting": getattr(self.compiled.execution, "greeting", None),
            "workspace_mode": getattr(self.compiled.execution, "workspace_mode", "auto"),
            # Visual metadata for client UI
            "icon": getattr(meta, "icon", ""),
            "color": getattr(meta, "color", ""),
            "category": getattr(meta, "category", "general"),
            "author": getattr(meta, "author", ""),
            "tags": getattr(meta, "tags", []),
            # Serialise the Pydantic `quick_prompts` to plain dicts so the
            # API response keeps the list-of-mappings contract.
            "quick_prompts": [
                (p.model_dump() if hasattr(p, "model_dump") else dict(p))
                for p in (getattr(meta, "quick_prompts", []) or [])
            ],
            "attachments": _expand_attachments(
                getattr(meta, "attachments", None)
            ),
            "attachments_mode": getattr(meta, "attachments_mode", "auto"),
            "builtin": getattr(self, "builtin", False),
        }

        top_features = dict(getattr(self.compiled, "features", {}) or {})
        nested_features = dict(getattr(meta, "features", {}) or {})
        merged_features = {**nested_features, **top_features}
        data["features"] = merged_features

        top_theme = dict(getattr(self.compiled, "theme", {}) or {})
        nested_theme = dict(getattr(meta, "theme", {}) or {})
        merged_theme = {**nested_theme, **top_theme}
        data["theme"] = merged_theme

        data["slash_commands"] = list(
            getattr(self.compiled, "slash_commands", []) or []
        )

        skill_list = list(getattr(self.compiled, "skills", []) or [])
        data["skills"] = [
            {
                "command": s.get("command", ""),
                "description": s.get("description", ""),
            }
            for s in skill_list
        ]
        data["allow_user_skills"] = bool(
            getattr(self.compiled, "allow_user_skills", False)
        )

        data["warnings"] = list(getattr(self.compiled, "warnings", []) or [])

        flow_block = getattr(self.compiled, "flow", None)
        if flow_block is not None:
            data["flow"] = {
                "id": getattr(flow_block, "id", None),
                "entry": getattr(flow_block, "entry", None),
                "node_count": len(getattr(flow_block, "nodes", []) or []),
                "max_iterations": getattr(flow_block, "max_iterations", 0),
            }
        else:
            data["flow"] = None
        execution = self.compiled.execution
        trigger_types: list[str] = []
        try:
            for t in getattr(execution, "triggers", []) or []:
                ttype = getattr(t, "type", None) or getattr(t, "trigger_type", None)
                if ttype:
                    trigger_types.append(str(ttype))
        except Exception as exc:
            logger.debug("_models best-effort block failed: %s", exc)
        # Channels module providers count as triggers too. Without this,
        # a telegram-only app would look "trigger-less" in the listing.
        try:
            channels_mod = self.modules.get("channels") if hasattr(self, "modules") else None
            providers = getattr(channels_mod, "_providers", None) or {}
            for prov in providers.values():
                ttype = getattr(getattr(prov, "config", None), "type", None) or getattr(prov, "type", None)
                if ttype and str(ttype) not in trigger_types:
                    trigger_types.append(str(ttype))
        except Exception as exc:
            logger.debug("_models best-effort block failed: %s", exc)

        ws_block = getattr(self.compiled, "workspace", None)
        if ws_block is not None:
            ws_data: dict[str, Any] = {
                "render_mode": getattr(ws_block, "render_mode", "auto"),
                "entry_file": getattr(ws_block, "entry_file", None),
                "title": getattr(ws_block, "title", None),
                "position": getattr(ws_block, "position", "right"),
                "width_pct": getattr(ws_block, "width_pct", 50),
                "auto_open_on_first_tool": getattr(
                    ws_block, "auto_open_on_first_tool", False,
                ),
                "default_open": getattr(
                    ws_block, "default_open", False,
                ),
                "default_view": getattr(
                    ws_block, "default_view", "auto",
                ),
                "hidden_views": list(
                    getattr(ws_block, "hidden_views", []) or []
                ),
            }
            chrome = getattr(ws_block, "preview_chrome", None)
            if chrome is not None:
                ws_data["preview_chrome"] = {
                    "enabled": getattr(chrome, "enabled", True),
                    "refresh": getattr(chrome, "refresh", True),
                    "open_in_new_tab": getattr(chrome, "open_in_new_tab", True),
                    "viewport_toggle": getattr(chrome, "viewport_toggle", False),
                    "url_bar": getattr(chrome, "url_bar", "auto"),
                }
            data["workspace"] = ws_data

        data["chat_layout"] = getattr(self.compiled, "chat_layout", "default")
        data["chat_density"] = getattr(self.compiled, "chat_density", "comfortable")
        thinking = getattr(self.compiled, "chat_thinking", None)
        if thinking is not None:
            data["chat_thinking"] = {
                "visible": getattr(thinking, "visible", True),
                "collapsed_default": getattr(thinking, "collapsed_default", True),
            }
        tool_calls_blk = getattr(self.compiled, "chat_tool_calls", None)
        if tool_calls_blk is not None:
            tc_data: dict[str, Any] = {
                "collapsed_default": getattr(tool_calls_blk, "collapsed_default", True),
                "show_silent": getattr(tool_calls_blk, "show_silent", False),
                "inject_intent": getattr(tool_calls_blk, "inject_intent", False),
                "hide_details": getattr(tool_calls_blk, "hide_details", False),
                "strict_mode": getattr(tool_calls_blk, "strict_mode", False),
            }
            phrases_cfg = getattr(tool_calls_blk, "intent_phrases", None)
            if phrases_cfg is not None:
                static_cfg = getattr(phrases_cfg, "static", None)
                tc_data["intent_phrases"] = {
                    "source": getattr(phrases_cfg, "source", "auto"),
                    "static": {
                        "phases": dict(getattr(static_cfg, "phases", {}) or {}),
                    } if static_cfg is not None else {"phases": {}},
                }
            data["chat_tool_calls"] = tc_data
        composer = getattr(self.compiled, "chat_composer", None)
        if composer is not None:
            data["chat_composer"] = {
                "file_upload": getattr(composer, "file_upload", True),
                "voice": getattr(composer, "voice", False),
                "slash_commands": getattr(composer, "slash_commands", True),
                "quick_prompts_visible": getattr(
                    composer, "quick_prompts_visible", True,
                ),
            }
        visual = getattr(self.compiled, "chat_visual", None)
        if visual is not None:
            data["chat_visual"] = {
                "accent": getattr(visual, "accent", ""),
                "bubble_style": getattr(visual, "bubble_style", "card"),
                "user_bubble_alignment": getattr(
                    visual, "user_bubble_alignment", "right",
                ),
            }
        slots = getattr(self.compiled, "chat_slots", None)
        if slots is not None:
            slot_payload: dict[str, Any] = {}
            for name in (
                "header", "sidebar_left", "sidebar_right",
                "footer_left", "footer_right",
            ):
                entry = getattr(slots, name, None)
                if entry is None:
                    continue
                slot_payload[name] = {
                    "kind": getattr(entry, "kind", "inline"),
                    "ref": getattr(entry, "ref", ""),
                }
            if slot_payload:
                data["chat_slots"] = slot_payload
        tr = getattr(self.compiled, "chat_tool_renderers", None)
        if tr is not None:
            tr_payload: dict[str, Any] = {
                "enabled": getattr(tr, "enabled", False),
                "fallback_on_error": getattr(tr, "fallback_on_error", True),
            }
            by_name = getattr(tr, "by_name", None) or {}
            if by_name:
                tr_payload["by_name"] = {
                    name: {"ref": getattr(entry, "ref", "")}
                    for name, entry in by_name.items()
                }
            by_pattern = getattr(tr, "by_pattern", None) or {}
            if by_pattern:
                tr_payload["by_pattern"] = {
                    pat: {"ref": getattr(entry, "ref", "")}
                    for pat, entry in by_pattern.items()
                }
            data["tool_renderers"] = tr_payload
        ma = getattr(self.compiled, "chat_message_actions", None)
        if ma is not None:
            ma_payload: dict[str, Any] = {
                "enabled": getattr(ma, "enabled", False),
                "fallback_on_error": getattr(ma, "fallback_on_error", True),
            }
            rules = getattr(ma, "rules", None) or []
            ma_payload["rules"] = [
                {
                    "match": {
                        k: v
                        for k, v in {
                            "role": getattr(r.match, "role", None),
                            "tool_used": getattr(r.match, "tool_used", None),
                            "tool_pattern": getattr(
                                r.match, "tool_pattern", None,
                            ),
                            "content_regex": getattr(
                                r.match, "content_regex", None,
                            ),
                            "has_tool_calls": getattr(
                                r.match, "has_tool_calls", None,
                            ),
                        }.items()
                        if v is not None
                    },
                    "ref": getattr(r, "ref", ""),
                }
                for r in rules
            ]
            data["message_actions"] = ma_payload
        activity = getattr(self.compiled, "chat_activity", None)
        if activity is not None:
            data["activity"] = {
                "enabled": getattr(activity, "enabled", True),
                "position": getattr(activity, "position", "right"),
                "title": getattr(activity, "title", None),
                "show_running": getattr(activity, "show_running", True),
                "show_recent": getattr(activity, "show_recent", True),
                "show_stats": getattr(activity, "show_stats", True),
                "show_bg_tasks": getattr(activity, "show_bg_tasks", True),
                "max_recent": getattr(activity, "max_recent", 50),
                "auto_open_on_spawn": getattr(
                    activity, "auto_open_on_spawn", False,
                ),
            }

        widgets_cfg = getattr(self.compiled, "widgets", None)
        inline_widgets = getattr(widgets_cfg, "inline", None) if widgets_cfg else None
        if inline_widgets:
            inline_payload: dict[str, Any] = {}
            for name, spec in inline_widgets.items():
                if spec is None:
                    continue
                if hasattr(spec, "model_dump"):
                    inline_payload[name] = spec.model_dump(
                        by_alias=True, exclude_none=True,
                    )
                elif isinstance(spec, dict):
                    inline_payload[name] = spec
            if inline_payload:
                data["inline_widgets"] = inline_payload

        data["trigger_types"] = trigger_types
        data["session_mode"] = getattr(execution, "session_mode", "mono")
        data["max_sessions_per_user"] = getattr(execution, "max_sessions_per_user", 10)
        data["payload_schema"] = getattr(execution, "payload_schema", None)
        mcp_module = self.modules.get("mcp")
        if mcp_module is not None:
            pool = getattr(mcp_module, "_pool", None)
            if pool is not None:
                servers = []
                for sid, entry in pool._servers.items():
                    needs_auth = entry.auth_config is not None and not entry.tools
                    servers.append({
                        "server_id": sid,
                        "status": entry.status,
                        "tools_count": len(entry.tools),
                        "needs_auth": needs_auth,
                        "provider": getattr(entry.auth_config, "provider", None),
                    })
                data["mcp_servers"] = servers
        return data


@dataclass
class TurnState:
    """Per-session in-flight turn state - the single source of truth the"""

    correlation_id: str
    started_at: float                 # unix seconds (time.time())
    last_activity_at: float           # bumped on every token / tool_call
    phase: str = "requesting"         # requesting|generating|thinking|tool_use|waiting|paused
    tool_calls_count: int = 0
    tokens_out: int = 0
    tokens_in: int = 0
    interrupted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": not self.interrupted,
            "correlation_id": self.correlation_id,
            "started_at": self.started_at,
            "last_activity_at": self.last_activity_at,
            "phase": self.phase,
            "tool_calls_count": self.tool_calls_count,
            "tokens_out": self.tokens_out,
            "tokens_in": self.tokens_in,
            "interrupted": self.interrupted,
            "duration_ms": int((time.time() - self.started_at) * 1000),
            "idle_ms": int((time.time() - self.last_activity_at) * 1000),
        }


def _normalize_scope(
    user_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    """Return (scope, owner_user_id) from the caller's args."""
    if scope == "user":
        if not user_id:
            raise ValueError("scope='user' requires a user_id")
        return "user", user_id
    if scope == "system":
        return "system", ""
    if user_id:
        return "user", user_id
    return "system", ""


def _scoped_slug(app_id: str, scope: str, owner_user_id: str) -> str:
    """Disk/bundle key for a scoped install."""
    if scope == "user" and owner_user_id:
        safe_owner = owner_user_id.replace("/", "_").replace("\\", "_")
        return f"_@{safe_owner}__{app_id}"
    return app_id


def _resolve_tool_display(
    deployed: Any, name: str, params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the `display` dict for a tool_call / tool_start SSE event."""
    try:
        from digitorn.core.runtime.tool_display import build_display
        from digitorn.core.runtime.tool_names import to_fqn

        spec = None
        if deployed is not None and getattr(deployed, "modules", None):
            inner_name = (
                (params or {}).get("name", name)
                if name == "execute_tool" else name
            )
            try:
                fqn = to_fqn(inner_name)
            except Exception:
                fqn = inner_name
            if "." in fqn:
                module_id, action = fqn.split(".", 1)
                module = deployed.modules.get(module_id)
                if module is not None:
                    getter = getattr(module, "_get_action_spec", None)
                    if callable(getter):
                        try:
                            spec = getter(action)
                        except Exception:
                            spec = None
        return build_display(name, params or {}, action_spec=spec)
    except Exception as exc:
        logger.debug("tool_display resolve failed for %s: %s", name, exc)
        return {
            "verb": name or "Tool",
            "detail": "",
            "icon": "tool",
            "channel": "chat",
            "hidden": False,
            "category": "action",
            "group": "",
        }


def _recover_interrupted_session(messages: list[dict[str, Any]]) -> int:
    """Recover an interrupted session by handling orphaned tool_calls."""
    if not messages:
        return 0

    # Find the last assistant message with tool_calls
    _assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            _assistant_idx = i
            break

    from digitorn.core.runtime.system_directives import (
        SYS_RESUME_CLEAN, SYS_RESUME_WITH_ORPHANS,
    )

    if _assistant_idx < 0:
        # No orphaned tool_calls. Inject the clean-resume directive.
        messages.append({"role": "system", "content": SYS_RESUME_CLEAN})
        return 0

    # Collect tool_call IDs that have results vs those that don't
    tool_calls = messages[_assistant_idx].get("tool_calls", [])
    expected_ids = set()
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        if tc_id:
            expected_ids.add(tc_id)

    # Check which tool results exist after the assistant message
    found_ids = set()
    for m in messages[_assistant_idx + 1:]:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            found_ids.add(m["tool_call_id"])

    orphaned_ids = expected_ids - found_ids
    if not orphaned_ids:
        # All tool results present - clean resume directive.
        messages.append({"role": "system", "content": SYS_RESUME_CLEAN})
        return 0

    # Inject synthetic "interrupted" results for orphaned tool_calls
    recovered = 0
    for tc in tool_calls:
        tc_id = tc.get("id", "")
        if tc_id not in orphaned_ids:
            continue
        fn = tc.get("function", {})
        tool_name = fn.get("name", "unknown")

        # Generic interrupted result - works for any tool type
        result_content = (
            f'{{"success": false, "error": "Session interrupted before this tool completed. '
            f'Re-execute this tool if the result is still needed.", '
            f'"interrupted": true, "tool": "{tool_name}"}}'
        )
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
        recovered += 1
        logger.info("Recovered interrupted tool_call: %s (id=%s)", tool_name, tc_id[:12])

    # Authoritative resume directive AFTER the synthetic results.
    messages.append({
        "role": "system",
        "content": SYS_RESUME_WITH_ORPHANS.format(n=recovered),
    })
    return recovered
