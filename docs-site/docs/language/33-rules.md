---
id: rules
title: Project Memory
sidebar_label: Project Memory
---

> Previously titled "Rules". The `runtime.project_memory` YAML
> field name is unchanged.

Digitorn loads a single **project memory** file into the agent's
system prompt at the start of every turn. It's the standard place
for project-specific conventions ("use ruff, not flake8", "all
modules go under `src/`", "tests live in `tests/`", ...).

> **The `.digitorn/rules/python.md` modular system referenced in
> older docs does not exist.** The actual implementation is one
> file at the project root, not a directory of modular rules.

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## How it works


`_load_project_memory`. At every turn boot:

1. The runtime checks `runtime.project_memory` (default `"auto"`).
2. With `auto`, it searches in this order:
   - `.digitorn/apps/{app_id}/.digitorn.md` - app-specific memory.
   - `.digitorn.md` in the workspace root - global project memory.
3. With a custom path (`runtime.project_memory: docs/AGENTS.md`),
   it loads that file directly.
4. The file is read and capped at **4 000 chars** (~1 000 tokens -
   `_PROJECT_MEMORY_MAX_CHARS = 4000`).
5. The content is prepended to the agent's system prompt under a
   `# Project Memory` header.

Returning `None` (file missing or `setting=""`) skips the
injection entirely.

## Configuration

```yaml
runtime:
  # Default: search the auto path
  project_memory: auto

  # Or pin to a specific file (workspace-relative)
  # project_memory: docs/AGENTS.md

  # Or disable
  # project_memory: ""
```

`runtime.project_memory` is a string, default `"auto"`
().

## Auto-load search order

 With `setting: auto`:

| Order | Path | Notes |
|-------|------|-------|
| 1 | `.digitorn/apps/<app_id>/.digitorn.md` | App-specific. Always safe to auto-load - lives in a per-app namespaced directory. |
| 2 | `<workspace>/.digitorn.md` | Project-wide memory. Explicitly namespaced for the framework. |

> **Security note.** `CLAUDE.md` and `README.md` in the workspace
> root are **not** auto-loaded under `auto`
>. Earlier versions did include them -
> this caused a cross-user leak when the daemon was launched from a
> developer's repo (the repo's `CLAUDE.md` containing internal
> architecture notes, paths, OAuth credentials was being silently
> injected into every session of generic conversational apps).
> Pin them explicitly via `runtime.project_memory: CLAUDE.md`
> if you really want them.

## File format - plain markdown

The project memory file is plain markdown. The runtime doesn't
parse it; it's inlined verbatim under the `# Project Memory`
header in the system prompt.

Practical content for `.digitorn.md`:

```markdown
# Project conventions

## Style
- Use ruff for linting; never flake8.
- 4-space indent, no tabs.
- Type hints on every public function.

## Layout
- `src/<package>/` - production code.
- `tests/` - pytest tests; mirror the src/ layout.
- `docs/` - markdown docs; one topic per file.

## Test command
- `pytest -x` (fail fast)
- `pytest tests/integration/ -m slow` for the slow suite

## Anti-patterns
- DO NOT add a new top-level dependency without discussion.
- DO NOT skip tests with @pytest.mark.skip - fix or delete.
```

The 4 000-char cap is enforced silently (
`_truncate_project_memory`). Write more if you need to - the
truncation just keeps the most informative leading content.

## Per-app memory

For multi-app deployments where each app needs different
conventions, use `.digitorn/apps/<app_id>/.digitorn.md`. The
runtime checks this path **first** (before the global
`.digitorn.md`) and loads it instead when present.

```text
my-workspace/
├── .digitorn.md                              # global rules
└── .digitorn/
    └── apps/
        ├── code-reviewer/.digitorn.md        # reviewer-specific rules
        └── doc-writer/.digitorn.md           # doc-writer-specific rules
```

The `WorkspaceLayout` helper computes
`layout.app_memory_file` for each `app_id` - that's the file
checked at step 1.

## Custom path

When neither default works for the project, set an explicit path:

```yaml
runtime:
  project_memory: docs/AGENT_INSTRUCTIONS.md
```

The path is resolved relative to the workspace root
(). Must exist as a file at compile-time
deploy; missing files return `None` (no error, no injection).

## Disabling

```yaml
runtime:
  project_memory: ""        # empty string disables the feature
```

When disabled, the agent's system prompt has no `# Project Memory`
section. Useful for stateless apps (a one-shot summarizer that
shouldn't be biased by repo-specific conventions).

## Where it lands in the prompt

 The injection is prepended to the
agent's user-prompt-time system block:

```text
# Project Memory

<file content here, capped at 4000 chars>

<rest of the system prompt assembled by build_system_prompt:
 identity / tool-discovery / skills / memory / behavior>
```

Project memory always runs first, before tool delivery and
skills. The agent reads it as ambient context for every decision.

## Cross-references

- App-config field reference (`runtime.project_memory`):
  [App Configuration → runtime](02-app-config.md#runtime---lifecycle-and-execution-policy)
- Workspace layout helper:
   `WorkspaceLayout`
- Cognitive memory (separate, in-process - survives compaction):
  [Cognitive Memory](05-memory.md)
- Bundle-side prompt fragments (different from project memory -
  authored at build time, not loaded from the workspace):
  [Bundle namespaces](38-bundle-namespaces.md)
- Behavior engine (runtime rule enforcement, separate from
  prompt-level memory):
  [Behavior Engine](43-behavior.md)
