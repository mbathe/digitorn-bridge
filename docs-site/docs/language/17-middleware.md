---
id: middleware
---

# Middleware Pipeline

Middleware sits between the agent loop and the data flowing through
it - intercepting, transforming, masking, or even short-circuiting
LLM calls and tool calls. Two pipelines exist:

| Pipeline | Wraps |
|----------|-------|
| **App-level** | Every LLM call (before / after) |
| **Module-level** | Every tool call to a specific module |

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## App-level middleware

Declared under `runtime.middleware`
(), runs around **every LLM call** in the agent
loop. Order matters: middleware runs top-to-bottom in `before`,
bottom-to-top in `after` (standard wrapping pattern).

```yaml
runtime:
  middleware:
    - mask_secrets:
        patterns: [api_key, password]
        replacement: "[MASKED]"
    - prompt_inject:
        position: prefix
        text: "Today is {{sys.date}}. Be concise."
    - content_filter:
        block_patterns: ["delete from .*"]
        action: block               # or "warn"
    - rag_inject:
        kb: documentation
        max_chunks: 3
    - response_filter:
        max_length: 2000
```

### `AppMiddleware` protocol

 Two methods, both `async`:

| Method | Receives | Returns |
|--------|----------|---------|
| `before(ctx)` | `AppMiddlewareContext` | `None` to proceed, or a string to **short-circuit** the LLM call (the string becomes the agent's response, no LLM is invoked). |
| `after(ctx, response, tool_calls)` | Same context + the LLM's response | The (possibly modified) response string. |

`AppMiddlewareContext` carries:
`agent_id`, `system_prompt`, `messages`, `turn`, `metadata`.
Middleware can mutate any of these in-place during `before`.

### Built-in app middleware

5 ship in :

#### `mask_secrets` - `SecretMaskMiddleware`

Mask sensitive patterns in user messages **before** sending to the
LLM, and in the response **after**. Default regex catches
`password=`, `api_key=`, `Bearer X`, `sk-...` (OpenAI), `ghp_...`
(GitHub), `glpat-...` (GitLab).

```yaml
- mask_secrets:
    patterns: [internal_token, my_secret]   # extra keywords beyond defaults
    replacement: "[MASKED]"                  # default
    mask_values: true                        # default
```

#### `prompt_inject` - `PromptInjectMiddleware`

Inject extra text into the system prompt at every turn (useful for
runtime context that should refresh every call - date, user
identity, deployment info).

```yaml
- prompt_inject:
    position: prefix          # or "suffix"
    text: |
      Current time: {{sys.timestamp}}
      User: {{event.headers.X-User-Id ?? 'anonymous'}}
```

#### `content_filter` - `ContentFilterMiddleware`

Apply allow/block regex against user messages.

```yaml
- content_filter:
    block_patterns:
      - "(?i)(drop|truncate)\\s+table"
      - "(?i)delete\\s+from"
    allow_patterns: []                # if non-empty, only matching passes
    action: block                     # or "warn"
    block_message: "I can't help with destructive SQL."
```

`action: block` short-circuits with `block_message`. `action: warn`
appends a warning but lets the call proceed.

#### `rag_inject` - `RagInjectMiddleware`

Inject relevant chunks from a knowledge base into the system prompt
based on the user's last message. Requires the `rag` module to be
loaded.

```yaml
- rag_inject:
    knowledge_base: documentation     # KB name (alias: collection)
    max_chunks: 5                     # default
    max_chars: 2000                   # default - total budget across all chunks
    position: append                  # default; or "prepend"
```

See [Advanced RAG](37-rag.md) for the KB declaration and the rag
module surface.

#### `response_filter` - `ResponseFilterMiddleware`

Apply allow/block regex against the LLM's response.

```yaml
- response_filter:
    block_patterns:
      - "(?i)<script>"             # strip suspicious HTML
    max_length: 4000               # truncate
    truncate_message: "... [truncated]"
```

## Module-level middleware

Declared under `tools.modules.<module_id>.middleware`
(), runs around **every action call** for that
module. Order matters the same way (top-down in pre, bottom-up in
post).

```yaml
tools:
  modules:
    database:
      middleware:
        - audit:
            log_params: true
            log_results: false
        - retry:
            max_attempts: 3
            backoff: exponential
        - timeout:
            seconds: 30
```

### `ModuleMiddleware` protocol

 Two methods:

| Method | Receives | Returns |
|--------|----------|---------|
| `pre(action, params)` | Action name and the dict of params | `(action, params)` (possibly modified) - or raise to abort. |
| `post(action, params, result, error)` | The original call + outcome | `(result, error)` (possibly modified). |

### Built-in module middleware

3 ship in :

#### `audit` - `ModuleAuditMiddleware`

Log every action call. Useful for compliance / debugging.

```yaml
- audit:
    log_params: true             # log call parameters
    log_results: false           # log return value (may be huge)
    log_errors: true
    redact_keys: [api_key, password, token]
```

#### `retry` - `ModuleRetryMiddleware`

Retry failed calls with backoff. Catches transient errors
(network blips, rate limits).

```yaml
- retry:
    max_attempts: 3
    backoff: exponential          # or "fixed"
    base_delay_ms: 100
    max_delay_ms: 5000
    retry_on: [TimeoutError, ConnectionError]
```

#### `timeout` - `ModuleTimeoutMiddleware`

Wrap each action call in `asyncio.wait_for(...)` with the given
ceiling.

```yaml
- timeout:
    seconds: 30
```

When the timeout elapses, the action raises `TimeoutError` (which
the `retry` middleware can pick up if it's chained next).

## Pipeline ordering

App middlewares declared earlier in the YAML wrap **outside** later
ones. Same for module middlewares. Concretely, with:

```yaml
runtime:
  middleware:
    - mask_secrets: { ... }
    - content_filter: { ... }
    - prompt_inject: { ... }
```

Execution order:
- `mask_secrets.before` → `content_filter.before` →
  `prompt_inject.before` → **LLM call** →
  `prompt_inject.after` → `content_filter.after` →
  `mask_secrets.after`

This matches every standard middleware framework. If
`content_filter.before` short-circuits with a string,
`prompt_inject.before` and the LLM call are skipped, and only the
already-fired `mask_secrets.before` ran (its `after` won't fire
because there was no LLM response - short-circuit returns the
string directly).

## Custom middleware (installable packages)

 `MiddlewareDescriptor`. Custom
middleware is a Python class that implements either `AppMiddleware`
or `ModuleMiddleware`, packaged as a small directory and installed
via the CLI:

```bash
digitorn middleware install /path/to/my-middleware
digitorn middleware list
digitorn middleware info my-middleware
digitorn middleware uninstall my-middleware
```

After install, reference the middleware by name in YAML:

```yaml
runtime:
  middleware:
    - my-middleware:
        custom_param: value
```

The package directory must contain a `digitorn-middleware.toml`
manifest that declares the entry point class. See
(`line 238`) for the
expected structure.

## Choosing app-level vs module-level

| Goal | Pipeline |
|------|----------|
| Mask secrets in user messages before any LLM sees them. | App (`mask_secrets`). |
| Inject runtime-fresh context every turn (date, user, deployment). | App (`prompt_inject`). |
| Block dangerous user requests before reaching the agent. | App (`content_filter`). |
| Inject KB chunks based on the user's last message. | App (`rag_inject`). |
| Audit every database query for compliance. | Module (`database.middleware: [audit]`). |
| Retry HTTP calls on transient failures. | Module (`http.middleware: [retry]`). |
| Cap the maximum time an MCP tool can take. | Module (`mcp.middleware: [timeout]`). |
| Custom logic specific to one module's actions. | Module - write a custom `ModuleMiddleware`. |
| Custom logic that affects the whole agent loop. | App - write a custom `AppMiddleware`. |

## Compile-time validation

The compiler resolves every entry under `runtime.middleware` and
`tools.modules.*.middleware` against the registered middleware set.
A typo (`mask_secret` instead of `mask_secrets`) raises a clear
error pointing at the bad entry. Custom middleware must be installed
via `digitorn middleware install` before referenced - the compiler
fails closed when a name doesn't resolve.

## Cross-references

- App-config block reference (`runtime.middleware`,
  `tools.modules.<id>.middleware`):
  [App Configuration](02-app-config.md)
- KB declaration (used by `rag_inject`):
  [Advanced RAG](37-rag.md)
- CLI surface (`digitorn middleware *`):
  [Index → CLI](/docs/language/#cli)
- Hooks vs middleware (different timing, different scope):
  [Tool Hooks](31-tool-hooks.md)
