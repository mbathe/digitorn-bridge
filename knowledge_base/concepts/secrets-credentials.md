---
id: secrets-credentials
title: "Secrets and Credentials"
type: concept
keywords: [secrets, credentials, api_key, oauth, encrypt, vault, provider, claude-code, per_user, per_app_shared, system_wide, required-secrets, reload, mcp_server, connection_string]
related: [app-lifecycle, common-errors, modules-overview, what-is-digitorn]
source: docs/
---

# Secrets and Credentials

## Overview

Digitorn has two complementary systems for managing sensitive data:

1. **Secrets** -- simple per-app key-value store for `{{secret.X}}` references in YAML
2. **Credentials** -- structured per-user/per-app credential providers with typed fields, OAuth flows, and MCP server integration

## Secrets

### What they are

Secrets are encrypted key-value pairs stored per app. They are the simplest way to inject API keys and sensitive values into your app YAML.

### Referencing secrets in YAML

Use `{{secret.KEY_NAME}}` anywhere in your app YAML:

```yaml
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

You can also use `{{env.KEY_NAME}}` for environment variables (not encrypted, not stored):

```yaml
config:
  api_key: "{{env.OPENAI_API_KEY}}"
```

### API operations

#### Check what secrets an app needs

```
GET /api/apps/{app_id}/required-secrets
```

Response:
```json
{
  "secrets": [
    {
      "key": "DEEPSEEK_API_KEY",
      "set": false,
      "locations": ["agents[0].brain.config.api_key"],
      "providers": ["deepseek"],
      "agents": ["main"]
    },
    {
      "key": "SLACK_WEBHOOK",
      "set": true,
      "locations": ["channels.slack_alerts.config.url"],
      "providers": [],
      "agents": []
    }
  ],
  "missing_count": 1,
  "total_required": 2,
  "unused_keys": ["OLD_KEY"]
}
```

The response tells you:
- Which secrets are referenced and where in the YAML
- Which ones are already set
- Which provider/agent uses each secret (inferred from context)
- Any stored secrets that are no longer referenced (`unused_keys`)

#### Set a secret

```
PUT /api/apps/{app_id}/secrets/DEEPSEEK_API_KEY
{
  "value": "sk-..."
}
```

By default, setting a secret triggers an automatic hot-reload of the app. Pass `?reload=false` to skip:

```
PUT /api/apps/{app_id}/secrets/DEEPSEEK_API_KEY?reload=false
{
  "value": "sk-..."
}
```

#### Set multiple secrets at once

```
PUT /api/apps/{app_id}/secrets
{
  "secrets": {
    "DEEPSEEK_API_KEY": "sk-...",
    "OPENAI_API_KEY": "sk-...",
    "SLACK_WEBHOOK": "https://hooks.slack.com/..."
  }
}
```

This writes all secrets and triggers a single reload at the end (instead of N reloads for N keys).

#### List secret keys (values are never returned)

```
GET /api/apps/{app_id}/secrets
```

Response: `{"keys": ["DEEPSEEK_API_KEY", "SLACK_WEBHOOK"]}`

#### Check if a specific secret exists

```
GET /api/apps/{app_id}/secrets/DEEPSEEK_API_KEY
```

Response: `{"key": "DEEPSEEK_API_KEY", "exists": true}`

#### Delete a secret

```
DELETE /api/apps/{app_id}/secrets/DEEPSEEK_API_KEY
```

### Special value: `"claude-code"`

When you set `api_key: "claude-code"` in a brain config, the Anthropic provider reads the OAuth token from `~/.claude/.credentials.json` (the `claudeAiOauth.accessToken` field).

```yaml
agents:
  - id: main
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      backend: anthropic
      config:
        api_key: "claude-code"
