---
id: howtos-index
title: How-tos
---

Task-oriented recipes. Each page is structured as
**problem -> minimal YAML -> deploy + verify**, with the live test
command included. Where a recipe touches a module, the page links
to the full module reference instead of repeating it.

## Standalone how-tos

| Recipe                                           | Page                                                |
|--------------------------------------------------|-----------------------------------------------------|
| Build a RAG bot over a folder of markdowns       | [Build a RAG bot](build-a-rag-bot.md)               |
| Attach files to a chat session (paperclip menu)  | [Attach files to chat](attach-files-to-chat.md)     |
| Add a new module to the daemon (contributor doc) | [Add a module](add-a-module.md)                     |
| Install an MCP server (Hub catalog or custom)    | [Install an MCP server](install-an-mcp-server.md)   |

## Linked recipes

Topics where the canonical reference page is already exhaustive;
the howto would just be a copy. Follow the links instead.

| Recipe                                          | Where to look                                                                                    |
|-------------------------------------------------|--------------------------------------------------------------------------------------------------|
| Add a new LLM provider                          | [Agents - Provider examples](../language/03-agents.md#provider-examples)                         |
| Secure shell access (allowed/blocked commands)  | [Shell module](../reference/modules/shell.md), [OS Sandbox](../language/35-sandbox.md)           |
| Debug an app (logs, dev CLI, tracing)           | [Dev CLI](../language/46-dev-cli.md), [Observability](../language/24-observability.md)           |
| Use the credentials vault                       | [Credentials](../reference/runtime/credentials.md)                                               |
| Migrate from legacy v1 YAML                     | [Language - migration table](/docs/language/#migration-from-the-legacy-flat-shape)               |
| Deploy to production                            | [Production deployment](../language/36-production.md), [Deployment](/docs/deployment/)           |
| Build a multi-agent team                        | [Tutorial - Multi-agent team](../tutorial/04-multi-agent.md)                                     |

## When to write a howto

Write a howto when:

- The task crosses 3+ pages of the reference and you want a single
  copy-pasteable answer.
- The task has a non-obvious gotcha (an order-of-operations,
  a credential scope, a sandbox interaction).
- A user asks the same question twice on the issue tracker.

A howto is **NOT** the place for new conceptual material. If the
recipe needs a paragraph of "why", that paragraph belongs in
[Concepts](/docs/concepts/) and the howto links to it.
