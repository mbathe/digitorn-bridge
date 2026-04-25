---
id: common-errors
title: "Common Errors"
type: concept
keywords: [errors, troubleshooting, compilation, runtime, debug, fix, provider, module, action, secret, credential, context_pressure, timeout, rate_limit, billing]
related: [app-lifecycle, app-structure, secrets-credentials, modules-overview]
source: docs/
---

# Common Errors

## Overview

Compilation and runtime errors you will encounter when building Digitorn apps, with their root causes and solutions.

## Compilation errors

These errors are returned by `POST /api/discovery/compile` or during `POST /api/apps/deploy`.

### `agents[0].brain.provider: invalid value`

**Cause:** The provider name in the brain config is not recognized.

**Fix:** Use a valid provider name. Check available providers:
```
GET /api/credentials/providers
```

Valid providers include: `anthropic`, `openai`, `deepseek`, `groq`, `mistral`, `together`, `ollama`, `lm_studio`, `minimax`.

```yaml
# WRONG
agents:
  - id: main
    brain:
      provider: deep-seek        # typo
      model: deepseek-chat

# CORRECT
agents:
  - id: main
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
```

### `module 'X' not found`

**Cause:** The module referenced in the `modules:` block or `capabilities:` block is not loaded in the daemon.

**Fix:** Check available modules:
```
GET /api/discovery/modules
```

Common mistakes:
- `file_system` instead of `filesystem`
- `search` instead of `web`
- `bash` instead of `shell`
- `db` instead of `database`

```yaml
# WRONG
modules:
  file_system: {}

# CORRECT
modules:
  filesystem: {}
```

### `action 'X' not found on module 'Y'`

**Cause:** The action name in a setup step, capability grant, or constraint does not exist on the target module.

**Fix:** Check available actions for the module:
```
GET /api/discovery/modules/{module_id}
```

The response lists every action with its parameters.

```yaml
# WRONG
capabilities:
  grant:
    - module: filesystem
      actions: [read_file, write_file]    # wrong action names

# CORRECT
capabilities:
  grant:
    - module: filesystem
      actions: [read, write, edit, glob, grep]
```

### `capabilities: grant references unknown module`

**Cause:** A module referenced in `capabilities.grant`, `capabilities.deny`, or `capabilities.hidden_modules` is not declared in the `modules:` block.

**Fix:** Either add the module to the `modules:` block or fix the typo in the capability reference:

```yaml
# WRONG -- shell not declared in modules
modules:
  filesystem: {}
capabilities:
  grant:
    - module: shell               # not in modules block
      actions: [bash]

# CORRECT
modules:
  filesystem: {}
  shell: {}                       # add the module
capabilities:
  grant:
    - module: shell
      actions: [bash]
```

### `execution.entry_agent: agent 'X' not found`

**Cause:** The `entry_agent` value does not match any agent's `id` in the `agents:` list.

**Fix:** Make sure the names match exactly (case-sensitive):

```yaml
agents:
  - id: coordinator               # <-- this is the id
    brain: { ... }

execution:
  entry_agent: coordinator        # must match exactly
```

If `entry_agent` is not set, the first agent in the list is used.

### `ModuleBlock: extra fields not permitted`

**Cause:** Config keys placed directly under a module block instead of inside `config:`.

**Fix:** Wrap module configuration inside the `config:` key:

```yaml
# WRONG -- keys are silently dropped, NOT an error in all cases
modules:
  rag:
    backend:                       # silently ignored
      type: qdrant

# CORRECT
modules:
  rag:
    config:
      backend:
        type: qdrant
```

Only 4 keys are valid directly under a module block: `config`, `setup`, `constraints`, `middleware`. Everything else is dropped.

## Runtime errors

These errors occur after deployment, during agent execution.

### `secret required but not set`

**Cause:** The YAML references `{{secret.X}}` but the secret has not been configured.

**Fix:**
1. Check what secrets are needed:
```
GET /api/apps/{app_id}/required-secrets
```

2. Set the missing secret:
```
PUT /api/apps/{app_id}/secrets/X
{
  "value": "your-secret-value"
}
```

3. The app auto-reloads after setting a secret.

### `credential_auth_required`

**Cause:** The app declares a `credentials_schema` with required providers, and the user has not configured their credentials.

**Fix:**
1. Check the credential schema:
```
GET /api/apps/{app_id}/credentials/schema
```

2. See what's configured:
```
GET /api/credentials/apps/{app_id}/{provider_name}
```

