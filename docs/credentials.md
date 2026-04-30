# Credential System

Centralized credential management for digitorn apps. Apps reference
credentials by name in YAML; users own their secrets in an encrypted
vault; the runtime injects the right value at the right scope, at
the right moment.

## Quick map

```
~/.digitorn/oauth_providers.toml         <- OAuth client_id / secret per provider
~/.digitorn/master.key (or env or KMS)   <- master encryption key
sqlite/postgres `credentials` table      <- encrypted vault rows
sqlite/postgres `credential_audit` table <- hash-chained audit log

YAML  ──┐
        │  credential: { ref, scope, provider }
        ▼
deploy ──> system_wide / per_app_shared resolved
session ──> per_user / per_app_per_user resolved + hot-swapped onto live providers
```

## Scopes

Four scopes carry different access semantics:

| Scope               | Resolved when    | Visible to                       |
|---------------------|------------------|----------------------------------|
| `system_wide`       | deploy           | every app, every user            |
| `per_app_shared`    | deploy           | this app, every user             |
| `per_user`          | session start    | this user, every app they own    |
| `per_app_per_user`  | session start    | this user, this app              |

OAuth flows are forced to `per_user` (the access token is a
delegation from one human user).

## YAML reference

Two equivalent shapes:

```yaml
brain:
  provider: openai
  model: gpt-4o
  credential: openai_main          # compact form, defaults to per_user
```

```yaml
brain:
  provider: openai
  model: gpt-4o
  credential:                       # explicit form (recommended)
    ref: openai_main
    scope: per_user
    provider: openai                # optional sanity check
```

`credential.provider` is an optional cross-check: at compile time
the daemon verifies the named vault entry's `provider_name` matches.

## Modules expose slots

A consumer module declares one or more `CredentialSlot` instances:

```python
class LLMProviderModule(BaseModule):
    credential_slots = [
        CredentialSlot(
            id="brain_credential",
            label="LLM provider credential",
            handler_types=["api_key", "bearer_token", "oauth2"],
            providers=["openai", "anthropic", "deepseek", ...],
            scopes_preferred=["per_user", "system_wide"],
            inject={
                "api_key":      "{block}.config.api_key",
                "organization": "{block}.config.organization",
                "base_url":     "{block}.config.base_url",
            },
            required=False,
        ),
    ]
```

The compiler walks slots + manifests every consumer block; the
runtime injector reads `inject` to write decrypted fields at the
right path.

## Handler types

19 handlers ship with the daemon:

| Type                 | Use case                                        |
|----------------------|-------------------------------------------------|
| `api_key`            | Single-field secret (most LLMs)                 |
| `bearer_token`       | OAuth-style bearer (GitHub PAT, MCP)            |
| `basic_auth`         | username + password                             |
| `oauth2`             | Authorization code (Google, Slack, …)           |
| `oauth2_pkce`        | Public clients (mobile, CLI)                    |
| `device_code`        | TVs, CLIs, IoT devices                          |
| `multi_field`        | Generic key/value bag                           |
| `connection_string`  | DB urls (Postgres, Mongo, Redis, …)             |
| `aws_access_key`     | AKID + secret + region                          |
| `gcp_service_account`| Service account JSON                            |
| `azure_ad`           | Tenant + client + secret                        |
| `ssh_key`            | Private key + passphrase                        |
| `client_certificate` | mTLS cert + key                                 |
| `mcp_server`         | stdio MCP config                                |
| `mcp_http`           | HTTP MCP url + auth                             |
| `hmac_signing_secret`| Webhook signing                                 |
| `database_fields`    | Discrete host/port/user/password                |
| `file_upload`        | Up to 10 MB files                               |
| `custom`             | Schemaless escape hatch                         |

Add a handler by subclassing `CredentialHandler` and registering it
on `default_registry`.

## Provider catalog

Each provider ships a TOML template under
`packages/digitorn/core/credentials/catalog/builtins/`. The template
overrides handler defaults (icon, display_name, field labels,
verify endpoint) without touching code:

```toml
[provider]
name         = "stripe"
display_name = "Stripe"
handler_type = "multi_field"
icon         = "stripe"
category     = "payments"

[[fields]]
name         = "secret_key"
label        = "Secret key"
prefix_check = "sk_"
required     = true

[verify]
endpoint      = "https://api.stripe.com/v1/balance"
method        = "GET"
auth_template = "Authorization: Bearer {secret_key}"
success_codes = [200]
```

Drop a TOML file in the directory + restart the daemon.

## Security architecture

* **Master key**: `DIGITORN_KMS=env|file|aws|gcp|azure|vault`. The
  default `env` reads `DIGITORN_MASTER_KEY` (32 bytes
  base64url-encoded). Production deployments set KMS to one of the
  cloud providers; the data key is wrapped inside each row's
  ciphertext (envelope encryption).
