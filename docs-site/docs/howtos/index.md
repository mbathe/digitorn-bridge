---
id: howtos-index
title: How-tos
---

# How-tos

Task-oriented recipes. Each page is structured as
**problem -> minimal YAML -> deploy + verify**, with the live test
command included. Where a recipe touches a module, the page links
to the full module reference instead of repeating it.

## Available

This section is being populated. Recipes already covered in detail
elsewhere are linked from their canonical page; the following are
the planned standalone howtos:

| Recipe | Status |
|--------|--------|
| Add a new LLM provider | Linked: [Agents - Provider examples](../language/03-agents.md#provider-examples). |
| Make a RAG app over a folder of PDFs | Linked: [RAG module](../language/37-rag.md). |
| Secure shell access (allowed/blocked commands) | Linked: [Shell module](../reference/modules/shell.md), [OS Sandbox](../language/35-sandbox.md). |
| Debug an app (logs, dev CLI, tracing) | Linked: [Dev CLI](../language/46-dev-cli.md), [Observability](../language/24-observability.md). |
| Use the credentials vault | Linked: [Credentials](../reference/runtime/credentials.md). |
| Migrate from legacy v1 YAML | Linked: [Language - migration table](/docs/language/#migration-from-the-legacy-flat-shape). |
| Deploy to production | Linked: [Production deployment](../language/36-production.md), [Deployment](../deployment/). |
| Build a multi-agent team | Linked: [Multi-agent](../language/12-multi-agent.md). |
| Expose an app as an MCP server | Linked: [App-as-MCP-server](../language/16-app-as-mcp-server.md). |

## When to write a howto

Write a howto when:

- The task crosses 3+ pages of the reference and you want a single
  copy-pasteable answer.
- The task has a non-obvious gotcha (an order-of-operations,
  a credential scope, a sandbox interaction).
- A user asks the same question twice on the issue tracker.

A howto is **NOT** the place for new conceptual material. If the
recipe needs a paragraph of "why", that paragraph belongs in
[Concepts](../concepts/) and the howto links to it.
