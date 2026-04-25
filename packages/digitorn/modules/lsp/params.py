"""LSP module — parameter models. Hidden params reduce LLM confusion."""

from __future__ import annotations

from pydantic import BaseModel, Field

_HIDDEN = {"hidden": True}


class DiagnosticsParams(BaseModel):
    """Get diagnostics (errors, warnings) for a file."""
    path: str | None = Field(default=None, description="File path. Omit = check all.")
    fix: bool = Field(default=False, json_schema_extra=_HIDDEN)


class CheckParams(BaseModel):
    """Quick pass/fail check for a file."""
    path: str = Field(..., description="File path to check.")


class NotifyChangeParams(BaseModel):
    """Notify that a file changed (triggers fresh diagnostics)."""
    path: str = Field(..., description="Path of the changed file.")
    content: str | None = Field(default=None, json_schema_extra=_HIDDEN)


class LspRequestParams(BaseModel):
    """Forward a raw LSP request (hover / goto / references / completion /
    rename / signatureHelp / documentSymbol / …) to the language server
    backing a given file. The server is chosen by the file's extension
    via the same routing as ``notify_change``.

    ``method`` + ``params`` follow the Language Server Protocol spec
    verbatim — Digitorn doesn't reshape payloads; the LSP spec IS the
    contract between client and server.
    """
    path: str = Field(
        ...,
        description=(
            "Absolute or workspace-relative file path. Used to pick the "
            "language server via the registered extension map."
        ),
    )
    method: str = Field(
        ...,
        description=(
            "LSP method name, e.g. 'textDocument/hover', "
            "'textDocument/definition', 'textDocument/completion'."
        ),
    )
    params: dict = Field(
        default_factory=dict,
        description=(
            "Raw LSP request params per the spec. For hover/goto: "
            "{textDocument: {uri}, position: {line, character}}. "
            "We auto-fill ``textDocument.uri`` from ``path`` if omitted."
        ),
    )
    timeout_seconds: float = Field(
        default=10.0,
        ge=0.1,
        le=60.0,
        json_schema_extra=_HIDDEN,
        description="Per-request timeout in seconds.",
    )
    request_id: str | None = Field(
        default=None,
        description=(
            "Client-supplied correlation id. Use it to cancel the request "
            "later via ``POST /lsp/cancel``. When omitted, the daemon "
            "generates one for the response."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Session the request belongs to — used as a prefix for "
            "supersession + cancellation lookups. Typically forwarded "
            "by the REST endpoint."
        ),
        json_schema_extra=_HIDDEN,
    )
    supersede_previous: bool = Field(
        default=True,
        description=(
            "When True, a new request for the same (session, path, method) "
            "triple cancels any in-flight request for that triple. Right "
            "default for keystroke-driven methods (completion, hover, "
            "signatureHelp). Set False for methods whose result the "
            "client always wants delivered (references, rename, "
            "documentSymbol)."
        ),
    )


class LspCancelParams(BaseModel):
    """Cancel an in-flight LSP request by ``request_id``."""
    request_id: str = Field(
        ..., description="Correlation id previously returned by /lsp/request.",
    )
    session_id: str | None = Field(
        default=None,
        description="Session scope. Optional — defaults to global lookup.",
        json_schema_extra=_HIDDEN,
    )
