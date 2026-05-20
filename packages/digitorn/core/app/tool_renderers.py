"""Phase-3 `tool_renderers` block - YAML-driven custom rendering for"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolRendererEntry(BaseModel):
    """A single `tool_renderers` mapping entry."""

    model_config = {"extra": "forbid"}

    ref: str = Field(
        ...,
        description=(
            "Name of an entry in `ui.widgets.inline` to render for "
            "this tool. The widget tree receives `{{tool.*}}` "
            "bindings (name, params, result, error, duration_ms, "
            "status) at render time."
        ),
    )


class ToolRenderersBlock(BaseModel):
    """Top-level `ui.tool_renderers` block."""

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle. When false (default), every tool call "
            "renders with the legacy chip and `by_name` / "
            "`by_pattern` are ignored. Flipping the flag turns the "
            "client dispatcher on - good for staged rollout: ship a "
            "renderer config but keep `enabled: false` until QA "
            "signs off."
        ),
    )
    by_name: dict[str, ToolRendererEntry] = Field(
        default_factory=dict,
        description=(
            "Exact-match map from tool name (short, e.g. `WsRead`) "
            "to a renderer entry. Checked first; an exact hit short-"
            "circuits the pattern lookup."
        ),
    )
    by_pattern: dict[str, ToolRendererEntry] = Field(
        default_factory=dict,
        description=(
            "Regex-match map. Each key is a Python re.search-style "
            "pattern (`^memory\\..+` etc.) tested against the tool "
            "name in iteration order. The first match wins. Use this "
            "for grouping (`filesystem.*` → one renderer for all "
            "filesystem tools) without listing each tool by name."
        ),
    )
    fallback_on_error: bool = Field(
        default=True,
        description=(
            "When the matched renderer throws / fails to mount, fall "
            "back to the legacy chip instead of showing a broken "
            "widget. Set false during local renderer dev to surface "
            "the failure inline."
        ),
    )
