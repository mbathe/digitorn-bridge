"""Phase-3 ``tool_renderers`` block - YAML-driven custom rendering for
tool call entries in the chat timeline.

This module is **isolated from the rest of the schema**:

* Daemon-side: the ``ToolRenderersBlock`` class lives here, never edits
  any pre-existing Pydantic model. ``UIBlock`` references it as a
  forward-string type so a missing import doesn't break ``schema.py``.

* Client-side: tool calls keep being rendered by the legacy code path
  whenever this block is absent or ``enabled: false``. Opt-in is
  binary - presence + truthy ``enabled`` flips the dispatcher to look
  up a custom renderer first and fall back to the legacy chip when no
  match is found.

Rollback path: delete this file, drop the ``tool_renderers`` field
from ``UIBlock``, remove the single ``<ToolRenderer>`` call in each
client's MessageBubble. No other surface is touched.

Bindings exposed to the renderer's widget tree (via the v1 widgets
template engine, ``{{tool.X}}``):

* ``tool.name``        — short tool name (``WsRead``, ``Bash``, …)
* ``tool.params``      — full param map (``tool.params.path`` etc.)
* ``tool.params.X``    — individual param fields
* ``tool.result``      — full result payload
* ``tool.result.X``    — individual result fields
* ``tool.error``       — error string (when status==error)
* ``tool.duration_ms`` — execution duration in milliseconds
* ``tool.status``      — ``running`` | ``success`` | ``error``
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolRendererEntry(BaseModel):
    """A single ``tool_renderers`` mapping entry.

    Two ways to bind the renderer to a tool:

    * ``ref``: name of an entry in ``ui.widgets.inline``. The widget
      tree there is rendered with the ``tool.*`` bindings injected.

    Future fields (Phase 3.1):

    * ``inline_tree``: literal widget tree inline at this site
      instead of pointing at ``ui.widgets.inline``. Convenient when
      the tree is small and doesn't need reuse.
    """

    model_config = {"extra": "forbid"}

    ref: str = Field(
        ...,
        description=(
            "Name of an entry in ``ui.widgets.inline`` to render for "
            "this tool. The widget tree receives ``{{tool.*}}`` "
            "bindings (name, params, result, error, duration_ms, "
            "status) at render time."
        ),
    )


class ToolRenderersBlock(BaseModel):
    """Top-level ``ui.tool_renderers`` block.

    Maps tool names (and optional regex patterns) to inline-widget
    refs. The dispatcher in each client checks ``by_name`` first
    (exact match, fastest), then ``by_pattern`` (re.search), then
    falls back to the legacy tool chip when neither matches.

    Disabled by default - the block being PRESENT and ``enabled``
    being truthy is what flips the dispatcher to consult these
    mappings. Apps without the block keep their historical chip
    rendering with zero behaviour change.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle. When false (default), every tool call "
            "renders with the legacy chip and ``by_name`` / "
            "``by_pattern`` are ignored. Flipping the flag turns the "
            "client dispatcher on - good for staged rollout: ship a "
            "renderer config but keep ``enabled: false`` until QA "
            "signs off."
        ),
    )
    by_name: dict[str, ToolRendererEntry] = Field(
        default_factory=dict,
        description=(
            "Exact-match map from tool name (short, e.g. ``WsRead``) "
            "to a renderer entry. Checked first; an exact hit short-"
            "circuits the pattern lookup."
        ),
    )
    by_pattern: dict[str, ToolRendererEntry] = Field(
        default_factory=dict,
        description=(
            "Regex-match map. Each key is a Python re.search-style "
            "pattern (``^memory\\..+`` etc.) tested against the tool "
            "name in iteration order. The first match wins. Use this "
            "for grouping (``filesystem.*`` → one renderer for all "
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
