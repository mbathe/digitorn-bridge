---
id: macros
---

# Macros — not a Digitorn feature

> **There is no `macros:` block in the canonical schema.**
> `AppDefinition` has exactly **8 top-level blocks**
> (`schema.py:2619`, `extra: forbid`):
> `app, runtime, agents, tools, security, ui, dev, flow`. Anything
> else is rejected by Pydantic at compile time.

Earlier drafts of this page described a `macros:` block with
parameters, action steps, branching, and nested macros. That feature
was never implemented and is not part of the v2 schema. This page
exists as a redirect for readers who land here looking for it.

## What you probably want

Three Digitorn features cover the use cases that "macros" suggested:

### Reusable commands → **Skills**

For commands the user can invoke (`/commit`, `/review`, `/audit`),
declare them under `dev.skills`. Each skill is a markdown file in
the bundle's `skills/` directory; the agent reads the file when the
command is called.

```yaml
dev:
  skills:
    - command: /commit
      description: "Stage + commit + push the current diff"
      path: skills/commit.md
    - command: /review
      description: "Adversarial code review with focus on security"
      path: skills/review.md
```

Full reference: [Skills System](21-skills.md).

### Reusable orchestration → **Flows**

For deterministic graphs (triage → specialist → approval → output),
use the [`flow:` block](07-flows.md). Flow nodes (agent, tool,
parallel, approval, decision, terminal) compose the same way macros
were intended to — but with a fully implemented runtime, schema
validation, and reachability checks.

### Reusable prompt fragments → **Bundle namespaces**

For sharing prompt content across agents, use the
filesystem-backed namespaces (`{{prompt.X}}`, `{{skill.X}}`,
`{{behavior.X}}`, `{{include:path}}`) — verified in
[App Configuration → Variables](02-app-config.md#variables) and
documented further in [Bundle namespaces](38-bundle-namespaces.md).

```yaml
agents:
  - id: assistant
    system_prompt: |
      You are an assistant.
      {{prompt.guidelines}}        # inlines prompts/guidelines.md
      {{include:./shared/header.md}}  # explicit path import
```

### Multi-step workflows → **Sub-agents**

For composable multi-step flows where each step is itself an agent
turn, use the multi-agent pattern via `Agent(...)` — coordinator
spawns specialists. See [Multi-Agent](12-multi-agent.md).

## Cross-references

- 8-block canonical schema:
  [App Configuration](02-app-config.md#yaml-structure)
- Skills (the closest match for "user-callable reusable commands"):
  [Skills System](21-skills.md)
- Flows (the closest match for "scripted orchestration"):
  [Flows](07-flows.md)
- Bundle namespaces (`{{prompt.X}}`, `{{include:}}`):
  [Bundle namespaces](38-bundle-namespaces.md)
- Multi-agent coordination:
  [Multi-Agent](12-multi-agent.md)
