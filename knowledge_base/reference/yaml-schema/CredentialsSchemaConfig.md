---
id: yaml-schema-credentialsschemaconfig
title: "CredentialsSchemaConfig - YAML schema reference"
type: schema-reference
model: CredentialsSchemaConfig
is_root: false
keywords: [credentialsschemaconfig, providers, required]
---

# CredentialsSchemaConfig

## Description
Declarative credentials schema for a Digitorn app.

When set, the Flutter client fetches this from
``GET /api/apps/{id}/credentials/schema`` and renders a typed
form for each provider. The daemon's resolver also uses it to
know what's expected so it can fail with a clean "credential
missing" error rather than a cryptic compile-time secret miss.

Example::

credentials_schema:
required: true
providers:
- name: openai
label: OpenAI
type: api_key
scope: per_user
fields:
- name: api_key
type: secret
required: true
validation_regex: "^sk-[A-Za-z0-9_-]{20,}$"
- name: notion
type: oauth2
oauth_provider: notion
scope: per_user
oauth_scopes: [read_content, update_content]
- name: notion_mcp
type: mcp_server
transport: stdio
command: ["npx", "-y", "@modelcontextprotocol/server-notion"]
env_template:
NOTION_API_KEY: "{{field.api_key}}"
fields:
- name: api_key
type: secret
required: true

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `required` | bool |  | `True` | If true, the daemon blocks activation when any required provider is not filled for the user. |
| `providers` | list[[CredentialProviderConfig](CredentialProviderConfig.md)] |  | `[]` | Declared credential providers. |

## Linked models
- [CredentialProviderConfig](CredentialProviderConfig.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
