"""Index module — core data types.

All indexed content is normalized into these structures, regardless of source
(filesystem, database, storage, API, etc.).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Source:
    """A registered data source to be indexed.

    Each source is owned by a specific module that knows how to read it
    (e.g. ``filesystem`` for files, ``database`` for SQL schemas).
    """

    source_id: str
    """Unique identifier for this source (e.g. ``"backend-project"``)."""

    module_id: str
    """Module responsible for reading this source (e.g. ``"filesystem"``)."""

    root: str
    """Root path or URI (e.g. ``"/home/user/project"``, ``"postgres://..."``).  """

    extractor: str
    """Name of the extractor to use (e.g. ``"text"``, ``"python"``, ``"sql"``). """

    scan_pattern: str = "*"
    """Glob/filter pattern for scanning (e.g. ``"**/*.py"``).  """

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra config for the extractor (e.g. ``{"encoding": "utf-8"}``).  """

    watch: bool = False
    """Whether this source is actively watched for changes."""

    watch_mode: str = "ephemeral"
    """Watch lifecycle: ``"ephemeral"`` (dies with app) or ``"persistent"`` (survives restarts)."""

    app_id: str | None = None
    """Application that registered this source (for ephemeral cleanup)."""

    last_scan: float | None = None
    """Timestamp of the last successful scan."""

    entry_count: int = 0
    """Number of entries currently indexed from this source."""


@dataclass
class IndexEntry:
    """A single indexed unit — generic across all content types.

    Examples:
      - A Python function: kind="function", name="calculate_discount",
        signature="def calculate_discount(price: float, ...)"
      - A SQL table: kind="table", name="users",
        signature="CREATE TABLE users (id INT, email TEXT, ...)"
      - A PDF section: kind="section", name="Chapter 3",
        signature="3. Legal Obligations"
    """

    entry_id: str
    """Unique ID, typically ``hash(source_id + path + name)``."""

    source_id: str
    """Source this entry belongs to."""

    path: str
    """Location within the source (file path, table name, page ref)."""

    kind: str
    """Type of entry: ``"file"``, ``"function"``, ``"class"``, ``"table"``,
    ``"section"``, ``"variable"``, ``"import"``, etc."""

    name: str
    """Human-readable name (function name, table name, heading, etc.)."""

    signature: str = ""
    """Compact representation (function signature, CREATE TABLE, heading).
    This is what the LLM sees first to decide if it needs more detail."""

    summary: str = ""
    """Brief description (first line of docstring, table comment, etc.)."""

    line_start: int | None = None
    """Start line in the source file (if applicable)."""

    line_end: int | None = None
    """End line in the source file (if applicable)."""

    content_hash: str = ""
    """Hash of the raw content — used for incremental change detection."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extensible metadata: language, schema, tags, visibility, etc."""

    updated_at: float = field(default_factory=time.time)
    """Timestamp of the last update."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "source_id": self.source_id,
            "path": self.path,
            "kind": self.kind,
            "name": self.name,
            "signature": self.signature,
            "summary": self.summary,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": self.metadata,
        }


@dataclass
class Relation:
    """A directed edge between two entries.

    Examples:
      - function A *calls* function B
      - file X *imports* module Y
      - class C *inherits* class D
      - table T *references* table U (foreign key)
    """

    from_id: str
    """Source entry ID."""

    to_id: str
    """Target entry ID."""

    kind: str
    """Relation type: ``"imports"``, ``"calls"``, ``"references"``,
    ``"contains"``, ``"inherits"``, ``"uses"``."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Extra info: line number, qualified name, etc."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_id": self.from_id,
            "to_id": self.to_id,
            "kind": self.kind,
            "metadata": self.metadata,
        }


def make_entry_id(source_id: str, path: str, name: str) -> str:
    """Deterministic entry ID from source + path + name."""
    raw = f"{source_id}:{path}:{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
