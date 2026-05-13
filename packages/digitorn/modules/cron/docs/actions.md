# cron actions

The cron module exposes **no actions** to the LLM. It is a pure
background-task host: its work happens inside `on_start` /
`on_stop`, not in response to tool calls.

This documentation file exists so the standard module loader's doc
validation passes. If a future iteration adds a status / introspection
action (e.g. `cron.list_jobs`), document it here.

## Background jobs run

| Job | Interval | Source |
|---|---|---|
| activation sweep | 60 s | ports the daemon's `_activation_sweeper` (sweep stuck-running rows) |

## Leader election

Single-leader via `~/.digitorn/.cron-leader.lock`. Multiple workers
listing `cron` in their `modules:` is supported -- the second worker
detects the held lease and stays idle. If the leader dies (process
crash or worker restart), the lease becomes stale after 90 s and the
next probing instance can take over.
