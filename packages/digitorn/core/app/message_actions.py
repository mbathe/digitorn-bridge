"""`message_actions` block: YAML-driven custom action rows rendered"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MessageActionsMatch(BaseModel):
    """Predicate evaluated against a message to decide if the rule"""

    model_config = {"extra": "forbid"}

    role: str | None = Field(
        default=None,
        description=(
            "Match the message role exactly. Common values: "
            "`user`, `assistant`, `system`. Null = any."
        ),
    )
    tool_used: str | None = Field(
        default=None,
        description=(
            "Match when the message contains a tool call with this "
            "EXACT name (e.g. `WsWrite`). For pattern matching "
            "use `tool_pattern` instead. Null = any."
        ),
    )
    tool_pattern: str | None = Field(
        default=None,
        description=(
            "Regex pattern (re.search semantics) matched against "
            "every tool call name in the message. First match in "
            "the message decides. Null = any."
        ),
    )
    content_regex: str | None = Field(
        default=None,
        description=(
            "Regex pattern (re.search) tested against the message "
            "text. Use sparingly - regex compile fires per render "
            "per rule per message. Null = any."
        ),
    )
    has_tool_calls: bool | None = Field(
        default=None,
        description=(
            "Match only when the message has at least one tool "
            "call (true), or no tool calls at all (false). Null = "
            "ignore the count entirely."
        ),
    )


class MessageActionsRule(BaseModel):
    """One `match → render` rule. The rules array is evaluated"""

    model_config = {"extra": "forbid"}

    match: MessageActionsMatch = Field(
        default_factory=MessageActionsMatch,
        description=(
            "Predicate. Empty match (no fields set) acts as a "
            "catch-all - useful at the bottom of the array."
        ),
    )
    ref: str = Field(
        ...,
        description=(
            "Name of an entry in `ui.widgets.inline`. The widget "
            "tree there is rendered UNDER the matching message "
            "body, with `{{message.*}}` bindings substituted."
        ),
    )


class MessageActionsBlock(BaseModel):
    """Top-level `ui.message_actions` block. Disabled by default"""

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle. When false (default), the dispatcher "
            "is short-circuited and no message actions render. "
            "Useful for staged rollouts: ship the rules but keep "
            "`enabled: false` until QA signs off."
        ),
    )
    rules: list[MessageActionsRule] = Field(
        default_factory=list,
        description=(
            "Ordered rules. First match wins. An empty array "
            "means the dispatcher runs but never matches - same "
            "as `enabled: false` for end users."
        ),
    )
    fallback_on_error: bool = Field(
        default=True,
        description=(
            "When the matched widget throws, swallow the error "
            "and render nothing under the message. Set false "
            "during local renderer dev to surface the failure."
        ),
    )
