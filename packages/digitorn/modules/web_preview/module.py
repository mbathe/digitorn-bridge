"""Session-scoped iframe preview attachments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.web_preview.params import (
    DetachParams,
    ProxyParams,
    PublishParams,
)

logger = logging.getLogger(__name__)

# Caps checked at attach time so a runaway agent can't spawn hundreds
# of dev servers. Per-session covers 1 proxy + 1 published + 1 bundled.
_MAX_ATTACHMENTS_PER_SESSION = 3
_MAX_ATTACHMENTS_PER_USER = 40

_IDLE_REAP_AFTER_SEC = 30 * 60  # 30 minutes
_REAPER_INTERVAL_SEC = 5 * 60   # scan every 5 minutes

class WebPreviewConfig(BaseModel):
    """Compile-time config (currently empty - kept for forward compat)."""

    model_config = {"extra": "allow"}

@dataclass
class Attachment:
    """One iframe-preview pointer for a (session, name) pair."""

    name: str
    session_id: str
    type: str = "proxy"
    # Proxy attachments
    port: int | None = None
    host: str = "127.0.0.1"
    path: str = ""  # URL path appended to host:port (e.g. "/landing.html")
    bash_task_id: str | None = None
    # Bundled + Published + TemplatePreview attachments (static serving)
    app_id: str | None = None
    install_dir: str | None = None  # bundled
    dist_dir: str | None = None  # published - absolute path to the copied dist/
    template_id: str | None = None  # template_preview - id of the seeded template
    # Common
    created_at: float = field(default_factory=time.time)
    last_hit_at: float = field(default_factory=time.time)
    user_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
        }
        if self.type == "proxy":
            out["port"] = self.port
            out["host"] = self.host
            if self.path:
                out["path"] = self.path
            if self.bash_task_id:
                out["bash_task_id"] = self.bash_task_id
        elif self.type == "published":
            out["app_id"] = self.app_id
            if self.path:
                out["path"] = self.path
            if self.dist_dir:
                out["dist_dir"] = self.dist_dir
        elif self.type == "template_preview":
            out["app_id"] = self.app_id
            out["template_id"] = self.template_id
        else:  # bundled
            out["app_id"] = self.app_id
        if self.user_id:
            out["user_id"] = self.user_id
        return out

    def touch(self) -> None:
        self.last_hit_at = time.time()

class WebPreviewModule(BaseModule):
    """Iframe-preview attachment registry, keyed by (session, name)."""

    MODULE_ID = "web_preview"
    VERSION = "1.0.0"
    CONFIG_MODEL = WebPreviewConfig
    # Daemon-wide singleton: one _attachments dict + one persistence file,
    # shared across every deployed app. Without this every app deploy would
    # spawn a fresh instance racing on the same JSON file.
    MODULE_SINGLETON = True

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Session-scoped iframe preview attachments. The agent "
                "publishes a (host, port) pair via PreviewProxy; the "
                "client iframe loads that URL directly. Multi-attach "
                "by name."
            ),
            "author": "Digitorn Team",
        })

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Inject the agent's mental model of the preview surface."""
        return [
            {
                "id": "web_preview.context",
                "title": "Live Preview - Environment Awareness",
                "priority": 40,
                "position": "after_tools",
                "content": (
                    "## What the user sees\n"
                    "The chat UI is paired with a Workspace panel "
                    "(docked side panel on desktop, dedicated view on "
                    "mobile). The panel has tabs: Code, Preview, "
                    "Changes. The Preview tab embeds an iframe that "
                    "loads whatever URL you publish via a preview "
                    "action.\n\n"
                    "## Preview actions available to you\n"
                    "Check your tool list - your app may expose only "
                    "ONE of these, or both:\n"
                    "- **PreviewPublish** - one-shot static build "
                    "(install + build + serve at a same-origin URL on "
                    "the daemon). No HMR; every change needs a "
                    "re-publish. Survives daemon restart, no port. "
                    "Cloud-friendly.\n"
                    "- **PreviewProxy** - live Vite dev server with HMR. "
                    "Install + run dev + attach in one call. Iframe "
                    "loads `http://localhost:<port>` direct-connect. "
                    "Right for local installs.\n\n"
                    "If both are exposed, prefer PreviewProxy for "
                    "iteration speed (HMR) and PreviewPublish for "
                    "shareable / stable URLs. If only one is exposed, "
                    "use it - your app YAML decides which mode fits "
                    "the deployment.\n\n"
                    "## Template-attached sessions\n"
                    "If the user picked a template from a gallery, "
                    "the daemon AUTOMATICALLY registered a pristine "
                    "preview (`template_preview` slot) before your "
                    "first turn. The iframe is ALREADY showing the "
                    "template - don't call any preview action just "
                    "to display it. Edit the files via the workspace "
                    "tools, THEN publish to update the iframe with "
                    "your customisations.\n\n"
                    "## How to communicate with the user\n"
                    "The user is waiting to see the preview. Keep "
                    "them in the loop:\n"
                    "- When the install/build runs, narrate it briefly.\n"
                    "- After a successful attach, ONE sentence: "
                    "*'Live in the Preview tab. What would you like to "
                    "change?'*\n"
                    "- Don't say 'PreviewProxy' / 'PreviewPublish' to "
                    "the user - they don't care which tool you used.\n\n"
                    "## Common pitfalls\n"
                    "- **PreviewPublish asset URLs**: your build MUST "
                    "emit relative paths. Vite: `base: './'`. "
                    "Without it, the iframe loads the HTML but every "
                    "asset 404s and the page is blank.\n"
                    "- **PreviewProxy override mode**: don't call "
                    "PreviewProxy with bash_task_id before the dev "
                    "server is bound. Watch bash output for 'Local:' / "
                    "'ready in' first.\n"
                    "- **Iframes inside your own app code**: if you "
                    "embed an iframe and write to its contentDocument, "
                    "use `sandbox=\"allow-scripts allow-same-origin\"` "
                    "(or no sandbox). `allow-scripts` alone throws "
                    "SecurityError on every contentDocument access."
                ),
            },
        ]

    _sio_ref: Any = None
    _public_url_template: str = "http://{host}:{port}"
    _enabled: bool = True

    @classmethod
    def attach_sio(cls, sio: Any) -> None:
        """Wire the AsyncSocketIO server. Called from `server.py`."""
        cls._sio_ref = sio

    @classmethod
    def configure(
        cls,
        *,
        public_url_template: str,
        enabled: bool = True,
    ) -> None:
        """Apply daemon-level settings. Called from `server.py`."""
        if public_url_template:
            cls._public_url_template = public_url_template
        cls._enabled = bool(enabled)
        if not cls._enabled:
            logger.warning(
                "web_preview kill switch ENABLED - new attachments "
                "will be refused (existing ones keep serving). Set "
                "DIGITORN_WEB_PREVIEW__ENABLED=true to re-enable."
            )

    @classmethod
    def render_public_url(
        cls,
        *,
        host: str,
        port: int,
        app_id: str,
        session_id: str,
        name: str,
    ) -> str:
        """Build the iframe-loadable URL for a proxy attachment."""
        if host in ("127.0.0.1", "::1", "0.0.0.0"):
            host = "localhost"
        try:
            return cls._public_url_template.format(
                host=host,
                port=port,
                app_id=app_id,
                session_id=session_id,
                name=name,
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "web_preview_url_template_failed template=%r err=%s - "
                "falling back to loopback",
                cls._public_url_template, exc,
            )
            return f"http://{host}:{port}"

    def __init__(self) -> None:
        super().__init__()
        self._attachments: dict[tuple[str, str], Attachment] = {}
        self._workspace: Any | None = None
        self._shell: Any | None = None
        self._shells_by_app: dict[str, Any] = {}
        self._reaper_task: asyncio.Task[None] | None = None
        self._load_persisted()

    def get_attachment(
        self, session_id: str, name: str = "default",
    ) -> Attachment | None:
        """Used by the HTTP proxy route to look up the target."""
        if not session_id:
            return None
        att = (
            self._attachments.get((session_id, "proxy"))
            or self._attachments.get((session_id, "published"))
            or self._attachments.get((session_id, "template_preview"))
            or self._attachments.get((session_id, "bundled"))
        )
        if att is not None:
            att.touch()
        return att

    def register_template_preview(
        self,
        session_id: str,
        app_id: str,
        template_id: str,
        user_id: str | None = None,
    ) -> Attachment:
        """Auto-register a preview attachment for a freshly-seeded template."""
        if not session_id or not app_id or not template_id:
            raise ValueError(
                "session_id, app_id, and template_id are all required"
            )
        att = Attachment(
            name="template_preview",
            session_id=session_id,
            type="template_preview",
            app_id=app_id,
            template_id=template_id,
            user_id=user_id,
        )
        self._attachments[(session_id, "template_preview")] = att
        self._persist_to_disk()
        self._emit_attached(att)
        self._emit_event(
            "preview_template",
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
            template_id=template_id,
        )
        return att

    def get_fallback_attachment(self, session_id: str) -> Attachment | None:
        """Return the session's `bundled` slot, if any."""
        if not session_id:
            return None
        return self._attachments.get((session_id, "bundled"))

    def list_session(self, session_id: str) -> list[Attachment]:
        """All attachments for a session (any name). Daemon-side helper."""
        if not session_id:
            return []
        return [
            att for (sid, _), att in self._attachments.items()
            if sid == session_id
        ]

    async def health_check(self) -> dict[str, Any]:
        """Standard module health check - exposed."""
        snap = self.health_snapshot()
        return {
            "status": "ok",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
            **snap,
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Operator-facing summary for the health endpoint. O(N)."""
        now = time.time()
        atts = list(self._attachments.values())
        by_type: dict[str, int] = {}
        by_user: dict[str, int] = {}
        sessions: set[str] = set()
        oldest_age = 0.0
        oldest_idle = 0.0
        for att in atts:
            by_type[att.type] = by_type.get(att.type, 0) + 1
            uid = att.user_id or "anonymous"
            by_user[uid] = by_user.get(uid, 0) + 1
            sessions.add(att.session_id)
            oldest_age = max(oldest_age, now - att.created_at)
            oldest_idle = max(oldest_idle, now - att.last_hit_at)
        return {
            "count": len(atts),
            "by_type": by_type,
            "by_user_count": len(by_user),
            "by_user": by_user,
            "session_count": len(sessions),
            "oldest_age_seconds": round(oldest_age, 1),
            "oldest_idle_seconds": round(oldest_idle, 1),
            "limits": {
                "max_per_session": _MAX_ATTACHMENTS_PER_SESSION,
                "max_per_user": _MAX_ATTACHMENTS_PER_USER,
                "idle_reap_after_seconds": _IDLE_REAP_AFTER_SEC,
            },
        }

    async def cleanup_session(self, session_id: str) -> None:
        """Drop every attachment owned by this session."""
        if not session_id:
            return
        import traceback
        logger.warning(
            "web_preview cleanup_session sid=%s caller-stack:\n%s",
            session_id,
            "".join(traceback.format_stack()[-6:-1]),
        )
        to_kill: list[str] = []
        dropped: list[Attachment] = []
        dist_dirs_to_remove: list[str] = []
        keys = [k for k in self._attachments if k[0] == session_id]
        for k in keys:
            att = self._attachments.pop(k, None)
            if att is None:
                continue
            dropped.append(att)
            if att.bash_task_id:
                to_kill.append(att.bash_task_id)
            if att.type == "published" and att.dist_dir:
                dist_dirs_to_remove.append(att.dist_dir)
        for task_id in to_kill:
            await self._kill_bash_task(session_id, task_id)
        for dist_dir in dist_dirs_to_remove:
            try:
                await asyncio.to_thread(
                    shutil.rmtree, dist_dir, ignore_errors=True,
                )
            except Exception as exc:
                logger.debug(
                    "web_preview cleanup_session: rmtree %s failed: %s",
                    dist_dir, exc,
                )
        if dropped:
            self._persist_to_disk()
            now = time.time()
            for att in dropped:
                self._emit_event(
                    "preview_detach",
                    app_id=self._app_id_str(),
                    session_id=att.session_id,
                    user_id=att.user_id,
                    name=att.name,
                    type=att.type,
                    port=att.port,
                    lifetime_seconds=round(now - att.created_at, 1),
                    reason="session_cleanup",
                    killed_bash=bool(att.bash_task_id),
                )

    def _emit_event(self, event: str, **fields: Any) -> None:
        try:
            payload = {
                "event": event,
                "module": self.MODULE_ID,
                "ts": round(time.time(), 3),
                **{k: v for k, v in fields.items() if v is not None},
            }
            logger.info("web_preview_event %s", json.dumps(payload, default=str))
        except Exception:
            logger.info("web_preview_event %s (serialize_failed)", event)

    def _app_id_str(self) -> str:
        try:
            ctx = self._context_var.get()
        except Exception:
            ctx = None
        ctx_app = getattr(ctx, "app_id", "") if ctx is not None else ""
        if ctx_app:
            return ctx_app
        return (
            getattr(self, "_app_id_override", None)
            or getattr(self, "_app_id", "default")
        )

    @staticmethod
    def _persist_path() -> Path:
        return Path.home() / ".digitorn" / "web_preview_attachments.json"

    def _load_persisted(self) -> None:
        path = self._persist_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "web_preview persist load failed (%s) - starting empty",
                exc,
            )
            return
        for entry in data.get("attachments", []):
            etype = entry.get("type", "proxy")
            if etype not in ("proxy", "bundled", "published", "template_preview"):
                continue
            try:
                if etype == "proxy":
                    port = entry.get("port")
                    if not isinstance(port, int):
                        continue
                    att = Attachment(
                        type="proxy",
                        name=entry["name"],
                        session_id=entry["session_id"],
                        port=port,
                        host=entry.get("host", "127.0.0.1"),
                        path=entry.get("path", ""),
                        bash_task_id=entry.get("bash_task_id"),
                        created_at=entry.get("created_at", time.time()),
                        last_hit_at=entry.get("last_hit_at", time.time()),
                        user_id=entry.get("user_id"),
                    )
                elif etype == "published":
                    dist_dir = entry.get("dist_dir")
                    if not dist_dir or not Path(dist_dir).is_dir():
                        # Stale entry: dir was wiped between daemon
                        # runs. Skip - the agent can re-publish.
                        continue
                    att = Attachment(
                        type="published",
                        name=entry["name"],
                        session_id=entry["session_id"],
                        app_id=entry.get("app_id"),
                        dist_dir=dist_dir,
                        path=entry.get("path", ""),
                        created_at=entry.get("created_at", time.time()),
                        last_hit_at=entry.get("last_hit_at", time.time()),
                        user_id=entry.get("user_id"),
                    )
                elif etype == "template_preview":
                    tpl_id = entry.get("template_id")
                    if not tpl_id:
                        continue
                    att = Attachment(
                        type="template_preview",
                        name=entry["name"],
                        session_id=entry["session_id"],
                        app_id=entry.get("app_id"),
                        template_id=tpl_id,
                        created_at=entry.get("created_at", time.time()),
                        last_hit_at=entry.get("last_hit_at", time.time()),
                        user_id=entry.get("user_id"),
                    )
                else:
                    install_dir = entry.get("install_dir")
                    if not install_dir:
                        continue
                    att = Attachment(
                        type="bundled",
                        name=entry["name"],
                        session_id=entry["session_id"],
                        app_id=entry.get("app_id"),
                        install_dir=install_dir,
                        created_at=entry.get("created_at", time.time()),
                        last_hit_at=entry.get("last_hit_at", time.time()),
                        user_id=entry.get("user_id"),
                    )
                # Slot key = (session_id, type); migrates legacy `name`-
                # keyed entries with last-write-wins.
                self._attachments[(att.session_id, att.type)] = att
            except (KeyError, TypeError) as exc:
                logger.debug("web_preview persist skip malformed entry: %s", exc)
        if self._attachments:
            logger.info(
                "web_preview restored %d attachment(s) from %s",
                len(self._attachments), path,
            )

    def _persist_to_disk(self) -> None:
        path = self._persist_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        entries: list[dict[str, Any]] = []
        for att in self._attachments.values():
            e: dict[str, Any] = {
                "session_id": att.session_id,
                "name": att.name,
                "type": att.type,
                "created_at": att.created_at,
                "last_hit_at": att.last_hit_at,
            }
            if att.type == "proxy":
                e["port"] = att.port
                e["host"] = att.host
                if att.path:
                    e["path"] = att.path
                if att.bash_task_id:
                    e["bash_task_id"] = att.bash_task_id
            else:  # bundled
                e["app_id"] = att.app_id
                e["install_dir"] = att.install_dir
            if att.user_id:
                e["user_id"] = att.user_id
            entries.append(e)

        payload = {"version": 1, "attachments": entries}
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8",
                dir=str(path.parent), delete=False,
                prefix=".web_preview_", suffix=".json.tmp",
            )
            json.dump(payload, tmp, indent=2)
            tmp.close()
            os.replace(tmp.name, path)
        except OSError as exc:
            logger.warning("web_preview persist write failed: %s", exc)

    def attachment_url(self, attachment: "Attachment") -> str:
        """Compute the URL the iframe should load for this attachment."""
        if attachment.type == "bundled":
            app_id = attachment.app_id or self._app_id_str()
            return f"/api/apps/{app_id}/web-static/index.html"
        if attachment.type == "template_preview":
            app_id = attachment.app_id or self._app_id_str()
            tpl_id = attachment.template_id or "default"
            return (
                f"/api/apps/{app_id}/template-assets/"
                f"templates/{tpl_id}/dist/index.html"
            )
        if attachment.type == "published":
            app_id = attachment.app_id or self._app_id_str()
            base = (
                f"/api/apps/{app_id}/sessions/{attachment.session_id}"
                f"/published/index.html"
            )
            suffix = (attachment.path or "").strip()
            if not suffix:
                return base
            # When the agent published a non-root entry (e.g. `/admin.html`),
            # swap `index.html` for that suffix. Otherwise the iframe
            # would load index.html and ignore the agent's intent.
            if not suffix.startswith("/"):
                suffix = "/" + suffix
            return (
                f"/api/apps/{app_id}/sessions/{attachment.session_id}"
                f"/published{suffix}"
            )
        # Proxy
        app_id_str = attachment.app_id or self._app_id_str()
        base = self.render_public_url(
            host=attachment.host,
            port=attachment.port or 0,
            app_id=app_id_str,
            session_id=attachment.session_id,
            name=attachment.name,
        )
        suffix = (attachment.path or "").strip()
        if not suffix:
            return base
        # Normalise: ensure suffix starts with '/' so we don't accidentally
        # produce 'http://hostlanding.html'. Also handle the rare case
        # where the template already ended with '/'.
        if not suffix.startswith("/"):
            suffix = "/" + suffix
        if base.endswith("/"):
            base = base[:-1]
        return base + suffix

    def _emit_attached(self, attachment: "Attachment") -> None:
        if self._sio_ref is None:
            return
        payload: dict[str, Any] = {
            "session_id": attachment.session_id,
            "name": attachment.name,
            "type": attachment.type,
            "url": self.attachment_url(attachment),
        }
        if attachment.port is not None:
            payload["port"] = attachment.port
        try:
            asyncio.create_task(
                self._sio_ref.emit(
                    "web_preview:attached",
                    payload,
                    room=f"session:{attachment.session_id}",
                    namespace="/events",
                )
            )
        except Exception as exc:
            logger.debug("web_preview emit failed: %s", exc)

    @action(
        description=(
            "Start (or attach to) the session's dev server preview."
        ),
        params_model=ProxyParams,
        tool_prompt=(
            "Start (or attach to) the user's live preview. Default mode "
            "is **fully automated** - call `PreviewProxy()` with no "
            "args once your project sits at the workspace root and the "
            "daemon does the rest:\n\n"
            "  1. Picks a free port (5173, then 5174/5175…).\n"
            "  2. `npm install` (foreground).\n"
            "  3. `npm run dev` on that port (background, auto-killed "
            "     when the session ends).\n"
            "  4. Waits for the port to bind.\n"
            "  5. Attaches the iframe.\n\n"
            "## Success\n"
            "Returns `{url, port, ...}`. Tell the user: 'Dev server "
            "live in the Preview tab.' - that's it.\n\n"
            "## Failure\n"
            "Returns `{success: false, error: '...'}` with the actual "
            "stderr / exit code so you can diagnose. Common causes the "
            "error message will point to:\n"
            "  - **No package.json** - your project isn't scaffolded yet.\n"
            "  - **npm install failed** - look at stderr (peer-dep "
            "    mismatch, missing binary, network error).\n"
            "  - **Dev server crashed** - vite/next config error, "
            "    missing file, port collision (the daemon already "
            "    fell back through 5173→5180, so this is rarer).\n"
            "  - **Port never bound** - the dev command runs but never "
            "    listens. Usually a config or import error.\n\n"
            "ALWAYS read the returned error before retrying. Don't loop "
            "blindly on PreviewProxy() - fix the cause, then call again.\n\n"
            "## Re-attach\n"
            "Calling `PreviewProxy()` again automatically replaces "
            "the previous proxy (kills the old dev server, spawns a "
            "fresh one). One proxy per session - no accumulation.\n\n"
            "## Override mode (advanced)\n"
            "If you really need to spawn the dev server yourself (custom "
            "command, env vars, non-npm runtime), pass both `port` and "
            "`bash_task_id` from your own `Bash(run_in_background=true)` "
            "call. The daemon then just attaches the iframe to your "
            "existing server, skipping install + run.\n\n"
            "## Static single-file previews\n"
            "Override mode + `path='/landing.html'` covers the python "
            "http.server case when the user just wants to look at one "
            "HTML file (no React/Vite). Spawn the server yourself."
        ),
        risk_level="low",
        tags=["preview", "proxy", "http"],
    )
    async def proxy(self, params: ProxyParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(
                success=False,
                error="No active session - PreviewProxy must be called from within a session.",
            )

        if not self._enabled:
            return ActionResult(
                success=False,
                error=(
                    "web_preview is currently disabled by the operator. "
                    "Existing attachments still serve, but new ones are "
                    "refused. Try again later or ask the operator to set "
                    "web_preview.enabled=true."
                ),
            )

        # Quota gate: per-user cap only - the per-session cap is
        # always 1 (proxy slot is always replaced, never grows).
        # Re-attaching the proxy slot for THIS session is a free pass.
        quota_err = self._check_attach_quota(sid, "proxy")
        if quota_err is not None:
            return ActionResult(success=False, error=quota_err)

        if params.bash_task_id is None:
            return await self._proxy_automated(params, sid)

        if params.port is None:
            return ActionResult(
                success=False,
                error=(
                    "Override mode (with bash_task_id) requires `port` "
                    "to be set. Omit bash_task_id to use the automated "
                    "mode instead."
                ),
            )

        # Verify the bash task is still alive: shell's 300 ms early-exit
        # watchdog can miss servers that die slightly later, leading us
        # to probe-bind onto a zombie listener.
        shell_for_app = self._resolve_shell_for_current_app()
        if params.bash_task_id and shell_for_app is not None:
            task = getattr(shell_for_app, "_tasks", {}).get(
                params.bash_task_id,
            )
            if task is not None and not task.is_running:
                stderr_tail = "\n".join(
                    list(getattr(task, "stderr_lines", []))[-10:]
                )
                exit_code = getattr(task, "exit_code", None)
                hint = (
                    "Your dev server died right after starting. Most "
                    "common cause: another process is already bound "
                    "to this port (often a zombie from a previous "
                    "session). Pick a different port and retry, or "
                    "kill the squatter first."
                )
                return ActionResult(
                    success=False,
                    error=(
                        f"Bash task '{params.bash_task_id}' is no longer "
                        f"running (exit_code={exit_code}). {hint}"
                        + (f"\nStderr: {stderr_tail}" if stderr_tail else "")
                    ),
                )

        # Bounded bind-wait so callers attach only once the dev server is up.
        # Override the budget per call via `wait_seconds` for slow stacks.
        if params.health_check:
            wait_budget_s = float(getattr(params, "wait_seconds", 0) or 0)
            if wait_budget_s <= 0:
                wait_budget_s = 15.0
            poll_interval_s = 0.5
            attempts = max(1, int(wait_budget_s / poll_interval_s))
            ok = False
            hint = ""
            for attempt in range(attempts):
                ok, hint = await self._probe_port(
                    params.host, params.port, timeout=poll_interval_s,
                )
                if ok:
                    if attempt > 0:
                        logger.info(
                            "web_preview_bind_wait_success sid=%s port=%d "
                            "attempts=%d elapsed=%.1fs",
                            sid, params.port, attempt + 1,
                            (attempt + 1) * poll_interval_s,
                        )
                    break
                if attempt < attempts - 1:
                    await asyncio.sleep(poll_interval_s)
            # Hard refuse when the port stays dead; surface the failure to the agent
            # so it can fix the spawn instead of handing the iframe a broken target.
            if not ok:
                bash_hint = ""
                if params.bash_task_id and self._shell is not None:
                    task = getattr(self._shell, "_tasks", {}).get(
                        params.bash_task_id,
                    )
                    if task is None:
                        bash_hint = (
                            f" Task '{params.bash_task_id}' is no "
                            f"longer tracked by the shell module - "
                            f"it likely exited with an error right "
                            f"after spawn (binary not found, syntax "
                            f"error, etc.). Re-run the spawn command "
                            f"in foreground (run_in_background=false) "
                            f"to see the actual stderr."
                        )
                    elif not task.is_running:
                        stderr_tail = "\n".join(
                            list(getattr(task, "stderr_lines", []))[-10:]
                        )
                        bash_hint = (
                            f" Bash task '{params.bash_task_id}' exited "
                            f"with code {task.exit_code}."
                            + (f" Stderr: {stderr_tail}" if stderr_tail else "")
                        )
                return ActionResult(
                    success=False,
                    error=(
                        f"Port {params.port} never bound after "
                        f"{wait_budget_s:.0f}s of retries ({hint}). "
                        f"Your server didn't start.{bash_hint} "
                        f"Common causes: command-not-found (missing "
                        f"binary like php, ruby, go), syntax error in "
                        f"server source, immediate crash on startup, "
                        f"or the server bound to a different "
                        f"host/interface."
                    ),
                )

        t0 = time.time()
        prev_proxy = self._attachments.get((sid, "proxy"))
        if prev_proxy is not None and prev_proxy.bash_task_id:
            try:
                await self._kill_bash_task(sid, prev_proxy.bash_task_id)
            except Exception as exc:
                logger.warning(
                    "preview_proxy_replace_kill_failed sid=%s task=%s err=%s",
                    sid, prev_proxy.bash_task_id, exc,
                )

        att = Attachment(
            name="proxy",
            session_id=sid,
            type="proxy",
            port=params.port,
            host=params.host,
            path=(params.path or "").strip(),
            user_id=self._current_user_id(),
            bash_task_id=params.bash_task_id,
        )
        self._attachments[(sid, "proxy")] = att
        self._ensure_reaper_running()
        self._persist_to_disk()
        self._emit_attached(att)

        self._emit_event(
            "preview_attach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=att.user_id,
            name="proxy",
            type="proxy",
            port=params.port,
            host=params.host,
            bash_task_id=params.bash_task_id,
            replaced=prev_proxy is not None,
            duration_ms=round((time.time() - t0) * 1000, 1),
        )
        return self._build_attach_result(att)

    @action(
        description=(
            "Build the project once and publish the static output "
            "same-origin under /api/apps/{id}/sessions/{sid}/published/."
        ),
        params_model=PublishParams,
        tool_prompt=(
            "Build the project ONCE and serve the static output from the "
            "daemon at a same-origin URL. The right tool for **cloud / "
            "multi-tenant deploys** where a live dev server per session "
            "is too expensive, AND for **shareable snapshots** (demos, "
            "stable links) on any deploy.\n\n"
            "## Pipeline\n"
            "  1. `npm install` (only if `node_modules` missing or "
            "     `install=true`).\n"
            "  2. `npm run build` (or whatever script you set in "
            "     `build_script`).\n"
            "  3. Copy the `output_dir` (default `dist/`) to "
            "     `~/.digitorn/published/<app_id>/<session_id>/`.\n"
            "  4. Register a `published` attachment + emit "
            "     `web_preview:attached` so the iframe reloads at "
            "     the new same-origin URL.\n\n"
            "## When to use Publish vs Proxy\n"
            "- `PreviewProxy` = live dev server with HMR. Right for **local "
            "  installs** where the user has the resources for a Vite process "
            "  and wants instant edit-to-preview feedback.\n"
            "- `PreviewPublish` = static build, no port, no HMR. Right for "
            "  **cloud deploys** (no port to expose), **demo URLs** "
            "  (survives daemon restart), and when you want a stable "
            "  snapshot the user can share or come back to later.\n\n"
            "## Failure modes\n"
            "Returns `{success: false, error: ...}` with the actual "
            "stderr / exit code on:\n"
            "  - No `package.json`: the project isn't scaffolded.\n"
            "  - `npm install` failed: peer-dep mismatch, network, etc.\n"
            "  - `npm run build` failed: TS error, missing import, "
            "    bad config - read the stderr.\n"
            "  - `output_dir` empty or missing after build: the build "
            "    script doesn't write where `output_dir` says.\n\n"
            "Read the error before retrying - same rule as PreviewProxy. "
            "Don't loop blindly. Fix the cause, call again.\n\n"
            "## Re-publish\n"
            "Calling `PreviewPublish` again replaces the previous "
            "publish for the session (old dir is overwritten). One "
            "published slot per session, no accumulation.\n\n"
            "## Asset URL gotcha (CRITICAL)\n"
            "The iframe loads the build under "
            "`/api/apps/<id>/sessions/<sid>/published/index.html`, "
            "NOT under `/` of the daemon. So your build MUST emit "
            "RELATIVE asset URLs (`./assets/index.js`), not "
            "absolute (`/assets/index.js`). For **Vite**: set "
            "`base: './'` in `vite.config.ts`. For **CRA**: set "
            "`\"homepage\": \".\"` in `package.json`. For "
            "**Next-export**: set `assetPrefix: ''` and use "
            "`next export`. If you skip this, the HTML loads but "
            "all the JS/CSS 404s and the iframe shows a blank page."
        ),
        risk_level="low",
        tags=["preview", "build", "static"],
    )
    async def publish(self, params: PublishParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(
                success=False,
                error=(
                    "No active session - PreviewPublish must be called "
                    "from within a session."
                ),
            )

        if not self._enabled:
            return ActionResult(
                success=False,
                error=(
                    "web_preview is currently disabled by the operator. "
                    "Existing attachments still serve, but new ones are "
                    "refused."
                ),
            )

        quota_err = self._check_attach_quota(sid, "published")
        if quota_err is not None:
            return ActionResult(success=False, error=quota_err)

        return await self._publish_automated(params, sid)

    async def _publish_automated(
        self, params: "PublishParams", sid: str,
    ) -> ActionResult:
        # 1. Workspace + package.json sanity check
        ws_path = self._resolve_workspace_path()
        if ws_path is None:
            return ActionResult(
                success=False,
                error=(
                    "Cannot resolve workspace directory. The session "
                    "has no workspace set."
                ),
            )
        pkg_json = ws_path / "package.json"
        if not pkg_json.is_file():
            return ActionResult(
                success=False,
                error=(
                    f"No package.json at the workspace root ({ws_path}). "
                    f"Scaffold a project first."
                ),
            )

        # 2. Shell module
        shell = self._resolve_shell_for_current_app()
        if shell is None:
            return ActionResult(
                success=False,
                error=(
                    "Shell module is not loaded for this app - "
                    "PreviewPublish needs it to run npm. Add "
                    "`shell: {}` to the app's modules block."
                ),
            )

        async def _run_bash(args: dict[str, Any]):
            raw = await shell.execute("bash", args)
            if isinstance(raw, ActionResult):
                return raw
            if isinstance(raw, dict):
                return ActionResult(
                    success=bool(raw.get("success", True)),
                    data=raw.get("data") or {
                        k: v for k, v in raw.items()
                        if k not in ("success", "error")
                    },
                    error=raw.get("error"),
                )
            return ActionResult(success=True, data={"raw": raw})

        # 3. npm install (skip if node_modules already there + install=false)
        node_modules = ws_path / "node_modules"
        if params.install and not node_modules.is_dir():
            install_result = await _run_bash({
                "command": "npm install",
                "timeout": params.timeout,
            })
            if not install_result.success:
                return ActionResult(
                    success=False,
                    error=(
                        f"`npm install` failed: "
                        f"{install_result.error or 'unknown error'}. "
                        f"Read stderr above, fix the cause, call "
                        f"PreviewPublish again."
                    ),
                    data=install_result.data or {},
                )

        # 3.5. Pre-build `tsc --noEmit` to catch cross-file type errors
        # before `vite build` (which transpiles broken TS by default).
        # Only runs when `tsconfig.json` exists.
        tsconfig = ws_path / "tsconfig.json"
        if tsconfig.is_file():
            tsc_result = await _run_bash({
                "command": "npx --no-install tsc --noEmit --pretty false",
                "timeout": min(120, params.timeout),
            })
            if not tsc_result.success:
                stderr_tail = ""
                exit_code = None
                if isinstance(tsc_result.data, dict):
                    stderr_tail = str(tsc_result.data.get("stderr", "")).strip()
                    stdout_tail = str(tsc_result.data.get("stdout", "")).strip()
                    # tsc writes to stdout, not stderr - surface both.
                    if not stderr_tail and stdout_tail:
                        stderr_tail = stdout_tail
                    exit_code = tsc_result.data.get("exit_code")
                return ActionResult(
                    success=False,
                    error=(
                        f"TypeScript check failed BEFORE build "
                        f"(saved you ~30-60 s on a doomed `vite build`). "
                        f"Exit code: {exit_code}. Fix these type errors, "
                        f"then call PreviewPublish again.\n\n"
                        f"{stderr_tail[:4000] if stderr_tail else tsc_result.error}"
                    ),
                    data=tsc_result.data or {},
                )

        # 4. npm run build (foreground, blocking)
        build_script = (params.build_script or "build").strip() or "build"
        build_result = await _run_bash({
            "command": f"npm run {build_script}",
            "timeout": params.timeout,
        })
        if not build_result.success:
            return ActionResult(
                success=False,
                error=(
                    f"`npm run {build_script}` failed: "
                    f"{build_result.error or 'unknown error'}. "
                    f"Read stderr above to find the failing module or "
                    f"runtime error, fix it, call PreviewPublish again."
                ),
                data=build_result.data or {},
            )

        # 5. Verify build output
        output_rel = (params.output_dir or "dist").strip("/\\") or "dist"
        output_dir = (ws_path / output_rel).resolve()
        try:
            output_dir.relative_to(ws_path.resolve())
        except ValueError:
            return ActionResult(
                success=False,
                error=(
                    f"output_dir `{output_rel}` escapes the workspace "
                    f"root. Use a path relative to the workspace."
                ),
            )
        if not output_dir.is_dir():
            return ActionResult(
                success=False,
                error=(
                    f"Build succeeded but `{output_rel}/` is missing under "
                    f"the workspace root ({ws_path}). Either the build "
                    f"script writes elsewhere - pass the correct "
                    f"`output_dir` - or it crashed silently."
                ),
            )
        index_html = output_dir / "index.html"
        if not index_html.is_file():
            return ActionResult(
                success=False,
                error=(
                    f"Build wrote to `{output_rel}/` but no index.html "
                    f"was produced. SPA / single-entry builds must "
                    f"produce index.html at the dist root."
                ),
            )

        # 6. Copy to ~/.digitorn/published/<app_id>/<session_id>/
        app_id = self._app_id_str()
        published_root = (
            Path.home() / ".digitorn" / "published" / app_id / sid
        )
        try:
            if published_root.exists():
                await asyncio.to_thread(
                    shutil.rmtree, published_root, ignore_errors=True,
                )
            published_root.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                shutil.copytree, str(output_dir), str(published_root),
            )
        except OSError as exc:
            return ActionResult(
                success=False,
                error=(
                    f"Failed to publish build output to "
                    f"{published_root}: {exc}"
                ),
            )

        # 7. Register the attachment (single 'published' slot per session,
        # overwrites any previous publish for the same session).
        att = Attachment(
            name="published",
            session_id=sid,
            type="published",
            app_id=app_id,
            path=(params.path or "").strip(),
            dist_dir=str(published_root),
            user_id=self._current_user_id(),
        )
        prev_published = self._attachments.get((sid, "published"))
        self._attachments[(sid, "published")] = att
        self._persist_to_disk()
        self._emit_attached(att)
        self._emit_event(
            "preview_publish",
            app_id=app_id,
            session_id=sid,
            user_id=att.user_id,
            name="published",
            type="published",
            dist_dir=str(published_root),
            replaced=prev_published is not None,
        )
        return self._build_attach_result(att)

    @action(
        description="Drop an attachment so the iframe stops loading it.",
        params_model=DetachParams,
        tool_prompt=(
            "Remove an attachment so the Preview tab no longer serves it. "
            "Use when:\n"
            "- The user asks to stop a specific preview surface "
            "(e.g. they killed a backend you'd attached as name='backend').\n"
            "- The dev server died and you want the iframe to show a "
            "clean empty state instead of a 502.\n"
            "- You're swapping the underlying server for the same name "
            "(detach FIRST to avoid stale routing during the swap).\n\n"
            "After detaching, tell the user the preview slot is free."
        ),
        risk_level="low",
        tags=["preview"],
    )
    async def detach(self, params: DetachParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(
                success=False,
                error="No active session.",
            )
        # Single-slot detach: drop the proxy attachment. Bundled (the
        # session's static fallback) is intentionally left in place so
        # the iframe can fall back to it once the proxy is gone.
        prev = self._attachments.pop((sid, "proxy"), None)
        if prev is None:
            return ActionResult(
                success=True,
                data={"removed": False, "hint": "No proxy attachment to remove."},
            )
        if prev.bash_task_id:
            try:
                await self._kill_bash_task(sid, prev.bash_task_id)
            except Exception as exc:
                logger.warning(
                    "preview_detach_kill_failed sid=%s task=%s err=%s",
                    sid, prev.bash_task_id, exc,
                )
        self._persist_to_disk()
        self._emit_event(
            "preview_detach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=prev.user_id,
            name=prev.name,
            type=prev.type,
            port=prev.port,
            lifetime_seconds=round(time.time() - prev.created_at, 1),
            reason="agent_request",
            killed_bash=bool(prev.bash_task_id),
        )
        bundled = self._attachments.get((sid, "bundled"))
        return ActionResult(
            success=True,
            data={
                "removed": True,
                "previous": prev.to_dict(),
                "attachments": {
                    "proxy": None,
                    "bundled": bundled.to_dict() if bundled else None,
                    "count": 1 if bundled else 0,
                },
            },
        )

    async def _proxy_automated(
        self, params: "ProxyParams", sid: str,
    ) -> ActionResult:
        # 1. Resolve workspace
        ws_path = self._resolve_workspace_path()
        if ws_path is None:
            return ActionResult(
                success=False,
                error=(
                    "Cannot resolve workspace directory. The session has "
                    "no workspace set. Check the app's workspace_mode."
                ),
            )
        pkg_json = ws_path / "package.json"
        if not pkg_json.is_file():
            return ActionResult(
                success=False,
                error=(
                    f"No package.json at the workspace root "
                    f"({ws_path}). Scaffold a project first "
                    f"(e.g. `npm create vite@latest .` in the workspace)."
                ),
            )

        # 2. Free port (preferred → fallback)
        preferred_port = params.port if params.port is not None else 5173
        free_port = await self._find_free_port(
            preferred=preferred_port, max_tries=8,
        )
        if free_port is None:
            return ActionResult(
                success=False,
                error=(
                    f"Could not find a free port in the range "
                    f"{preferred_port}–{preferred_port + 7}. Stop any "
                    f"running dev servers and retry."
                ),
            )

        # 3. Get shell module
        shell = self._resolve_shell_for_current_app()
        if shell is None:
            return ActionResult(
                success=False,
                error=(
                    "Shell module is not loaded for this app - the "
                    "automated PreviewProxy needs it to run npm. Add "
                    "`shell: {}` to the app's modules block."
                ),
            )

        # Dispatch through `execute` so worker-mode proxies work; pass
        # our own context so shell._check_cwd resolves the workspace
        # instead of clobbering the shared contextvar to `None`.
        our_ctx = self._context_var.get()
        async def _run_bash(args: dict[str, Any]):
            raw = await shell.execute("bash", args, context=our_ctx)
            # Normalise: `execute` returns whatever the handler
            # returned (usually an ActionResult, sometimes a dict
            # from the worker-proxy path).
            if isinstance(raw, ActionResult):
                return raw
            if isinstance(raw, dict):
                return ActionResult(
                    success=bool(raw.get("success", True)),
                    data=raw.get("data") or {
                        k: v for k, v in raw.items()
                        if k not in ("success", "error")
                    },
                    error=raw.get("error"),
                )
            return ActionResult(success=True, data={"raw": raw})

        # 4. npm install (foreground), if requested
        if params.install:
            install_result = await _run_bash({
                "command": "npm install",
                "timeout": 300,
            })
            if not install_result.success:
                return ActionResult(
                    success=False,
                    error=(
                        f"`npm install` failed: "
                        f"{install_result.error or 'unknown error'}. "
                        f"Read the stderr above to find the failing "
                        f"package or peer-dep mismatch, fix the cause, "
                        f"then call PreviewProxy() again."
                    ),
                    data=install_result.data or {},
                )

        # 5. Spawn `npm run dev` in bg
        dev_cmd = (
            f"npm run dev -- --host 0.0.0.0 --port {free_port}"
        )
        dev_result = await _run_bash({
            "command": dev_cmd,
            "run_in_background": True,
        })
        if not dev_result.success:
            return ActionResult(
                success=False,
                error=(
                    f"Failed to spawn dev server: "
                    f"{dev_result.error or 'unknown error'}."
                ),
            )
        bash_task_id = (dev_result.data or {}).get("task_id")
        if not bash_task_id:
            return ActionResult(
                success=False,
                error=(
                    "Spawned dev server but no task_id returned by the "
                    "shell module - this should never happen. Retry."
                ),
            )

        # 6. Wait for port to bind
        wait_budget_s = float(getattr(params, "wait_seconds", 0) or 0) or 15.0
        ok, hint = await self._wait_for_bind(
            params.host, free_port, wait_budget_s,
        )
        if not ok:
            # Capture stderr from the dev task before killing it.
            task = getattr(shell, "_tasks", {}).get(bash_task_id)
            stderr_tail = ""
            exit_code: int | None = None
            if task is not None:
                stderr_tail = "\n".join(
                    list(getattr(task, "stderr_lines", []))[-20:]
                )
                exit_code = getattr(task, "exit_code", None)
            # Kill the orphan so we don't leak.
            try:
                await _run_bash({"task_id": bash_task_id, "kill": True})
            except Exception as exc:
                logger.debug(
                    "preview_automated_kill_failed task=%s err=%s",
                    bash_task_id, exc,
                )
            return ActionResult(
                success=False,
                error=(
                    f"Dev server didn't bind port {free_port} after "
                    f"{wait_budget_s:.0f}s ({hint}). "
                    + (
                        f"Exit code: {exit_code}. "
                        if exit_code is not None else ""
                    )
                    + (
                        f"Stderr tail:\n{stderr_tail}\n"
                        if stderr_tail else
                        "No stderr captured - the process may still be "
                        "starting; bump `wait_seconds` for slow SSR "
                        "frameworks (Next.js, Remix, Nuxt). "
                    )
                    + "Fix the root cause and call PreviewProxy() again."
                ),
            )

        # 7. Register attachment (replace previous proxy slot)
        t0 = time.time()
        prev_proxy = self._attachments.get((sid, "proxy"))
        if prev_proxy is not None and prev_proxy.bash_task_id:
            try:
                await self._kill_bash_task(sid, prev_proxy.bash_task_id)
            except Exception as exc:
                logger.warning(
                    "preview_proxy_replace_kill_failed sid=%s task=%s err=%s",
                    sid, prev_proxy.bash_task_id, exc,
                )

        att = Attachment(
            name="proxy",
            session_id=sid,
            type="proxy",
            port=free_port,
            host=params.host,
            path=(params.path or "").strip(),
            user_id=self._current_user_id(),
            bash_task_id=bash_task_id,
        )
        self._attachments[(sid, "proxy")] = att
        self._ensure_reaper_running()
        self._persist_to_disk()
        self._emit_attached(att)
        self._emit_event(
            "preview_attach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=att.user_id,
            name="proxy",
            type="proxy",
            port=free_port,
            host=params.host,
            bash_task_id=bash_task_id,
            replaced=prev_proxy is not None,
            automated=True,
            duration_ms=round((time.time() - t0) * 1000, 1),
        )
        return self._build_attach_result(att)

    def _resolve_workspace_path(self) -> Path | None:
        ctx = self._context_var.get()
        ws = getattr(ctx, "workspace", None) if ctx else None
        if not ws:
            ws_mod = getattr(self, "_workspace_module", None)
            if ws_mod is not None:
                try:
                    ws = ws_mod._resolve_sync_dir()
                except Exception:
                    ws = None
        if not ws:
            return None
        try:
            p = Path(ws).expanduser().resolve()
        except Exception:
            return None
        return p if p.is_dir() else None

    async def _find_free_port(
        self, preferred: int, max_tries: int = 8,
    ) -> int | None:
        import socket as _socket

        def _bind_test(port: int) -> bool:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            try:
                # SO_REUSEADDR off so we get the real "bindable" answer.
                # Bind to 0.0.0.0 because that's what the dev server
                # will use.
                s.bind(("0.0.0.0", port))
            except OSError:
                return False
            finally:
                s.close()
            return True

        for offset in range(max_tries):
            candidate = preferred + offset
            if candidate < 1 or candidate > 65535:
                continue
            try:
                free = await asyncio.to_thread(_bind_test, candidate)
            except Exception:
                free = False
            if free:
                return candidate
        return None

    async def _wait_for_bind(
        self, host: str, port: int, budget_s: float,
    ) -> tuple[bool, str]:
        poll_interval_s = 0.5
        attempts = max(1, int(budget_s / poll_interval_s))
        hint = ""
        for attempt in range(attempts):
            ok, hint = await self._probe_port(
                host, port, timeout=poll_interval_s,
            )
            if ok:
                return True, ""
            if attempt < attempts - 1:
                await asyncio.sleep(poll_interval_s)
        return False, hint

    def _build_attach_result(
        self,
        attachment: "Attachment",
    ) -> ActionResult:
        sid = attachment.session_id
        proxy = self._attachments.get((sid, "proxy"))
        bundled = self._attachments.get((sid, "bundled"))
        active_count = sum(1 for a in (proxy, bundled) if a is not None)

        base: dict[str, Any] = {
            **attachment.to_dict(),
            "iframe_url": self.attachment_url(attachment),
            "attachments": {
                "proxy": proxy.to_dict() if proxy else None,
                "bundled": bundled.to_dict() if bundled else None,
                "count": active_count,
            },
        }
        fallback_msg = ""
        if bundled is not None and attachment.type == "proxy":
            fallback_msg = (
                f" Bundled fallback is also registered "
                f"({self.attachment_url(bundled)}); if your dev server "
                f"dies the iframe falls back to it."
            )
        base["hint"] = (
            f"ATTACHED. The user's Preview tab is now showing your dev "
            f"server on port {attachment.port}. {active_count} active "
            f"attachment(s) on this session.{fallback_msg} Tell the user: "
            f"'Dev server is live in the Preview tab.'"
        )
        return ActionResult(success=True, data=base)

    def auto_attach_bundled_dist(
        self,
        *,
        session_id: str,
        app_id: str,
        install_dir: str,
        user_id: str | None = None,
        name: str = "bundled",
    ) -> bool:
        """Register the session's bundled-dist fallback attachment."""
        if not self._enabled:
            return False
        if not session_id or not app_id or not install_dir:
            return False
        # Don't clobber an existing bundled slot.
        if (session_id, "bundled") in self._attachments:
            return False
        index_html = Path(install_dir) / "web" / "dist" / "index.html"
        if not index_html.is_file():
            return False
        att = Attachment(
            type="bundled",
            name=name,
            session_id=session_id,
            app_id=app_id,
            install_dir=install_dir,
            user_id=user_id,
        )
        self._attachments[(session_id, "bundled")] = att
        self._persist_to_disk()
        self._emit_attached(att)
        self._emit_event(
            "preview_attach",
            app_id=app_id,
            session_id=session_id,
            user_id=user_id,
            name=name,
            type="bundled",
            install_dir=install_dir,
        )
        return True

    def _check_attach_quota(
        self, session_id: str, name: str,
    ) -> str | None:
        # Re-attach over an existing slot → free pass.
        if (session_id, name) in self._attachments:
            return None

        user_id = self._current_user_id()
        if user_id:
            per_user = sum(
                1 for att in self._attachments.values()
                if att.user_id == user_id
            )
            if per_user >= _MAX_ATTACHMENTS_PER_USER:
                return (
                    f"Refused: user '{user_id}' already has "
                    f"{per_user} active preview attachments across "
                    f"all sessions (limit: {_MAX_ATTACHMENTS_PER_USER}). "
                    f"Close some sessions before adding a new "
                    f"attachment."
                )
        return None

    def _current_user_id(self) -> str | None:
        try:
            ctx = self._context_var.get()
        except Exception:
            return None
        if ctx is None:
            return None
        uid = getattr(ctx, "user_id", None)
        return uid if uid else None

    def _ensure_reaper_running(self) -> None:
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop (test context); skip.
        self._reaper_task = loop.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SEC)
                await self._reap_idle_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("web_preview_reaper_iter_failed: %s", exc)

    async def _reap_idle_once(self) -> int:
        now = time.time()
        cutoff = now - _IDLE_REAP_AFTER_SEC
        stale_keys: list[tuple[str, str]] = []
        for key, att in self._attachments.items():
            if att.last_hit_at < cutoff:
                stale_keys.append(key)
        if not stale_keys:
            return 0
        for key in stale_keys:
            att = self._attachments.pop(key, None)
            if att is None:
                continue
            idle_for = round(now - att.last_hit_at, 1)
            self._emit_event(
                "preview_reap",
                app_id=self._app_id_str(),
                session_id=att.session_id,
                user_id=att.user_id,
                name=att.name,
                type=att.type,
                port=att.port,
                idle_seconds=idle_for,
                lifetime_seconds=round(now - att.created_at, 1),
                killed_bash=bool(att.bash_task_id),
            )
            if att.bash_task_id:
                await self._kill_bash_task(att.session_id, att.bash_task_id)
        self._persist_to_disk()
        return len(stale_keys)

    def _resolve_shell_for_current_app(self) -> Any | None:
        app_id = self._app_id_str()
        shell_mod = self._shells_by_app.get(app_id)
        if shell_mod is not None:
            return shell_mod
        return self._shell

    def _resolve_shell_for_attachment(
        self, attachment: "Attachment",
    ) -> Any | None:
        if attachment.app_id:
            shell_mod = self._shells_by_app.get(attachment.app_id)
            if shell_mod is not None:
                return shell_mod
        return self._shell

    async def _kill_bash_task(
        self, session_id: str, task_id: str,
        shell_mod: Any | None = None,
    ) -> None:
        if shell_mod is None:
            shell_mod = self._shell
        if shell_mod is None:
            logger.debug(
                "web_preview_kill_bash_no_shell sid=%s task=%s",
                session_id, task_id,
            )
            return
        try:
            from digitorn.modules.shell.params import BashParams
            params = BashParams(task_id=task_id, kill=True)
            await shell_mod.execute(
                "bash", params.model_dump(), context=self._context_var.get(),
            )
            logger.info(
                "web_preview_killed_bash sid=%s task=%s",
                session_id, task_id,
            )
        except Exception as exc:
            logger.debug(
                "web_preview_kill_bash_failed sid=%s task=%s err=%s",
                session_id, task_id, exc,
            )

    def _current_session_id(self) -> str | None:
        try:
            ctx = self._context_var.get()
        except Exception:
            return None
        if ctx is None:
            return None
        sid = getattr(ctx, "session_id", None)
        return sid if sid else None

    @staticmethod
    async def _probe_port(
        host: str, port: int, timeout: float = 0.8,
    ) -> tuple[bool, str]:
        # Step 1: TCP probe.
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            return False, f"port {port} not bound ({type(exc).__name__})"

        # Step 2: HTTP GET to detect host-header rejection.
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout,
            )
            req = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                f"User-Agent: digitorn-web-preview-probe\r\n"
                f"Accept: text/html\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(req)
            await writer.drain()
            data = await asyncio.wait_for(
                reader.read(4096), timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception as exc:
                logger.debug("module best-effort block failed: %s", exc)
            head = data[:600].decode("utf-8", errors="replace")
            status_line = head.split("\r\n", 1)[0] if head else ""
            body_lower = head.lower()
            if "blocked request" in body_lower:
                return False, (
                    f"Vite rejected the request (Host check). Add "
                    f"`server.allowedHosts: 'all'` to vite.config.js or "
                    f"explicitly allow the iframe domain."
                )
            if "invalid host header" in body_lower:
                return False, (
                    f"webpack-dev-server rejected the host. Set "
                    f"DANGEROUSLY_DISABLE_HOST_CHECK=true OR configure "
                    f"`devServer.allowedHosts: 'all'`."
                )
            if "cross origin request blocked" in body_lower:
                return False, (
                    f"Next.js dev server rejected cross-origin. Restart "
                    f"with `next dev -H 0.0.0.0 -p {port}`."
                )
            if not status_line.startswith("HTTP/"):
                return False, (
                    f"port {port} responded with non-HTTP data - is this "
                    f"actually a web server?"
                )
            # Accept any 2xx or 3xx as healthy
            if " 2" in status_line[:20] or " 3" in status_line[:20]:
                return True, f"HTTP probe ok ({status_line.strip()})"
            return True, f"HTTP probe got {status_line.strip()} (attaching anyway)"
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            # TCP worked, HTTP didn't - server hung or non-HTTP. Still
            # consider OK because the port is alive; some dev servers
            # need more than 800ms to first-respond.
            return True, f"TCP ok, HTTP probe inconclusive ({type(exc).__name__})"
