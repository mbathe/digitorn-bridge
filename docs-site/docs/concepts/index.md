---
id: concepts-index
title: Concepts
---

# Concepts

The mental models behind Digitorn. Read these to understand *why*
the framework looks the way it does; for *what* each field does,
go to [Reference](/docs/reference/) or [Language](/docs/language/).

| Concept | Why it matters |
|---------|---------------|
| [App packages](app-packages.md) | The canonical packaging format (`package.toml` + `app.yaml`), how versioning works, how the Hub resolves dependencies. |
| [Widgets](widgets.md) | End-to-end wiring of declarative UI: where the tree lives, how bindings resolve, how actions dispatch back to the agent. |

## Cross-cutting ideas

A few patterns are repeated across the codebase. Knowing them up
front makes everything else click.

### One tool, N modes

Many tools that look like a tool family are actually a single tool
with branching behaviour controlled by params. The `Agent` spawn
tool (8 modes via params), `background_run` (5 modes), the file
edit tool's fuzzy matcher, the workspace `WsRead` reader (offset
and limit selection). When you see this pattern, look for the
"mode hint" parameter in the params model.

### `extra: forbid` on every YAML block

Every Pydantic model in and
declares `model_config = {"extra": "forbid"}`. This is a deliberate
correctness choice: a typo in a YAML key is a compile error, not a
silent default. If you see "Extra inputs are not permitted" with a
list of accepted keys, the schema is showing you exactly where
the typo is.

### Compile time vs run time

The boundary is sharp. Anything that depends only on `app.yaml` +
`os.environ` + the bundle directory + the daemon's catalog is
compile-time. Anything that depends on a live LLM, a fired trigger,
a live tool result is run-time. The doc and the schema reflect this:
a `runtime.X` field never resolves at compile, a `dev.variables`
template always does.

### Modules are stateful, slots are typed

A module is a single Python class that owns its state for the
life of the daemon (`isolation: shared`) or per-app
(`isolation: per_app`). A slot is the typed declaration of what
config a module accepts and what credential it consumes. The
distinction matters when you reason about "two apps share the
same vector index": that's a `shared` module that exposes a
per-app override (see how the rag module reuses the same Qdrant
collection across apps but isolates its in-memory KB list per app).

### Direct vs FQN names

The LLM sees short PascalCase tool names (`Bash`, `Write`, `Edit`,
`Agent`, `Remember`). Internally the daemon dispatches via FQNs
(`shell.bash`, `filesystem.write`, `filesystem.edit`, `agent_spawn.spawn`,
`memory.remember`). YAML constraints and capabilities use the FQN
suffix (`{ shell: [bash] }`). The mapping is one source of truth:
[](https://github.com/).
