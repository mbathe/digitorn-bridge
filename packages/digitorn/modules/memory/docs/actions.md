# Memory Module Actions

## Working Memory
- `set_goal` — Set the main objective
- `set_plan` — Set the step-by-step plan
- `update_plan_step` — Advance to a specific step
- `task_create` — Add a task (short: `TaskCreate`)
- `task_update` — Update task status (short: `TaskUpdate`)
- `track_entity` — Track an active entity

## Semantic Memory
- `remember` — Store a fact that survives context compaction (shared across workers)
- `add_fact` — (internal) Remember an important fact
- `add_relationship` — (internal) Add entity relationship

## Episodic Memory
- `add_episode` — Record a session summary

## Utility
- `get_snapshot` — Get the full memory view
- `cache_content` — Cache file content for O(1) access
