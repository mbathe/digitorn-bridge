"""Flow block - declarative orchestration graph for multi-agent apps.

A flow describes how nodes (agents, tools, decisions, gates, ...) chain
together: who runs first, what comes next, what happens on success vs
error, when to fan-out and when to gate on a human.

The flow lives at the top level under ``flow:`` and is OPTIONAL. When
present, the runtime drives the app along the explicit graph instead of
relying on the agents' system prompts to coordinate themselves.

The schema is enforced at compile time via Pydantic. Cross-references
(node ids, agent ids, tool names, reachability, cycles) are validated
in :func:`validate_flow_references`.

Public API::

    from digitorn.core.app.flow import FlowConfig, validate_flow_references

The compiler integrates this module via two hooks:

  1. ``AppDefinition.flow: FlowConfig | None`` (Pydantic field).
  2. ``_compile_body`` calls ``validate_flow_references`` after the
     dependency graph validation pass.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


# ─── Routes ─────────────────────────────────────────────────────


class FlowRoute(BaseModel):
    """A directed edge from the current node to ``to`` under condition ``when``.

    ``when`` is either a literal expression (``"input.kind == 'refund'"``)
    or the sentinel ``"default"`` meaning "match if no other route matched
    first". Routes are evaluated top-to-bottom; the first matching route
    wins.

    The expression syntax is intentionally NOT validated here - that
    happens in the runtime's expression engine. The schema only checks
    that ``when`` is a non-empty string and ``to`` references something."""

    model_config = {"extra": "forbid"}

    when: str = Field(
        default="default",
        description="Condition expression or 'default'. First match wins.",
    )
    to: str = Field(
        ...,
        description="Target node id, or the literal sentinel 'end'.",
    )


class FlowOnErrorRoute(BaseModel):
    """Error-handling edge. Matched when the source node raises.

    Either ``match`` (regex on the error type/message) plus ``to``, or
    ``default: True`` plus ``to`` for the catch-all branch. Listed in
    order; first match wins. ``default`` must be the last entry."""

    model_config = {"extra": "forbid"}

    match: str | None = Field(
        default=None,
        description="Regex matched against the runtime error type or message.",
    )
    default: bool = Field(
        default=False,
        description="Catch-all clause. Must come last when present.",
    )
    to: str = Field(
        ...,
        description="Target node id when this clause matches.",
    )


# ─── Join policy for parallel ───────────────────────────────────


class FlowJoin(BaseModel):
    """Join policy for parallel fan-outs.

    - ``all`` (default): wait for every branch to complete.
    - ``any``: continue as soon as one branch returns.
    - ``first``: same as ``any``, alias for clarity.
    - ``count``: wait for exactly ``count`` branches.

    ``timeout`` is the per-join wall-clock cap in seconds. Any branch
    still running when it elapses is cancelled and treated as failed."""

    model_config = {"extra": "forbid"}

    type: Literal["all", "any", "first", "count"] = Field(
        default="all",
        description="How many branches must complete before joining.",
    )
    count: int = Field(
        default=0,
        ge=0,
        description="Required only when type='count'. Number of branches to wait for.",
    )
    timeout: float = Field(
        default=60.0,
        gt=0,
        description="Per-join wall-clock cap in seconds.",
    )

    @model_validator(mode="after")
    def _check_count_for_count_type(self) -> "FlowJoin":
        if self.type == "count" and self.count < 1:
            raise ValueError(
                "join.type='count' requires join.count >= 1"
            )
        return self


# ─── Node base + variants ───────────────────────────────────────


class _BaseNode(BaseModel):
    """Common fields shared by every node type.

    Subclasses add a ``type`` literal and the type-specific fields. Each
    subclass keeps ``extra: forbid`` so that bogus fields surface
    immediately as schema errors with the right type's allowed-fields
    list in the message."""

    id: str = Field(..., description="Unique node identifier within the flow.")
    description: str = Field(
        default="",
        description="Free-form description, surfaced on the canvas tooltip.",
    )
    routes: list[FlowRoute] = Field(
        default_factory=list,
        description="Outgoing edges. Top-to-bottom evaluation order.",
    )
    on_error: list[FlowOnErrorRoute] = Field(
        default_factory=list,
        description="Error-handling edges. Catch-all (default: true) must be last.",
    )

    @model_validator(mode="after")
    def _check_default_on_error_last(self) -> "_BaseNode":
        seen_default = False
        for r in self.on_error:
            if seen_default:
                raise ValueError(
                    f"on_error: a clause appears AFTER the default catch-all "
                    f"on node '{self.id}'. Move the default clause to the end."
                )
            if r.default:
                seen_default = True
        return self


class AgentNode(_BaseNode):
    """Run an existing declared agent for one turn.

    The agent is identified by the ``agent`` field which must reference
    a declared ``agents[].id`` (validated in
    :func:`validate_flow_references`)."""

    model_config = {"extra": "forbid"}

    type: Literal["agent"]
    agent: str = Field(..., description="agents[].id to execute.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Static or templated input passed to the agent.",
    )


class ToolNode(_BaseNode):
    """Direct tool invocation, no LLM in the loop.

    The ``tool`` field must be a ``module.action`` FQN that resolves to
    a real action of a declared module."""

    model_config = {"extra": "forbid"}

    type: Literal["tool"]
    tool: str = Field(..., description="Tool FQN, e.g. 'web.search' or 'http.post'.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters passed to the tool. Supports {{templates}}.",
    )


class ParallelNode(_BaseNode):
    """Fan-out into N parallel branches, join, then continue.

    Each branch is a ``FlowRoute`` whose ``to`` points to a node that
    runs concurrently with its siblings. The ``join`` field specifies
    how many branches must complete before the flow continues via the
    parent's ``routes``."""

    model_config = {"extra": "forbid"}

    type: Literal["parallel"]
    branches: list[FlowRoute] = Field(
        ...,
        min_length=2,
        description="Concurrent branches (>= 2 entries).",
    )
    join: FlowJoin = Field(
        default_factory=FlowJoin,
        description="Join policy after the branches complete.",
    )


