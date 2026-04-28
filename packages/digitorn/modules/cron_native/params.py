"""Parameter models for the 2 cron_native actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ScheduleParams(BaseModel):
    """Schedule a tool call to run later (one-shot or recurring).

    REQUIRED: when, action.
    OPTIONAL: args, name, output_channel, max_runs.

    Example calls:
        schedule(when="in 5m", action="shell.bash", args={"command": "ls"})
        schedule(when="0 9 * * 1-5", action="http.get", args={"url": "..."})
        schedule(when="2026-12-25T09:00:00Z", action="channels.send_message",
                 args={"channel": "email", "message": "..."})
    """

    when: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "REQUIRED. When to fire. THREE accepted formats: "
            "(1) RELATIVE delay one-shot - 'in 5m', 'in 2h', 'in 1d', 'in 30s'. "
            "(2) ISO 8601 timestamp one-shot - '2026-04-15T09:00:00Z' "
            "(must include the 'T' between date and time). "
            "(3) CRON expression recurring - 5 fields 'minute hour day month weekday'. "
            "Common cron patterns: '0 9 * * *' (every day 9am), "
            "'0 9 * * 1-5' (weekdays 9am), '*/15 * * * *' (every 15 min), "
            "'0 0 1 * *' (1st of each month at midnight). "
            "Weekday: 0=Sun, 1=Mon, ..., 6=Sat."
        ),
    )
    action: str = Field(
        ...,
        min_length=3,
        max_length=128,
        description=(
            "REQUIRED. Fully qualified tool name to invoke when the job "
            "fires, in 'module.action' format. Examples: 'http.get', "
            "'http.post', 'shell.bash', 'channels.send_message', "
            "'rag.query', 'web.search', 'filesystem.read'. Use the same "
            "name you would use to call the tool directly without scheduling."
        ),
    )
    args: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Parameters passed to the target tool when it fires. Same exact "
            "keys you would pass when calling the tool directly. Example: "
            "for action='http.get', use args={'url': 'https://...'}; for "
            "action='shell.bash', use args={'command': 'pytest tests/'}; for "
            "action='channels.send_message', use args={'channel': 'email', "
            "'message': '...'}."
        ),
    )
    name: str = Field(
        default="",
        max_length=64,
        description=(
            "Optional human-readable name for this job. If empty, a random "
            "id is generated. Reuse the SAME name to overwrite an existing "
            "job (useful when replacing a recurring schedule). Example: "
            "'weekly_report', 'daily_backup', 'health_check'."
        ),
    )
    output_channel: str = Field(
        default="",
        max_length=64,
        description=(
            "Optional output channel where the tool's result will be "
            "delivered when the job fires. Common values: 'email', 'slack', "
            "'webhook'. Leave empty (default) to deliver via the standard "
            "in-conversation llm_notification channel."
        ),
    )
    max_runs: int = Field(
        default=0,
        ge=0,
        description=(
            "Cap a recurring (cron) job at this many runs total. "
            "0 = unlimited (default). Ignored for one-shot jobs (ISO/'in X'). "
            "Example: max_runs=52 for a weekly job that runs for one year."
        ),
    )


class CancelScheduleParams(BaseModel):
    """Cancel a scheduled job by its job_id.

    Example call:
        cancel_schedule(job_id="cron_myapp_weekly_report")
    """

    job_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "REQUIRED. The exact job_id returned by `schedule()` in the "
            "`job_id` field of its result. Format is usually "
            "'cron_<app_id>_<name>' for named jobs or "
            "'cron_<app_id>_<random_hex>' for unnamed ones. Do NOT guess: "
            "use only ids you received from a previous schedule() call or "
            "that the user explicitly gave you."
        ),
    )


class RemindParams(BaseModel):
    """Schedule a self-reminder that wakes the current session later.

    Example call:
        remind(when="in 2m", message="Check the build status")
    """

    when: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "REQUIRED. When the reminder fires. Same THREE formats as "
            "`schedule()`: relative ('in 5m', 'in 2h', 'in 1d'), ISO 8601 "
            "('2026-04-15T09:00:00Z'), or cron expression ('0 9 * * 1-5')."
        ),
    )
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "REQUIRED. The reminder text. When the cron fires, this is "
            "injected back into the same session as a system message "
            "prefixed with [REMINDER from cron]. Write it as a clear "
            "instruction to your future self, e.g. 'Check whether the build "
            "passed' or 'Send the weekly summary to the user'."
        ),
    )
    name: str = Field(
        default="",
        max_length=64,
        description=(
            "Optional human-readable name for the reminder job. If empty, "
            "a random id is generated. Reuse the same name to overwrite an "
            "existing reminder."
        ),
    )
