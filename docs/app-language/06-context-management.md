---
id: context-management
---

# Context Management

LLM context windows are finite. As the conversation grows, the
context fills up. Digitorn's context-management layer keeps the
agent's input under the model's limit by automatic **compaction**
and provides emergency recovery when an overflow slips through.

Every behaviour and field on this page maps to real code.

## What lives in the context

A typical agent context per turn:

| Section | Tokens (typical) | Source |
|---------|------------------|--------|
| System prompt + identity | 500-2 000 | per-agent `system_prompt` + memory block + tool-discovery instructions |
| Tool schemas | 0-15 000 | depends on `runtime.tool_injection` (direct / compact / discovery) |
| Conversation history | grows | every user/assistant/tool message since session start |
| Memory snapshot | 100-500 | rendered by `memory.get_prompt_sections()` |

When the **token pressure** (used / total) crosses the configured
threshold, the runtime fires a compaction pass that rewrites the
conversation history in place.

## `runtime.context` — the configuration block

`schema.py:759` `ContextConfig` (`extra: forbid`). Eight fields. The
full reference is in
[App Configuration → runtime.context](02-app-config.md#runtimecontext--context-window-management);
this page focuses on the behaviour each field controls.

```yaml
runtime:
  context:
    max_tokens: 200000          # 0 = auto-detect from provider
    output_reserved: 4096       # tokens reserved for the LLM reply
    strategy: summarize         # truncate | summarize
    keep_recent: 10             # most-recent messages preserved verbatim
    compression_trigger: 0.75   # pressure ratio (0.0-1.0) that fires compaction
    summary_max_tokens: 1024    # cap on the synthesised summary
    auto_compact: true          # auto-inject a context_pressure hook
    summary_brain:              # optional cheap brain dedicated to summaries
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
```

Each agent can override the app-level config via
`agents[].brain.context` (`schema.py:954`).

### Provider auto-detection

When `max_tokens: 0`, the bootstrap pass calls
`_refine_context_config_for_provider()`
(`bootstrap.py:1343`) which reads the provider's known context
window. Override only when the provider doesn't report one (custom
endpoints, niche local models).

## Token pressure and the auto-compact hook

`bootstrap.py:1163-1171`. When `runtime.context.auto_compact: true`
(default), the runtime auto-injects a hook equivalent to:

```yaml
runtime:
  hooks:
    - id: _auto_compact
      "on": turn_start
      condition:
        type: context_pressure
        threshold: 0.75            # = compression_trigger
      action:
        type: compact_context
        strategy: summarize        # = strategy
        keep_recent: 10            # = keep_recent (alias: keep_last)
        summary_max_tokens: 1024
      cooldown: 30.0
```

The injection is skipped when an explicit `compact_context` hook
already exists in `runtime.hooks` (so apps can override the
behaviour without surprise duplication).

The `context_pressure` condition is at `hooks.py:207`
(`_eval_context_pressure`). Default threshold `0.75`.
`token_pressure` is computed per turn from the actual usage reported
by the provider (or estimated via
`compaction.estimate_tokens()` — `~ 4 chars / token`,
`compaction.py:26`).

The `compact_context` action is at `hooks.py:395`
(`@register_action("compact_context", ...)`).

## Compaction strategies

### `truncate`

`hooks.py::_do_truncate`. The cheap path:

1. Find a "safe split point" near the boundary
   (`_find_safe_split_point`) — never splits a tool-call/tool-result
   pair, never strands an orphan `tool_use_id`.
2. Drop everything before the split.
3. Inject a single system message
   (`_build_context_reminder`, `hooks.py:536`) recapping the goal +
   recent activity so the agent doesn't lose continuity.

No LLM call. Best when the deeper history is repetitive (a long
file-grep loop, polling a watcher) and the agent doesn't need it.

### `summarize`

`hooks.py:754` `_do_summarize`. The smart path:

1. Find the safe split point (same as truncate).
2. Send the dropped slice to the **summary brain** (the agent's
   main brain, or `runtime.context.summary_brain` if configured)
   with a structured prompt asking for a recap of decisions, files
   touched, key facts.
3. Replace the dropped slice with a single system message
   containing the summary.
4. Keep the recent messages verbatim
   (count = `keep_recent`).

Costs one LLM call per compaction. Recommended when the deeper
history matters (the agent has been making decisions over many
turns).

The summary is hard-capped at `summary_max_tokens` to prevent the
summary itself from growing unbounded.

### `summary_brain` — using a cheap model for compaction

`schema.py:820` `ContextConfig.summary_brain`. Setting it routes the
summarisation call to a cheaper / faster model than the agent's
main brain. The block accepts the full `AgentBrain` shape
recursively (provider, model, backend, config, temperature, ...).

