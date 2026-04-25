# Cron Native Module — Action Reference

## create_schedule

Create a named cron schedule.

**Parameters:**
- `name` (required): Unique schedule name (e.g. `daily-report`, `cleanup-weekly`).
- `cron_expr` (required): Cron expression (5/6/7 fields). Supports L/W/#/B modifiers.
- `action_type`: What to do when the schedule fires: `tool_call`, `llm_prompt`, `notification` (default: `tool_call`).
- `tool_name`: Tool to execute (`module.action` format). Required if action_type=`tool_call`.
- `tool_params`: Parameters for the tool call.
- `prompt`: Prompt/message for `llm_prompt` or `notification` actions.
- `timezone`: IANA timezone, e.g. `Europe/Paris`, `America/New_York` (default: `UTC`).
- `max_runs`: Max number of runs, 0 = unlimited (default: 0).
- `output_channel`: Output channel for results (default: `llm_notification`).
- `output_config`: Output channel config.
- `tags`: Tags for organizing schedules.
- `enabled`: Whether to activate immediately (default: true).

## update_schedule

Update an existing schedule's properties.

**Parameters:**
- `name` (required): Schedule name to update.
- `cron_expr`: New cron expression.
- `timezone`: New timezone.
- `tool_name`: New tool name.
- `tool_params`: New tool params.
- `prompt`: New prompt.
- `max_runs`: New max runs.
- `output_channel`: New output channel.
- `output_config`: New output config.
- `tags`: New tags.

## delete_schedule

Delete a named schedule and all its associated data.

**Parameters:**
- `name` (required): Schedule name to delete.
- `remove_dependencies`: Also remove any dependencies involving this schedule (default: true).

## list_schedules

List all schedules, optionally filtered.

**Parameters:**
- `status`: Filter by status: `active`, `paused`, `completed`.
- `tag`: Filter by tag.

## schedule_info

Get detailed information about a specific schedule.

**Parameters:**
- `name` (required): Schedule name.
- `include_history`: Include recent execution history (default: false).
- `history_limit`: Max history entries to return (default: 10, max: 100).

## explain_cron

Get a human-readable explanation of a cron expression.

**Parameters:**
- `cron_expr` (required): Cron expression to explain.

## validate_cron

Validate a cron expression syntax.

**Parameters:**
- `cron_expr` (required): Cron expression to validate.

## next_runs

Compute the next N fire times for a schedule or expression.

**Parameters:**
- `name`: Schedule name (uses its stored expression and timezone).
- `cron_expr`: Or provide a raw expression to test.
- `timezone`: Timezone for raw expressions (default: `UTC`).
- `count`: Number of next runs to compute (default: 10, max: 100).

## pause_schedule

Pause a running schedule.

**Parameters:**
- `name` (required): Schedule name to pause.

## resume_schedule

Resume a paused schedule.

**Parameters:**
- `name` (required): Schedule name to resume.

## run_now

Trigger an immediate execution of a schedule.

**Parameters:**
- `name` (required): Schedule name to trigger.
- `override_params`: Override tool params for this run only.

## execution_history

Retrieve execution history for a schedule.

**Parameters:**
- `name` (required): Schedule name.
- `limit`: Max entries to return (default: 20, max: 500).
- `status_filter`: Filter by result: `success`, `failure`, `skipped`.

## add_dependency

Add a dependency: schedule B runs only after schedule A completes.

**Parameters:**
- `schedule` (required): Schedule that depends on another.
- `depends_on` (required): Schedule that must complete first.
- `condition`: Condition: `success`, `failure`, `any` (default: `success`).

## remove_dependency

Remove a dependency between two schedules.

**Parameters:**
- `schedule` (required): Dependent schedule.
- `depends_on` (required): Dependency to remove.

## set_retry_policy

Configure retry behavior for a schedule on failure.

**Parameters:**
- `name` (required): Schedule name.
- `max_retries`: Max retry attempts after failure (default: 3, max: 50).
- `retry_delay`: Initial delay between retries in seconds (default: 60).
- `backoff_multiplier`: Exponential backoff multiplier (default: 2.0, max: 10.0).
- `max_retry_delay`: Max delay between retries in seconds (default: 3600).

## set_execution_window

Restrict when a schedule is allowed to execute.

**Parameters:**
- `name` (required): Schedule name.
- `start_time`: Window start time in HH:MM format (default: `00:00`).
- `end_time`: Window end time in HH:MM format (default: `23:59`).
- `days_of_week`: Allowed days, 0=Mon through 6=Sun (default: all days).
- `skip_holidays`: Skip execution on holidays (default: false).

## add_holiday

Add a holiday to the calendar (one-time or recurring).

**Parameters:**
- `date` (required): Date in ISO format (YYYY-MM-DD).
- `name` (required): Holiday name (e.g. `Christmas`, `Company Day Off`).
- `recurring`: True = same month/day every year (default: false).

## remove_holiday

Remove a holiday from the calendar.

**Parameters:**
- `date` (required): Date in ISO format (YYYY-MM-DD).

## list_holidays

List all configured holidays.

**Parameters:**
- `year`: Filter by year for one-time holidays. None = all.

## bulk_create

Create multiple schedules at once.

**Parameters:**
- `schedules` (required): List of schedule definitions. Each has the same fields as `create_schedule`.

## calendar_view

Generate a calendar view of all scheduled runs in a date range.

**Parameters:**
- `start_date` (required): Start date (YYYY-MM-DD).
- `end_date` (required): End date (YYYY-MM-DD).
- `schedules`: Filter to specific schedule names. None = all.
