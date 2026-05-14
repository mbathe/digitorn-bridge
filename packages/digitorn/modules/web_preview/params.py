"""Parameter models for ``web_preview`` actions.

Each model is the source of truth for the JSON schema the LLM sees.
Hidden fields (``json_schema_extra={"hidden": True}``) are stripped
from the schema before it's sent to the model — kept here only so
power-users / API callers can still pass them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = ["ProxyParams", "DetachParams", "PublishParams"]

_HIDDEN = {"hidden": True}


class ProxyParams(BaseModel):
    """Start (or attach to) the session's dev server preview.

    The default mode is **fully automated**: call with no arguments
    once your project sits at the workspace root, and the daemon will:

      1. Find a free port (preferred 5173, then 5174…).
      2. ``npm install`` (foreground).
      3. ``npm run dev`` on that port (background, auto-killed when
         the session ends).
      4. Wait for the port to bind.
      5. Register the iframe attachment.

    On failure (no ``package.json``, install error, dev server crash,
    port never bound) you get a structured error with stderr + exit
    code so you can diagnose and try again. Exactly ONE proxy
    attachment per session — each call replaces the previous proxy
    and kills its old dev server.

    **Override mode** (advanced): if you already spawned a dev server
    yourself, pass ``port`` AND ``bash_task_id`` to skip the
    automated install+run.
    """

    port: int | None = Field(
        default=None,
        ge=1, le=65535,
        description=(
            "Preferred TCP port for the dev server. Default 5173 (Vite). "
            "If the port is busy the daemon auto-falls back to the next "
            "available 5174/5175/… In ``override`` mode (with "
            "``bash_task_id``) this is the port your own dev server is "
            "listening on — no fallback."
        ),
    )
    host: str = Field(
        "127.0.0.1",
        description="Host the dev server is bound to. Almost always '127.0.0.1'.",
        json_schema_extra=_HIDDEN,
    )
    health_check: bool = Field(
        True,
        description=(
            "Wait up to 15s for the port to bind before reporting success. "
            "Leave true unless you know the server takes minutes to start "
            "and you don't care about reporting that to the user."
        ),
        json_schema_extra=_HIDDEN,
    )
    bash_task_id: str | None = Field(
        default=None,
        description=(
            "Override mode: pass the ``task_id`` of a dev server you "
            "spawned yourself via ``Bash(run_in_background=true)``. "
            "Skips the automated install+run flow and just attaches "
            "the iframe to your existing server. Default ``None`` = "
            "fully automated mode."
        ),
        json_schema_extra=_HIDDEN,
    )
    path: str = Field(
        "",
        max_length=512,
        description=(
            "Optional URL path the iframe should load AFTER host:port. "
            "Use when the entry point isn't the server root '/'. "
            "Examples: '/landing.html', '/admin'. Default empty = '/'. "
            "ALWAYS start with '/' when set."
        ),
        json_schema_extra=_HIDDEN,
    )
    install: bool = Field(
        True,
        description=(
            "Run ``npm install`` before spawning the dev server. Leave "
            "true unless you know dependencies are already up to date "
            "and you want to save 5-30s. Ignored in override mode."
        ),
        json_schema_extra=_HIDDEN,
    )
    wait_seconds: int = Field(
        0,
        ge=0, le=120,
        description=(
            "Override for the bind-wait budget (default 15s). Bump to "
            "30-60s for SSR frameworks with slow first-compile "
            "(Next.js, Remix, Nuxt). 0 keeps the default."
        ),
        json_schema_extra=_HIDDEN,
    )


class DetachParams(BaseModel):
    """Drop the session's proxy attachment.

    Bundled attachments (auto-attached for SDK apps that ship a
    ``web/dist``) are intentionally NOT touched — they survive as
    fallback.
    """
    pass


class PublishParams(BaseModel):
    """Build the project once and serve the static output same-origin.

    Designed for the **cloud / multi-tenant** deploy where a dev server
    per session is too expensive. Pipeline:

      1. ``npm install`` (if requested and ``node_modules`` is missing).
      2. ``npm run build`` (e.g. ``vite build`` → produces ``dist/``).
      3. Copy ``dist/`` to ``~/.digitorn/published/{app_id}/{session_id}/``.
      4. Register a ``published`` attachment; iframe loads from
         ``/api/apps/{app_id}/sessions/{session_id}/published/`` (same
         origin as the parent — no PNA / CORS / mixed-content issues,
         survives daemon restart).

    No HMR — every change requires a re-publish. For interactive
    iteration on a local install where the user has the resources for
    a dev server, prefer ``PreviewProxy``. ``PreviewPublish`` is the
    right tool for cloud previews, demos, and snapshots.
    """

    install: bool = Field(
        True,
        description=(
            "Run ``npm install`` before the build if ``node_modules`` "
            "is missing. Leave true on the first publish; set false on "
            "subsequent ones to save 5-30s when deps haven't changed."
        ),
        json_schema_extra=_HIDDEN,
    )
    build_script: str = Field(
        "build",
        max_length=64,
        description=(
            "Name of the npm script that produces ``dist/`` (or whatever "
            "directory is configured in ``output_dir``). Default ``build`` "
            "covers Vite / CRA / Astro / Next-export. Override for "
            "frameworks with a non-standard script name."
        ),
        json_schema_extra=_HIDDEN,
    )
    output_dir: str = Field(
        "dist",
        max_length=128,
        description=(
            "Directory the build script writes to, relative to the "
            "workspace root. Default ``dist`` (Vite, Astro). Common "
            "alternatives: ``build`` (CRA), ``out`` (Next export)."
        ),
        json_schema_extra=_HIDDEN,
    )
    path: str = Field(
        "",
        max_length=512,
        description=(
            "Optional URL sub-path the iframe should load AFTER the "
            "published base URL. Use when the entry isn't ``index.html`` "
            "at the root. Default empty = root ``index.html``. "
            "Always start with '/' when set."
        ),
        json_schema_extra=_HIDDEN,
    )
    timeout: int = Field(
        300,
        ge=30, le=900,
        description=(
            "Maximum seconds to wait for ``npm install`` + ``npm run build`` "
            "to complete. Default 300s (5 min) covers most React/Vite "
            "projects. Bump to 600+ for large monorepos."
        ),
        json_schema_extra=_HIDDEN,
    )
