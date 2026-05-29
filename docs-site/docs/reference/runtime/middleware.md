---
id: middleware
title: Middleware
sidebar_label: Middleware
sidebar_position: 7
description: Pluggable middleware at three levels - App (LLM call), Module (action call), MCP (raw tool call).
---

# Middleware

Pluggable middleware pipeline at three levels, from
outermost to innermost:

| Level | Wraps | Where | Use cases |
|-------|-------|-------|-----------|
| **App-level** | The LLM call inside the agent loop | `runtime.middleware` | Mask secrets, inject RAG, filter content, modify prompts. |
| **Module-level** | Any module's `execute` call | `tools.modules.<id>.middleware` | Audit, retry, timeout, transform. |
| **MCP-level** | Raw MCP `call_tool` | | Specialised MCP tool wrapping. |

## Pipeline pattern

All levels share the same protocol:

1. `before` hooks run **in declaration order**.
2. If any `before` returns a string, it
   **short-circuits** - no LLM / module call.
3. The actual call executes.
4. `after` hooks run **in reverse order**.

```mermaid
flowchart LR
    req["Request"] --> b1["MW1.before"] --> b2["MW2.before"] --> core["LLM / Module"]
    core --> a2["MW2.after"] --> a1["MW1.after"] --> resp["Response"]
    classDef mw fill:#0d0d0d,stroke:#A78BFA,stroke-width:1.4px,color:#E6E6E6;
    classDef io fill:#1a1a1a,stroke:#3B82F6,stroke-width:1.6px,color:#E6E6E6;
    class b1,b2,a1,a2 mw;
    class req,resp,core io;
```

## App-level middleware

`runtime.middleware`. Wraps every LLM
call.

```yaml
runtime:
  middleware:
    - mask_secrets:
        patterns: [password, api_key, token]
    - rag_inject:
        max_chunks: 5
        max_chars: 2000
        collection: my-docs
    - prompt_inject:
        system: "Always respond in French."
    - content_filter:
        block_patterns: ["DROP TABLE", "rm -rf /"]
    - response_filter:
        max_length: 5000
        mask_secrets: true
```

### 5 built-in app middlewares:

#### `mask_secrets` - `SecretMaskMiddleware`

Masks sensitive patterns (passwords, API keys, tokens) in
user messages and LLM responses with `[MASKED]`.

```yaml
- mask_secrets:
    patterns: [password, api_key, secret_key]    # additional patterns
    replacement: "[MASKED]"                       # default
    mask_values: true                             # mask values, not just keys
```

Built-in patterns include: `password`, `api_key`,
`secret_key`, `token`, `bearer`, `sk-*`, `ghp_*`, `glpat-*`.

#### `prompt_inject` - `PromptInjectMiddleware`

Dynamically inject content into the system prompt.

```yaml
- prompt_inject:
    system: "Always respond in French."
    position: append              # append (default) | prepend
```

#### `content_filter` - `ContentFilterMiddleware`

Block messages containing dangerous patterns. Short-circuits
the agent loop with a rejection message.

```yaml
- content_filter:
    block_patterns: ["DROP TABLE", "rm -rf", "DELETE FROM"]
    rejection_message: "This request has been blocked for safety."
```

#### `rag_inject` - `RagInjectMiddleware`

Inject retrieval-augmented generation context before each
LLM call. Retrieves relevant chunks from a knowledge base
and appends / prepends them to the system prompt.

```yaml
- rag_inject:
    max_chunks: 5
    max_chars: 2000
    position: append              # append | prepend
    collection: "my-docs"         # KB collection name
```

#### `response_filter` - `ResponseFilterMiddleware`

Filter or transform the LLM's response. Length cap + secret
masking on output.

```yaml
- response_filter:
    max_length: 5000              # truncate longer responses
    mask_secrets: true            # apply secret masking on output
```

## Module-level middleware

`tools.modules.<id>.middleware`. Wraps the
module's `execute` call.

```yaml
tools:
  modules:
    filesystem:
      middleware:
        - audit:
            log_params: true
            log_result: false
        - retry:
            max_attempts: 3
            base_delay: 1.0
            backoff: exponential
        - timeout:
            seconds: 30.0
```

