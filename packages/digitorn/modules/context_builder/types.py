"""Core types for the context_builder module.

All data structures are plain dataclasses — no ORM, no Pydantic,
no runtime overhead.  Everything is built once at bootstrap and
read-only at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Decision(str, Enum):
    """Action execution policy for a tool."""

    AUTO = "auto"
    APPROVE = "approve"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class IndexedTool:
    """A single tool entry in the index.

    Pre-resolved at bootstrap: the ``module`` ref is a live instance,
    ``policy_decision`` is already computed.  Zero lookups at runtime.
    """

    fqn: str
    module_id: str
    action_name: str
    description: str
    risk_level: str
    tags: list[str]
    params_schema: dict[str, Any]
    examples: list[dict[str, Any]]
    module: Any
    policy_decision: Decision
    aliases: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    irreversible: bool = False
    tool_prompt: str = ""
    output_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    """Summary of a module's visible tools."""

    module_id: str
    summary: str
    tool_count: int
    tool_names: list[str]


@dataclass(frozen=True, slots=True)
class ScoredTool:
    """A search result with relevance score."""

    fqn: str
    description: str
    risk_level: str
    params_summary: str
    relevance: float
    tags: list[str] = field(default_factory=list)


@dataclass
class ToolIndex:
    """Pre-computed inverted index over all visible tools.

    Built once at bootstrap.  All lookups are O(1) dict access.
    """

    tools: dict[str, IndexedTool] = field(default_factory=dict)

    keyword_index: dict[str, set[str]] = field(default_factory=dict)

    keyword_prefixes: dict[str, set[str]] = field(default_factory=dict)

    categories: dict[str, CategoryInfo] = field(default_factory=dict)

    tag_index: dict[str, set[str]] = field(default_factory=dict)

    fqn_index: dict[str, set[str]] = field(default_factory=dict)

    semantic_index: Any = field(default=None, repr=False)

    mcp_structural_hints: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def total_tools(self) -> int:
        return len(self.tools)

    @property
    def total_categories(self) -> int:
        return len(self.categories)
