"""Shell module - one bash action."""

from __future__ import annotations

from pydantic import BaseModel, Field

_HIDDEN = {"hidden": True}

class BashParams(BaseModel):
    """Execute a shell command (sync or async)."""

    command: str | None = Field(
        None,
        description=(
            "Shell command to execute. Use && to chain commands, ; for independent commands.\n"
            "Not needed if checking status via task_id."
        )
    )
    description: str = Field(
        "",
        description="Short label shown in the UI (e.g. 'Running tests')."
    )
    run_in_background: bool = Field(
        False,
        description="Run in background (async). Returns task_id immediately. Useful for long-running tasks."
    )

    # Status/management params (hidden)
    task_id: str | None = Field(
        None,
        json_schema_extra=_HIDDEN,
        description="Task ID from previous run_in_background=true. Omit command to check status."
    )
    kill: bool = Field(
        False,
        json_schema_extra=_HIDDEN,
        description="Set true to kill a running task (requires task_id)."
    )
    stdin_text: str | None = Field(
        None,
        json_schema_extra=_HIDDEN,
        description="Text to send to running task's stdin (requires task_id). Newline appended automatically."
    )
    keep_stdin_open: bool = Field(
        False,
        json_schema_extra=_HIDDEN,
        description=(
            "Keep stdin open on a background task so future stdin_text "
            "calls can send input. Default false: stdin is closed after "
            "spawn (sends EOF to the child) so prompt-driven CLIs "
            "(npm install confirm, apt 'continue?', git password) abort "
            "instead of hanging."
        ),
    )
    wait: bool = Field(
        False,
        json_schema_extra=_HIDDEN,
        description="Block until background task completes (requires task_id). Returns final result.",
    )
    stream: bool = Field(
        False,
        json_schema_extra=_HIDDEN,
        description="Stream output with line-by-line notifications (like Monitor tool).",
    )
    stream_pattern: str | None = Field(
        None,
        json_schema_extra=_HIDDEN,
        description="Regex pattern to filter streamed lines. Only matching lines trigger notifications.",
    )

    # Execution params (hidden)
    timeout: float = Field(
        300.0,
        ge=1.0,
        le=1800.0,
        json_schema_extra=_HIDDEN,
        description="Max time to wait (seconds)"
    )
    cwd: str = Field(
        ".",
        json_schema_extra=_HIDDEN,
        description="Working directory"
    )
