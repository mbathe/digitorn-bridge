---
id: middleware
title: Middleware System
sidebar_label: Middleware
sidebar_position: 7
description: Pluggable middleware pipeline at three levels - App, Module, and MCP.
---

# Middleware System

Digitorn provides a pluggable middleware pipeline at three levels, from outermost
to innermost:

| Level | Intercepts | Use cases |
|-------|-----------|-----------|
| **App-level** | Before/after the LLM call in the agent loop | Mask secrets, inject RAG, filter content, modify prompts |
| **Module-level** | Before/after any module's `execute()` call | Audit, retry, timeout, transform |
| **MCP-level** | Before/after raw MCP `call_tool()` | Specialized MCP tool wrapping (in `mcp/middleware.py`) |

---

## Pipeline Pattern

All levels share the same protocol:

1. **before()** hooks run in declaration order
2. If any `before()` returns a string, it **short-circuits** (no LLM/module call)
3. The actual call executes
4. **after()** hooks run in **reverse** order

```
Request → [MW1.before] → [MW2.before] → [LLM/Module] → [MW2.after] → [MW1.after] → Response
```

---

## App-Level Middleware

Configured under `app.middleware` in YAML. Intercepts before/after the LLM call.

```yaml
app:
  middleware:
    - mask_secrets:
        patterns: ["password", "api_key", "token"]
    - rag_inject:
        max_chunks: 5
        max_chars: 2000
    - prompt_inject:
        system: "Always respond in French."
    - content_filter:
        block_patterns: ["DROP TABLE", "rm -rf /"]
    - response_filter:
        max_length: 5000
        mask_secrets: true
```
### Built-in App Middlewares

#### SecretMask

Masks sensitive patterns (passwords, API keys, tokens) in user messages and
LLM responses with `[MASKED]`.

```yaml
- mask_secrets:
    patterns: ["password", "api_key", "secret_key"]   # Additional patterns
    replacement: "[MASKED]"                             # Default: "[MASKED]"
    mask_values: true                                   # Mask values, not just keys
```
Built-in patterns include: `password`, `api_key`, `secret_key`, `token`,
`bearer`, `sk-*`, `ghp_*`, `glpat-*`.

#### PromptInject

Dynamically inject content into the system prompt.

```yaml
- prompt_inject:
    system: "Always respond in French."
    position: append         # "append" (default) or "prepend"
```
#### ContentFilter

Block messages containing dangerous patterns. Short-circuits the agent loop
with a rejection message.

```yaml
- content_filter:
    block_patterns: ["DROP TABLE", "rm -rf", "DELETE FROM"]
    rejection_message: "This request has been blocked for safety."
```
#### RagInject

Inject retrieval-augmented generation context before each LLM call.
Retrieves relevant chunks from a knowledge source and appends/prepends them
to the system prompt.

```yaml
- rag_inject:
    max_chunks: 5            # Max chunks to inject
    max_chars: 2000          # Max total characters
    position: append         # "append" or "prepend"
    collection: "my-docs"    # Knowledge base collection name
```
#### ResponseFilter

Filter or transform the LLM's response. Can enforce length limits and mask
secrets in the output.

```yaml
- response_filter:
    max_length: 5000         # Truncate responses longer than this
    mask_secrets: true        # Apply secret masking to responses
```
---

## Module-Level Middleware

Configured under each module's `middleware` key. Wraps the module's `execute()` call.

```yaml
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
### Built-in Module Middlewares

#### ModuleAudit

Audit logging for module calls. Logs module, action, duration, and success/failure.

```yaml
- audit:
    log_params: true         # Log input parameters
    log_result: false        # Log output result
```
#### ModuleRetry

Retry failed module calls with configurable backoff.

```yaml
- retry:
    max_attempts: 3          # Max retries (default: 3)
    base_delay: 1.0          # Base delay in seconds
    backoff: exponential     # "exponential" or "fixed"
```
Exponential backoff: delay doubles each attempt (1s, 2s, 4s...), capped at 30s.

#### ModuleTimeout

Per-call timeout for module execution. Raises `asyncio.TimeoutError` if exceeded.

```yaml
- timeout:
    seconds: 30.0            # Timeout in seconds (default: 30)
```
---

## MCP-Level Middleware

Specialized middleware for MCP (Model Context Protocol) tool calls. Configured
in the MCP module and wraps raw `call_tool()` invocations.

See `packages/digitorn/modules/mcp/middleware.py` for the implementation.

---

## Custom Middleware

Load a custom middleware class from a Python file or installed package.

### From a local file

```yaml
app:
  middleware:
    - custom:
        path: "./middlewares/my_middleware.py"
        class: "MyAppMiddleware"
        config:
          key: value
```
The `path` is resolved relative to the app YAML location. If not found,
the system also checks a `middleware/` subdirectory.

### From an installed package

```yaml
app:
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
        """Called before LLM call. Return string to short-circuit."""
        # Modify ctx.system_prompt, ctx.messages, etc.
        return None  # None = continue to LLM

    async def after(self, ctx, response, tool_calls):
        """Called after LLM response. Can modify the response."""
        return response
```

### Writing a custom module middleware

```python
class MyModuleMiddleware:
    async def __call__(self, ctx, next_):
        """Wraps module execute(). Must call next_(ctx) to continue."""
        print(f"Before: {ctx.module_id}.{ctx.action}")
        result = await next_(ctx)
        print(f"After: {ctx.module_id}.{ctx.action}")
        return result
```

---

## Middleware Resolution

For each middleware name, resolution follows this order:

1. **TOML registry** - middleware packages registered via `digitorn-middleware.toml`
2. **Inline fallback** - built-in classes hardcoded in `middleware.py`

App-level fallback registry: `mask_secrets`, `prompt_inject`, `content_filter`,
`rag_inject`, `response_filter`.

Module-level fallback registry: `audit`, `retry`, `timeout`.

---

## Complete Example

```yaml
app:
  app_id: secure-assistant
  name: "Secure Assistant"
  middleware:
    - mask_secrets:
        patterns: ["database_password", "stripe_key"]
    - content_filter:
        block_patterns: ["DROP TABLE", "TRUNCATE", "rm -rf"]
    - rag_inject:
        max_chunks: 3
        collection: "company-docs"
    - prompt_inject:
        system: "You are a helpful assistant. Never reveal API keys."
    - response_filter:
        max_length: 10000
        mask_secrets: true

modules:
  filesystem:
    middleware:
      - audit:
          log_params: true
      - retry:
          max_attempts: 2
  shell:
    middleware:
      - audit:
          log_params: true
      - timeout:
          seconds: 60.0
```