class ApprovalNode(_BaseNode):
    """Human-in-the-loop gate. Pauses until a human chooses an option.

    The decision becomes part of the flow context as
    ``approvals.<node_id>`` so downstream routes can branch on it via
    ``when: "approvals.<id> == 'approve'"``."""

    model_config = {"extra": "forbid"}

    type: Literal["approval"]
    message: str = Field(..., min_length=1, description="Question shown to the user.")
    choices: list[str] = Field(
        default_factory=lambda: ["approve", "reject"],
        min_length=2,
        description="Selectable answers. The user picks one.",
    )


class DecisionNode(_BaseNode):
    """Pure routing decision - no LLM, no tool, just an expression.

    ``expr`` is evaluated against the flow context. The result is
    matched against ``routes[].when`` clauses to pick the next hop."""

    model_config = {"extra": "forbid"}

    type: Literal["decision"]
    expr: str = Field(..., min_length=1, description="Expression that drives routing.")


class TerminalNode(_BaseNode):
    """End of a flow path. Carries an optional output payload.

    Terminal nodes typically have empty ``routes`` (the flow stops here).
    If they do declare routes, they're treated as a sub-flow continuation
    point useful for subflow node returns - but the runtime treats the
    path as ended for the caller's purposes."""

    model_config = {"extra": "forbid"}

    type: Literal["terminal"]
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Final output payload returned by this flow path.",
    )


FlowNode = Annotated[
    Union[
        AgentNode,
        ToolNode,
        ParallelNode,
        ApprovalNode,
        DecisionNode,
        TerminalNode,
    ],
    Field(discriminator="type"),
]


# ─── Top-level flow config ──────────────────────────────────────


class FlowConfig(BaseModel):
    """Declarative orchestration graph for a Digitorn app.

    Example::

        flow:
          id: support_main
          entry: triage
          max_iterations: 100
          nodes:
            - id: triage
              type: agent
              agent: triage
              routes:
                - { when: "category == 'refund'", to: refund }
                - { when: "default", to: end }
            - id: refund
              type: agent
              agent: refund_specialist
              routes:
                - { to: gate }
            - id: gate
              type: approval
              message: "Confirm refund?"
              routes:
                - { when: "approvals.gate == 'approve'", to: end }
                - { when: "default", to: end }
    """

    model_config = {"extra": "forbid"}

    id: str = Field(..., min_length=1, description="Flow identifier (unique within the app).")
    entry: str = Field(..., min_length=1, description="Starting node id.")
    description: str = Field(default="", description="Free-form summary of the flow.")
    max_iterations: int = Field(
        default=0,
        ge=0,
        description=(
            "Per-flow cap on total node visits. 0 = no cap (only valid for "
            "acyclic flows). Required (>= 1) when the graph has any cycle "
            "to prevent infinite loops at runtime."
        ),
    )
    nodes: list[FlowNode] = Field(
        ...,
        min_length=1,
        description="Nodes that compose the graph.",
    )


# ─── Cross-reference validator (called from compiler) ───────────


_END_SENTINEL = "end"


def _node_outgoing_targets(node: Any) -> list[str]:
    """Collect every outgoing target id from a node (routes + branches +
    on_error). Used for reachability and cycle detection."""
    out: list[str] = [r.to for r in (getattr(node, "routes", []) or [])]
    if getattr(node, "type", "") == "parallel":
        out.extend(r.to for r in (getattr(node, "branches", []) or []))
    out.extend(r.to for r in (getattr(node, "on_error", []) or []))
    return out


