"""Dataclass models extracted from manager.py.

These are line-equivalent copies of ``DeployedApp`` and ``TurnState``
from :mod:`digitorn.core.app.manager`. Other modules import them from
the original location, so we re-export them via the package
``__init__`` to preserve compatibility.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from digitorn.core.app.compiler import CompiledApp
from digitorn.core.runtime.types import AgentContext

logger = logging.getLogger(__name__)


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
    preview_manager: Any = None  # PreviewManager (dev server supervisor)
    # Scoping - "system" deploys are visible to every user,
    # "user" deploys only to their owner.
    scope: str = "system"
    owner_user_id: str | None = None

    @property
    def mode(self) -> str:
        return self.compiled.execution.mode

    @property
    def entry_context(self) -> AgentContext:
        # Ghost-app guard: a DeployedApp with no contexts is a broken
        # installation that slipped past bootstrap. The old code did
        # `next(iter({}))` here, which raised StopIteration mid-request
        # and silently dropped POSTs. Explicit RuntimeError is caught
        # by the API layer, which can return 503 Degraded.
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
            "quick_prompts": getattr(meta, "quick_prompts", []),
            "builtin": getattr(self, "builtin", False),
        }

        # ── Client manifest extensions ────────────────────────
        # The Flutter / web client reads these three blocks to tailor
        # the UI (hide panels, override theme, surface /commands).
        # They're always present in the response (empty by default) so
        # the client can rely on a stable shape.
        # features / theme can live top-level on the YAML OR nested
        # under `app:` - the compiler merges both locations, top-level
        # wins on conflict.
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
        # Background-mode metadata the Flutter dashboard needs to know
        # *before* it opens an app: which trigger types are wired, which
        # session mode applies, and the optional declarative payload
        # schema (so the form can be built without a second round-trip).
        execution = self.compiled.execution
        trigger_types: list[str] = []
        try:
            for t in getattr(execution, "triggers", []) or []:
                ttype = getattr(t, "type", None) or getattr(t, "trigger_type", None)
                if ttype:
                    trigger_types.append(str(ttype))
        except Exception:
            pass
        # Channels module providers count as triggers too. Without this,
        # a telegram-only app would look "trigger-less" in the listing.
        try:
            channels_mod = self.modules.get("channels") if hasattr(self, "modules") else None
            providers = getattr(channels_mod, "_providers", None) or {}
            for prov in providers.values():
                ttype = getattr(getattr(prov, "config", None), "type", None) or getattr(prov, "type", None)
                if ttype and str(ttype) not in trigger_types:
                    trigger_types.append(str(ttype))
        except Exception:
            pass

        # Workspace block - the client uses this to know the app has
        # a virtual file workspace and which renderer to use.
        ws_block = getattr(self.compiled, "workspace", None)
        if ws_block is not None:
            data["workspace"] = {
                "render_mode": getattr(ws_block, "render_mode", "auto"),
                "entry_file": getattr(ws_block, "entry_file", None),
                "title": getattr(ws_block, "title", None),
            }

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
    """Per-session in-flight turn state - the single source of truth the
    state envelope reports to the client.

    The client's UI (animated send button, progress bar, queue chip) is
    derived exclusively from this dataclass at snapshot time. An event
    stream merely carries deltas that the server applies to this
    dataclass, and the client mirrors them. If the client is ever
    unsure (reconnect, session switch, missed event), it pulls the
    envelope and rebuilds its UI from scratch.

    Lifecycle: created at ``_chat_locked`` start, updated on each
    provider / tool event, removed on ``message_done`` /
    ``message_cancelled`` / ``error`` terminal events. Also removed
    (and flagged ``interrupted=True``) by the stale-turn watchdog
    when ``last_activity_at`` lags by > 5 minutes.
    """

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
            # Derived convenience for the client - duration the turn
            # has been running, ms since last observable activity. The
            # client can compute these too from ``server_time`` but
            # pre-computing avoids clock-skew confusion.
            "duration_ms": int((time.time() - self.started_at) * 1000),
            "idle_ms": int((time.time() - self.last_activity_at) * 1000),
        }


# ── Multi-tenant scoping helpers ──────────────────────────────────
# An app install is uniquely identified by (app_id, scope, owner_user_id):
#   - scope="system", owner=""     → install visible to everyone
#   - scope="user",   owner="<uid>"→ Alice's private install
#
# These helpers normalise the two tuple-ish values that callers pass
# around (user_id + scope) into canonical pair, and derive a single
# "slug" used as the BundleStore/disk key so user and system bundles
# never overwrite each other.
def _normalize_scope(
    user_id: str | None = None,
    scope: str | None = None,
) -> tuple[str, str]:
    """Return (scope, owner_user_id) from the caller's args.

    Rules:
      - Explicit ``scope="user"`` requires a non-empty user_id.
      - Explicit ``scope="system"`` always wins (admin path) - owner
        is coerced to "".
      - When ``scope`` is None: user_id present → ("user", user_id);
        user_id absent → ("system", "").
    """
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
    """Disk/bundle key for a scoped install.

    System scope returns the bare app_id so legacy deployments
    (~/.digitorn/apps/{app_id}/) keep their existing location unchanged.
    User scope prefixes with ``_@<uid>__`` - a pattern that is invalid
    as a real app_id (app_ids are [a-z0-9_-]) so there can never be a
    collision with a genuine system app.
    """
    if scope == "user" and owner_user_id:
        safe_owner = owner_user_id.replace("/", "_").replace("\\", "_")
        return f"_@{safe_owner}__{app_id}"
    return app_id


def _resolve_tool_display(
    deployed: Any, name: str, params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the ``display`` dict for a tool_call / tool_start SSE event.

    Looks up the action's ``ActionSpec`` via the deployed app's
    module registry when possible, then delegates to
    ``build_display`` which handles the full resolution cascade
    (ActionSpec → legacy labels → regex fallbacks → defaults).
    Never raises - a failure returns the final-defaults display.
    """
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
    """Recover an interrupted session by handling orphaned tool_calls.

    When a session crashes mid-turn, the last assistant message may have
    tool_calls with no corresponding tool results. Instead of deleting
    those tool_calls (losing info), we inject synthetic "interrupted"
    results for each. The LLM sees these and can re-execute the tools.

    Also handles orphaned sub-agent spawn calls by injecting a result
    telling the LLM to re-spawn them.

    Returns the number of recovered tool_calls.
    """
    if not messages:
        return 0

    # Find the last assistant message with tool_calls
    _assistant_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
            _assistant_idx = i
            break

    if _assistant_idx < 0:
        # No orphaned tool_calls - just inject a resume note
        messages.append({
            "role": "system",
            "content": (
                "[Session resumed after interruption. "
                "Continue working on the task from where you left off. "
                "Do NOT restart or repeat completed work.]"
            ),
        })
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
        # All tool results present - just add resume note
        messages.append({
            "role": "system",
            "content": (
                "[Session resumed after interruption. "
                "All previous tool calls completed. "
                "Continue working on the task.]"
            ),
        })
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

    # System note AFTER the synthetic results
    messages.append({
        "role": "system",
        "content": (
            f"[Session resumed after interruption. "
            f"{recovered} tool call(s) were interrupted and returned errors above. "
            f"Re-execute any that are still needed to continue the task. "
            f"Do NOT apologize or restart - continue from where you left off.]"
        ),
    })
    return recovered