* **Cipher**: AES-256-GCM with a per-record nonce. Versioned format
  with a 1-byte version, 1-byte flags, 1-byte backend identifier,
  2-byte wrapped-DEK length, then `nonce || ct`.
* **Audit log**: Every CRUD + inject + auth flow writes one row to
  `credential_audit`. Each row has `prev_hash || this_hash` chain.
  Verify integrity via `POST /api/admin/credentials/audit/verify`.
* **Log scrubbing**: Every plaintext value is registered with the
  global `LogScrubber` at decryption time. Subsequent log lines
  carrying the value have it redacted before write.
* **RBAC**: 4 roles (system_admin, app_admin, app_user, viewer)
  enforced via FastAPI deps `require_role`, `require_scope_read`,
  `require_scope_write`.

## API endpoints

```
GET    /api/credentials/providers              -> catalog (TOML + legacy)
GET    /api/credentials                        -> my vault
POST   /api/credentials                        -> create
GET    /api/credentials/{id}                   -> read (no plaintext)
PUT    /api/credentials/{id}                   -> update
DELETE /api/credentials/{id}                   -> delete
POST   /api/credentials/{id}/grant             -> grant to current app
POST   /api/credentials/{id}/grants            -> grant to specified app
DELETE /api/credentials/{id}/grants/{app_id}   -> revoke

POST   /api/oauth/start                        -> kick off OAuth flow
GET    /api/oauth/status                       -> poll flow state
POST   /api/oauth/refresh                      -> manual refresh
GET    /api/oauth/callback                     -> provider redirect target

GET    /api/apps/{id}/credentials/manifest     -> per-app credential needs
GET    /api/apps/{id}/credentials/schema       -> compile-time schema

GET    /api/credentials/health                 -> subsystem healthcheck
GET    /api/admin/credentials                  -> admin: all system creds
POST   /api/admin/credentials                  -> admin: create system cred
DELETE /api/admin/credentials/{id}             -> admin: delete
GET    /api/admin/credentials/audit            -> recent audit events
POST   /api/admin/credentials/audit/verify     -> chain integrity check
```

## CLI

```
digitorn credentials list
digitorn credentials show <id>
digitorn credentials create --provider X -f api_key=sk-...
digitorn credentials delete <id>
digitorn credentials admin-list
digitorn credentials admin-create --provider X -f api_key=sk-...

digitorn yaml migrate-credentials <file-or-dir> [--write] [--recursive]
```

## Migration from `{{secret.X}}` / `{{env.X}}`

Old apps used inline templates:

```yaml
brain:
  provider: deepseek
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"
```

New apps add a `credential:` block (the inline template can stay
as a fallback for dev):

```yaml
brain:
  provider: deepseek
  credential:
    ref: deepseek_main
    scope: per_user
    provider: deepseek
  config:
    api_key: "{{env.DEEPSEEK_API_KEY}}"
```

Run the migrator to do this automatically:

```
digitorn yaml migrate-credentials path/to/app.yaml --write
```

The compiler emits a warning when an app uses templates with no
`credential:` block, pointing to the migrate command.

## Lifecycle

* **Filled**: user just stored fields, never verified.
* **Valid**: passed `test_live_connection` or got a successful
  refresh.
* **Expired**: TTL hit, refresh failed, or admin marked it.
* **Invalid**: revoked, or remote rejected the credential.
* **Pending**: OAuth flow in progress.

The OAuth refresh loop runs every 5 minutes and refreshes any
credential whose `expires_at - now < 600 s`. Failures flip the
status to `expired` so the next chat shows the picker dialog.

## Files

```
core/credentials/cipher.py                AES-GCM + envelope wrapping
core/credentials/master_key/              KMS providers (env, file, AWS, GCP, Azure, Vault)
core/credentials/handler.py               Base handler + registry
core/credentials/handlers/                19 handlers
core/credentials/field_spec.py            Typed field schema
core/credentials/slot.py                  CredentialSlot dataclass
core/credentials/catalog/                 Provider TOML loader
core/credentials/store.py                 SQL-backed vault
core/credentials/audit/                   Hash-chained audit log + scrubber
core/credentials/rbac/                    4-role matrix
core/credentials/schema_yaml.py           CredentialReference Pydantic model
core/credentials/compile_credentials.py   Compile-time validation
core/credentials/inject_deploy_time.py    Deploy-time injection
core/credentials/inject_session_time.py   Session-time injection
core/credentials/runtime_resolver.py      Legacy `{{secret.X}}` fallback
core/credentials/oauth_flow.py            PendingFlowStore + TokenExchange
core/credentials/oauth_providers.py       OAuth registry (5 builtins)
core/credentials/oauth_refresh_loop.py    Background refresh task
core/api/credentials.py                   30+ HTTP endpoints
core/cli/credentials.py                   `digitorn credentials ...`
core/cli/yaml_migrate.py                  `digitorn yaml migrate-credentials`
```