### 3 built-in module middlewares

#### `audit` - `ModuleAuditMiddleware`

Audit log per module call: module, action, duration,
success / failure.

```yaml
- audit:
    log_params: true              # log input parameters
    log_result: false             # log output result
```

#### `retry` - `ModuleRetryMiddleware`

Retry failed module calls with configurable backoff.

```yaml
- retry:
    max_attempts: 3               # default 3
    base_delay: 1.0               # seconds
    backoff: exponential          # exponential | fixed
```

Exponential backoff doubles each attempt (1 s, 2 s, 4 s, ...),
capped at 30 s.

#### `timeout` - `ModuleTimeoutMiddleware`

Per-call timeout. Raises `asyncio.TimeoutError` on overrun.

```yaml
- timeout:
    seconds: 30.0                 # default 30
```

## MCP-level middleware

Wraps raw `call_tool` invocations on MCP servers. Used internally
for caching, retries, and error normalisation.

## Custom middleware

Load a custom class from a Python file or installed package.

### From a local file

```yaml
runtime:
  middleware:
    - custom:
        path: "./middlewares/my_middleware.ts"
        class: "MyAppMiddleware"
        config:
          key: value
```

The `path` is resolved relative to the app YAML location.
The system also checks a `middleware/` subdirectory if the
file isn't found at the literal path.

### From an installed package

```yaml
runtime:
  middleware:
    - custom:
        module: "my_package.middleware"
        class: "MyAppMiddleware"
        config:
          key: value
```

### Writing a custom app middleware

```python
class MyAppMiddleware:
    def __init__(self, key: str = "default"):
        self.key = key

    async def before(self, ctx):
        """Called before the LLM call. Return a string to short-circuit."""
        # Modify ctx.system_prompt, ctx.messages, etc.
        return None    # None = continue to LLM

    async def after(self, ctx, response, tool_calls):
        """Called after the LLM response. Can modify the response."""
        return response
```

### Writing a custom module middleware

```python
class MyModuleMiddleware:
    async def __call__(self, ctx, next_):
        """Wraps module.execute(). Must call next_(ctx) to continue."""
        print(f"Before: {ctx.module_id}.{ctx.action}")
        result = await next_(ctx)
        print(f"After: {ctx.module_id}.{ctx.action}")
        return result
```

## Middleware resolution order

For each middleware name, resolution follows this order:

1. **TOML registry** - middleware packages registered via
   `digitorn-middleware.toml`.
2. **Inline fallback** - built-in classes hard-coded in


App-level fallback registry: `mask_secrets`,
`prompt_inject`, `content_filter`, `rag_inject`,
`response_filter`.

Module-level fallback registry: `audit`, `retry`, `timeout`.

## Complete example

```yaml
app:
  app_id: secure-assistant
  name: "Secure Assistant"

runtime:
  mode: conversation
  middleware:
    - mask_secrets:
        patterns: [database_password, stripe_key]
    - content_filter:
        block_patterns: ["DROP TABLE", "TRUNCATE", "rm -rf"]
    - rag_inject:
        max_chunks: 3
        collection: company-docs
    - prompt_inject:
        system: "You are a helpful assistant. Never reveal API keys."
    - response_filter:
        max_length: 10000
        mask_secrets: true

agents:
  - id: assistant
    role: assistant
    brain: { ... }
    system_prompt: "..."

tools:
  modules:
    filesystem:
      middleware:
        - audit: { log_params: true }
        - retry: { max_attempts: 2 }
    shell:
      middleware:
        - audit: { log_params: true }
        - timeout: { seconds: 60.0 }
```

## Cross-references

- App-config block reference (`runtime.middleware` +
  `tools.modules.<id>.middleware`):
  [App Configuration → runtime](../../language/02-app-config.md#runtime---lifecycle-and-execution-policy)
- Hooks (different mechanism - fires on agent-loop events,
  not LLM calls): [hooks.md](hooks.md)
- Behaviour engine (per-tool runtime checks):
  [Behavior Engine](../../language/43-behavior.md)
