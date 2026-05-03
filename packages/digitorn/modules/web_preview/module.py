"""Session-scoped iframe preview attachments.

The agent attaches the ``/api/apps/{id}/preview/`` endpoint to one of:

* a running dev server on a TCP port (vite/next/python http.server / etc.) —
  ``PreviewProxy(port=5173, name="default")``. The daemon HTTP-proxies
  requests to that server. HMR works as long as the dev server supports it.
* a directory inside the session workspace — ``PreviewStatic(path="dist")``.
  The daemon serves files from disk directly, no process to spawn or kill.
  Rebuilding the artifact (``npm run build``) is picked up on the next page
  load with no re-attach.

Both attachments are *session-scoped*: two different sessions of the same
app see two independent previews. Multiple attachments per session are
supported via the ``name`` field, e.g. one app can expose
``name="frontend"`` and ``name="backend"`` simultaneously.

The daemon HTTP route reads ``_attachments[(session_id, name)]`` directly —
no context-var look-up — so it doesn't matter that the route runs outside
the LLM's tool-call context. We resolve the workspace path at attach-time,
when the context-var IS set, and store the absolute path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest
from digitorn.modules.web_preview.params import (
    DetachParams,
    ListParams,
    ProxyParams,
    StaticParams,
)

logger = logging.getLogger(__name__)

# Time the daemon waits for the client to confirm it switched to the
# Preview tab and rendered the iframe. Beyond this we return success
# anyway so the agent isn't stuck — the attachment is registered, only
# the UI handshake failed (client offline, slow render, etc).
_CLIENT_ACK_TIMEOUT_SEC = 8.0

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
    """One iframe-preview pointer for a (session, name) pair."""

    type: Literal["proxy", "static"]
    name: str
    session_id: str
    created_at: float = field(default_factory=time.time)
    # Last time the iframe HTTP-touched this attachment. Bumped on
    # every proxy redirect / static file serve / 302 fallback. The
    # idle reaper uses this to decide what's abandoned.
    last_hit_at: float = field(default_factory=time.time)
    # User this attachment belongs to. Filled at attach-time when
    # available so quotas can be enforced per-user across sessions.
    user_id: str | None = None
    # Optional bash task_id the agent supplied (returned by
    # ``Bash(run_in_background=true)``). The reaper kills it via the
    # shell module when the attachment is dropped for inactivity.
    bash_task_id: str | None = None
    # proxy-only
    port: int | None = None
    host: str = "127.0.0.1"
    # static-only — absolute, already-resolved path on disk
    abs_path: str | None = None
    index_file: str = "index.html"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_hit_at": self.last_hit_at,
        }
        if self.user_id:
            out["user_id"] = self.user_id
        if self.bash_task_id:
            out["bash_task_id"] = self.bash_task_id
        if self.type == "proxy":
            out["port"] = self.port
            out["host"] = self.host
        else:
            out["path"] = self.abs_path
            out["index_file"] = self.index_file
        return out

    def touch(self) -> None:
        """Bump ``last_hit_at`` to ``now`` — call from the proxy /
        redirect / static-serve path."""
        self.last_hit_at = time.time()


class WebPreviewModule(BaseModule):
    """Iframe-preview attachment registry, keyed by (session, name)."""

    MODULE_ID = "web_preview"
    VERSION = "1.0.0"
    CONFIG_MODEL = WebPreviewConfig

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Session-scoped iframe preview attachments. The agent "
                "attaches /preview/ to a running dev server (proxy) or a "
                "static directory in the workspace (static). Multi-attach "
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
                    "The chat UI is paired with a **Workspace panel** "
                    "(docked side panel on desktop, dedicated view on "
                    "mobile). The panel has tabs: **Code**, **Preview**, "
                    "**Changes**. The Preview tab embeds an iframe that "
                    "loads `/api/apps/{app_id}/preview/?session_id={sid}` — "
                    "whatever you attach via PreviewProxy / PreviewStatic "
                    "appears there live.\n\n"
                    "## Three preview modes\n"
                    "1. **Live dev server** — when you're actively coding "
                    "and want HMR. You spawn a dev server (`Bash` with "
                    "`run_in_background=true`), wait for it to bind, then "
                    "call `PreviewProxy(port=N, bash_task_id=<task_id>)`. "
                    "Pass the bash task_id so the daemon can clean the "
                    "dev server up if the session is idle for 30 min. "
                    "The iframe connects to your dev server directly.\n"
                    "2. **Built static** — when the app is ready and you "
                    "want a lightweight, no-process preview. You run "
                    "`npm run build` (or equivalent), then call "
                    "`PreviewStatic(path='dist')`. The daemon serves files "
                    "directly from disk; rebuilding is visible on next "
                    "page load with no re-attach.\n"
                    "3. **Declarative** — the app already ships a built "
                    "`web/dist/` and the iframe loads it without you "
                    "doing anything. You don't need PreviewProxy/"
                    "PreviewStatic in this case — just write the files "
                    "the iframe expects (e.g. via the workspace module).\n"
                    "If you don't know which mode applies, look at the "
                    "user's request: 'build me an app that does X' usually "
                    "means mode 1 or 2. 'preview my dist' means mode 2. "
                    "If the app pre-exists with a web/dist, mode 3 (don't "
                    "fight the framework).\n\n"
                    "## How to communicate with the user\n"
                    "The user is **waiting to see the preview**. Keep them "
                    "in the loop:\n"
                    "- When you start a dev server in background, say so: "
                    "  'Starting the dev server in background — should be "
                    "  ready in a few seconds.'\n"
                    "- When the build/install is long, narrate progress.\n"
                    "- After you call PreviewProxy or PreviewStatic, "
                    "  **explicitly tell the user the preview is ready** "
                    "  and where to look: \n"
                    "    'Preview is live — open the **Preview** tab in "
                    "    the Workspace panel to see your app.'\n"
                    "- If the user might not have the workspace panel open, "
                    "  guide them: 'If you don't see the Workspace panel, "
                    "  click the workspace icon in the chat toolbar.'\n"
                    "- For multi-attach (e.g. frontend + backend), tell the "
                    "  user which name maps to what: 'Frontend on tab 1, "
                    "  API on tab 2.'\n\n"
                    "## Common pitfalls\n"
                    "- Don't call PreviewProxy BEFORE the dev server is "
                    "bound. Watch the bash output: only attach once you "
                    "see 'Local:', 'ready in', or equivalent.\n"
                    "- If the port is already in use, YOU resolve it: "
                    "kill the zombie (`Bash` again with the right kill "
                    "command), or pick a different port and restart your "
                    "dev server with that port.\n"
                    "- Don't switch back and forth between Proxy and Static "
                    "for the same name — detach first, then re-attach.\n"
                    "- Static path is workspace-relative (e.g. `dist`, "
                    "`web/dist`), not absolute. The directory must exist "
                    "on disk first (you ran the build).\n"
                    "- Use `PreviewList` to confirm what's currently "
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

    # Kill switch. ``False`` makes ``proxy()`` / ``static()`` refuse
    # new attachments with a clear error message. Existing attachments
    # keep working — operators can drain in place without yanking the
    # rug out from under live sessions.
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
        # Pending client ACKs keyed by request_id. The HTTP/SIO ack
        # handler resolves these futures so the proxy/static action
        # can return only after the iframe has actually rendered.
        self._pending_acks: dict[str, asyncio.Future[dict[str, Any]]] = {}
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
                    path=att.abs_path,
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
            "## Required sequence (don't skip steps)\n"
            "1. Spawn the dev server with the right host + port flags: "
            "`Bash(command='cd web && npm run dev -- --host 0.0.0.0 --port 5173', "
            "run_in_background=true)`. Capture the returned ``task_id``.\n"
            "2. **Wait for it to bind**. Read the bash output until you see "
            "  `Local: http://...:PORT`, `ready in Xms`, `Listening on PORT` "
            "  or similar. If 5 seconds pass with no such marker, re-read "
            "  the task output. Don't attach to a port that isn't bound yet — "
            "  health_check will warn but you'll waste user time.\n"
            "3. `PreviewProxy(port=<that port>, bash_task_id=<task_id>)`. "
            "  Passing ``bash_task_id`` lets the daemon kill your dev "
            "  server automatically if the session goes idle for 30+ min, "
            "  avoiding leaked processes. **Leave `name` at its default "
            "  ``\"default\"``** for single-preview apps; only pass a custom "
            "  name when you have multiple surfaces (e.g. frontend + "
            "  backend).\n"
            "4. **Tell the user the preview is live and where to find it**: "
            "   'Dev server running on port N — open the Preview tab in "
            "   the Workspace panel to see it.'\n\n"
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

        if params.health_check:
            ok, hint = await self._probe_port(params.host, params.port)
            if not ok:
                logger.info(
                    "web_preview_health_check_warn sid=%s port=%d hint=%s",
                    sid, params.port, hint,
                )

        t0 = time.time()
        att = Attachment(
            type="proxy",
            name=params.name,
            session_id=sid,
            port=params.port,
            host=params.host,
            user_id=self._current_user_id(),
            bash_task_id=params.bash_task_id,
        )
        self._attachments[(sid, params.name)] = att
        self._ensure_reaper_running()

        ack_result = await self._emit_attach_and_wait(sid, att)
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
            client_status=str(ack_result.get("status") or ""),
        )
        return self._build_attach_result(att, ack_result, kind="proxy")

    @action(
        description="Attach the iframe preview to a static directory in the session workspace.",
        params_model=StaticParams,
        tool_prompt=(
            "Point the user's Preview tab at a built static directory. "
            "Use this when the app is ready to ship and you don't need "
            "HMR — way lighter than a dev server (no Node process, no "
            "port).\n\n"
            "## Required sequence\n"
            "1. Build the app: `Bash(command='cd web && npm run build')`. "
            "  Wait for it to finish (this is foreground bash, not "
            "  background — you NEED the build to be done).\n"
            "2. Verify the build produced a directory (e.g. `dist/`). If "
            "  the build failed, fix the error first; don't attach to "
            "  a non-existent dir.\n"
            "3. `PreviewStatic(path='web/dist')` (path is "
            "  workspace-relative). The default `name` is "
            "  ``\"default\"``; **leave it unchanged for single-preview "
            "  apps** — pass a custom `name` only if you have multiple "
            "  preview surfaces (e.g. frontend + backend / docs). The "
            "  client looks up `name='default'` first; using a custom "
            "  name without telling the client breaks the iframe.\n"
            "4. **Tell the user**: 'Built and served — open the Preview "
            "  tab in the Workspace panel to view your app.'\n\n"
            "## Subsequent rebuilds\n"
            "Once attached, re-running `npm run build` is enough — no "
            "re-attach needed. The daemon reads files from disk on every "
            "request, so the next page reload picks up the new bundle. "
            "Tell the user to refresh the Preview tab after each rebuild.\n\n"
            "## Common mistakes\n"
            "- Path is **workspace-relative**, NEVER absolute. `dist`, "
            "`web/dist`, `apps/site/dist` are valid; `/home/.../dist` is "
            "rejected.\n"
            "- The directory must exist on disk. Build outputs by tool: "
            "Vite=`dist/`, CRA=`build/`, Next.js=`out/` (after "
            "`output: 'export'`), Astro=`dist/`, Nuxt=`.output/public/`, "
            "Remix=`build/client/`, vanilla webpack=`dist/`.\n"
            "- workspace.sync_to_disk must be true (default). If the app "
            "explicitly sets it to false, PreviewStatic can't see your "
            "build artifact.\n\n"
            "## Build configuration — daemon does the heavy lifting\n"
            "**You don't need to set a `base` URL in your bundler config.** "
            "The daemon rewrites root-absolute asset paths (`/assets/`, "
            "`/_next/`, `/_astro/`, `/static/`, `/favicon.ico`, ...) on "
            "the fly when serving HTML and CSS, AND injects a `<base>` "
            "tag so relative URLs resolve under the preview route. "
            "Default Vite/CRA/Next builds work as-is — you don't need "
            "`base: './'` or `homepage: '.'`.\n\n"
            "## When NOT to use Static — the limits\n"
            "PreviewStatic only serves files. The browser DOES make "
            "subsequent requests from the SPA's JavaScript: `fetch()`, "
            "`XMLHttpRequest`, `WebSocket`, `EventSource`, "
            "`navigator.serviceWorker.register()`. These are JS string "
            "literals — the daemon doesn't rewrite them. If your app "
            "needs ANY of these:\n"
            "- API calls (`fetch('/api/data')`) → **use PreviewProxy** "
            "  with a backend dev server, not Static\n"
            "- WebSockets / SSE → **use PreviewProxy**\n"
            "- Service workers → not supported in Static (scope issues "
            "  + URL resolution); **use PreviewProxy**\n"
            "- HMR / live reload → not supported in Static (it's a "
            "  static snapshot); **use PreviewProxy** during iteration, "
            "  switch to Static for the final demo.\n\n"
            "Pure client-side apps with no backend (landing pages, "
            "static portfolios, presentational React/Vue/Svelte demos) "
            "are perfect for Static."
        ),
        risk_level="low",
        tags=["preview", "static", "files"],
    )
    async def static(self, params: StaticParams) -> ActionResult:
        sid = self._current_session_id()
        if not sid:
            return ActionResult(
                success=False,
                error="No active session — PreviewStatic must be called from within a session.",
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

        quota_err = self._check_attach_quota(sid, params.name)
        if quota_err is not None:
            return ActionResult(success=False, error=quota_err)

        ws_dir = self._resolve_workspace_dir()
        if ws_dir is None:
            return ActionResult(
                success=False,
                error=(
                    "No workspace directory available. Make sure the workspace "
                    "module is loaded with sync_to_disk: true (the default)."
                ),
            )

        rel = params.path.replace("\\", "/").strip("/").strip()
        abs_path = os.path.normpath(os.path.join(ws_dir, rel))
        # Sandbox: refuse to escape the workspace dir.
        ws_norm = os.path.normpath(ws_dir)
        if not abs_path.startswith(ws_norm):
            return ActionResult(
                success=False,
                error=(
                    f"Refused: path '{params.path}' resolves outside the "
                    f"workspace directory."
                ),
            )
        if not os.path.isdir(abs_path):
            return ActionResult(
                success=False,
                error=(
                    f"Directory does not exist: {abs_path}. Build the app "
                    f"first (e.g. 'npm run build') then attach."
                ),
            )

        t0 = time.time()
        att = Attachment(
            type="static",
            name=params.name,
            session_id=sid,
            abs_path=abs_path,
            index_file=params.index_file,
            user_id=self._current_user_id(),
            bash_task_id=params.bash_task_id,
        )
        self._attachments[(sid, params.name)] = att
        self._ensure_reaper_running()

        ack_result = await self._emit_attach_and_wait(sid, att)
        self._emit_event(
            "preview_attach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=att.user_id,
            name=params.name,
            type="static",
            path=abs_path,
            bash_task_id=params.bash_task_id,
            duration_ms=round((time.time() - t0) * 1000, 1),
            client_status=str(ack_result.get("status") or ""),
        )
        return self._build_attach_result(att, ack_result, kind="static")

    @action(
        description="Drop an attachment so /preview/ stops serving it.",
        params_model=DetachParams,
        tool_prompt=(
            "Remove an attachment so the Preview tab no longer serves it. "
            "Use when:\n"
            "- Switching from dev-mode (PreviewProxy) to built-mode "
            "(PreviewStatic) for the same name — detach FIRST to avoid "
            "stale routing during the swap.\n"
            "- The user asks to stop a specific preview surface "
            "(e.g. they killed a backend you'd attached as name='backend').\n"
            "- The dev server died and you want the iframe to show a "
            "clean 404 instead of a 502.\n\n"
            "After detaching, **tell the user** the preview slot is "
            "free (e.g. 'Preview detached — the tab will show 404 until "
            "I attach a new one.')."
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
        self._emit_event(
            "preview_detach",
            app_id=self._app_id_str(),
            session_id=sid,
            user_id=prev.user_id,
            name=prev.name,
            type=prev.type,
            port=prev.port,
            path=prev.abs_path,
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
            "Returns each (name, type, port-or-path) so you can:\n"
            "- Confirm a previous PreviewProxy / PreviewStatic landed.\n"
            "- Avoid re-attaching when something is already there.\n"
            "- Decide whether to detach + re-attach vs reuse.\n"
            "Cheap to call (just reads memory) — use it whenever you're "
            "unsure of state. Don't ask the user 'is the preview attached?' "
            "— just call PreviewList and check yourself."
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

    # ─── client handshake ────────────────────────────────────────────

    async def _emit_attach_and_wait(
        self, session_id: str, attachment: "Attachment",
    ) -> dict[str, Any]:
        """Emit ``web_preview:attach`` and wait for the client's ACK.

        The contract:

        * Daemon emits to ``session:{sid}`` room with a unique
          ``request_id`` plus the attachment payload.
        * Client (web/Flutter) handles the event:
            - switches the workspace panel to the Preview tab
            - re-mounts the iframe with the new URL
            - waits for the iframe ``onLoad`` (the daemon's proxy
              has actually served bytes by that point)
            - emits ``web_preview:attach_ack`` carrying the same
              ``request_id`` and a status ("rendered" | "failed:...")
        * Daemon resolves the matching pending future, action
          returns to the agent with the verified status.

        Falls back gracefully:
        - sio not wired (tests, no daemon) → ``{status: "no_sio"}``
        - no client connected / ack times out → ``{status: "timeout"}``
          (the attachment IS still registered — the user can refresh)
        - client rejected / errored → ``{status: "failed:<reason>"}``
        """
        if self._sio_ref is None:
            return {"status": "no_sio", "client_rendered": False}

        request_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_acks[request_id] = fut

        payload = {
            "request_id": request_id,
            "session_id": session_id,
            "name": attachment.name,
            "type": attachment.type,
            "preview_url_template": self._preview_url(attachment.name),
        }
        if attachment.type == "proxy":
            payload["port"] = attachment.port
            payload["host"] = attachment.host
            # The publicly reachable URL for this attachment. The
            # browser opens the iframe at this URL DIRECTLY — the
            # daemon is no longer in the request path. For local dev
            # this is just ``http://127.0.0.1:{port}``; in cloud the
            # operator configures a wildcard subdomain template.
            app_id_str = (
                getattr(self, "_app_id_override", None)
                or getattr(self, "_app_id", "default")
            )
            payload["iframe_url"] = self.render_public_url(
                host=attachment.host,
                port=attachment.port,
                app_id=app_id_str,
                session_id=session_id,
                name=attachment.name,
            )
        else:
            payload["path"] = attachment.abs_path
            payload["index_file"] = attachment.index_file

        try:
            await self._sio_ref.emit(
                "web_preview:attach",
                payload,
                room=f"session:{session_id}",
                namespace="/events",
            )
        except Exception as exc:
            self._pending_acks.pop(request_id, None)
            logger.warning("web_preview_emit_failed sid=%s: %s", session_id, exc)
            return {"status": f"emit_failed:{exc}", "client_rendered": False}

        try:
            ack = await asyncio.wait_for(fut, timeout=_CLIENT_ACK_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            ack = {"status": "timeout", "client_rendered": False}
        finally:
            self._pending_acks.pop(request_id, None)

        return ack

    def handle_ack(self, request_id: str, data: dict[str, Any]) -> bool:
        """Resolve the future a pending attach call is awaiting.

        Returns True when a future was waiting; False when the ack is
        spurious (request_id unknown — late reply, double-ack, etc).
        """
        fut = self._pending_acks.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(dict(data) if isinstance(data, dict) else {})
        return True

    def _build_attach_result(
        self,
        attachment: "Attachment",
        ack: dict[str, Any],
        *,
        kind: Literal["proxy", "static"],
    ) -> ActionResult:
        """Shape the final ActionResult for both proxy and static."""
        client_rendered = bool(ack.get("client_rendered"))
        status = str(ack.get("status") or "")
        base = {
            **attachment.to_dict(),
            "preview_url": attachment.name and (
                f"/api/apps/{{app_id}}/preview/?session_id={{session_id}}"
                + (f"&name={attachment.name}" if attachment.name != "default" else "")
            ),
            "client_rendered": client_rendered,
            "client_status": status,
        }
        if kind == "proxy" and attachment.port is not None:
            app_id_str = (
                getattr(self, "_app_id_override", None)
                or getattr(self, "_app_id", "default")
            )
            base["iframe_url"] = self.render_public_url(
                host=attachment.host,
                port=attachment.port,
                app_id=app_id_str,
                session_id=attachment.session_id,
                name=attachment.name,
            )
        if client_rendered:
            verb = "Dev server" if kind == "proxy" else "Built artifact"
            base["hint"] = (
                f"ATTACHED + RENDERED. The user's Preview tab is now showing "
                f"the {kind} attachment. Tell them what they're looking at: "
                f"'{verb} '{attachment.name}' is live in the Preview tab.' "
                f"You don't need to ask them to open the tab — the client "
                f"already switched."
            )
        elif status == "timeout":
            base["hint"] = (
                "ATTACHED but the client didn't confirm within "
                f"{_CLIENT_ACK_TIMEOUT_SEC:.0f}s. The attachment IS "
                f"registered — most likely the user has the chat tab in "
                f"the background. Tell them to open the Preview tab in "
                f"the Workspace panel manually."
            )
        elif status == "no_sio":
            base["hint"] = (
                "ATTACHED. (No live client connection detected — running "
                "headless or the user's session is offline.) The route "
                "is registered; subsequent /preview/ requests will work."
            )
        else:
            base["hint"] = (
                f"ATTACHED but the client reported '{status}'. The "
                f"attachment is registered; ask the user to refresh the "
                f"Preview tab manually."
            )
        return ActionResult(success=True, data=base)

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
                path=att.abs_path,
                idle_seconds=idle_for,
                lifetime_seconds=round(now - att.created_at, 1),
                killed_bash=bool(att.bash_task_id),
            )
            if att.bash_task_id:
                await self._kill_bash_task(att.session_id, att.bash_task_id)
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

    def _resolve_workspace_dir(self) -> str | None:
        """Absolute on-disk path of the current session's workspace.

        We delegate to the workspace module's resolver because it
        encodes every fallback rule (Lovable user-chosen dir, YAML
        sync_path, ctx.workspace, per-session auto-isolation). Failing
        that, we re-implement the auto-isolation default — same shape
        as the workspace module — so the LLM can attach a static
        preview even in apps that don't load the workspace module.
        """
        ws = self._workspace
        if ws is not None:
            try:
                p = ws._resolve_sync_dir()
                if p:
                    return os.path.abspath(p)
            except Exception as exc:
                logger.debug("web_preview_workspace_resolve_failed: %s", exc)

        # Fallback: replicate the workspace module's auto-isolation.
        sid = self._current_session_id()
        if not sid:
            return None
        app_id = (
            getattr(self, "_app_id_override", None)
            or getattr(self, "_app_id", "default")
        )
        return os.path.join(
            str(Path.home()), ".digitorn", "workspaces", app_id, sid,
        )

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

    def _preview_url(self, name: str) -> str:
        suffix = "" if name == "default" else f"&name={name}"
        return f"/api/apps/{{app_id}}/preview/?session_id={{session_id}}{suffix}"