def validate_flow_references(
    flow: FlowConfig,
    *,
    declared_agents: set[str],
    known_tools: set[str],
    errors: list[str],
) -> None:
    """Compile-time cross-reference check on a parsed flow.

    Errors are appended to ``errors`` rather than raised so the compiler
    can collect every problem in a single pass.

    Checks performed:

      1. Node ids unique within the flow.
      2. Entry references a declared node.
      3. Every route target ('routes', 'branches', 'on_error') resolves
         to a declared node id or the literal 'end'.
      4. Every ``agent`` node references a declared agent.
      5. Every ``tool`` node references a known tool FQN.
      6. Every non-entry node is reachable from entry.
      7. Cycles require ``flow.max_iterations >= 1``.
    """
    if not flow.nodes:
        errors.append("flow.nodes: at least one node is required.")
        return

    seen: dict[str, Any] = {}
    for n in flow.nodes:
        nid = n.id
        # 'end' is the reserved sentinel meaning 'terminate the flow'.
        # Allowing a node to take that id would collide with route
        # targets (`to: end`) and with the reachability walker which
        # treats the sentinel as a no-op terminal.
        if nid == _END_SENTINEL:
            errors.append(
                f"flow.nodes: node id '{nid}' is reserved as the flow "
                f"termination sentinel. Pick another id (e.g. 'finish', "
                f"'done', 'the_end')."
            )
            continue
        if nid in seen:
            errors.append(
                f"flow.nodes: duplicate node id '{nid}'. Ids must be unique within the flow."
            )
            continue
        seen[nid] = n

    if flow.entry not in seen:
        errors.append(
            f"flow.entry: '{flow.entry}' is not a declared node id. "
            f"Available nodes: {sorted(seen.keys())}."
        )

    def _check_target(target: str, ctx: str) -> None:
        if target == _END_SENTINEL:
            return
        if target not in seen:
            import difflib as _df
            sug = _df.get_close_matches(target, seen.keys(), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
            errors.append(
                f"{ctx}: route target '{target}' does not resolve to any declared "
                f"node. Use a node id or the sentinel 'end'.{hint}"
            )

    for nid, n in seen.items():
        for i, r in enumerate(getattr(n, "routes", []) or []):
            _check_target(r.to, f"flow.nodes[{nid}].routes[{i}].to")
        if getattr(n, "type", "") == "parallel":
            for i, r in enumerate(getattr(n, "branches", []) or []):
                _check_target(r.to, f"flow.nodes[{nid}].branches[{i}].to")
        for i, r in enumerate(getattr(n, "on_error", []) or []):
            _check_target(r.to, f"flow.nodes[{nid}].on_error[{i}].to")

    for nid, n in seen.items():
        ntype = getattr(n, "type", "")
        if ntype == "agent":
            target_agent = getattr(n, "agent", "")
            if target_agent and target_agent not in declared_agents:
                import difflib as _df
                sug = _df.get_close_matches(target_agent, declared_agents, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"flow.nodes[{nid}].agent: '{target_agent}' is not a declared "
                    f"agent. Declared: {sorted(declared_agents)}.{hint}"
                )
        elif ntype == "tool":
            tool = getattr(n, "tool", "")
            if tool and known_tools and tool not in known_tools:
                import difflib as _df
                sug = _df.get_close_matches(tool, known_tools, n=3, cutoff=0.6)
                hint = f" Did you mean: {', '.join(sug)}?" if sug else ""
                errors.append(
                    f"flow.nodes[{nid}].tool: '{tool}' is not a known tool of "
                    f"this app. Modules and actions must be declared.{hint}"
                )

    if flow.entry in seen:
        reachable: set[str] = set()
        stack = [flow.entry]
        while stack:
            cur = stack.pop()
            if cur in reachable or cur == _END_SENTINEL:
                continue
            reachable.add(cur)
            cur_node = seen.get(cur)
            if cur_node is None:
                continue
            for t in _node_outgoing_targets(cur_node):
                if t != _END_SENTINEL and t in seen and t not in reachable:
                    stack.append(t)
        for nid in seen:
            if nid not in reachable:
                errors.append(
                    f"flow.nodes[{nid}]: orphan node, not reachable from entry "
                    f"'{flow.entry}'. Either connect it via a route or remove it."
                )

    if flow.max_iterations == 0:
        if _has_cycle(seen):
            errors.append(
                "flow: graph contains a cycle but flow.max_iterations is unset "
                "(== 0). Set flow.max_iterations to a positive cap to allow "
                "the cycle, or break the loop."
            )


def _has_cycle(nodes: dict[str, Any]) -> bool:
    """DFS-based cycle detection over the node graph (routes + branches)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {nid: WHITE for nid in nodes}

    def visit(nid: str) -> bool:
        if nid == _END_SENTINEL or nid not in nodes:
            return False
        if color[nid] == GRAY:
            return True
        if color[nid] == BLACK:
            return False
        color[nid] = GRAY
        for t in _node_outgoing_targets(nodes[nid]):
            if visit(t):
                return True
        color[nid] = BLACK
        return False

    return any(visit(nid) for nid in nodes)
