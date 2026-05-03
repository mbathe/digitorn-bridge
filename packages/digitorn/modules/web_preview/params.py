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


class StaticParams(BaseModel):
    """Serve a directory inside the session workspace as static files."""

    path: str = Field(
        "dist",
        min_length=1, max_length=512,
        description=(
            "Workspace-relative path to the directory to serve. Defaults to "
            "'dist'. The directory must exist on disk under the session's "
            "workspace (i.e. workspace.sync_to_disk must be true). Each "
            "request reads from disk live, so re-running 'npm run build' "
            "is reflected on the next page load with no re-attach needed."
        ),
    )
    name: str = Field(
        "default",
        min_length=1, max_length=64,
        description=(
            "Logical name for this attachment, same semantics as in PreviewProxy."
        ),
    )
    index_file: str = Field(
        "index.html",
        description="File served when the request path is '' or '/'.",
        json_schema_extra=_HIDDEN,
    )
    bash_task_id: str | None = Field(
        default=None,
        description=(
            "Optional ``Bash`` task id for a process that owns the "
            "static directory's lifecycle (rare for static — usually "
            "left None). The reaper kills it on idle timeout."
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