3. Set the credential:
```
PUT /api/credentials/apps/{app_id}/{provider_name}
{
  "fields": {
    "api_key": "sk-..."
  }
}
```

### `402 Insufficient Balance` / billing errors

**Cause:** The LLM provider account has no credits or has exceeded its spending limit.

**Fix:**
- Add credits to your LLM provider account (OpenAI, Anthropic, DeepSeek, etc.)
- Switch to a free/cheaper model temporarily
- Use a local model via Ollama or LM Studio

The error classification system categorizes this as `category: "billing"` with `retry: false`.

### `429 Rate Limit` / rate_limit errors

**Cause:** Too many requests to the LLM provider in a short time.

**Fix:**
- The daemon has built-in retry logic with exponential backoff (up to 15 retries for the Anthropic provider)
- If persistent, reduce `max_concurrent_activations` for background apps
- Use a different API key or upgrade your plan with the provider

The error classification: `category: "rate_limit"`, `retry: true`.

### `context_pressure` / context window full

**Cause:** The conversation has accumulated too many tokens and the context window is full.

**Fix:** Add a compaction hook to auto-compress the context:

```yaml
execution:
  context:
    max_tokens: 131072             # set based on your model
    strategy: summarize
    keep_recent: 10
    compression_trigger: 0.75
    auto_compact: true             # auto-inject compaction hook

  hooks:
    - id: compact
      on: turn_end
      condition:
        type: context_pressure
        threshold: 0.75
      action:
        type: compact_context
        strategy: summarize
        keep_last: 10
      cooldown: 30
```

You can also set `auto_compact: true` (default) to let the runtime inject the hook automatically.

Manual compaction via API:
```
POST /api/apps/{app_id}/sessions/{session_id}/compact
```

### `tool_timeout` / tool took too long

**Cause:** A tool execution exceeded the timeout limit.

**Fix:** Increase the execution timeout:

```yaml
execution:
  timeout: 600                    # seconds (default 300)
```

For specific long-running tools (e.g. builds, tests), consider using `bash_background` instead of `bash` to run them asynchronously.

### `401 Unauthorized` / auth errors

**Cause:** Invalid or expired API key/token.

**Fix:**
- For `api_key: "claude-code"`: the OAuth token may have expired. Claude Code automatically refreshes it, but you may need to re-authenticate in Claude Code.
- For regular API keys: verify the key is correct in `GET /api/apps/{app_id}/required-secrets` and re-set it if needed.

The Anthropic provider auto-reloads the token from `~/.claude/.credentials.json` on 401 errors.

### `max_output_tokens` reached

**Cause:** The LLM hit its output token limit mid-response.

**Fix:**
- The daemon has built-in auto-resume (3 attempts) when the LLM's `finish_reason` is `max_tokens`
- Increase `max_tokens` in the agent brain config:

```yaml
agents:
  - id: main
    brain:
      max_tokens: 16384            # increase from default
```

### `network` / connection errors

**Cause:** Cannot reach the LLM provider API (DNS failure, firewall, proxy issues).

**Fix:**
- Check network connectivity to the provider's base URL
- If behind a proxy, configure it via environment variables
- For local models (Ollama), verify the server is running

## Debugging checklist

When an app doesn't work:

1. **Compile check**: `POST /api/discovery/compile` with the YAML
2. **Secrets check**: `GET /api/apps/{app_id}/required-secrets` -- any missing?
3. **Status check**: `GET /api/apps/{app_id}/status` -- deployed and healthy?
4. **Module check**: `GET /api/discovery/modules` -- all needed modules available?
5. **Credentials check**: `GET /api/apps/{app_id}/credentials/schema` -- user credentials configured?
6. **Preview check** (if applicable): `GET /api/apps/{app_id}/preview-server/status`
7. **Reload**: `POST /api/apps/{app_id}/reload` -- force recompile with current secrets

## Error classification

The daemon classifies all errors into structured responses:

```json
{
  "error": "Insufficient Balance",
  "code": 402,
  "category": "billing",
  "retry": false,
  "detail": "Your DeepSeek account has no credits."
}
```

Categories: `billing`, `auth`, `rate_limit`, `provider`, `network`, `internal`.

The `retry` field tells the client whether retrying the request might succeed.

## See also

- app-lifecycle -- the full lifecycle with all API endpoints
- secrets-credentials -- managing secrets and credentials
- modules-overview -- finding valid module/action names
- app-structure -- correct YAML structure
