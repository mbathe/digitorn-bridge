"""Watcher actions mixin for ContextBuilderModule."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from digitorn.modules.base import ActionResult
from digitorn.modules.context_builder.params import (
    WatcherIdParams,
    WatchHistoryParams,
    WatchListParams,
    WatchStartParams,
)
from digitorn.modules.context_builder.types import Decision
from digitorn.modules.decorators import action

logger = logging.getLogger(__name__)


@dataclass
class Watcher:
    """Tracks a persistent periodic watcher launched via watch_start."""

    watcher_id: str
    tool_name: str
    params: dict[str, Any]
    interval: float
    label: str
    notify_when: str
    notify_config: dict[str, Any]
    max_checks: int = 0
    status: str = "running"
    check_count: int = 0
    notify_count: int = 0
    last_result: Any = None
    last_error: str | None = None
    last_check_at: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    history: list[dict[str, Any]] = field(default_factory=list)
    _asyncio_task: asyncio.Task[Any] | None = field(default=None, repr=False)
    _accumulator: list[dict[str, Any]] = field(default_factory=list)
    _prev_result: Any = field(default=None, repr=False)
    _prev_error: str | None = field(default=None, repr=False)
    _was_error: bool = field(default=False, repr=False)

    _HISTORY_MAX = 100

    def to_summary(self) -> dict[str, Any]:
        return {
            "watcher_id": self.watcher_id,
            "tool_name": self.tool_name,
            "label": self.label,
            "status": self.status,
            "interval": self.interval,
            "notify_when": self.notify_when,
            "max_checks": self.max_checks,
            "check_count": self.check_count,
            "notify_count": self.notify_count,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }

    def add_history(self, entry: dict[str, Any]) -> None:
        self.history.append(entry)
        if len(self.history) > self._HISTORY_MAX:
            self.history = self.history[-self._HISTORY_MAX:]


import uuid


class WatcherActionsMixin:
    """Watcher actions - start/stop/pause/resume/status/list/history + loop."""

    @action(
        description=(
            "Start a persistent watcher that periodically executes a tool and "
            "reports back ONLY when something interesting happens. Use this to "
            "monitor APIs, track process progress, observe file changes, watch "
            "database metrics, etc. The watcher runs in the background and does "
            "NOT block the conversation - you can keep chatting normally. "
            "Notifications are smart: 'on_change' only notifies when the result "
            "differs from the previous check, 'on_error' only on errors, "
            "'on_threshold' when a condition is met, 'summary' batches N checks."
        ),
        params_model=WatchStartParams,
        risk_level="medium",
        tags=["watcher", "monitoring", "primitive"],
        side_effects=["state_mutation"],
        aliases=["surveiller", "monitorer", "watch", "observer"],
        cli_label="Watch",
        cli_param="name",
    )
    async def watch_start(self, params: WatchStartParams) -> ActionResult:
        tool, error = self._resolve_tool(params.name)
        if error:
            return ActionResult(success=False, error=error)

        if tool.policy_decision == Decision.APPROVE:
            return ActionResult(
                success=False,
                error=(
                    f"Tool '{params.name}' requires user approval. "
                    "Watchers cannot be started on approval-required tools."
                ),
            )

        valid_strategies = ("on_change", "on_error", "on_threshold", "summary", "always")
        if params.notify_when not in valid_strategies:
            return ActionResult(
                success=False,
                error=(
                    f"Invalid notify_when: '{params.notify_when}'. "
                    f"Must be one of: {', '.join(valid_strategies)}"
                ),
            )

        if params.notify_when == "on_threshold":
            expr = params.notify_config.get("expression", "")
            if not expr:
                return ActionResult(
                    success=False,
                    error=(
                        "notify_when='on_threshold' requires "
                        "notify_config.expression (e.g. 'result.status_code != 200')."
                    ),
                )

        if params.notify_when == "summary":
            batch_size = params.notify_config.get("batch_size", 10)
            if not isinstance(batch_size, int) or batch_size <= 0:
                return ActionResult(
                    success=False,
                    error="notify_when='summary' requires notify_config.batch_size > 0",
                )

        entry = tool.module._action_registry.get(tool.action_name)
        if entry is not None and getattr(entry, "params_model", None) is not None:
            try:
                from pydantic import ValidationError as _VE
                entry.params_model.model_validate(dict(params.params))
            except _VE as ve:
                field_errors = []
                for err in ve.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    field_errors.append(f"  - {loc}: {err['msg']}")
                return ActionResult(
                    success=False,
                    error=(
                        f"Invalid params for tool '{tool.fqn}':\n"
                        + "\n".join(field_errors)
                        + f"\n\nExpected params schema: {tool.params_schema}"
                    ),
                )

        watcher_id = uuid.uuid4().hex[:12]
        watcher = Watcher(
            watcher_id=watcher_id,
            tool_name=tool.fqn,
            params=dict(params.params),
            interval=params.interval,
            label=params.label or f"Watch {tool.fqn}",
            notify_when=params.notify_when,
            notify_config=dict(params.notify_config),
            max_checks=params.max_checks,
        )
        self._watchers[watcher_id] = watcher

        self._persist_watcher(watcher)

        watcher._asyncio_task = asyncio.create_task(
            self._watcher_loop(watcher)
        )

        logger.info(
            "watcher_started id=%s tool=%s interval=%.0fs strategy=%s",
            watcher_id, tool.fqn, params.interval, params.notify_when,
        )

        return ActionResult(
            success=True,
            data={
                **watcher.to_summary(),
                "hint": (
                    f"Watcher '{watcher.label}' started. Checking {tool.fqn} "
                    f"every {params.interval}s. You'll be notified "
                    f"via '{params.notify_when}' strategy."
                ),
            },
        )

    @action(
        description=(
            "Stop and remove a watcher. The watcher is cancelled and its "
            "history is discarded."
        ),
        params_model=WatcherIdParams,
        risk_level="low",
        tags=["watcher", "primitive"],
        aliases=["arreter_surveillance", "stop_watch"],
        cli_label="Stop watch",
        cli_param="watcher_id",
    )
    async def watch_stop(self, params: WatcherIdParams) -> ActionResult:
        watcher = self._watchers.get(params.watcher_id)
        if watcher is None:
            return ActionResult(
                success=False,
                error=(
                    f"Watcher '{params.watcher_id}' not found. "
                    "Use watch_list to see active watchers."
                ),
            )

        watcher.status = "stopped"
        if watcher._asyncio_task and not watcher._asyncio_task.done():
            watcher._asyncio_task.cancel()

        self._unpersist_watcher(params.watcher_id)

        summary = watcher.to_summary()
        del self._watchers[params.watcher_id]

        logger.info("watcher_stopped id=%s tool=%s", params.watcher_id, watcher.tool_name)
        return ActionResult(
            success=True,
            data={
                **summary,
                "hint": (
                    f"Watcher stopped after {watcher.check_count} checks "
                    f"({watcher.notify_count} notifications sent)."
                ),
            },
        )

    @action(
        description=(
            "Pause a running watcher. The timer continues but checks are "
            "skipped. History is preserved. Use watch_resume to restart."
        ),
        params_model=WatcherIdParams,
        risk_level="low",
        tags=["watcher", "primitive"],
        aliases=["pause_surveillance", "pause_watch"],
        cli_label="Pause watch",
        cli_param="watcher_id",
    )
    async def watch_pause(self, params: WatcherIdParams) -> ActionResult:
        watcher = self._watchers.get(params.watcher_id)
        if watcher is None:
            return ActionResult(
                success=False,
                error=f"Watcher '{params.watcher_id}' not found.",
            )

        if watcher.status != "running":
            return ActionResult(
                success=False,
                error=f"Watcher is '{watcher.status}', can only pause a running watcher.",
            )

        watcher.status = "paused"
        self._persist_watcher(watcher)
        logger.info("watcher_paused id=%s", params.watcher_id)
        return ActionResult(
            success=True,
            data={**watcher.to_summary(), "hint": "Watcher paused. Use watch_resume to restart."},
        )

    @action(
        description="Resume a paused watcher. Checks restart immediately.",
        params_model=WatcherIdParams,
        risk_level="low",
        tags=["watcher", "primitive"],
        aliases=["reprendre_surveillance", "resume_watch"],
        cli_label="Resume watch",
        cli_param="watcher_id",
    )
    async def watch_resume(self, params: WatcherIdParams) -> ActionResult:
        watcher = self._watchers.get(params.watcher_id)
        if watcher is None:
            return ActionResult(
                success=False,
                error=f"Watcher '{params.watcher_id}' not found.",
            )

        if watcher.status != "paused":
            return ActionResult(
                success=False,
                error=f"Watcher is '{watcher.status}', can only resume a paused watcher.",
            )

        watcher.status = "running"
        self._persist_watcher(watcher)
        logger.info("watcher_resumed id=%s", params.watcher_id)
        return ActionResult(
            success=True,
            data={**watcher.to_summary(), "hint": "Watcher resumed. Checks will restart."},
        )

    @action(
        description=(
            "Get detailed status of a watcher: metrics, last result, "
            "configuration, and recent history."
        ),
        params_model=WatcherIdParams,
        risk_level="low",
        tags=["watcher", "status", "primitive"],
        aliases=["statut_surveillance", "watch_info"],
        cli_label="Watch status",
        cli_param="watcher_id",
    )
    async def watch_status(self, params: WatcherIdParams) -> ActionResult:
        watcher = self._watchers.get(params.watcher_id)
        if watcher is None:
            return ActionResult(
                success=False,
                error=f"Watcher '{params.watcher_id}' not found.",
            )

        data = {
            **watcher.to_summary(),
            "params": watcher.params,
            "notify_config": watcher.notify_config,
            "last_result": watcher.last_result,
            "recent_history": watcher.history[-5:] if watcher.history else [],
        }
        return ActionResult(success=True, data=data)

    @action(
        description=(
            "List all watchers with their current status, check counts, "
            "and notification counts. Running watchers are shown first."
        ),
        params_model=WatchListParams,
        risk_level="low",
        tags=["watcher", "list", "primitive"],
        aliases=["liste_surveillances", "list_watches"],
        cli_label="List watches",
    )
    async def watch_list(self, params: WatchListParams) -> ActionResult:
        sorted_watchers = sorted(
            self._watchers.values(),
            key=lambda w: (w.status != "running", w.status != "paused", w.created_at),
        )
        watchers = [w.to_summary() for w in sorted_watchers]
        running = sum(1 for w in self._watchers.values() if w.status == "running")
        paused = sum(1 for w in self._watchers.values() if w.status == "paused")

        data: dict[str, Any] = {
            "watchers": watchers,
            "total": len(watchers),
            "running": running,
            "paused": paused,
        }

        if not watchers:
            data["hint"] = "No watchers. Use watch_start to create one."
        elif running > 0:
            data["hint"] = (
                f"{running} running, {paused} paused. "
                "Use watch_status(watcher_id) for details."
            )
        else:
            data["hint"] = f"All watchers paused ({paused})."

        return ActionResult(success=True, data=data)

    @action(
        description=(
            "Get the last N check results from a watcher's history. "
            "Each entry includes timestamp, result/error, and whether "
            "a notification was triggered."
        ),
        params_model=WatchHistoryParams,
        risk_level="low",
        tags=["watcher", "history", "primitive"],
        aliases=["historique_surveillance", "watch_checks"],
        cli_label="Watch history",
        cli_param="watcher_id",
    )
    async def watch_history(self, params: WatchHistoryParams) -> ActionResult:
        watcher = self._watchers.get(params.watcher_id)
        if watcher is None:
            return ActionResult(
                success=False,
                error=f"Watcher '{params.watcher_id}' not found.",
            )

        entries = watcher.history[-params.last_n:]
        return ActionResult(
            success=True,
            data={
                "watcher_id": watcher.watcher_id,
                "label": watcher.label,
                "total_checks": watcher.check_count,
                "entries": entries,
                "showing": len(entries),
            },
        )

    # ── Watcher internals ────────────────────────────────────────────

    async def _watcher_loop(self, watcher: Watcher) -> None:
        try:
            while watcher.status != "stopped":
                await asyncio.sleep(watcher.interval)

                if watcher.status == "paused":
                    continue
                if watcher.status == "stopped":
                    break

                result, error = await self._execute_watcher_check(watcher)
                watcher.check_count += 1
                now = datetime.now(timezone.utc).isoformat()
                watcher.last_check_at = now
                watcher.last_result = result
                watcher.last_error = error

                should_notify = self._evaluate_notify(watcher, result, error)

                entry: dict[str, Any] = {
                    "check_number": watcher.check_count,
                    "timestamp": now,
                    "notified": should_notify,
                }
                if error:
                    entry["error"] = error
                else:
                    result_str = str(result)
                    if len(result_str) > 500:
                        entry["result_preview"] = result_str[:500] + "..."
                    else:
                        entry["result"] = result
                watcher.add_history(entry)

                watcher._prev_result = result
                watcher._prev_error = error
                watcher._was_error = error is not None

                if should_notify:
                    watcher.notify_count += 1
                    self._push_watcher_notification(watcher, result, error)

                if watcher.check_count % 10 == 0:
                    self._persist_watcher(watcher)

                if watcher.max_checks > 0 and watcher.check_count >= watcher.max_checks:
                    watcher.status = "completed"
                    self._unpersist_watcher(watcher.watcher_id)
                    logger.info(
                        "watcher_auto_stopped id=%s after %d/%d checks",
                        watcher.watcher_id, watcher.check_count, watcher.max_checks,
                    )
                    break

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("watcher_loop_crashed id=%s", watcher.watcher_id)
            watcher.status = "failed"
            watcher.last_error = f"Loop crashed: {type(exc).__name__}: {exc}"
            self._unpersist_watcher(watcher.watcher_id)

    async def _execute_watcher_check(
        self, watcher: Watcher
    ) -> tuple[Any, str | None]:
        tool = self._index.tools.get(watcher.tool_name)
        if tool is None:
            return None, f"Tool '{watcher.tool_name}' no longer available."

        try:
            raw = await tool.module.execute(
                tool.action_name, dict(watcher.params), context=self._exec_context,
            )
            if isinstance(raw, ActionResult):
                if raw.success:
                    return raw.data, None
                return raw.data, raw.error
            return raw, None
        except Exception as exc:
            return None, str(exc)

    def _evaluate_notify(
        self, watcher: Watcher, result: Any, error: str | None
    ) -> bool:
        strategy = watcher.notify_when

        if strategy == "always":
            return True

        if strategy == "on_change":
            if watcher.check_count == 1:
                return False
            if error != watcher._prev_error:
                return True
            if error is None and result != watcher._prev_result:
                return True
            return False

        if strategy == "on_error":
            if error is not None:
                return True
            if watcher._was_error and error is None:
                return True
            return False

        if strategy == "on_threshold":
            expr = watcher.notify_config.get("expression", "")
            if not expr:
                return False
            return self._eval_threshold(expr, result, error)

        if strategy == "summary":
            batch_size = watcher.notify_config.get("batch_size", 10)
            watcher._accumulator.append({
                "check": watcher.check_count,
                "result": result,
                "error": error,
            })
            if len(watcher._accumulator) >= batch_size:
                return True
            return False

        return False

    def _eval_threshold(
        self, expression: str, result: Any, error: str | None
    ) -> bool:
        ops = ("!=", ">=", "<=", "==", ">", "<")
        op_found = None
        left_str = ""
        right_str = ""

        for op in ops:
            if op in expression:
                parts = expression.split(op, 1)
                if len(parts) == 2:
                    left_str = parts[0].strip()
                    right_str = parts[1].strip()
                    op_found = op
                    break

        if op_found is None:
            return False

        left_val = self._resolve_dot_path(left_str, result, error)
        right_val = self._parse_literal(right_str)

        try:
            if op_found == "==":
                return left_val == right_val
            if op_found == "!=":
                return left_val != right_val
            if op_found == ">":
                return left_val > right_val
            if op_found == "<":
                return left_val < right_val
            if op_found == ">=":
                return left_val >= right_val
            if op_found == "<=":
                return left_val <= right_val
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _resolve_dot_path(path: str, result: Any, error: str | None) -> Any:
        parts = path.split(".")
        if not parts:
            return None

        if parts[0] == "result":
            current = result
            parts = parts[1:]
        elif parts[0] == "error":
            return error
        else:
            current = result

        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif hasattr(current, part):
                current = getattr(current, part)
            else:
                try:
                    current = current[int(part)]
                except (ValueError, IndexError, TypeError, KeyError):
                    return None
        return current

    @staticmethod
    def _parse_literal(value: str) -> Any:
        if value == "null" or value == "None":
            return None
        if value == "true" or value == "True":
            return True
        if value == "false" or value == "False":
            return False
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            pass
        if (value.startswith('"') and value.endswith('"')) or \
           (value.startswith("'") and value.endswith("'")):
            return value[1:-1]
        return value

    def _push_watcher_notification(
        self, watcher: Watcher, result: Any, error: str | None
    ) -> None:
        notification: dict[str, Any] = {
            "type": "watcher",
            "watcher_id": watcher.watcher_id,
            "tool_name": watcher.tool_name,
            "label": watcher.label,
            "check_number": watcher.check_count,
            "notify_count": watcher.notify_count,
            "interval": watcher.interval,
            "strategy": watcher.notify_when,
        }

        if error:
            notification["error"] = error
        else:
            result_str = str(result)
            if len(result_str) > 2000:
                notification["result_preview"] = result_str[:2000] + f"... ({len(result_str)} chars)"
            else:
                notification["result"] = result

        if watcher.notify_when == "summary" and watcher._accumulator:
            notification["summary_batch"] = list(watcher._accumulator)
            watcher._accumulator.clear()

        # Route to the correct session queue
        self.push_module_notification(notification)

    # ── Prompt sections ────────────────────────────────

    def _prompt_sections_watchers(self) -> list[dict[str, Any]]:
        """Prompt instructions for persistent watchers. Gated by _watchers_enabled."""
        if not getattr(self, "_watchers_enabled", False):
            return []
        return [{
            "title": "",
            "content": (
                "## Watchers (Persistent Monitoring)\n"
                "- **watch_start**(name, params, interval?, label?, notify_when?, max_checks?): "
                "Start a periodic watcher\n"
                "- **watch_stop**(watcher_id): Stop and remove a watcher\n"
                "- **watch_pause**(watcher_id): Pause a running watcher\n"
                "- **watch_resume**(watcher_id): Resume a paused watcher\n"
                "- **watch_status**(watcher_id): Get watcher details\n"
                "- **watch_list**(status?): List all watchers\n"
                "- **watch_history**(watcher_id, limit?): View recent check results\n"
                "\n"
                "Use watchers to **monitor data sources over time**. A watcher periodically "
                "executes any tool and only notifies you when something interesting happens - "
                "saving tokens dramatically.\n"
                "\n"
                "**Escalation strategies** (notify_when):\n"
                "- `on_change` (default): notify only when result differs from previous\n"
                "- `on_error`: notify only on errors or recovery from error\n"
                "- `on_threshold`: notify when expression is true (e.g. `result.status_code != 200`)\n"
                "- `summary`: batch N checks then send summary\n"
                "- `always`: every check (debug only)\n"
                "\n"
                'Example: monitor an HTTP endpoint every 30s, notify only on change:\n'
                '  watch_start(name="http.get", params={"url": "https://api.example.com/status"}, '
                'interval=30, label="API health", notify_when="on_change")\n'
                "\n"
                "**IMPORTANT**: The `params` field must contain ALL required parameters for the tool. "
                "For example, http.get requires `url`, so params MUST include it: "
                '`params={"url": "https://..."}`.\n'
                "\n"
                "**One-shot timer / reminder**: Set `max_checks=1` to auto-stop after one execution. "
                "Combined with `notify_when=\"always\"`, this creates a delayed action:\n"
                '  watch_start(name="shell.run", params={"command": "date"}, '
                'interval=120, notify_when="always", max_checks=1, label="Reminder in 2 min")\n'
                "Use `max_checks=N` (N>0) to auto-stop after exactly N checks. 0 = unlimited (default).\n"
                "\n"
                "**AUTO-NOTIFICATION**: When a watcher's condition triggers, you receive:\n"
                '  [WATCHER UPDATE] watcher_id=abc123, label="API health", tool=http.get\n'
                '  Check #42 (interval: 30s, 3 notification(s) so far, strategy: on_change)\n'
                '  Result: {"status_code": 200, "body": "OK"}\n'
                "\n"
                "**CRITICAL - How to handle watcher notifications:**\n"
                "- When you receive a [WATCHER UPDATE], simply **report the result** to the user\n"
                "- Do **NOT** stop, restart, or recreate the watcher - it is still running correctly\n"
                "- The watcher will continue checking automatically at the configured interval\n"
                "- Only stop a watcher if the **user explicitly asks** you to stop it\n"
                '- Example response: "The API returned status 500 - this is a change from the previous 200."\n'
                "\n"
                "Watchers do NOT block the conversation - you can keep chatting normally. "
                "Use watch_pause/watch_resume to control them, watch_stop to remove them."
            ),
            "priority": 17,
        }]
