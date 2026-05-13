# cron

Worker-hosted background scheduler. Hosts the activation sweep loop and
related periodic maintenance tasks **outside** the daemon's main asyncio
loop so the daemon never blocks on Postgres queries running on its own
schedule.

When you list `cron` in a worker's `modules:` list, the worker:

1. Acquires a file-based leader lock at `~/.digitorn/.cron-leader.lock`
   so only one instance is active cluster-wide.
2. Starts the activation sweep loop (marks zombie `running` activations
   as failed every 60 s).
3. Renews its lease every 30 s.

When `cron` is **not** hosted by a worker (the default), the daemon
runs the sweep in its own lifespan exactly as today.

This module exposes no `@action` to the LLM. It is a pure background
host. The leader file is co-located with the workers shared secret in
`~/.digitorn/`.

## Status

Phase 5 ships the activation-sweep loop. Future work:

- OAuth refresh loop (currently still daemon-side, depends on
  `credential_store`).
- File watchers (config / yaml reload).
- Inbox/queue reapers.

These move to the cron module as their daemon-side dependencies are
unblocked.
