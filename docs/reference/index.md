---
id: reference-index
title: Reference
---

# Reference

This section is the alphabetical, exhaustive surface of every API
the daemon exposes. It is the source of truth for fields, types,
defaults, and runtime behaviour. The text under each entry assumes
you already know *why* you are reaching for that primitive; for the
mental model, see [Concepts](../concepts/), and for guided learning
paths see [Tutorial](../tutorial/).

## Sections

| Section | What it documents |
|---------|-------------------|
| [Modules](modules/) | Every module shipped under `packages/digitorn/modules/` (23 modules). One page per module, listing every `@action`, params model, return shape, and constraint spec. |
| [Runtime](runtime/) | Cross-cutting subsystems that aren't a single module: hooks, middleware, credentials vault, multimodal images, voice, configuration, tool chaining. |
| [HTTP API](api/) | REST endpoints (`/api/...`) plus the Socket.IO event protocol and the DAP debug-adapter protocol. |
| [CLI](cli/) | Every `digitorn ...` sub-command, its flags, and a worked example. |
| [Client SDKs](client-sdks/) | Flutter chat client, React preview SDK, Python testing SDK, web client spec. |

## Where things actually live

The runtime is an in-process Python framework. When you call a tool
from the LLM, the path is:

1. The LLM emits a tool call (native API or a parser-recoverable text
   format, see [Agents - Native vs text-based](../language/03-agents.md#native-vs-text-based-tool-calling)).
2. The agent loop dispatches the call through `context_builder.execute_tool`.
3. The capabilities gate
   ([Tools - capabilities](../language/04-tools.md#toolscapabilities--grant--approve--deny))
   verifies the action is allowed; high-risk actions pause for approval.
4. The behavior engine
   ([Behavior Engine](../language/43-behavior.md)) runs `pre_tool_check`
   then post-call `post_tool_check`.
5. The module's `@action`-decorated method runs and returns a result.

Hooks ([Hooks](runtime/hooks.md)) fire around the call as configured.

## How to navigate this section

- **Looking for a specific module?** Open
  [reference/modules/](modules/) and pick by name.
- **Looking for an HTTP endpoint?** Open [reference/api/rest.md](api/rest.md).
- **Looking for a Socket.IO event?** Open
  [reference/api/socketio.md](api/socketio.md).
- **Trying to figure out what a YAML field does?** That's not here -
  the YAML language is documented in [Language](../language/).
