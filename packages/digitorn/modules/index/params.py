"""Index module - Pydantic parameter models for all actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

class RegisterSourceParams(BaseModel):
    """Parameters for the register_source action."""

    source_id: str = Field(
        description="Unique identifier for this source (e.g. 'backend-project', 'crm-db').",
    )
    module_id: str = Field(
        description=(
            "Module responsible for reading this source. "
            "For filesystem sources use 'filesystem', for databases use 'database', etc."
        ),
    )
    root: str = Field(
        description="Root path or URI of the source (e.g. '/home/user/project', 'postgres://...').",
    )
    extractor: str = Field(
        default="auto",
        description=(
            "Extractor to use: 'auto' (detect from content), 'text' (simple), "
            "'python' (AST-based), or a custom extractor registered by another module."
        ),
    )
    scan_pattern: str = Field(
        default="**/*",
        description="Glob/filter pattern for scanning (e.g. '**/*.py', 'public.*').",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra config passed to the extractor (e.g. encoding, schema).",
    )
    watch: bool = Field(
        default=False,
        description=(
            "Enable automatic change detection on this source. "
            "When true, the watcher service monitors the source and "
            "automatically re-indexes changed content."
        ),
    )
    watch_mode: str = Field(
        default="ephemeral",
        description=(
            "Watch lifecycle mode: "
            "'ephemeral' - watch stops when the app disconnects. "
            "'persistent' - watch survives daemon restarts."
        ),
        pattern="^(ephemeral|persistent)$",
    )

class RegisterExtractorParams(BaseModel):
    """Parameters for the register_extractor action."""

    name: str = Field(
        description="Unique name for this extractor (e.g. 'sql', 'pdf', 'markdown').",
    )
    module_id: str = Field(
        description="Module that provides the extraction logic.",
    )
    extract_action: str = Field(
        description="Action name on the module that performs extraction.",
    )

class ScanParams(BaseModel):
    """Parameters for the scan action."""

    source_id: str = Field(
        description="Source to scan (must be registered first).",
    )
    force: bool = Field(
        default=False,
        description="Force full rescan even if content hashes haven't changed.",
    )

class QueryParams(BaseModel):
    """Parameters for the query action."""

    q: str = Field(
        description=(
            "Search query - matches against entry names, signatures, and summaries. "
            "Examples: 'calculate_discount', 'users table', 'authentication'."
        ),
    )
    kind: str | None = Field(
        default=None,
        description="Filter by entry kind: 'file', 'function', 'class', 'table', 'import', etc.",
    )
    source_id: str | None = Field(
        default=None,
        description="Filter results to a specific source.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of results to return.",
    )

class RelationsParams(BaseModel):
    """Parameters for the relations action."""

    entry_id: str = Field(
        description="Entry ID to get relations for (from a previous query result).",
    )
    direction: str = Field(
        default="both",
        description="Direction: 'in' (who references me), 'out' (what I reference), 'both'.",
        pattern="^(in|out|both)$",
    )
    kind: str | None = Field(
        default=None,
        description="Filter by relation kind: 'imports', 'calls', 'contains', 'inherits', etc.",
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=5,
        description="Traversal depth in the relation graph.",
    )

class ContextParams(BaseModel):
    """Parameters for the context action - the LLM's primary tool."""

    target: str = Field(
        description=(
            "What to get context for - can be an entry_id, a file path, "
            "or a search query (e.g. 'calculate_discount', '/project/pricing.py')."
        ),
    )
    token_budget: int = Field(
        default=4000,
        ge=100,
        le=100000,
        description="Maximum approximate tokens for the returned context.",
    )
    include_relations: bool = Field(
        default=True,
        description="Include related entries (dependencies, callers) in the context.",
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Relation traversal depth.",
    )

class InvalidateParams(BaseModel):
    """Parameters for the invalidate action."""

    source_id: str | None = Field(
        default=None,
        description="Invalidate all entries from this source.",
    )
    path: str | None = Field(
        default=None,
        description="Invalidate all entries for this specific path.",
    )
