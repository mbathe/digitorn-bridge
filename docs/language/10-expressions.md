---
id: expressions
---

# Expressions

Digitorn's template syntax for dynamic values in YAML. Every
`{{...}}` placeholder is resolved by `resolve_variables()`
(`packages/digitorn/core/app/variables.py`) before the YAML
reaches Pydantic. Template syntax is **intentionally minimal** —
just namespaces, a fallback operator, and quoted literals. No
filters, no comparison operators, no logic.

This page documents what's actually supported. Everything else
(`{{x | upper}}`, `{{x == 'foo'}}`, `{{x && y}}`, ...) is **not
implemented**. The resolver checks `if "|" in expr: return
match.group(0)` and gives up on pipes (`variables.py`).

For complex routing logic (in `flow:` route conditions, hook
conditions, channel activation rules), each subsystem evaluates its
own expressions at runtime — see the cross-references at the end.

## Syntax

```
{{<expression>}}
```

Whitespace inside the braces is trimmed. The expression itself is
one of:

- A **plain name** (matches a key in `dev.variables`).
- A **namespaced reference** (`env.X`, `secret.X`, `sys.X`,
  `app.X`, `prompt.X`, `skill.X`, `behavior.X`, `asset.X`,
  `asset_b64.X`, `include:path`).
- A **quoted literal** (`'foo'` or `"foo"`).
- A **fallback expression** (`<expr> ?? <fallback>`).
- Anything else — passed through unchanged for runtime resolution
  by the consuming module (channels, hooks, flow expressions).

## Namespaces

`variables.py` `_lookup()`. Resolution time = compile time
unless noted "runtime passthrough".

