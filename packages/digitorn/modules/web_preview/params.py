"""Parameter models for ``web_preview`` actions.

Each model is the source of truth for the JSON schema the LLM sees.
Hidden fields (``json_schema_extra={"hidden": True}``) are stripped
from the schema before it's sent to the model — kept here only so
power-users / API callers can still pass them.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

_HIDDEN = {"hidden": True}


class ProxyParams(BaseModel):
    """Attach the iframe preview to a running dev server."""

    port: int = Field(
        ...,
        ge=1, le=65535,
        description=(
            "TCP port the dev server is listening on. The LLM is responsible "
            "for spawning the server first (typically via "
            "Bash(run_in_background=true)) and waiting until it binds."
        ),
    )
    name: str = Field(
        "default",
        min_length=1, max_length=64,
        description=(
            "Logical name for this attachment — used when an app exposes "
            "multiple previews in parallel (e.g. 'frontend' + 'backend'). "
            "If omitted, replaces the existing 'default' attachment."
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
            "Try a quick HTTP HEAD before registering. Logs a warning if the "
            "server doesn't answer but registers anyway — the LLM may know "
            "better (e.g. server bound but not yet serving the root path)."
        ),
        json_schema_extra=_HIDDEN,
    )
    bash_task_id: str | None = Field(
        default=None,
        description=(
            "If you spawned the dev server via "
            "``Bash(command=..., run_in_background=true)`` you got a "
            "``task_id`` back. Pass it here so the daemon can "
            "auto-kill the process when the attachment is reaped due "
            "to inactivity (no HTTP traffic for 30 minutes), avoiding "
            "leaked dev servers."
        ),
    )
    path: str = Field(
        "",
        max_length=512,
        description=(
            "Optional URL path the iframe should load AFTER host:port. "
            "Use when the entry point isn't the server root '/'. "
            "Examples: '/landing.html' for a single-file static page "
            "served by python http.server, '/admin' for a sub-route. "
            "Default ''/empty serves the root - which works for dev "
            "servers that have an index.html (Vite/Next/CRA always do). "
            "ALWAYS start with '/' when set."
        ),
    )
    wait_seconds: int = Field(
        0,
        ge=0, le=120,
        description=(
            "Optional override for the bind-wait budget (default 15s). "
            "Bump this to 30-60s for SSR frameworks whose first-compile "
            "is slow (Next.js dev with type-checking, Remix, Nuxt with "
            "many dependencies). 0 keeps the default."
        ),
        json_schema_extra=_HIDDEN,
    )


class DetachParams(BaseModel):
    """Drop a previously-registered attachment."""

    name: str = Field(
        "default",
        min_length=1, max_length=64,
        description="Name of the attachment to remove. Defaults to 'default'.",
    )


class ListParams(BaseModel):
    """List active attachments for the current session."""
    pass
