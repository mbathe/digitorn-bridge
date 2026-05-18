---
id: security-05-credentials
title: "Security 5 - Credentials vault and scopes"
sidebar_label: "Security 5: Credentials"
---

API keys, OAuth tokens, mTLS pairs - every real Digitorn app
needs at least one secret. The naive way is to drop them in env
vars and pull them through `{{env.X}}` at compile time. That's
fine for a single-user dev daemon. The moment you have **more
than one user** or **more than one app** sharing a daemon, env
vars stop scaling: the same env value gets resolved for everyone,
secrets land in the same compiled bundle, rotation requires a
redeploy.

The **credentials vault** is the structured alternative. Every
secret lives in an encrypted store with **four scope rules**, a
typed contract declared in the YAML, and **compile-time
validation** that catches typos before the app ever runs.

## The four scopes

| Scope               | Resolved when     | Visible to                                          | Typical use                          |
|---------------------|-------------------|-----------------------------------------------------|--------------------------------------|
| `system_wide`       | Deploy time       | Every app, every user (set by an admin)             | Shared infrastructure keys           |
| `per_app_shared`    | Deploy time       | This app, every user                                | App-wide service account             |
| `per_user`          | Session start     | This user, every app they own                       | Personal API key                     |
| `per_app_per_user`  | Session start     | This user, this app only                            | OAuth tokens scoped to one workspace |

Resolution is **strict**: the YAML's `credential.ref` matches by
name and scope. If a credential exists with the same name at a
different scope, it does **not** resolve. No fallback cascade.
This is intentional - silent fallbacks are how production secret
mixups happen.

## Live - creating credentials at two scopes

Before the test the user has nothing in the vault. Two
`create_user_credential` calls plant credentials at **different
scopes** with **different provider names**:

```python
# 1. Per-user DeepSeek key (used by every DeepSeek-backed app)
client.create_user_credential(
    provider_name="deepseek",
    fields={"api_key": "sk-..."},
    label="deepseek_main",
    scope="per_user",
)

# 2. Per-app-per-user OpenAI key (used by exactly one app per user)
client.create_user_credential(
    provider_name="openai",
    fields={"api_key": "sk-..."},
    label="openai_for_memory",
    scope="per_app_per_user",
)
```

Listing the user's credentials returns both, with their scopes
captured:

```text
- name=deepseek label=deepseek_main      scope=per_user           provider=deepseek
- name=openai   label=openai_for_memory  scope=per_app_per_user   provider=openai
```

The two co-exist. They never collide because one is `provider=deepseek`
and the other `provider=openai`. The `per_app_per_user` scope
will be populated with the **app_id** at session start, when the
user actually opens a session against the app.

## Compile-time schema validation

The vault decides what *can be stored*. The app's
**`credentials_schema`** decides what the app *expects to find*.
The compiler cross-references the two and catches mismatches
before deploy succeeds.

```yaml
agents:
  - id: main
    brain:
      provider: deepseek
      credential:
        ref: nonexistent_credential       # typo
        scope: per_user
        provider: deepseek

security:
  credentials_schema:
    required: true
    providers:
      - name: deepseek_main               # only this is declared
        label: DeepSeek
        type: api_key
        scope: per_user
        fields:
          - name: api_key
            type: secret
            required: true
```

Real deploy attempt against the daemon:

```text
deploy success: False
error: App compilation failed (1 error(s)): agents[0].brain.credential:
       credential ref 'nonexistent_credential' is not declared in
       execution.credentials_schema.providers.
       Declared: ['deepseek_main'].
```

The mistake is caught at compile - the agent never spins up with
a broken credential reference. Compare with env-var-based config
where the typo would surface as `KeyError` in production logs at
the first LLM call.

## The schema's other job

`credentials_schema` is also the contract the **client renders as
a form**. When a user installs the app, the the chat client / web client
pulls the schema and shows a typed input for every required
field:

```yaml
security:
  credentials_schema:
    required: true
    providers:
      - name: notion_main
        label: "Notion workspace"
        type: oauth2
        scope: per_user
        oauth_provider: notion          # pre-registered OAuth flow
      - name: stripe_secret
        label: "Stripe API key"
        type: api_key
        scope: per_app_per_user
        fields:
          - name: api_key
            type: secret
            required: true
            validation_regex: "^sk_(live|test)_[a-zA-Z0-9]{24,}$"
```

The user sees an "Install app" page with two fields: an OAuth
button for Notion and a text input for the Stripe key. The
client validates the Stripe key against the regex client-side
before posting; the daemon validates it again server-side.

`oauth2` types skip the field rendering entirely - the client
opens the OAuth provider's authorisation page and the daemon
handles the token exchange + refresh in the background.

## Encryption + audit

Every credential row in the vault is **envelope-encrypted**: the
field values are encrypted with a per-row data encryption key
(DEK), the DEK itself is encrypted with the daemon's master key
(`DIGITORN_MASTER_KEY`), and only the wrapped DEK is stored in
the database. Decryption requires both the row and the master
key - dumping the database alone gives nothing.

The master key supports several backends via `DIGITORN_KMS=env|
file|aws|gcp|azure|vault`. Production deployments use a real KMS
so the master key never sits on disk.

Every read, write, refresh, or revoke also writes a row to the
**hash-chained `credential_audit` table**. Each row carries the
hash of the previous row; verifying the chain
(`POST /api/admin/credentials/audit/verify`) detects any
tampering or selective deletion.

## Resolution at runtime

When a session starts, the daemon resolves each `credential.ref`
in the YAML by querying the vault for `(provider_name, scope,
user_id, app_id)`. The match must be exact - no fallback. If
nothing matches, the session **fails fast** with a structured
"credential missing" error instead of attempting the LLM call
and hitting a cryptic 401.

Per-user credentials get hot-swapped onto the live LLM provider
instances at session start; the agent loop never sees a stale key.
Per-app-shared and system-wide creds resolve at deploy time and
get baked into the compiled app definition.

The full lifecycle - field encryption, master-key wrapping,
session-time injection, OAuth refresh loop, revocation flow - is
documented in
[Credentials reference](../reference/runtime/credentials.md).

## Picking the right scope

Three quick rules:

1. **If the secret is your personal account credential**
   (your OpenAI key, your GitHub token), use `per_user`.
   Resolves for every app you run.
2. **If the secret is bound to an app's data** (the OAuth
   token for "your Notion workspace, accessed by the Notion
   assistant app"), use `per_app_per_user`. Different per
   app, different per user.
3. **If the secret is a service account** the app shares
   across all users (a single Stripe restricted key for a
   shared admin tool), use `per_app_shared`.

`system_wide` is admin territory and rarely the right choice
for a tenant-isolated daemon.

## Going further

- Full credentials reference (the 19 handler types, OAuth
  refresh loop, audit-log integrity check):
  [Credentials](../reference/runtime/credentials.md).
- The legacy `{{secret.X}}` template form still works for
  backwards compatibility but doesn't get the typed schema or
  the audit chain - migrate via:
  [`digitorn yaml migrate-credentials`](../language/46-dev-cli.md).
- Companion to the seven security gates - the credentials
  resolver runs **before** gate 0 in the request flow:
  [Security 2 - Seven gates](security-02-gates.md).