| Namespace | Pattern | Source | Resolution time |
|-----------|---------|--------|-----------------|
| User variables | `{{my_var}}` | `dev.variables` | Compile |
| Environment | `{{env.VAR_NAME}}` | `os.environ`. Raises a compile error when unset. Use `??` for optional. | Compile |
| Secret | `{{secret.VAR}}` | Encrypted DB first, `os.environ` fallback. | Compile |
| System | `{{sys.X}}` | `_SYS_VARIABLES` dict (`variables.py`). 22 keys — full list in [App Configuration → System variables](02-app-config.md#system-variables-syssys). | Compile |
| App | `{{app.id}}`, `{{app.name}}`, `{{app.version}}`, `{{app.author}}`, `{{app.description}}` | `app:` block | Compile |
| Prompt file | `{{prompt.X}}` → `prompts/X.md` | Bundle dir | Compile (file content inlined) |
| Skill file | `{{skill.X}}` → `skills/X.md` | Bundle dir | Compile (file content inlined) |
| Behavior profile | `{{behavior.X}}` → `behavior/X.yaml` | Bundle dir | Compile (parsed dict as JSON string) |
| Asset URL | `{{asset.X}}` → `/api/apps/<app_id>/assets/assets/X` | Bundle dir | Compile (URL substitution) |
| Asset (base64) | `{{asset_b64.X}}` | Bundle dir | Compile (file inlined as base64) |
| Include | `{{include:path/to/file}}` | Bundle dir | Compile (file content inlined) |
| Runtime passthrough | Any other dotpath (`event.X`, `caller.X`, `tool.X`, ...) | Module-specific runtime | Run time |

## Fallback operator `??`

`variables.py`. Returns the right side if the left side
fails to resolve. Strict semantics — `env.X` returning a passthrough
template (because the variable isn't set) is treated as failure and
falls through.

```yaml
dev:
  variables:
    timeout:    "{{env.TIMEOUT ?? '30'}}"             # fallback to '30'
    region:     "{{env.AWS_REGION ?? 'eu-west-1'}}"   # default region
    api_token:  "{{secret.API_TOKEN ?? env.API_TOKEN ?? 'dev-token'}}"
                                                       # 3-level fallback chain
```

The right side is a full expression — it can chain to another
namespace, another fallback, or a quoted literal.

## Quoted string literals

`variables.py`. A single- or double-quoted string is
returned verbatim:

```yaml
dev:
  variables:
    greeting: "{{ 'Hello, world' }}"     # literal string
    fallback: "{{ env.X ?? 'unset' }}"   # quoted fallback value
```

Useful as the right side of `??` or as a guarded literal in a
context where the templating layer would otherwise interpret the
value.

## What's NOT supported in template resolution

The resolver bails out (`variables.py`) on pipes and treats the
template as runtime passthrough. The following constructs **do not
work** in `{{...}}` placeholders:

| Pattern | Status |
|---------|--------|
| `{{x \| upper}}`, `{{x \| join: ', '}}`, `{{x \| length}}`, ... | **Not implemented.** Pipes cause the template to be passed through unresolved. |
| `{{x.0}}`, `{{x[0]}}` | **Not implemented.** Use the consuming module's runtime resolution if it supports indexing. |
| `{{x?.y}}` | **Not implemented.** Use `??` instead: `{{x.y ?? 'default'}}`. |
| `{{x == 'foo'}}`, `{{x != 'foo'}}` | **Not implemented at the template layer.** Comparisons in `flow:` routes and hook conditions use the runtime expression engine — see the cross-references below. |
| `{{x && y}}`, `{{x \|\| y}}` | **Not implemented at the template layer.** Same — runtime expression engine handles boolean logic in `flow:` and hooks. |
| Arithmetic | **Not implemented.** |
| Format specifiers (`{{date:%Y-%m-%d}}`) | **Not implemented.** Format dates at the source (`{{sys.date}}` is already pre-formatted). |

## Templating contexts

Where Digitorn applies `resolve_variables()`:

- **Anywhere in the YAML** — every string value across the eight
  blocks is rendered through the resolver before Pydantic
  validation. Includes `agents[].system_prompt`, `tools.modules.*.config.*`, `runtime.triggers[].message`, and so on.

The resolver does NOT walk into binary or non-string Pydantic
fields (a numeric `runtime.max_turns` won't accept `"{{env.X}}"`
unless the variable resolves to a valid integer literal at compile
time).

## Runtime expression engines (separate from templates)

A few subsystems evaluate **expressions** (not just template
substitution) at runtime, using their own engines:

- **Flow routes** (`flow:` block, `routes[].when`) — boolean
  expressions over the flow context (`category == 'refund'`,
  `approvals.gate == 'approve'`, `default`). Documented in
  [Flows](07-flows.md). The schema only checks the `when` field is
  a non-empty string — the syntax is validated when the route
  fires.
- **Hook conditions** (`runtime.hooks[].condition`) — declarative
  condition tree (`context_pressure`, `tool_calls`, `tool_failed`,
  `content_contains`, `error_type`, `expression`, plus composite
  `all_of`/`any_of`/`not`). Documented in
  [Tool Hooks](31-tool-hooks.md).
- **Channel activation `prepare:` steps** — module action results
  are bound to a name (`as: caller`) and become available as
  `{{caller.X}}` in subsequent activations of the same channel.
  Documented in [Channels](40-channels.md).

These engines accept richer syntax than the template resolver —
boolean operators, dotted paths, comparisons. But they are
**separate from `{{...}}` template substitution**. Don't expect
filters or comparisons to work inside a `{{...}}` placeholder.

## Compile-time resolution order

`variables.py` recursively resolves until no `{{...}}` remains
or the maximum depth (`_MAX_DEPTH = 10`, line 65) is reached. A
self-referencing variable produces a cycle error.

```yaml
dev:
  variables:
    base: "{{env.BASE_URL}}"
    api:  "{{base}}/v1/api"           # resolves to <BASE>/v1/api
    full: "{{api}}/users"             # resolves to <BASE>/v1/api/users
                                       # works via recursion
```

## Cross-references

- Source of truth (every namespace, every supported syntax):
  `packages/digitorn/core/app/variables.py`
- Variables overview with full system variable list:
  [App Configuration → Variables](02-app-config.md#variables)
- Flow route expressions (`when:`):
  [Flows](07-flows.md#routes-and-edges)
- Hook conditions (registered conditions + composite operators):
  [Tool Hooks](31-tool-hooks.md)
- Channel activation pipeline (`prepare:`):
  [Channels (Bidirectional I/O)](40-channels.md)
- Bundle namespaces deep dive (`{{prompt.X}}`, `{{include:}}`,
  frontmatter, hot reload):
  [Bundle namespaces](38-bundle-namespaces.md)
