"""Meta-tool actions mixin for ContextBuilderModule."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from digitorn.modules.base import ActionResult
from digitorn.modules.context_builder.params import (
    AskUserParams,
    BrowseCategoryParams,
    CallAppParams,
    ExecuteToolParams,
    GetToolParams,
    ListCategoriesParams,
    RunParallelParams,
    SearchToolsParams,
    UseSkillParams,
)
from digitorn.modules.context_builder.scoring import search
from digitorn.modules.context_builder.types import Decision
from digitorn.modules.decorators import action

logger = logging.getLogger(__name__)

_PAGE_SIZE = 20

def _format_ask_user_response(params: Any, raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    has_choices = bool(getattr(params, "choices", None))
    allow_multi = bool(getattr(params, "allow_multiple", False))
    has_form = bool(getattr(params, "form", None))

    if has_form:
        try:
            import json as _json
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                lines = [f"  - {k}: {v}" for k, v in parsed.items()]
                return "User submitted form:\n" + "\n".join(lines)
        except (ValueError, TypeError):
            pass

    if has_choices and allow_multi:
        items = [c.strip() for c in raw.split(",") if c.strip()]
        if len(items) > 1:
            return "User selected: " + ", ".join(items)

    return raw

def _levenshtein(s: str, t: str) -> int:
    if len(s) < len(t):
        s, t = t, s
    if not t:
        return len(s)
    prev = list(range(len(t) + 1))
    curr = [0] * (len(t) + 1)
    for i in range(len(s)):
        curr[0] = i + 1
        for j in range(len(t)):
            cost = 0 if s[i] == t[j] else 1
            curr[j + 1] = min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost)
        prev, curr = curr, prev
    return prev[-1]

class MetaToolsMixin:
    """Meta-tools for tool discovery + execution + skills + app composition."""

    @action(
        description=(
            "Search for tools by keyword or description. "
            "Returns matching tools with full parameter schemas "
            "so you can call ExecuteTool directly."
        ),
        tool_prompt=(
            "Search for tools when you need a capability not in your current tool list.\n"
            "\n"
            "## How to use\n"
            "1. SearchTools(query='what you need') - find relevant tools\n"
            "2. The result includes the full parameter schema for each tool\n"
            "3. Call ExecuteTool(name='module.action', params={...}) to use it\n"
            "\n"
            "## Tips\n"
            "- Use natural language: 'send email', 'create chart', 'query database'\n"
            "- Results are ranked by relevance\n"
            "- The parameters field shows exactly what params to pass to ExecuteTool"
        ),
        params_model=SearchToolsParams,
        risk_level="low",
        tags=["discovery", "search"],
        cli_label='Search tools',
        cli_param='query',
    )
    async def search_tools(self, params: SearchToolsParams) -> ActionResult:
        results = search(
            self._index, params.query, params.max_results,
            usage_counts=self._usage_counts,
        )
        tools_data = []
        for r in results:
            tool_entry = {
                "name": r.fqn,
                "description": r.description,
                "risk_level": r.risk_level,
                "params_summary": r.params_summary,
                "relevance": r.relevance,
                "tags": r.tags,
            }
            indexed = self._index.tools.get(r.fqn)
            if indexed and hasattr(indexed, "params_schema"):
                tool_entry["parameters"] = indexed.params_schema
            tools_data.append(tool_entry)

        return ActionResult(
            success=True,
            data={
                "tools": tools_data,
                "count": len(tools_data),
                "query": params.query,
            },
        )

    @action(
        description=(
            "Get the full schema for a specific tool. "
            "Internal - SearchTools now returns schemas directly."
        ),
        params_model=GetToolParams,
        risk_level="low",
        tags=["discovery", "schema", "internal"],
        cli_label='Tool info',
        cli_param='name',
    )
    async def get_tool(self, params: GetToolParams) -> ActionResult:
        name, tool = self._normalize_tool_name(params.name)
        if tool is None:
            error = self._tool_not_found_error(name)
            return ActionResult(success=False, error=error)

        return ActionResult(
            success=True,
            data={
                "name": tool.fqn,
                "description": tool.description,
                "module": tool.module_id,
                "risk_level": tool.risk_level,
                "tags": tool.tags,
                "parameters": tool.params_schema,
                "examples": tool.examples,
                "side_effects": tool.side_effects,
                "irreversible": tool.irreversible,
                "policy": tool.policy_decision.value,
            },
        )

    @action(
        description=(
            "Execute a tool by name. Use SearchTools first to find the tool "
            "and see its parameter schema."
        ),
        tool_prompt=(
            "Execute a discovered tool by its fully qualified name (module.action).\n"
            "\n"
            "## Workflow\n"
            "1. SearchTools('query') - find the tool and its parameter schema\n"
            "2. ExecuteTool(name='module.action', params={...}) - execute it\n"
            "\n"
            "## Important\n"
            "- Use the exact name from SearchTools results (e.g. 'database.sql', not 'Sql')\n"
            "- Pass params as a dict matching the schema from SearchTools"
        ),
        params_model=ExecuteToolParams,
        risk_level="medium",
        tags=["execution"],
        side_effects=["state_mutation"],
        cli_label='Execute',
        cli_param='name',
    )
    async def execute_tool(self, params: ExecuteToolParams) -> ActionResult:
        name, tool = self._normalize_tool_name(params.name)
        if tool is None:
            if await self._try_refresh_mcp_index():
                name, tool = self._normalize_tool_name(params.name)
            if tool is None:
                error = self._tool_not_found_error(name)
                return ActionResult(success=False, error=error)

        if tool.policy_decision == Decision.BLOCK:
            return ActionResult(
                success=False,
                error=f"Tool '{name}' is blocked by security policy.",
                metadata={"blocked": True},
            )
        if tool.policy_decision == Decision.APPROVE and not params.params.get("_approved"):
            approval_queue = None
            agent_ctx = getattr(self, "_agent_context", None)
            if agent_ctx is not None:
                approval_queue = getattr(agent_ctx, "approval_queue", None)
            if approval_queue is None:
                approval_queue = getattr(self, "_approval_queue", None)

            if approval_queue is not None:
                try:
                    from digitorn.core.cli.ui import _tool_label
                    label, detail = _tool_label(name, params.params)
                except Exception:
                    label, detail = name, ""
                try:
                    approved, message = await approval_queue.enqueue(
                        agent_id=getattr(agent_ctx, "agent_id", "main") if agent_ctx else "main",
                        tool_name=name,
                        tool_params=dict(params.params),
                        risk_level=getattr(tool, "risk_level", "medium"),
                        description=f"{label} {detail}".strip(),
                        user_id=getattr(agent_ctx, "user_id", "") if agent_ctx else "",
                        session_id=getattr(agent_ctx, "session_id", "") or "" if agent_ctx else "",
                    )
                except Exception as exc:
                    return ActionResult(
                        success=False,
                        error=f"Approval request failed: {type(exc).__name__}: {exc}",
                        metadata={"requires_approval": True},
                    )
                if not approved:
                    return ActionResult(
                        success=False,
                        error=f"User denied: {message}" if message else "User denied approval.",
                        metadata={"requires_approval": True, "denied": True},
                    )
                params.params["_approved"] = True
            else:
                return ActionResult(
                    success=False,
                    error=f"Tool '{name}' requires approval before execution.",
                    metadata={"requires_approval": True},
                )

        try:
            ctx = self._exec_context
            if ctx is not None and tool.module_id != "context_builder":
                from digitorn.modules.base import ExecutionContext
                module_constraints = {}
                agent_ctx = getattr(self, "_agent_context", None)
                if agent_ctx and hasattr(agent_ctx, "compiled_constraints"):
                    module_constraints = agent_ctx.compiled_constraints.get(tool.module_id, {})
                # tool.policy_decision already enforced; drop the profile
                # to avoid a redundant second gate check.
                ctx = ExecutionContext(
                    plan_id=ctx.plan_id,
                    action_id=f"{tool.module_id}.{tool.action_name}",
                    app_id=ctx.app_id,
                    service_bus=ctx.service_bus,
                    security_profile=None,
                    session_id=ctx.session_id,
                    user_id=ctx.user_id,
                    workspace=ctx.workspace,
                    constraints=module_constraints,
                )
            result = await tool.module.execute(
                tool.action_name, params.params, context=ctx,
            )
            self._usage_counts[name] = self._usage_counts.get(name, 0) + 1
            if isinstance(result, ActionResult):
                return result
            return ActionResult(success=True, data=result)
        except Exception as exc:
            logger.error("execute_tool_failed tool=%s error=%s", name, exc, exc_info=True)
            return ActionResult(
                success=False,
                error=f"Tool '{name}' failed: {type(exc).__name__}: {exc}",
                data={"tool": name, "exception": type(exc).__name__},
            )

    @action(
        description=(
            "List all available tool categories (modules) with their "
            "descriptions and tool counts. Use this to get an overview "
            "of what's available."
        ),
        params_model=ListCategoriesParams,
        risk_level="low",
        tags=["discovery", "navigation"],
        cli_label='Categories',
    )
    async def list_categories(self, params: ListCategoriesParams) -> ActionResult:
        categories = []
        for cat in self._index.categories.values():
            categories.append({
                "id": cat.module_id,
                "summary": cat.summary,
                "tool_count": cat.tool_count,
            })

        return ActionResult(
            success=True,
            data={
                "categories": categories,
                "total_categories": len(categories),
                "total_tools": self._index.total_tools,
            },
        )

    @action(
        description=(
            "Browse all tools in a specific category (module). "
            "Shows tool names, descriptions, and risk levels. "
            "Paginated (20 tools per page)."
        ),
        params_model=BrowseCategoryParams,
        risk_level="low",
        tags=["discovery", "navigation"],
        cli_label='Browse',
        cli_param='category',
    )
    async def browse_category(self, params: BrowseCategoryParams) -> ActionResult:
        cat = self._index.categories.get(params.category)
        if cat is None:
            available = list(self._index.categories.keys())
            return ActionResult(
                success=False,
                error=(
                    f"Category '{params.category}' not found. "
                    f"Available categories: {available}"
                ),
            )

        start = (params.page - 1) * _PAGE_SIZE
        end = start + _PAGE_SIZE
        page_fqns = cat.tool_names[start:end]
        total_pages = max(1, (cat.tool_count + _PAGE_SIZE - 1) // _PAGE_SIZE)

        tools = []
        for fqn in page_fqns:
            tool = self._index.tools.get(fqn)
            if tool:
                tools.append({
                    "name": tool.fqn,
                    "description": tool.description,
                    "risk_level": tool.risk_level,
                    "tags": tool.tags,
                })

        return ActionResult(
            success=True,
            data={
                "category": params.category,
                "summary": cat.summary,
                "tools": tools,
                "page": params.page,
                "total_pages": total_pages,
                "total_tools": cat.tool_count,
            },
        )

    @action(
        description="Execute multiple tool calls in parallel.",
        tool_prompt=(
            "Run multiple independent tool calls concurrently - 3x to 10x faster than sequential.\n"
            "\n"
            "## When to use\n"
            "- Read multiple files at once\n"
            "- Run multiple Grep searches at once\n"
            "- Any independent operations that don't depend on each other\n"
            "\n"
            "## Format\n"
            "actions: [{name: 'filesystem.read', params: {file_path: 'a.py'}}, {name: 'filesystem.read', params: {file_path: 'b.py'}}]\n"
            "\n"
            "## Important\n"
            "- Results are returned in the same order as input actions\n"
            "- Failures in one action do NOT cancel the others"
        ),
        params_model=RunParallelParams,
        risk_level="medium",
        tags=["execution", "parallel", "primitive"],
        side_effects=["state_mutation"],
        aliases=["parallele", "executer_parallele", "batch", "concurrent"],
        cli_label='Parallel',
    )
    async def run_parallel(self, params: RunParallelParams) -> ActionResult:
        from digitorn.modules.executor import gather_actions
        import asyncio as _aio

        resolved: list[tuple[int, str, Any, dict]] = []
        needs_approval: list[tuple[int, str, Any, dict]] = []
        errors: list[dict[str, Any]] = []

        for i, act in enumerate(params.actions):
            name, tool = self._normalize_tool_name(act.name)
            if tool is None:
                short = name.rsplit(".", 1)[-1] if "." in name else name
                errors.append({
                    "index": i, "name": act.name,
                    "label": short.replace("_", " ").capitalize(),
                    "detail": "",
                    "success": False,
                    "error": self._tool_not_found_error(name),
                })
                continue

            if tool.policy_decision == Decision.BLOCK:
                short = name.rsplit(".", 1)[-1] if "." in name else name
                errors.append({
                    "index": i, "name": name,
                    "label": short.replace("_", " ").capitalize(),
                    "detail": "",
                    "success": False,
                    "error": f"Tool '{name}' is blocked by security policy.",
                })
                continue

            if tool.policy_decision == Decision.APPROVE and not act.params.get("_approved"):
                needs_approval.append((i, name, tool, dict(act.params)))
                continue

            resolved.append((i, name, tool, dict(act.params)))

        if needs_approval:
            approval_queue = None
            agent_ctx = getattr(self, "_agent_context", None)
            if agent_ctx is not None:
                approval_queue = getattr(agent_ctx, "approval_queue", None)
            if approval_queue is None:
                approval_queue = getattr(self, "_approval_queue", None)

            if approval_queue is not None:
                async def _request_approval(idx, name, tool, action_params):
                    try:
                        from digitorn.core.cli.ui import _tool_label
                        label, detail = _tool_label(name, action_params)
                    except Exception:
                        label, detail = name, ""
                    approved, message = await approval_queue.enqueue(
                        agent_id=getattr(agent_ctx, "agent_id", "main"),
                        tool_name=name,
                        tool_params=action_params,
                        risk_level=getattr(tool, "risk_level", "medium") if hasattr(tool, "risk_level") else "medium",
                        description=f"Parallel action: {label} {detail}".strip(),
                        user_id=getattr(agent_ctx, "user_id", ""),
                        session_id=getattr(agent_ctx, "session_id", "") or "",
                    )
                    return idx, name, tool, action_params, approved, message

                # keep parallel info list so we can recover idx/name
                # even when _request_approval itself raises.
                approval_requests = list(needs_approval)
                approval_results = await _aio.gather(
                    *[_request_approval(i, n, t, p) for i, n, t, p in approval_requests],
                    return_exceptions=True,
                )

                for req_info, result in zip(approval_requests, approval_results):
                    req_idx, req_name, req_tool, req_params = req_info
                    if isinstance(result, BaseException):
                        try:
                            from digitorn.core.cli.ui import _tool_label
                            label, detail = _tool_label(req_name, req_params)
                        except Exception:
                            label, detail = req_name, ""
                        errors.append({
                            "index": req_idx, "name": req_name,
                            "label": label, "detail": detail,
                            "success": False,
                            "error": f"Approval request failed: {type(result).__name__}: {result}",
                        })
                        continue
                    idx, name, tool, action_params, approved, msg = result
                    if approved:
                        action_params["_approved"] = True
                        resolved.append((idx, name, tool, action_params))
                    else:
                        try:
                            from digitorn.core.cli.ui import _tool_label
                            label, detail = _tool_label(name, action_params)
                        except Exception:
                            label, detail = name, ""
                        errors.append({
                            "index": idx, "name": name,
                            "label": label, "detail": detail,
                            "success": False,
                            "error": f"User denied: {msg}" if msg else "User denied approval.",
                        })
            else:
                for i, name, tool, action_params in needs_approval:
                    errors.append({
                        "index": i, "name": name,
                        "label": name.rsplit(".", 1)[-1],
                        "detail": "",
                        "success": False,
                        "error": f"Tool '{name}' requires approval but no approval queue available.",
                    })

        if not resolved and errors:
            # group identical errors so 20+ failures collapse to a
            # few lines the LLM actually reads.
            from collections import Counter
            grouped: Counter[tuple[str, str]] = Counter()
            for e in errors:
                grouped[(e.get("name", "?"), e.get("error", ""))] += 1
            parts: list[str] = []
            for (tname, emsg), cnt in grouped.most_common(5):
                snippet = emsg.splitlines()[0] if emsg else "denied"
                if len(snippet) > 160:
                    snippet = snippet[:157] + "..."
                multi = f" x{cnt}" if cnt > 1 else ""
                parts.append(f"{tname}{multi}: {snippet}")
            head = (
                f"All {len(errors)} parallel action(s) failed before execution."
            )
            if parts:
                head += " " + " | ".join(parts)
            return ActionResult(
                success=False,
                error=head,
                data={"results": errors},
            )

        # tool.policy_decision already enforced; drop the profile
        # to avoid a redundant second gate check.
        _ctx = self._exec_context
        _safe_ctx = None
        if _ctx is not None:
            from digitorn.modules.base import ExecutionContext
            _safe_ctx = ExecutionContext(
                plan_id=_ctx.plan_id,
                action_id=_ctx.action_id,
                app_id=_ctx.app_id,
                service_bus=_ctx.service_bus,
                security_profile=None,
                session_id=_ctx.session_id,
                user_id=_ctx.user_id,
                workspace=_ctx.workspace,
            )
        calls = [
            (tool.module, tool.action_name, action_params, _safe_ctx)
            for _, _, tool, action_params in resolved
        ]

        start = time.perf_counter()
        raw_results = await gather_actions(calls, return_exceptions=True)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)

        results: list[dict[str, Any]] = []
        for (idx, name, tool, action_params), raw in zip(resolved, raw_results):
            try:
                from digitorn.core.cli.ui import _tool_label
                label, detail = _tool_label(name, action_params)
            except Exception:
                label, detail = name.rsplit(".", 1)[-1] if "." in name else name, ""

            if isinstance(raw, BaseException):
                results.append({
                    "index": idx, "name": name,
                    "label": label, "detail": detail,
                    "success": False, "error": str(raw),
                })
            elif isinstance(raw, ActionResult):
                results.append({
                    "index": idx, "name": name,
                    "label": label, "detail": detail,
                    "success": bool(raw.success),
                    "data": raw.data,
                    "error": str(raw.error) if raw.error else "",
                })
            else:
                results.append({
                    "index": idx, "name": name,
                    "label": label, "detail": detail,
                    "success": True, "data": raw,
                    "error": "",
                })

        all_results = sorted(results + errors, key=lambda r: r.get("index", 0))
        succeeded = sum(1 for r in all_results if r.get("success"))

        return ActionResult(
            success=True,
            data={
                "results": all_results,
                "total": len(params.actions),
                "succeeded": succeeded,
                "failed": len(params.actions) - succeeded,
                "elapsed_ms": elapsed_ms,
                "hint": (
                    f"Executed {len(params.actions)} actions in parallel "
                    f"({elapsed_ms}ms): {succeeded} succeeded, "
                    f"{len(params.actions) - succeeded} failed."
                ),
            },
        )

    def _normalize_tool_name(self, name: str) -> tuple[str, Any | None]:
        name = name.strip()
        tool = self._index.tools.get(name)
        if tool:
            return name, tool
        from digitorn.core.runtime.tool_names import to_fqn
        fqn = to_fqn(name)
        if fqn != name:
            tool = self._index.tools.get(fqn)
            if tool:
                return fqn, tool
        if "__" in name and "." not in name:
            dotted = name.replace("__", ".")
            tool = self._index.tools.get(dotted)
            if tool:
                return dotted, tool
        if "_" in name and "." not in name:
            parts = name.split("_", 1)
            dotted = f"{parts[0]}.{parts[1]}"
            tool = self._index.tools.get(dotted)
            if tool:
                return dotted, tool
        # Last resort: match by action-name suffix, case-insensitive.
        name_lower = name.lower()
        for fqn_key, idx_tool in self._index.tools.items():
            if fqn_key.rsplit(".", 1)[-1].lower() == name_lower:
                return fqn_key, idx_tool
        return name, None

    def _resolve_tool(self, name: str) -> tuple[Any | None, str | None]:
        name, tool = self._normalize_tool_name(name)
        if tool is None:
            return None, self._tool_not_found_error(name)
        if tool.policy_decision == Decision.BLOCK:
            return None, f"Tool '{name}' is blocked by security policy."
        return tool, None

    def _prompt_sections_meta(self) -> list[dict[str, Any]]:
        return [{
            "title": "EXECUTION PRIMITIVES (always available)",
            "content": (
                "These are TOOL CALLS - call them directly like any other tool.\n"
                "NEVER pass them as strings to shell.run or execute_tool.\n"
                "They work with ANY action from ANY module.\n"
                "\n"
                "## Parallel Execution\n"
                "- **run_parallel**(actions): Execute multiple actions simultaneously\n"
                "\n"
                "Use run_parallel when you have independent tasks (e.g. read 3 files, "
                "call 2 APIs, query a database - all at the same time). "
                "3x-10x faster than sequential execution.\n"
                "\n"
                "Example tool call:\n"
                "  run_parallel(actions=[\n"
                '    {"name": "filesystem.read", "params": {"path": "/tmp/a.txt"}},\n'
                '    {"name": "filesystem.read", "params": {"path": "/tmp/b.txt"}}\n'
                "  ])"
            ),
            "priority": 15,
        }]

    def _tool_not_found_error(self, name: str) -> str:
        all_fqns = list(self._index.tools.keys())

        prefix_matches = [f for f in all_fqns if f.startswith(name.split(".")[0] + ".")]

        best: str | None = None
        best_dist = 4
        name_lower = name.lower()
        for fqn in all_fqns:
            d = _levenshtein(name_lower, fqn.lower())
            if d < best_dist:
                best_dist = d
                best = fqn

        parts = [f"Tool '{name}' not found."]

        if best and best_dist <= 3:
            parts.append(f"Did you mean '{best}'?")
        elif prefix_matches:
            shown = prefix_matches[:5]
            parts.append(f"Available in that module: {', '.join(shown)}.")
        else:
            # advertise discovery meta-tools only if this agent has them.
            has_discovery = False
            try:
                index = getattr(self, "_index", None)
                if index is not None:
                    fqns = {t.fqn for t in index.tools.values()}
                    has_discovery = (
                        "context_builder.search_tools" in fqns
                        or "context_builder.list_categories" in fqns
                    )
            except Exception:
                has_discovery = False
            if has_discovery:
                parts.append(
                    "Use search_tools or list_categories to discover "
                    "available tools.",
                )
            else:
                try:
                    index = getattr(self, "_index", None)
                    names = (
                        sorted(t.fqn for t in index.tools.values())[:15]
                        if index is not None else []
                    )
                    if names:
                        parts.append(
                            f"Available tools: {', '.join(names)}.",
                        )
                except Exception as exc:
                    logger.debug("actions_meta best-effort block failed: %s", exc)

        return " ".join(parts)

    @action(
        description=(
            "Load a skill -- a reusable workflow with detailed instructions. "
            "Skills provide step-by-step methodology for specific tasks "
            "(e.g. /commit, /review, /security-audit). "
            "The skill content is returned so you can follow its instructions."
        ),
        params_model=UseSkillParams,
        risk_level="low",
        tags=["skills", "primitive"],
        cli_label="Skill",
        cli_param="name",
    )
    async def use_skill(self, params: UseSkillParams) -> ActionResult:
        command = params.command

        if not command:
            return ActionResult(success=False, error="Missing 'command' parameter")
        if not command.startswith("/"):
            command = f"/{command}"

        for skill in self._skills:
            if skill.get("command") == command:
                return ActionResult(success=True, data={
                    "command": command,
                    "description": skill.get("description", ""),
                    "content": skill.get("content", ""),
                    "note": "Follow these instructions to complete the task.",
                })

        available = [s["command"] for s in self._skills]
        return ActionResult(
            success=False,
            error=f"Skill '{command}' not found. Available: {', '.join(available) if available else 'none'}",
        )

    @action(
        description=(
            "Call another deployed Digitorn app and return its result. "
            "The target app must be deployed on the daemon and in one_shot mode. "
            "Use this to compose multiple apps into a pipeline. "
            "Example: call_app(app_id='code-analyzer', input='src/auth.py')"
        ),
        params_model=CallAppParams,
        risk_level="medium",
        tags=["composition", "pipeline", "orchestration"],
        cli_label="Call app",
        cli_param="app_id",
    )
    async def call_app(self, params: CallAppParams) -> ActionResult:
        # prefer in-process dispatch via AppManager; the HTTP loopback
        # fallback is broken under auth middleware (no loopback bypass).
        manager = getattr(self, "_app_manager", None)
        if manager is not None and hasattr(manager, "run_one_shot"):
            try:
                user_id = None
                agent_ctx = getattr(self, "_agent_context", None)
                if agent_ctx is not None:
                    user_id = getattr(agent_ctx, "user_id", None)
                result = await asyncio.wait_for(
                    manager.run_one_shot(
                        params.app_id, params.input,
                        user_id=user_id or None,
                    ),
                    timeout=params.timeout,
                )
                content = getattr(result, "content", "") or ""
                tcc = getattr(result, "tool_calls_count", 0) or 0
                err = getattr(result, "error", None)
                if err:
                    return ActionResult(
                        success=False,
                        error=str(err),
                        data={"app_id": params.app_id, "output": content},
                    )
                return ActionResult(
                    success=True,
                    data={
                        "app_id": params.app_id,
                        "output": content,
                        "tool_calls": tcc,
                    },
                )
            except asyncio.TimeoutError:
                return ActionResult(
                    success=False,
                    error=f"App '{params.app_id}' timed out after {params.timeout}s",
                )
            except RuntimeError as exc:
                return ActionResult(success=False, error=str(exc))
            except Exception as exc:
                return ActionResult(
                    success=False,
                    error=f"call_app failed: {type(exc).__name__}: {exc}",
                )

        # Legacy HTTP fallback - only works when auth is disabled.
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:8000/api/apps/{params.app_id}/run",
                    json={"input": params.input},
                    timeout=params.timeout,
                )
                if resp.status_code == 401:
                    return ActionResult(
                        success=False,
                        error=(
                            "call_app cannot reach the target app: the daemon's "
                            "auth middleware requires a Bearer token on /api/*. "
                            "In-process dispatch is unavailable on this build "
                            "(`cb._app_manager` not wired). Disable auth in dev "
                            "or invoke the target app's tools directly via "
                            "run_parallel / Agent()."
                        ),
                    )
                data = resp.json()
                if data.get("success"):
                    return ActionResult(
                        success=True,
                        data={
                            "app_id": params.app_id,
                            "output": data.get("data", {}).get("content", ""),
                            "tool_calls": data.get("data", {}).get("tool_calls_count", 0),
                        },
                    )
                return ActionResult(
                    success=False,
                    error=data.get("error", f"App '{params.app_id}' returned an error"),
                )
        except httpx.ConnectError:
            return ActionResult(success=False, error="Daemon not reachable. Is it running?")
        except httpx.TimeoutException:
            return ActionResult(success=False, error=f"App '{params.app_id}' timed out after {params.timeout}s")
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    @action(
        description=(
            "Ask the user a question and WAIT for their response. The agent pauses until the user replies. "
            "Supports: simple questions, multiple choices, content review, and structured forms."
        ),
        tool_prompt=(
            "Ask the user a question and WAIT for their response.\n"
            "The agent pauses completely until the user replies - use this wisely.\n\n"
            "## When to use\n"
            "- Before destructive actions (delete, overwrite)\n"
            "- When choosing between approaches\n"
            "- To review a plan or code before proceeding\n"
            "- When you need specific information the user hasn't provided\n\n"
            "## Modes\n\n"
            "**Simple question** - just ask:\n"
            '  ask_user(question="Should I proceed with this plan?")\n\n'
            "**With choices** - user clicks a button instead of typing:\n"
            '  ask_user(question="Which framework?", choices=["FastAPI", "Django", "Flask"])\n\n'
            "**Multi-select choices:**\n"
            '  ask_user(question="Which features?", choices=["Auth", "DB", "Tests", "Docker"], allow_multiple=true)\n\n'
            "**Content review** - user sees and can edit a long document:\n"
            '  ask_user(question="Review and approve this plan.", content="## Plan\\n1. Create auth\\n2. Add routes\\n3. Write tests")\n\n'
            "**Structured form** - user fills in multiple fields:\n"
            '  ask_user(question="Configure the project", form=[\n'
            '    {"type": "select", "name": "framework", "label": "Framework", "options": ["FastAPI", "Django"]},\n'
            '    {"type": "text", "name": "name", "label": "Project name", "placeholder": "my-app"},\n'
            '    {"type": "checkbox", "name": "features", "label": "Features", "options": ["Auth", "DB", "Tests"]},\n'
            '    {"type": "toggle", "name": "docker", "label": "Docker?", "default": true}\n'
            "  ])\n\n"
            "## Form field types\n"
            "- select: dropdown with options\n"
            "- text: single line input\n"
            "- textarea: multi-line input\n"
            "- checkbox: multiple select with checkboxes\n"
            "- toggle: on/off switch\n"
            "- number: numeric input\n\n"
            "## Rules\n"
            "- Do NOT ask trivial questions - use your best judgment for simple decisions\n"
            "- Do NOT ask multiple questions in one call - split them\n"
            "- If the user already gave you enough info, just proceed\n"
            "- Use choices when there are 2-6 clear options\n"
            "- Use form when you need multiple pieces of info at once\n"
            "- Use content when presenting a plan or code for review"
        ),
        params_model=AskUserParams,
        risk_level="low",
        tags=["meta", "interaction", "approval"],
        aliases=["confirm", "demander", "confirmer", "approval", "question"],
        cli_label="Ask",
        cli_param="question",
    )
    async def ask_user(self, params: AskUserParams) -> ActionResult:
        """Ask the user a question and wait for their response via the approval queue."""
        ctx = self._context_var.get() if hasattr(self, "_context_var") else None
        approval_queue = None

        if ctx is not None:
            approval_queue = getattr(ctx, "approval_queue", None)
        if approval_queue is None:
            approval_queue = getattr(self, "_approval_queue", None)

        if approval_queue is None:
            logger.warning("ask_user: no approval_queue available (standalone mode?)")
            return ActionResult(
                success=False,
                data={
                    "status": "no_approval_queue",
                    "question": params.question,
                    "content": params.content,
                    "message": (
                        "Cannot ask user - no approval queue configured. "
                        "In daemon mode, ensure the app has an approval queue. "
                        f"Question was: {params.question}"
                    ),
                    "requires_user_response": True,
                },
            )

        tool_params: dict[str, Any] = {"question": params.question}
        if params.content:
            tool_params["content"] = params.content
        if params.choices:
            tool_params["choices"] = params.choices
            tool_params["allow_multiple"] = params.allow_multiple
        if params.form:
            tool_params["form"] = params.form

        description = params.question
        if params.choices:
            description += "\n\nOptions: " + ", ".join(params.choices)
        if params.content:
            preview = params.content[:200]
            if len(params.content) > 200:
                preview += "..."
            description = f"{params.question}\n\n---\n{preview}"

        approved, user_message = await approval_queue.enqueue(
            agent_id=getattr(ctx, "agent_id", "main") if ctx else "main",
            tool_name="ask_user",
            tool_params=tool_params,
            risk_level="low",
            description=description,
            timeout=params.timeout,
            user_id=getattr(ctx, "user_id", "local") if ctx else "local",
            session_id=getattr(ctx, "session_id", "") or "" if ctx else "",
        )

        formatted = _format_ask_user_response(params, user_message)
        logger.info(
            "ask_user_resolved: approved=%s payload_len=%d formatted=%r",
            approved, len(user_message or ""), formatted[:120],
        )

        if approved:
            result_data: dict[str, Any] = {
                "status": "approved",
                "question": params.question,
                "user_response": formatted or "approved",
                "raw_response": user_message,
            }
            if params.content and user_message:
                result_data["edited_content"] = user_message
                result_data["content_was_edited"] = True
            elif params.content:
                result_data["content"] = params.content
                result_data["content_was_edited"] = False
            return ActionResult(success=True, data=result_data)
        else:
            return ActionResult(
                success=True,
                data={
                    "status": "denied",
                    "question": params.question,
                    "user_response": user_message or "User declined.",
                    "user_feedback": user_message,
                },
            )