```

Features:
- Token is cached in memory with expiry check
- Auto-reloads from disk on 401 errors
- Sends special headers: `x-app: cli`, `anthropic-beta: oauth-2025-04-20,claude-code-20250219`
- 15 retries with exponential backoff for rate limits

### After setting secrets

Always reload the app to apply new secrets:

```
POST /api/apps/{app_id}/reload
```

If you used `PUT /api/apps/{app_id}/secrets/{key}` (without `?reload=false`), the reload happens automatically.

## Credentials (structured)

### What they are

Credentials are a more powerful system for apps that need external service integrations. Unlike secrets (simple key-value), credentials support:

- Typed fields (secret, string, URL, select, connection_string)
- OAuth2 flows (Notion, Google, GitHub, Slack)
- MCP server lifecycle management
- Per-user vs per-app vs system-wide scoping
- Live connection testing
- Validation regex

### Declaring credentials in app.yaml

Use `credentials_schema` in the `execution:` block:

```yaml
execution:
  credentials_schema:
    required: true
    providers:
      # Simple API key
      - name: openai
        label: "OpenAI"
        type: api_key
        scope: per_user
        required: true
        docs_url: "https://platform.openai.com/api-keys"
        fields:
          - name: api_key
            type: secret
            required: true
            label: "API Key"
            placeholder: "sk-..."
            validation_regex: "^sk-[A-Za-z0-9_-]{20,}$"
        test:
          method: GET
          url: "https://api.openai.com/v1/models"
          auth_header: "Bearer {{field.api_key}}"
          expected_status: 200

      # Multi-field credential
      - name: twilio
        label: "Twilio SMS"
        type: multi_field
        scope: per_app_shared
        fields:
          - name: account_sid
            type: string
            required: true
            label: "Account SID"
          - name: auth_token
            type: secret
            required: true
            label: "Auth Token"
          - name: from_number
            type: string
            required: true
            label: "From Number"
            placeholder: "+33600000000"

      # OAuth2 flow
      - name: notion
        label: "Notion"
        type: oauth2
        scope: per_user
        oauth_provider: notion
        oauth_scopes: [read_content, update_content]

      # MCP server with credential fields
      - name: notion_mcp
        label: "Notion (MCP)"
        type: mcp_server
        transport: stdio
        command: ["npx", "-y", "@modelcontextprotocol/server-notion"]
        env_template:
          NOTION_API_KEY: "{{field.api_key}}"
        fields:
          - name: api_key
            type: secret
            required: true
            label: "Notion API Key"

      # Connection string
      - name: postgres
        label: "PostgreSQL"
        type: connection_string
        scope: per_user
        fields:
          - name: connection_string
            type: connection_string
            required: true
            label: "Connection URL"
            placeholder: "postgresql://user:pass@host:5432/dbname"
        test:
          test_query: "SELECT 1"
```

### Credential scopes

| Scope | Who sets it | Who sees it | Use case |
|-------|------------|-------------|----------|
| `per_user` | Each user | Only that user | Personal API keys, OAuth tokens |
| `per_app_shared` | App admin | All users of this app | Shared Twilio account, shared webhook |
| `system_wide` | Daemon admin | All apps | Global API keys, infrastructure creds |

### API operations

#### Get the credential schema

```
GET /api/apps/{app_id}/credentials/schema
```

Returns the full schema as declared in `credentials_schema`. The Flutter client uses this to render a typed form.

#### Get a credential

```
GET /api/credentials/apps/{app_id}/{provider_name}
```

Returns the credential status (filled/missing fields). Values are never returned in full -- only boolean flags.

#### Set a credential

```
PUT /api/credentials/apps/{app_id}/{provider_name}
{
  "fields": {
    "api_key": "sk-..."
  }
}
```

#### Delete a credential

```
DELETE /api/credentials/apps/{app_id}/{provider_name}
```

#### List available providers

```
GET /api/credentials/providers
```

Returns all known provider types with their capabilities.

#### Start OAuth flow

```
POST /api/credentials/apps/{app_id}/{provider_name}/oauth/start
```

Returns `{"auth_url": "https://..."}` -- redirect the user there.

#### Check OAuth status

```
GET /api/credentials/apps/{app_id}/{provider_name}/oauth/status
```

#### Start/stop MCP server

```
POST /api/credentials/apps/{app_id}/{provider_name}/mcp/start
POST /api/credentials/apps/{app_id}/{provider_name}/mcp/stop
GET  /api/credentials/apps/{app_id}/{provider_name}/mcp/status
```

### Per-user credentials on shared apps

When an app is deployed system-wide (scope: system), each user can attach their own credentials. The app's `credentials_schema` with `scope: per_user` providers means each user fills in their own API key. The daemon resolves the right credential for each session based on the authenticated user.

This enables one app to serve many users, each with their own LLM provider account.

## Secrets vs Credentials -- when to use which

| Feature | Secrets | Credentials |
|---------|---------|------------|
| Complexity | Simple key-value | Structured typed fields |
| UI | No built-in UI | Auto-generated form |
| OAuth | No | Yes |
| MCP servers | No | Yes (lifecycle management) |
| Validation | No | Regex, type checking, live test |
| Scope | Per-app only | Per-user, per-app, system-wide |
| YAML reference | `{{secret.X}}` | Via credentials resolver |

**Use secrets** when you just need to inject an API key and don't need per-user isolation.

**Use credentials** when:
- Multiple users need their own API keys
- You need OAuth flows (Google, Notion, GitHub)
- You need MCP server management
- You want the client to render a typed form
- You need connection testing

## Common pattern -- quick setup

For a simple app with one LLM provider:

```yaml
app:
  app_id: my-app
  name: "My App"

agents:
  - id: main
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"

execution:
  mode: conversation
```

After deploying:
```bash
# 1. Check what's needed
GET /api/apps/my-app/required-secrets
# Response: DEEPSEEK_API_KEY is missing

# 2. Set it
PUT /api/apps/my-app/secrets/DEEPSEEK_API_KEY
{"value": "sk-..."}
# App auto-reloads

# 3. Ready to use
POST /api/apps/my-app/sessions
```

## See also

- app-lifecycle -- where secrets fit in the deployment flow
- common-errors -- secret/credential error troubleshooting
- modules-overview -- which modules need secrets
- what-is-digitorn -- architecture overview
