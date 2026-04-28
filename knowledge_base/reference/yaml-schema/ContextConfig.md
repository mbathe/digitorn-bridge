---
id: yaml-schema-contextconfig
title: "ContextConfig - YAML schema reference"
type: schema-reference
model: ContextConfig
is_root: false
keywords: [contextconfig, auto_compact, compression_trigger, keep_recent, max_tokens, output_reserved, strategy, summary_brain, summary_max_tokens]
---

# ContextConfig

## Description
Context management configuration for the agent loop.

Controls how the context window is managed to prevent overflow errors.
When the context fills up, the runtime can automatically compact it
using the configured strategy.

Can be set at two levels:
- ``execution.context`` - default for all agents
- ``agent.brain.context`` - per-brain override (multi-agent apps)

Example::

brain:
provider: deepseek
model: deepseek-chat
context:
max_tokens: 131072
strategy: summarize
keep_recent: 30
compression_trigger: 0.70

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `max_tokens` | int |  | `0` | Context window size in tokens. 0 = auto-detect from provider. Override if the provider doesn't report it. |
| `output_reserved` | int |  | `4096` | Tokens reserved for output generation. Subtracted from max_tokens for pressure calculation. |
| `strategy` | 'truncate' \| 'summarize' |  | `'summarize'` | Compaction strategy: 'truncate' or 'summarize'. |
| `keep_recent` | int |  | `10` | Number of recent messages to preserve during compaction. |
| `compression_trigger` | float |  | `0.75` | Token pressure ratio (0.0-1.0) at which automatic compaction triggers. |
| `summary_max_tokens` | int |  | `1024` | Maximum tokens for the summary when using 'summarize' strategy. |
| `auto_compact` | bool |  | `True` | Enable automatic compaction. When true, the runtime injects a context_pressure hook automatically if none is declared. |
| `summary_brain` | [AgentBrain](AgentBrain.md) \| null |  | `None` | Optional separate brain for summarization during compaction. Use a fast/cheap model for summaries instead of the main brain. If not set, the agent's main brain is used. |

## Linked models
- [AgentBrain](AgentBrain.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
