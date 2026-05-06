"""Phase-2 ``message_actions`` block - YAML-driven custom action
rows rendered UNDER each chat message that matches a rule.

Architecturally identical to ``tool_renderers.py`` (Phase 3): a
separate module with one forward-string field on ``UIBlock``, opt-in
via ``enabled: true``, fallback to no-op when no rule matches. The
client mounts the inline widget tree referenced by the rule under
the message body, with ``{{message.*}}`` template bindings injected.

Rule matching is FIRST-MATCH-WINS in YAML iteration order. Each
rule carries a ``match:`` predicate (any combination of role,
tool, content_regex, has_tool_calls) and a ``ref:`` pointing at an
entry in ``ui.widgets.inline``. The widget tree there is rendered
with these bindings:

* ``message.role``         — ``user`` | ``assistant`` | ``system``
* ``message.id``           — message id (stable across reruns)
* ``message.text``         — concatenated text content
* ``message.has_tools``    — bool, true when tool calls present
* ``message.tools``        — list of tool names called by this msg
* ``message.first_tool``   — first tool name (convenience)
* ``message.tool_status``  — ``running`` | ``success`` | ``error``
                             (worst-case across all tools)

Rollback: delete this file + drop the ``message_actions`` field from
``UIBlock``. Each client has a single call site in MessageBubble
that returns null when the block is absent — so deleting the file
on the daemon ALONE leaves the clients harmlessly inert.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MessageActionsMatch(BaseModel):
    """Predicate evaluated against a message to decide if the rule
    fires. Every field is optional and ANDed - rules with no fields
    set always match.

    Match criteria are intentionally narrow to keep the dispatcher
    fast (each message walks the rules array on every render). Use
    ``content_regex`` sparingly - regex compile + test on every
    message can show up in profiles for chat panes with thousands
    of messages.
    """

    model_config = {"extra": "forbid"}

    role: str | None = Field(
        default=None,
        description=(
            "Match the message role exactly. Common values: "
            "``user``, ``assistant``, ``system``. Null = any."
        ),
    )
    tool_used: str | None = Field(
        default=None,
        description=(
            "Match when the message contains a tool call with this "
            "EXACT name (e.g. ``WsWrite``). For pattern matching "
            "use ``tool_pattern`` instead. Null = any."
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
    """One ``match → render`` rule. The rules array is evaluated
    top-to-bottom and the FIRST matching rule wins - put the most
    specific rules first.
    """

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
            "Name of an entry in ``ui.widgets.inline``. The widget "
            "tree there is rendered UNDER the matching message "
            "body, with ``{{message.*}}`` bindings substituted."
        ),
    )


class MessageActionsBlock(BaseModel):
    """Top-level ``ui.message_actions`` block. Disabled by default
    so apps without the block keep their historical "no extra row
    under messages" behaviour with zero behaviour change.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = Field(
        default=False,
        description=(
            "Master toggle. When false (default), the dispatcher "
            "is short-circuited and no message actions render. "
            "Useful for staged rollouts: ship the rules but keep "
            "``enabled: false`` until QA signs off."
        ),
    )
    rules: list[MessageActionsRule] = Field(
        default_factory=list,
        description=(
            "Ordered rules. First match wins. An empty array "
            "means the dispatcher runs but never matches - same "
            "as ``enabled: false`` for end users."
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
