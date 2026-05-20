"""Flow block - declarative orchestration graph for multi-agent apps."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator


class FlowRoute(BaseModel):
    """A directed edge from the current node to `to` under condition `when`."""

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
    """Error-handling edge. Matched when the source node raises."""

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


class FlowJoin(BaseModel):
    """Join policy for parallel fan-outs."""

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


class _BaseNode(BaseModel):
    """Common fields shared by every node type."""

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
    """Run an existing declared agent for one turn."""

    model_config = {"extra": "forbid"}

    type: Literal["agent"]
    agent: str = Field(..., description="agents[].id to execute.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Static or templated input passed to the agent.",
    )


class ToolNode(_BaseNode):
    """Direct tool invocation, no LLM in the loop."""

    model_config = {"extra": "forbid"}

    type: Literal["tool"]
    tool: str = Field(..., description="Tool FQN, e.g. 'web.search' or 'http.post'.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters passed to the tool. Supports {{templates}}.",
    )


class ParallelNode(_BaseNode):
    """Fan-out into N parallel branches, join, then continue."""

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
    """Human-in-the-loop gate. Pauses until a human chooses an option."""

    model_config = {"extra": "forbid"}

    type: Literal["approval"]
    message: str = Field(..., min_length=1, description="Question shown to the user.")
    choices: list[str] = Field(
        default_factory=lambda: ["approve", "reject"],
        min_length=2,
        description="Selectable answers. The user picks one.",
    )


class DecisionNode(_BaseNode):
    """Pure routing decision - no LLM, no tool, just an expression."""

    model_config = {"extra": "forbid"}

    type: Literal["decision"]
    expr: str = Field(..., min_length=1, description="Expression that drives routing.")


class TerminalNode(_BaseNode):
    """End of a flow path. Carries an optional output payload."""

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


class FlowConfig(BaseModel):
    """Declarative orchestration graph for a Digitorn app."""

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


_END_SENTINEL = "end"


def _node_outgoing_targets(node: Any) -> list[str]:
    """Collect every outgoing target id from a node (routes + branches +"""
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
    """Compile-time cross-reference check on a parsed flow."""
    if not flow.nodes:
        errors.append("flow.nodes: at least one node is required.")
        return

    seen: dict[str, Any] = {}
    for n in flow.nodes:
        nid = n.id
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