```yaml
runtime:
  context:
    strategy: summarize
    summary_brain:
      provider: deepseek           # cheap model for summaries
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
      temperature: 0.0
      max_tokens: 1024
```

If `summary_brain` is not set, the agent's main brain handles
summarisation (`hooks.py:3115` `_summary_provider`). For a Claude
Sonnet agent doing daily compactions, switching to DeepSeek for
summaries can cut the compaction cost by an order of magnitude
without affecting the agent's runtime behaviour.

## Emergency compaction (overflow recovery)

`compaction.py:45` `emergency_compact`. When the LLM responds with a
context-overflow error, the runtime:

1. Detects the error via `is_context_overflow(exc)`
   (`compaction.py:13`) — matches `maximum context length`,
   `context_length_exceeded`, `context window`, `reduce the length
   of the messages`, `too many tokens`, `token limit`.
2. Halves `keep_recent` (minimum 4) and runs an aggressive truncate
   compaction (no LLM call — the LLM is refusing).
3. Persists a `compaction` event to `history_log` with
   `reason: "context_overflow"` for the audit trail.
4. Retries the turn.

This is a safety net — `auto_compact + compression_trigger` should
prevent overflow in practice. The emergency path exists for the
edge case where the threshold misfires (a single huge tool result
blowing past 25% of the budget at once).

For oversized **single messages** (a 200 KB tool result that
wouldn't fit anywhere), `truncate_oversized_messages`
(`compaction.py:190`) and `snip_oversized_messages`
(`compaction.py:212`) clip the offending content with a marker
indicating the truncation.

## Persistence and resume

`compaction_persistence.py`. Every compaction emits a durable event
with:

- `_snapshot_tools` (`line 43`) — current tool inventory + injection
  mode
- `_snapshot_memory` (`line 146`) — memory state snapshot
- `_extract_tool_examples` (`line 105`) — recent tool call shapes,
  so a resumed session re-ingests realistic examples
- `build_system_note_from_payload` (`line 331`) — rebuilds the
  system note injected into the post-compaction conversation

When a session resumes after the daemon restarts, the snapshot is
replayed so the agent picks up with the same memory + tool examples
it had pre-compaction.

## Manual triggers

Two ways to compact outside the auto-hook:

- **Custom hook in the YAML** — declare a `compact_context` hook
  with your own condition (e.g. `turn_count: every: 10`,
  `tool_calls: threshold: 50`). The auto-injection skips when an
  explicit `compact_context` hook is present, so this completely
  replaces the default.
- **REST API** — `POST /api/apps/{app_id}/sessions/{sid}/compact`
  triggers `emergency_compact` from outside the runtime. Useful for
  ops scripts, or when a user clicks a "Free up context" button.

## Per-agent override

In multi-agent apps, each agent can declare its own context budget:

```yaml
agents:
  - id: explorer
    brain:
      provider: deepseek
      model: deepseek-chat
      context:
        max_tokens: 65536          # explorer has a smaller window
        keep_recent: 15
        strategy: truncate         # cheap for read-only browsing

  - id: writer
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      context:
        max_tokens: 200000         # writer keeps full Sonnet window
        keep_recent: 8
        strategy: summarize        # smart summaries for long edits
```

The agent-level `brain.context` is resolved by
`_resolve_context_config()` (`bootstrap.py:1327`) — it fully
overrides `runtime.context` for that agent. Unspecified fields fall
back to the app-level defaults.

## Tuning checklist

| Symptom | Fix |
|---------|-----|
| Compaction fires too often (every 2-3 turns) | Lower `compression_trigger` to `0.85`, or raise `max_tokens` if the model supports more. |
| Agent loses thread after compaction | Switch `strategy: truncate` → `strategy: summarize`. Bump `summary_max_tokens` to ~2048. |
| Summarisation is slow / expensive | Set `summary_brain` to a cheap model (DeepSeek, Groq, Ollama). |
| Overflow errors despite auto_compact | Lower `compression_trigger` (e.g. `0.6`); inspect tool results — one of them is probably huge. Use `truncate_oversized_messages` settings. |
| Tool calls span many turns and clutter context | Add a `tool_calls` condition hook that compacts every N tool calls regardless of pressure. |
| Want to disable auto-compaction entirely | Set `auto_compact: false` and declare your own hook (or accept overflow + emergency recovery). |

## Cross-references

- Block reference (every `ContextConfig` field):
  [App Configuration → runtime.context](02-app-config.md#runtimecontext--context-window-management)
- Per-agent override field on `AgentBrain`:
  [Agents → Per-brain context configuration](03-agents.md#per-brain-context-configuration)
- The hook engine (`compact_context` action, `context_pressure`
  condition, custom hooks): [Tool Hooks](31-tool-hooks.md)
- Memory injected into the prompt: [Cognitive Memory](05-memory.md)
