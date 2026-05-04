"""Session-scoped iframe preview attachments.

The agent points an iframe at a running dev server it spawned via Bash:
``PreviewProxy(port=5173, name="default")``. The daemon stores the
``(session_id, name) -> port/host`` mapping and emits a Socket.IO
``web_preview:attached`` event carrying the direct-connect URL the
client should load. The daemon does NOT proxy HTTP, does NOT serve
static files, does NOT spawn processes - it is purely a registry.

Attachments are *session-scoped*: two different sessions of the same
app see two independent previews. Multiple attachments per session
are supported via the ``name`` field, e.g. one app can expose
``name="frontend"`` and ``name="backend"`` simultaneously.

For static-built apps (e.g. ``npm run build`` -> ``dist/``), the agent
runs ``python -m http.server`` on a port via Bash, then PreviewProxy.
Same path for everything - one tool, one mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    ListParams,
    ProxyParams,
)

logger = logging.getLogger(__name__)

# Hard limits to keep a single agent / session from accidentally
# spawning hundreds of dev servers and bringing the daemon to its
# knees. Both ceilings are checked at attach time. The agent gets a
# clear error so it can either detach an existing one or revisit
# its strategy. Numbers chosen to be roomy for legitimate use
# (frontend + backend + docs + admin = 4) but tight enough to catch
# runaway loops.
_MAX_ATTACHMENTS_PER_SESSION = 5
_MAX_ATTACHMENTS_PER_USER = 20

# Idle reaper: an attachment with no HTTP traffic for this long is
# considered abandoned and gets dropped. The matching bash task is
# killed too if it was registered (best-effort). Conservative
# default; an actively-used preview hits HTTP at least every few
# minutes (HMR pings, asset reloads, user navigation).
_IDLE_REAP_AFTER_SEC = 30 * 60  # 30 minutes
_REAPER_INTERVAL_SEC = 5 * 60   # scan every 5 minutes


class WebPreviewConfig(BaseModel):
    """Compile-time config (currently empty — kept for forward compat)."""

    model_config = {"extra": "allow"}


@dataclass
class Attachment:
    """One iframe-preview pointer for a (session, name) pair.

    Two sources of URL:
      - ``proxy``:  agent registered a dev server via PreviewProxy(port=N).
                    URL = ``http://{host}:{port}`` (browser direct-connect).
      - ``bundled``: app ships a pre-built ``web/dist/`` and uses the SDK.
                    Auto-registered at session create. URL points at the
                    daemon's ``/api/apps/{app_id}/web-static/index.html``
                    static-file route. No process to spawn or reap.
    """

    name: str
    session_id: str
    type: str = "proxy"
    # Proxy attachments
    port: int | None = None
    host: str = "127.0.0.1"
    bash_task_id: str | None = None
    # Bundled attachments (SDK apps)
    app_id: str | None = None
    install_dir: str | None = None
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
            if self.bash_task_id:
                out["bash_task_id"] = self.bash_task_id
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
        """Inject the agent's mental model of the preview surface.

        Without this, the agent knows the tools but not the UX:
        where the preview lives, when the user expects to see it,
        what to say when the dev server starts, etc.
        """
        return [
            {
                "id": "web_preview.context",
                "title": "Live Preview — Environment Awareness",
                "priority": 40,
                "position": "after_tools",
                "content": (
                    "## What the user sees\n"
                    "The chat UI is paired with a Workspace panel (docked "
                    "side panel on desktop, dedicated view on mobile). The "
                    "panel has tabs: Code, Preview, Changes. The Preview "
                    "tab embeds an iframe that loads whatever URL you "
                    "publish via PreviewProxy. The browser connects "
                    "directly to your dev server; the daemon is out of "
                    "the path.\n\n"
                    "## One preview mode: dev server on a port\n"
                    "Spawn the server with `Bash(..., run_in_background=true)`, "
                    "wait for it to bind, then call "
                    "`PreviewProxy(port=N, bash_task_id=<task_id>)`. "
                    "Pass the bash task_id so the daemon can clean the "
                    "dev server up if the session is idle for 30 min.\n"
                    "For built-static apps (`npm run build` -> `dist/`), "
                    "spawn `python -m http.server <port>` from the dist "
                    "directory and PreviewProxy that port. Same path for "
                    "everything.\n\n"
                    "## How to communicate with the user\n"
                    "The user is waiting to see the preview. Keep them in "
                    "the loop:\n"
                    "- When you start a dev server in background, say so.\n"
                    "- When the build/install is long, narrate progress.\n"
                    "- After PreviewProxy succeeds, tell the user: "
                    "'Preview is live - open the Preview tab in the "
                    "Workspace panel to see your app.'\n"
                    "- For multi-attach (frontend + backend), tell the "
                    "user which name maps to what.\n\n"
                    "## Common pitfalls\n"
                    "- Don't call PreviewProxy BEFORE the dev server is "
                    "bound. Watch the bash output: only attach once you "
                    "see 'Local:', 'ready in', or equivalent.\n"
                    "- If the port is already in use, kill the zombie or "
                    "pick a different port and restart your dev server.\n"
                    "- Use PreviewList to confirm what's currently "
                    "attached if you're unsure of state."
                ),
            },
        ]

    # Daemon-singleton sio reference. Set once at server startup via
    # ``WebPreviewModule.attach_sio(sio)``. Class-level because the
    # module is ``isolation=shared`` (one instance for the whole
    # daemon) and the sio is also a daemon-level resource.
    _sio_ref: Any = None

    # Operator-controlled template for the publicly reachable URL of
    # a proxy attachment. Set once at startup from ``settings.web_preview``.
    # Default works for local dev (loopback), cloud deploys override.
    _public_url_template: str = "http://{host}:{port}"

    # Kill switch. ``False`` makes ``proxy()`` refuse new attachments
    # with a clear error message. Existing attachments keep working -
    # operators can drain in place without yanking the rug out from
    # under live sessions.
    _enabled: bool = True

    @classmethod
    def attach_sio(cls, sio: Any) -> None:
        """Wire the AsyncSocketIO server. Called from ``server.py``
        once at startup, before any deploy."""
        cls._sio_ref = sio

    @classmethod
    def configure(
        cls,
        *,
        public_url_template: str,
        enabled: bool = True,
    ) -> None:
        """Apply daemon-level settings. Called from ``server.py`` at
        startup."""
        if public_url_template:
            cls._public_url_template = public_url_template
        cls._enabled = bool(enabled)
        if not cls._enabled:
            logger.warning(
                "web_preview kill switch ENABLED — new attachments "
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
        """Build the iframe-loadable URL for a proxy attachment.

        Templated via ``str.format`` so a missing field doesn't break
        the daemon — falls back to the loopback default on any error
        (KeyError / ValueError) so a malformed config is never fatal.

        Loopback IPs (``127.0.0.1`` and ``::1``) are normalised to the
        ``localhost`` hostname. Browsers treat ``127.0.0.1`` and
        ``localhost`` as DIFFERENT sites (different host strings),
        so an iframe at ``127.0.0.1:3001`` inside a parent at
        ``localhost:3000`` is third-party — third-party cookies are
        blocked, storage is partitioned, some apps refuse to render.
        Both names resolve to the same loopback so swapping is safe.
        """
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
                "web_preview_url_template_failed template=%r err=%s — "
                "falling back to loopback",
                cls._public_url_template, exc,
            )
            return f"http://{host}:{port}"

    def __init__(self) -> None:
        super().__init__()
        # (session_id, name) → Attachment
        self._attachments: dict[tuple[str, str], Attachment] = {}
        # Injected by bootstrap.
        self._workspace: Any | None = None
        # Shell module reference — injected by bootstrap so the
        # idle reaper / cleanup_session can kill bash tasks the
        # agent registered alongside the attachment.
        self._shell: Any | None = None
        # Idle reaper task — started lazily on first attach so
        # daemon boot stays fast (and so that tests / scripts that
        # import the module don't get a runaway background task).
        self._reaper_task: asyncio.Task[None] | None = None
        # Restore any attachments that survived a daemon restart.
        # Stale entries (port no longer bound) are filtered out lazily
        # on first lookup or by the idle reaper, so this load is fast
        # and never blocks daemon boot.
        self._load_persisted()

    # ─── public daemon-side accessors ────────────────────────────────

    def get_attachment(
        self, session_id: str, name: str = "default",
    ) -> Attachment | None:
        """Used by the HTTP proxy route to look up the target.

        Bumps ``last_hit_at`` on the attachment so the idle reaper
        knows it's still in use — the proxy / 302 redirect / static
        serve all flow through this single accessor, so a single
        ``touch`` call is sufficient.

        Returns ``None`` when nothing is attached for that pair.

        **Implicit single-attachment fallback**: when ``name`` is the
        canonical ``"default"`` but no attachment was registered under
        that name, AND the session has exactly ONE attachment under a
        different name, route to it. This makes the daemon forgiving
        of two real-world scenarios:

          * the agent invented a custom name (e.g. ``"lumen-landing"``)
            despite the tool prompt asking it to leave ``name`` at
            its default — the user shouldn't see a 404 because the
            agent picked a cute name;
          * the client bundle hasn't been redeployed with the
            ``web_preview:attach`` handshake bridge yet, so the iframe
            URL never gets the right ``&name=...`` hint.

        Multiple attachments under different names disable the
        fallback (ambiguous - the agent IS deliberately publishing
        several surfaces) and the explicit ``&name=`` lookup is
        required.
        """
        if not session_id:
            return None
        att = self._attachments.get((session_id, name))
        if att is None and name == "default":
            session_atts = [
                a for (sid, _), a in self._attachments.items()
                if sid == session_id
            ]
            if len(session_atts) == 1:
                att = session_atts[0]
        if att is not None:
            att.touch()
        return att

    def list_session(self, session_id: str) -> list[Attachment]:
        """All attachments for a session (any name). Daemon-side helper."""
        if not session_id:
            return []
        return [
            att for (sid, _), att in self._attachments.items()
            if sid == session_id
        ]

    async def health_check(self) -> dict[str, Any]:
        """Standard module health check — exposed at
        ``GET /api/modules/web_preview/health``.

        Wraps :meth:`health_snapshot` and adds the standard
        ``status``/``module_id``/``version`` envelope. Operator
        observability for the production launch: query this every
        minute to track active attachments, oldest-idle, etc.
        """
        snap = self.health_snapshot()
        return {
            "status": "ok",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
            **snap,
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Operator-facing summary for the health endpoint. O(N)
        over the attachments map, cheap.

        Returns ``{count, by_type, by_user, oldest_age_seconds,
        oldest_idle_seconds, sessions, max_per_session, max_per_user,
        idle_reap_after_seconds}``.
        """
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
        """Drop every attachment owned by this session.

        Also kills any bash task the agent had associated with the
        attachment — best-effort, so a missing shell module / dead
        process doesn't break session teardown. The shell module's
        own ``cleanup_session`` runs in parallel and would catch any
        miss; we just guarantee web_preview-owned tasks get a
        proper kill signal as part of attachment teardown.
        """
        if not session_id:
            return
        # Trace caller — knowing WHO triggered cleanup is critical for
        # debugging "my attachment disappeared" mysteries. Stack trace
        # shows the path through manager.end_session, abort handler,
        # or wherever the cleanup was kicked off.
        import traceback
        logger.warning(
            "web_preview cleanup_session sid=%s caller-stack:\n%s",
            session_id,
            "".join(traceback.format_stack()[-6:-1]),
        )
        to_kill: list[str] = []
        dropped: list[Attachment] = []
        keys = [k for k in self._attachments if k[0] == session_id]
        for k in keys:
            att = self._attachments.pop(k, None)
            if att is None:
                continue
            dropped.append(att)
            if att.bash_task_id:
                to_kill.append(att.bash_task_id)
        for task_id in to_kill:
            await self._kill_bash_task(session_id, task_id)
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

    # ─── structured logging ──────────────────────────────────────────

    def _emit_event(self, event: str, **fields: Any) -> None:
        """Log a single-line JSON event for post-mortem analysis.

        Operators can ``grep '"event":"preview_attach"' digitorn.log |
        jq` to count attaches per user, drill into a session, etc.
        Failure to serialize is swallowed so a weird payload field
        never breaks the runtime path that triggered the log.
        """
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
        return (
            getattr(self, "_app_id_override", None)
            or getattr(self, "_app_id", "default")
        )

    # ─── persistence ─────────────────────────────────────────────────

    @staticmethod
    def _persist_path() -> Path:
        """Daemon-wide JSON file holding all live attachments. Single
        file (not per-session) so the daemon can reload everything on
        boot with one read instead of walking workspace dirs."""
        return Path.home() / ".digitorn" / "web_preview_attachments.json"

    def _load_persisted(self) -> None:
        """Restore attachments from the on-disk JSON. Best-effort —
        file missing / corrupt = empty registry, daemon keeps booting.

        Stale entries (port no longer bound for proxy attachments) are
        kept in memory; the idle reaper or first-lookup probe will
        clean them up. Avoiding the network probe here keeps daemon
        boot synchronous and fast.
        """
        path = self._persist_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "web_preview persist load failed (%s) — starting empty",
                exc,
            )
            return
        for entry in data.get("attachments", []):
            etype = entry.get("type", "proxy")
            if etype not in ("proxy", "bundled"):
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
                        bash_task_id=entry.get("bash_task_id"),
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
                self._attachments[(att.session_id, att.name)] = att
            except (KeyError, TypeError) as exc:
                logger.debug("web_preview persist skip malformed entry: %s", exc)
        if self._attachments:
            logger.info(
                "web_preview restored %d attachment(s) from %s",
                len(self._attachments), path,
            )

    def _persist_to_disk(self) -> None:
        """Write the current registry to disk atomically.

        Writes to a temp file then renames (POSIX-atomic on Unix,
        best-effort on Windows). Synchronous and cheap for the
        registry size we expect (max ~100 entries across all
        sessions).
        """
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

    # ─── client notification (no ack) ─────────────────────────────────

    def attachment_url(self, attachment: "Attachment") -> str:
        """Compute the URL the iframe should load for this attachment.

        Proxy attachments (agent dev server): browser direct-connect URL,
        templated by ``_public_url_template``. Bundled attachments (SDK
        apps shipped with their own ``web/dist/``): the daemon's static
        file route, served from the app's install dir. In both cases the
        client treats the URL identically.
        """
        if attachment.type == "bundled":
            app_id = attachment.app_id or self._app_id_str()
            return f"/api/apps/{app_id}/web-static/index.html"
        # Proxy
        app_id_str = attachment.app_id or self._app_id_str()
        return self.render_public_url(
            host=attachment.host,
            port=attachment.port or 0,
            app_id=app_id_str,
            session_id=attachment.session_id,
            name=attachment.name,
        )

    def _emit_attached(self, attachment: "Attachment") -> None:
        """Fire-and-forget Socket.IO event to wake up the client.

        The client switches to the Preview tab + mounts the iframe at
        the URL we publish. Single emit, no ack expected.
        """
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

    # ─── @action handlers ────────────────────────────────────────────

    @action(
        description=(
            "Attach the iframe preview to a running dev server (HTTP proxy)."
        ),
        params_model=ProxyParams,
        tool_prompt=(
            "Point the user's Preview tab at a dev server you spawned in the "
            "background. Use this for live coding when you want HMR.\n\n"
            "## Scaffolding new apps — prefer official generators\n"
            "Don't hand-write `package.json` for a known framework — version "
            "mismatches between the framework core and its plugins are the "
            "#1 cause of `npm install` failures + dev server crashes that "
            "force you into ugly workarounds (dropping plugins, downgrading "
            "deps). Use the framework's own scaffolder, which always emits "
            "compatible versions:\n"
            "- Vite + React: `npm create vite@latest web -- --template react`\n"
            "- Vite + React+TS: `npm create vite@latest web -- --template react-ts`\n"
            "- Vite + Vue: `npm create vite@latest web -- --template vue`\n"
            "- Vite + Svelte: `npm create vite@latest web -- --template svelte`\n"
            "- Next.js: `npx create-next-app@latest web --javascript "
            "--tailwind=false --eslint=false --app --no-src-dir --import-alias='@/*'`\n"
            "- Astro: `npm create astro@latest web -- --template minimal "
            "--no-install --no-git --typescript=strict`\n"
            "- Nuxt: `npx nuxi@latest init web --packageManager npm --no-install`\n"
            "Then `cd web && npm install` (foreground, timeout=300). The "
            "scaffolder writes the canonical config files (vite.config, "
            "next.config, etc.) — only edit them when the user asks for "
            "specific behavior.\n\n"
            "If you can't use a generator (offline, network restricted, "
            "or the user explicitly asks for a hand-roll), pin known-good "
            "version pairs: Vite 5 + @vitejs/plugin-react 4, Vite 6 + "
            "@vitejs/plugin-react 4 + plugin-react-swc. Never drop a "
            "framework-recommended plugin to dodge a peer-dep error — "
            "fix the version pin instead.\n\n"
            "## Required sequence (just 2 calls)\n"
            "1. Spawn the dev server in background: "
            "`Bash(command='cd web && npm run dev -- --host 0.0.0.0 --port 5173', "
            "run_in_background=true)`. Capture the returned ``task_id``. "
            "The Bash call returns immediately - the process keeps running.\n"
            "2. `PreviewProxy(port=5173, bash_task_id=<task_id>)`. "
            "   PreviewProxy waits up to 15s for the port to bind "
            "   (built-in TCP+HTTP probe with retry), so YOU don't need "
            "   to wait, poll, tail any log, or read any output. Passing "
            "   ``bash_task_id`` lets the daemon kill your dev server "
            "   automatically when the session goes idle, avoiding leaked "
            "   processes. **Leave ``name`` at its default ``\"default\"``** "
            "   for single-preview apps; only pass a custom name for "
            "   multi-surface apps (frontend + backend).\n"
            "3. **Tell the user**: 'Dev server running on port N - open "
            "the Preview tab in the Workspace panel.'\n\n"
            "## What NOT to do (the framework will reject these)\n"
            "- `tail -f /dev/null`, `sleep infinity`, `cat /dev/zero`, "
            "  `while true; do :; done`: infinite-wait patterns. The "
            "  shell module rejects them outright - you lose a turn.\n"
            "- Manual port-binding loops: PreviewProxy does this for "
            "  you. Don't add a step.\n"
            "- Don't run `npm install` in the background - it has no "
            "  visible progress and wastes user attention. Run it "
            "  foreground with `timeout=300`.\n\n"
            "## Framework-specific dev server config\n"
            "**Why bind on 0.0.0.0**: when the daemon is on a separate host "
            "from the user's browser (cloud deploy), 127.0.0.1-only binding "
            "isn't reachable. Always pass `--host 0.0.0.0` so the dev server "
            "listens on every interface. Local dev still works the same.\n\n"
            "**Vite (v5+, including Vite 6)**: by default Vite rejects "
            "requests with a Host header that doesn't match localhost. "
            "Configure in `vite.config.js`:\n"
            "```js\n"
            "export default {\n"
            "  server: {\n"
            "    host: '0.0.0.0',\n"
            "    port: 5173,\n"
            "    allowedHosts: 'all',  // OR ['preview-5173.your-domain.com']\n"
            "  },\n"
            "}\n"
            "```\n"
            "Without `allowedHosts`, the iframe shows a 'Blocked request' "
            "page on any non-localhost domain.\n\n"
            "**Next.js (next dev)**: bind external with "
            "`npm run dev -- -H 0.0.0.0 -p 3000`. No host check by default. "
            "HMR works through standard WebSocket upgrade.\n\n"
            "**Create React App (react-scripts start)**: set env vars before "
            "the bash command — `Bash('cd web && HOST=0.0.0.0 PORT=3000 "
            "DANGEROUSLY_DISABLE_HOST_CHECK=true npm start', "
            "run_in_background=true)`.\n\n"
            "**Astro / Nuxt / SvelteKit**: same pattern as Vite — pass "
            "`--host 0.0.0.0 --port <N>`; check the framework docs for any "
            "host-allowlist config.\n\n"
            "**Plain Express / Fastify / Hono server**: usually binds 0.0.0.0 "
            "by default. Pass the port explicitly via env var.\n\n"
            "## Port conflicts (your responsibility)\n"
            "If the dev server fails to start with 'port already in use':\n"
            "- Try `Bash('lsof -ti :PORT | xargs -r kill -9')` (Linux/macOS) "
            "  or `Bash('powershell -c \"Get-NetTCPConnection -LocalPort PORT "
            "  | ForEach-Object {Stop-Process -Id $_.OwningProcess -Force}\"')` "
            "  on Windows / Git Bash.\n"
            "- Or restart the dev server with an explicit `--port N` on a "
            "  different port (4001, 4711, 5234, 8765 are usually free) and "
            "  attach to that.\n"
            "- **Avoid these ports**: `<1024` (privileged on Linux), "
            "  `8000` (digitorn daemon), `3000` & `5173` & `8080` (busy on "
            "  most dev machines).\n\n"
            "## Long npm install (>60s)\n"
            "Run install BEFORE attaching: a foreground `Bash('cd web && "
            "npm install', timeout=300)` (5 min cap is plenty for cold "
            "installs). Don't background-run install — the user has nothing "
            "to look at while it runs.\n\n"
            "## HMR / WebSocket (live reload)\n"
            "HMR rides the same port as the dev server. For LOCAL dev: works "
            "natively (browser → 127.0.0.1:PORT, dev server's WS upgrade "
            "succeeds). For CLOUD: the daemon operator's edge proxy must "
            "forward `Upgrade: websocket` headers — see "
            "`docs/cloud-deployment/PREVIEW_CLOUD_DEPLOYMENT.md` for the "
            "nginx / Caddy snippets that get this right.\n\n"
            "## Multi-attach (frontend + backend)\n"
            "`PreviewProxy(port=5173, name='frontend')` and "
            "`PreviewProxy(port=8001, name='backend')` register independently. "
            "Tell the user which is which: 'Frontend at /preview/?name=frontend, "
            "API at /preview/?name=backend.' Each attachment counts toward the "
            "5-per-session quota."
        ),
        risk_level="low",
        tags=["preview", "proxy", "http"],
    )
    async def proxy(self, params: ProxyParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(
                success=False,
                error="No active session — PreviewProxy must be called from within a session.",
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

        # Quota gate: refuse if the session or user is already at
        # the cap. Re-attaching the SAME name is allowed (replaces
        # the existing entry). The error message tells the agent
        # exactly what to do (detach or reuse) so it can self-recover.
        quota_err = self._check_attach_quota(sid, params.name)
        if quota_err is not None:
            return ActionResult(success=False, error=quota_err)

        # Bind-wait with bounded retry. LLMs used to do this manually
        # via `tail -f /dev/null` / `until` loops which were either
        # broken or wasteful. Doing it inside the action eliminates the
        # whole pattern: agents call PreviewProxy once, we wait for
        # the dev server to bind (up to ~15s), then attach. If the
        # port still isn't bound after the budget, log a warning and
        # attach anyway - the iframe's auto-retry covers the last 1-2s.
        if params.health_check:
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
                # Don't sleep after the last attempt - we'll fall through
                # to the "still not bound" path immediately.
                if attempt < attempts - 1:
                    await asyncio.sleep(poll_interval_s)
            if not ok:
                logger.info(
                    "web_preview_bind_wait_giveup sid=%s port=%d "
                    "budget=%.1fs hint=%s - attaching anyway, iframe "
                    "auto-retry will cover the rest",
                    sid, params.port, wait_budget_s, hint,
                )

        t0 = time.time()
        att = Attachment(
            name=params.name,
            session_id=sid,
            port=params.port,
            host=params.host,
            user_id=self._current_user_id(),
            bash_task_id=params.bash_task_id,
        )
        self._attachments[(sid, params.name)] = att
        self._ensure_reaper_running()
        self._persist_to_disk()
        self._emit_attached(att)

        self._emit_event(
            "preview_attach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=att.user_id,
            name=params.name,
            type="proxy",
            port=params.port,
            host=params.host,
            bash_task_id=params.bash_task_id,
            duration_ms=round((time.time() - t0) * 1000, 1),
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
        prev = self._attachments.pop((sid, params.name), None)
        if prev is None:
            return ActionResult(
                success=True,
                data={"name": params.name, "removed": False, "hint": "Nothing was attached under this name."},
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
        )
        return ActionResult(
            success=True,
            data={"name": params.name, "removed": True, "previous": prev.to_dict()},
        )

    @action(
        description="List active iframe-preview attachments for the current session.",
        params_model=ListParams,
        tool_prompt=(
            "Inspect the current session's attachments before acting. "
            "Returns each (name, type, port) so you can:\n"
            "- Confirm a previous PreviewProxy landed.\n"
            "- Avoid re-attaching when something is already there.\n"
            "- Decide whether to detach + re-attach vs reuse.\n"
            "Cheap to call (just reads memory) - use it whenever you're "
            "unsure of state. Don't ask the user 'is the preview attached?' "
            "- just call PreviewList and check yourself."
        ),
        risk_level="low",
        tags=["preview"],
    )
    async def list(self, params: ListParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(success=False, error="No active session.")
        items = [att.to_dict() for att in self.list_session(sid)]
        return ActionResult(success=True, data={"attachments": items, "count": len(items)})

    def _build_attach_result(
        self,
        attachment: "Attachment",
    ) -> ActionResult:
        """Shape the final ActionResult for proxy attachments.

        Registration is synchronous; the Socket.IO ``web_preview:attached``
        notification fires fire-and-forget so the agent gets
        ``success: true`` in <100ms.
        """
        base: dict[str, Any] = {
            **attachment.to_dict(),
            "iframe_url": self.attachment_url(attachment),
        }
        base["hint"] = (
            f"ATTACHED. The user's Preview tab is now showing dev server "
            f"on port {attachment.port} (name='{attachment.name}'). Tell them: "
            f"'Dev server is live in the Preview tab.'"
        )
        return ActionResult(success=True, data=base)

    # ─── auto-attach for SDK / bundled-dist apps ────────────────────

    def auto_attach_bundled_dist(
        self,
        *,
        session_id: str,
        app_id: str,
        install_dir: str,
        user_id: str | None = None,
        name: str = "default",
    ) -> bool:
        """Register an attachment that points at the app's bundled
        ``web/dist/`` served via the daemon's static-file route.

        Called by the session-create path for SDK apps that ship their
        own preview UI (digitorn-builder, digitorn-react-sandbox, etc.).
        The agent doesn't need to do anything - the iframe mounts the
        SDK app as soon as the session is alive.

        Returns ``True`` when an attachment was added, ``False`` when
        the dist isn't there or the kill switch is off (caller can log).
        """
        if not self._enabled:
            return False
        if not session_id or not app_id or not install_dir:
            return False
        # Don't clobber an existing user-driven attachment - if the
        # agent already called PreviewProxy on this name, leave it.
        if (session_id, name) in self._attachments:
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
        self._attachments[(session_id, name)] = att
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

    # ─── quotas, reaper, helpers ─────────────────────────────────────

    def _check_attach_quota(
        self, session_id: str, name: str,
    ) -> str | None:
        """Return ``None`` when a new attachment is allowed; an error
        message string otherwise.

        Re-attaching the SAME ``(session_id, name)`` is always allowed:
        it overwrites the existing entry without consuming an extra
        slot. This keeps the agent's "kill old, attach new" loop
        simple — no need to call ``PreviewDetach`` first.
        """
        # If this is a re-attach over an existing slot, we don't grow
        # any counter — let it through.
        if (session_id, name) in self._attachments:
            return None

        per_session = sum(
            1 for (sid, _) in self._attachments if sid == session_id
        )
        if per_session >= _MAX_ATTACHMENTS_PER_SESSION:
            return (
                f"Refused: this session already has "
                f"{per_session} active preview attachments "
                f"(limit: {_MAX_ATTACHMENTS_PER_SESSION}). "
                f"Detach one with PreviewDetach(name=...) before "
                f"adding a new one. Use PreviewList to see what's "
                f"currently attached."
            )

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
                    f"Close some sessions or call PreviewDetach in "
                    f"another session before adding a new attachment."
                )
        return None

    def _current_user_id(self) -> str | None:
        """Read user_id off the contextvar (set by the runtime when
        an action runs inside a session). Falls back to None for
        out-of-band calls (tests, scripts)."""
        try:
            ctx = self._context_var.get()
        except Exception:
            return None
        if ctx is None:
            return None
        uid = getattr(ctx, "user_id", None)
        return uid if uid else None

    # ─── idle reaper ────────────────────────────────────────────────

    def _ensure_reaper_running(self) -> None:
        """Start the idle reaper task if it isn't already.

        Lazily started on first attach to keep daemon boot fast and
        to avoid a permanent background task in tests / scripts that
        instantiate the module without ever attaching anything.
        """
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # No running loop (test context); skip.
        self._reaper_task = loop.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self) -> None:
        """Scan attachments every ``_REAPER_INTERVAL_SEC`` seconds,
        drop any that haven't been hit in ``_IDLE_REAP_AFTER_SEC``.

        For each reaped attachment, also kill the agent's bash task
        if one was registered (best-effort via the shell module).
        Never raises — a one-off scan failure must not break the
        loop.
        """
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SEC)
                await self._reap_idle_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("web_preview_reaper_iter_failed: %s", exc)

    async def _reap_idle_once(self) -> int:
        """Run a single pass of the idle reaper. Returns the count
        of attachments dropped."""
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

    async def _kill_bash_task(
        self, session_id: str, task_id: str,
    ) -> None:
        """Best-effort kill of the agent's background bash process.

        Uses the shell module reference injected at bootstrap time.
        Falls through silently when shell isn't loaded — tests /
        static-only apps work without it.
        """
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
            await shell_mod.execute("bash", params.model_dump())
            logger.info(
                "web_preview_killed_bash sid=%s task=%s",
                session_id, task_id,
            )
        except Exception as exc:
            logger.debug(
                "web_preview_kill_bash_failed sid=%s task=%s err=%s",
                session_id, task_id, exc,
            )

    # ─── helpers ─────────────────────────────────────────────────────

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
        """Probe a dev-server endpoint and return (ok, hint).

        Goes beyond a TCP open: also fires an HTTP GET / and inspects
        the response body for known dev-server rejection patterns
        (Vite ``Blocked request``, webpack-dev-server ``Invalid Host
        header``, Next.js ``Cross origin request blocked``). When any
        of those fire, the port IS bound but the server will refuse
        the iframe's requests — we surface that as a structured hint
        so the agent can react before the user sees a broken iframe.

        ``ok=True`` only when both TCP succeeds AND the HTTP response
        looks healthy (any 2xx / 3xx). Returns the hint string so the
        caller can log it without parsing.
        """
        # Phase 1: TCP probe
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            return False, f"port {port} not bound ({type(exc).__name__})"

        # Phase 2: HTTP GET to detect host-header rejection
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
            except Exception:
                pass
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
                    f"port {port} responded with non-HTTP data — is this "
                    f"actually a web server?"
                )
            # Accept any 2xx or 3xx as healthy
            if " 2" in status_line[:20] or " 3" in status_line[:20]:
                return True, f"HTTP probe ok ({status_line.strip()})"
            return True, f"HTTP probe got {status_line.strip()} (attaching anyway)"
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
            # TCP worked, HTTP didn't — server hung or non-HTTP. Still
            # consider OK because the port is alive; some dev servers
            # need more than 800ms to first-respond.
            return True, f"TCP ok, HTTP probe inconclusive ({type(exc).__name__})"
