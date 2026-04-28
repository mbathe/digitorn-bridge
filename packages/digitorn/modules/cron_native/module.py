"""Cron native - single ``schedule()`` action backed by croniter + JobStore.

Design: 2 actions, no enterprise features (DAG, holidays, retry policies,
execution windows, calendar view). The whole point of this module is to
give the LLM ONE primitive that schedules anything, and ONE primitive
that cancels it. Everything else lives on the existing SchedulerService
+ JobStore infrastructure already wired in the daemon.

The ``when`` field accepts three formats, parsed in order:

1. **Cron expression** (5 fields): ``"0 9 * * 1-5"`` → recurring
2. **ISO 8601 timestamp**: ``"2026-04-15T09:00:00Z"`` → one-shot
3. **Relative offset**: ``"in 5m"`` / ``"in 2h"`` / ``"in 1d"`` → one-shot

For richer formats ("tomorrow at 9am") the LLM converts to ISO itself.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from croniter import croniter
from pydantic import BaseModel, Field

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest

from .params import CancelScheduleParams, RemindParams, ScheduleParams

logger = logging.getLogger(__name__)


# ── Config model (compile-time validation via CONFIG_MODEL) ──────


class CronNativeConfig(BaseModel):
    """Pydantic config for the cron_native module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")


# case-sensitive: s=seconds, m=minutes, h=hours, d=days (no uppercase M=months)
_RELATIVE_RE = re.compile(r"^\s*in\s+(\d+)\s*([smhd])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# Bounds for relative and one-shot schedules, to avoid DoS and absurd schedules.
_MIN_DELAY_SECONDS = 1                # reject "in 0s"
_MAX_DELAY_SECONDS = 10 * 365 * 86400  # 10 years
# Grace period when checking that an ISO timestamp isn't in the past.
_PAST_GRACE_SECONDS = 5


def _parse_when(when: str, now: datetime) -> tuple[str, str | None, str | None]:
    """Parse a ``when`` string into ``(schedule_type, next_run_iso, cron_expr)``.

    schedule_type is one of ``"cron"`` or ``"once"``. For cron, ``cron_expr``
    is set and ``next_run_iso`` is the first fire time. For one-shot,
    ``cron_expr`` is None and ``next_run_iso`` is the absolute ISO time.

    Raises ``ValueError`` if the format isn't recognized or violates bounds.
    """
    s = when.strip()
    if not s:
        raise ValueError("when cannot be empty")

    # 1. Relative ("in 5m" / "in 2h" / "in 1d")
    m = _RELATIVE_RE.match(s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delay = amount * _UNIT_SECONDS[unit]
        if delay < _MIN_DELAY_SECONDS:
            raise ValueError(
                f"Relative delay too short: {when!r} resolves to {delay}s. "
                f"Minimum is {_MIN_DELAY_SECONDS}s. Just do the work inline instead.",
            )
        if delay > _MAX_DELAY_SECONDS:
            raise ValueError(
                f"Relative delay too large: {when!r} resolves to {delay}s. "
                f"Maximum is {_MAX_DELAY_SECONDS}s (~10 years).",
            )
        target = now + timedelta(seconds=delay)
        return "once", target.isoformat(), None

    # 2. ISO 8601 - reject past timestamps.
    if s[0].isdigit() and "-" in s and "T" in s.upper():
        try:
            iso = s.replace("Z", "+00:00")
            target = datetime.fromisoformat(iso)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            # Reject past timestamps (5s grace for clock skew).
            if target < now - timedelta(seconds=_PAST_GRACE_SECONDS):
                raise ValueError(
                    f"ISO timestamp is in the past: {when!r} "
                    f"({target.isoformat()} < now={now.isoformat()}). "
                    f"Schedule cannot fire backwards.",
                )
            if (target - now).total_seconds() > _MAX_DELAY_SECONDS:
                raise ValueError(
                    f"ISO timestamp too far in the future: {when!r}. "
                    f"Maximum horizon is {_MAX_DELAY_SECONDS}s (~10 years).",
                )
            return "once", target.isoformat(), None
        except ValueError as exc:
            # Clock/range errors are fatal - don't fall through to cron which
            # would produce confusing errors for what is clearly an ISO intent.
            if "past" in str(exc) or "future" in str(exc):
                raise
            # fall through to cron parsing for actual parse errors

    # 3. Cron expression. We require EXACTLY 5 fields (minute hour dom month dow)
    #    matching the public contract. croniter accepts 6/7 (seconds, year) but
    #    those open the door to per-second scheduling (DoS vector) and aren't
    #    part of the documented surface.
    if not s.startswith("@"):
        field_count = len(s.split())
        if field_count != 5:
            raise ValueError(
                f"Invalid 'when' format: {when!r}. Cron must have exactly 5 "
                f"fields (minute hour day month weekday), got {field_count}. "
                f"Examples: '0 9 * * *' (daily 9am), '*/15 * * * *' (every 15min).",
            )
    try:
        cron = croniter(s, now)
        nxt = cron.get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=timezone.utc)
        return "cron", nxt.isoformat(), s
    except (ValueError, KeyError) as exc:
        raise ValueError(
            f"Invalid 'when' format: {when!r}. Expected cron expression "
            f"(e.g. '0 9 * * 1-5'), ISO 8601 (e.g. '2026-04-15T09:00:00Z'), "
            f"or relative offset (e.g. 'in 5m', 'in 2h', 'in 1d'). "
            f"Parser error: {exc}",
        ) from exc


def _validate_action_name(action: str) -> None:
    """Validate that ``action`` has 'module.action' shape with non-empty sides.

    Raises ``ValueError`` otherwise. Called from schedule() before registering.
    """
    if "." not in action:
        raise ValueError(
            f"action must be 'module.action' format, got {action!r}",
        )
    module, _, act = action.partition(".")
    if not module or not act:
        raise ValueError(
            f"action 'module.action' halves must be non-empty, got {action!r}",
        )


class CronNativeModule(BaseModule):
    """Two ultra-powerful actions: schedule + cancel_schedule."""

    MODULE_ID = "cron_native"
    VERSION = "2.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = CronNativeConfig

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=self.MODULE_ID,
            version=self.VERSION,
            description="Native cron scheduling for background apps.",
            actions=[entry.spec for entry in self._action_registry.values()],
        )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._app_id: str = "default"
        self._active_session_id: str | None = None

    async def on_start(self) -> None:
        self._app_id = getattr(self, "_app_id_override", "default")
        logger.info("cron_native_started app=%s", self._app_id)

    async def on_stop(self) -> None:
        logger.info("cron_native_stopped app=%s", self._app_id)

    def set_active_session(self, session_id: str | None) -> None:
        self._active_session_id = session_id

    # ── Internal helpers ────────────────────────────────────────────

    def _scheduler(self) -> Any:
        ctx = getattr(self, "_context", None)
        return getattr(ctx, "scheduler", None) if ctx else None

    def _job_store(self) -> Any:
        ctx = getattr(self, "_context", None)
        return getattr(ctx, "job_store", None) if ctx else None

    def _make_job_id(self, name: str | None) -> str:
        suffix = name.strip() if name and name.strip() else uuid.uuid4().hex[:12]
        return f"cron_{self._app_id}_{suffix}"

    # ── Public actions ──────────────────────────────────────────────

    @action(
        description=(
            "Schedule any tool to run later - once or recurring. ONE action "
            "covers all scheduling needs. Pick the `when` format that fits: "
            "natural delay ('in 5m'), exact moment (ISO 8601), or recurrence "
            "(cron). The tool fires automatically at that time and the result "
            "is delivered back."
        ),
        tool_prompt=(
            "# schedule - fire any tool at a future time\n"
            "\n"
            "Use this when the user asks for ANYTHING that should happen "
            "later, on a delay, or on a schedule. Reminders, recurring "
            "reports, follow-ups, retries, batch jobs, periodic checks - "
            "they all go through this single action.\n"
            "\n"
            "## How to choose the `when` format\n"
            "\n"
            "Pick the simplest format that matches the user's intent:\n"
            "\n"
            "**1. Relative delay** - when the user says 'in N minutes/hours/days':\n"
            "   `when='in 5m'`   → fires once in 5 minutes\n"
            "   `when='in 2h'`   → fires once in 2 hours\n"
            "   `when='in 1d'`   → fires once in 24 hours\n"
            "   `when='in 30s'`  → fires once in 30 seconds\n"
            "\n"
            "**2. ISO 8601 timestamp** - when the user gives an exact date/time:\n"
            "   `when='2026-04-15T09:00:00Z'`        → April 15, 9am UTC\n"
            "   `when='2026-12-25T18:30:00+01:00'`   → with timezone\n"
            "   For natural language like 'tomorrow at 9am', YOU compute the\n"
            "   ISO yourself before calling. Use the current date you know.\n"
            "\n"
            "**3. Cron expression** - for recurring schedules. Format is the\n"
            "   standard 5-field cron `minute hour day month weekday`:\n"
            "   `when='0 9 * * *'`     → every day at 9:00\n"
            "   `when='0 9 * * 1-5'`   → weekdays at 9:00\n"
            "   `when='0 9 * * 1'`     → every Monday at 9:00\n"
            "   `when='*/15 * * * *'`  → every 15 minutes\n"
            "   `when='0 * * * *'`     → every hour on the hour\n"
            "   `when='0 0 1 * *'`     → 1st day of every month at midnight\n"
            "   `when='30 14 * * 0'`   → every Sunday at 14:30\n"
            "   `when='0 9-17 * * 1-5'` → every hour 9am-5pm, weekdays\n"
            "   Weekday: 0=Sun, 1=Mon, ..., 6=Sat. Range: `1-5`. Step: `*/15`.\n"
            "   List: `1,3,5`. Combine: `0 9 1,15 * *` = 1st and 15th at 9am.\n"
            "\n"
            "## The `action` parameter\n"
            "\n"
            "The fully-qualified tool name in `module.action` format. Examples:\n"
            "  - `'shell.bash'`              → run a shell command\n"
            "  - `'http.get'` / `'http.post'` → call an API\n"
            "  - `'channels.send_message'`   → deliver via email/slack/etc.\n"
            "  - `'rag.query'`               → run a knowledge base query\n"
            "  - `'web.search'`              → web search\n"
            "  - `'filesystem.read'`         → read a file\n"
            "Same module.action you would call directly without scheduling.\n"
            "\n"
            "## The `args` parameter\n"
            "\n"
            "The dict you would normally pass to that tool. Same exact keys.\n"
            "Example: if `http.get` takes `{'url': '...'}`, then\n"
            "`schedule(action='http.get', args={'url': '...'})` works the same.\n"
            "\n"
            "## Full examples - copy these patterns\n"
            "\n"
            "### Reminder in N minutes (most common case)\n"
            "    schedule(\n"
            "        when='in 10m',\n"
            "        action='channels.send_message',\n"
            "        args={'channel': 'llm_notification',\n"
            "              'message': 'Time to check the build status.'}\n"
            "    )\n"
            "\n"
            "### Run a shell command in 1 hour\n"
            "    schedule(\n"
            "        when='in 1h',\n"
            "        action='shell.bash',\n"
            "        args={'command': 'pytest tests/ -v'}\n"
            "    )\n"
            "\n"
            "### Daily report at 9am every weekday\n"
            "    schedule(\n"
            "        when='0 9 * * 1-5',\n"
            "        action='http.get',\n"
            "        args={'url': 'https://api.example.com/daily-report'},\n"
            "        name='weekday_report'\n"
            "    )\n"
            "\n"
            "### Weekly digest every Monday morning, capped at 52 runs\n"
            "    schedule(\n"
            "        when='0 8 * * 1',\n"
            "        action='rag.query',\n"
            "        args={'kb': 'inbox', 'query': 'summarize last week'},\n"
            "        name='monday_digest',\n"
            "        max_runs=52\n"
            "    )\n"
            "\n"
            "### One-shot at an exact ISO timestamp\n"
            "    schedule(\n"
            "        when='2026-12-25T09:00:00Z',\n"
            "        action='channels.send_message',\n"
            "        args={'channel': 'email', 'to': 'user@example.com',\n"
            "              'subject': 'Merry Xmas', 'message': 'Happy holidays!'}\n"
            "    )\n"
            "\n"
            "### Health check every 5 minutes, deliver to slack\n"
            "    schedule(\n"
            "        when='*/5 * * * *',\n"
            "        action='http.get',\n"
            "        args={'url': 'https://my-api/health'},\n"
            "        output_channel='slack',\n"
            "        name='health_check'\n"
            "    )\n"
            "\n"
            "## Returns\n"
            "\n"
            "On success: `{job_id, schedule_type, next_run, label, cron_expr?}`.\n"
            "Always remember the `job_id` - you need it to cancel later.\n"
            "If the same `name` is reused, the existing job is overwritten.\n"
            "\n"
            "## Recurring vs one-shot\n"
            "\n"
            "- Cron format → schedule_type='cron', fires every match\n"
            "- ISO 8601 or 'in X' → schedule_type='once', fires exactly once\n"
            "- Use `max_runs` to cap a cron schedule (0 = unlimited)\n"
            "\n"
            "## When NOT to use schedule\n"
            "\n"
            "- For something that should happen RIGHT NOW: just call the tool\n"
            "  directly. Don't schedule(when='in 0s', ...).\n"
            "- For 'remind me to keep this fact in mind during this conversation':\n"
            "  use `memory.remember` instead - that's not a schedule.\n"
            "- For very short delays (< 30s): consider whether you really need\n"
            "  scheduling vs just continuing the work inline.\n"
            "\n"
            "## Common mistakes\n"
            "\n"
            "- Cron uses `*` for 'any value', not blank. `'9 * * *'` is INVALID\n"
            "  (only 4 fields). Always 5 fields: minute hour dom month dow.\n"
            "- 'Every weekday' = `1-5` (Mon-Fri), not `0-4` and not `MON-FRI`.\n"
            "- 'Every hour at minute 0' = `0 * * * *`, not `* 0 * * *`.\n"
            "- ISO timestamps must include the 'T' between date and time:\n"
            "  `'2026-04-15T09:00:00Z'` ✓, `'2026-04-15 09:00:00'` ✗.\n"
            "- The `args` dict keys must match the target tool's actual params.\n"
            "  Read the target tool's signature first if unsure."
        ),
        params_model=ScheduleParams,
        risk_level="low",
        tags=["scheduler", "cron"],
        display_verb="Schedule",
        display_detail_param="when",
        display_icon="tool",
        display_category="action",
    )
    async def schedule(self, params: ScheduleParams) -> ActionResult:
        scheduler = self._scheduler()
        job_store = self._job_store()
        if scheduler is None or job_store is None:
            return ActionResult(
                success=False,
                error="Scheduler not available - daemon not fully booted.",
            )

        try:
            _validate_action_name(params.action)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))

        now = datetime.now(timezone.utc)
        try:
            schedule_type, next_run, cron_expr = _parse_when(params.when, now)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))

        # max_runs only makes sense for cron. For one-shot, reject non-zero
        # so the LLM doesn't get a silently-ignored cap.
        if schedule_type == "once" and params.max_runs and params.max_runs != 1:
            return ActionResult(
                success=False,
                error=(
                    f"max_runs={params.max_runs} is meaningless for a one-shot "
                    f"job (when={params.when!r} is relative/ISO, fires exactly "
                    f"once). Either remove max_runs or use a cron expression."
                ),
            )

        from digitorn.core.app.job_store import ScheduledJob

        job_id = self._make_job_id(params.name)

        # Name reuse: a prior job with the same name must be unregistered from
        # the in-memory scheduler before re-registering, otherwise the old
        # trigger stays active and the new one is added on top → double firing.
        try:
            scheduler.unregister_job(self._app_id, job_id)
        except Exception:
            pass  # old job may not exist; that's the common case

        job = ScheduledJob(
            job_id=job_id,
            app_id=self._app_id,
            schedule_type=schedule_type,
            action_type="tool_call",
            cron_expr=cron_expr,
            run_at=next_run if schedule_type == "once" else None,
            tool_name=params.action,
            tool_params=params.args,
            label=params.name or job_id,
            output_channel=params.output_channel or "llm_notification",
            session_id=self._active_session_id,
            status="active",
            max_runs=params.max_runs,
            next_run_at=next_run,
        )
        try:
            job_store.put_job(job)
            scheduler.register_job(job)
        except Exception as exc:
            return ActionResult(success=False, error=f"Failed to register job: {exc}")

        data: dict[str, Any] = {
            "job_id": job_id,
            "schedule_type": schedule_type,
            "next_run": next_run,
            "label": job.label,
        }
        if cron_expr:
            data["cron_expr"] = cron_expr
        return ActionResult(success=True, data=data)

    @action(
        description=(
            "Cancel a previously scheduled job. Pass the `job_id` returned "
            "by `schedule()`. After cancellation the job is removed from "
            "the scheduler and will never fire again."
        ),
        tool_prompt=(
            "# cancel_schedule - remove a scheduled job\n"
            "\n"
            "Use this when the user asks to stop a scheduled job, change "
            "their mind about a reminder, or you need to replace a recurring "
            "job (cancel the old one, then `schedule()` a new one).\n"
            "\n"
            "## Parameter\n"
            "\n"
            "`job_id` - the exact value returned by `schedule()` in the\n"
            "`job_id` field of its result. Format is usually\n"
            "`cron_<app_id>_<name>` for named jobs, or\n"
            "`cron_<app_id>_<random_hex>` for unnamed ones.\n"
            "\n"
            "## Examples\n"
            "\n"
            "### Cancel a job you just created\n"
            "    result = schedule(when='in 1h', action='shell.bash',\n"
            "                      args={'command': 'pytest'})\n"
            "    # later, user changes their mind:\n"
            "    cancel_schedule(job_id=result.data['job_id'])\n"
            "\n"
            "### Cancel a named recurring job\n"
            "    cancel_schedule(job_id='cron_myapp_weekly_report')\n"
            "\n"
            "### Replace a recurring job (cancel + recreate)\n"
            "    cancel_schedule(job_id='cron_myapp_daily_backup')\n"
            "    schedule(when='0 3 * * *',  # new schedule\n"
            "             action='shell.bash',\n"
            "             args={'command': 'backup.sh'},\n"
            "             name='daily_backup')\n"
            "\n"
            "## Returns\n"
            "\n"
            "On success: `{job_id, cancelled: True, ran_count}`.\n"
            "`ran_count` tells you how many times the job had already fired\n"
            "before being cancelled (useful for reporting to the user).\n"
            "\n"
            "## When NOT to use\n"
            "\n"
            "- Don't call this for a job that fired only once (one-shot jobs\n"
            "  auto-complete and are removed by the scheduler - you don't\n"
            "  need to cancel them).\n"
            "- Don't call this with a guessed job_id. Only use ids you got\n"
            "  back from a previous `schedule()` call in this same session\n"
            "  or that the user explicitly gave you."
        ),
        params_model=CancelScheduleParams,
        risk_level="low",
        tags=["scheduler", "cron"],
        display_verb="Cancel schedule",
        display_detail_param="job_id",
        display_icon="tool",
        display_category="action",
    )
    async def cancel_schedule(self, params: CancelScheduleParams) -> ActionResult:
        scheduler = self._scheduler()
        job_store = self._job_store()
        if scheduler is None or job_store is None:
            return ActionResult(
                success=False,
                error="Scheduler not available - daemon not fully booted.",
            )

        job = job_store.get_job(self._app_id, params.job_id)
        if job is None:
            return ActionResult(
                success=False,
                error=f"Job {params.job_id!r} not found for app {self._app_id!r}",
            )

        scheduler.unregister_job(self._app_id, params.job_id)
        job_store.delete_job(self._app_id, params.job_id)

        return ActionResult(success=True, data={
            "job_id": params.job_id,
            "cancelled": True,
            "ran_count": job.run_count,
        })

    @action(
        description=(
            "Schedule a self-reminder. When the time comes, the daemon "
            "wakes the SAME session you are in right now, reloads its "
            "full context (history, memory, goal), and injects your "
            "message as a system reminder so you can act on what you "
            "promised. The session resumes naturally even if the user "
            "had closed it in the meantime."
        ),
        tool_prompt=(
            "# remind - schedule a self-reminder that wakes this session\n"
            "\n"
            "Use this when YOU need to be reminded to do something later "
            "in the SAME conversation. The fired reminder reloads the full "
            "session (all messages, memory, goal, todos) and re-injects "
            "you as if you just received a system message - so you can "
            "pick up exactly where you left off and execute what you "
            "committed to.\n"
            "\n"
            "## When to use this vs schedule()\n"
            "\n"
            "- Use `remind` when YOU (the agent) need to come back to a\n"
            "  task in the same conversation. The session is woken with\n"
            "  full context.\n"
            "- Use `schedule` when you need to fire a SPECIFIC TOOL CALL\n"
            "  (e.g. an HTTP request, a shell command) at a future time,\n"
            "  with no need to resume the conversation.\n"
            "- Use `memory.remember` when you want to STORE A FACT that\n"
            "  survives compaction but doesn't need to fire at a time.\n"
            "\n"
            "## Format of `when`\n"
            "\n"
            "Same three formats as `schedule()`:\n"
            "  - Relative: 'in 5m', 'in 2h', 'in 1d', 'in 30s'\n"
            "  - ISO 8601: '2026-04-15T09:00:00Z'\n"
            "  - Cron: '0 9 * * 1-5' (recurring)\n"
            "\n"
            "## Examples\n"
            "\n"
            "### User asks you to ping them in 2 minutes\n"
            "    remind(when='in 2m', message='Time to check the build status.')\n"
            "\n"
            "### You started a long-running build, want to check on it later\n"
            "    remind(when='in 10m',\n"
            "           message='Check whether the npm build finished and report back.')\n"
            "\n"
            "### Daily standup reminder for a recurring agent\n"
            "    remind(when='0 9 * * 1-5',\n"
            "           message='Pull the latest commits and summarize the team work.',\n"
            "           name='daily_standup')\n"
            "\n"
            "### Follow-up after a user request\n"
            "    remind(when='in 1h',\n"
            "           message='Follow up on the deployment and confirm it is healthy.')\n"
            "\n"
            "## What happens at fire time\n"
            "\n"
            "1. The daemon reloads your session (history + memory + goal)\n"
            "2. Your `message` is injected as: '[REMINDER from cron] ...'\n"
            "3. A new turn starts with you having full context\n"
            "4. You see WHY you set the reminder and act on it\n"
            "\n"
            "## Returns\n"
            "\n"
            "{job_id, next_run, label}. Save job_id if you want to cancel\n"
            "via cancel_schedule(job_id) before it fires.\n"
            "\n"
            "## When NOT to use\n"
            "\n"
            "- Don't use for very short delays (< 30s): just keep working.\n"
            "- Don't use to fire a tool call without resuming conversation:\n"
            "  use `schedule` instead.\n"
            "- Don't use to store a passive fact: use `memory.remember`."
        ),
        params_model=RemindParams,
        risk_level="low",
        tags=["scheduler", "cron", "reminder"],
        display_verb="Remind me",
        display_detail_param="when",
        display_icon="tool",
        display_category="action",
    )
    async def remind(self, params: RemindParams) -> ActionResult:
        scheduler = self._scheduler()
        job_store = self._job_store()
        if scheduler is None or job_store is None:
            return ActionResult(
                success=False,
                error="Scheduler not available - daemon not fully booted.",
            )

        if not self._active_session_id:
            return ActionResult(
                success=False,
                error=(
                    "remind() requires an active session. The daemon did "
                    "not bind a session_id to this turn."
                ),
            )

        now = datetime.now(timezone.utc)
        try:
            schedule_type, next_run, cron_expr = _parse_when(params.when, now)
        except ValueError as exc:
            return ActionResult(success=False, error=str(exc))

        from digitorn.core.app.job_store import ScheduledJob

        job_id = self._make_job_id(params.name)

        # Name reuse: unregister the prior job from scheduler before re-adding
        # to avoid leaving a stale trigger that fires alongside the new one.
        try:
            scheduler.unregister_job(self._app_id, job_id)
        except Exception:
            pass  # old job may not exist; that's the common case

        job = ScheduledJob(
            job_id=job_id,
            app_id=self._app_id,
            schedule_type=schedule_type,
            action_type="llm_prompt",
            cron_expr=cron_expr,
            run_at=next_run if schedule_type == "once" else None,
            prompt=params.message,
            label=params.name or job_id,
            output_channel="llm_notification",
            session_id=self._active_session_id,
            status="active",
            next_run_at=next_run,
        )
        try:
            job_store.put_job(job)
            scheduler.register_job(job)
        except Exception as exc:
            return ActionResult(success=False, error=f"Failed to register reminder: {exc}")

        return ActionResult(success=True, data={
            "job_id": job_id,
            "schedule_type": schedule_type,
            "next_run": next_run,
            "label": job.label,
            "session_id": self._active_session_id,
        })
