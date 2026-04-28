---
id: yaml-schema-credentialproviderconfig
title: "CredentialProviderConfig - YAML schema reference"
type: schema-reference
model: CredentialProviderConfig
is_root: false
keywords: [credentialproviderconfig, command, docs_url, env_template, fields, health_check, icon, label, name, oauth_provider, oauth_scopes]
---

# CredentialProviderConfig

## Description
One provider entry inside ``credentials_schema.providers``.

Each provider declares which fields are needed, which handler
should process them (``type``), and which scope rules apply
(``per_user`` / ``per_app_shared`` / ``system_wide``).

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | str | ✓ | - | Internal provider id. Used as the path segment in ``/credentials/{app_id}/{provider_name}`` routes. |
| `label` | str |  | `''` | Human label for the UI. |
| `type` | 'api_key' \| 'multi_field' \| 'oauth2' \| 'connection_string' \| 'mcp_server' \| 'custom' |  | `'api_key'` | Handler type. Determines the form widget, validation rules, and lifecycle behaviour. |
| `scope` | 'per_user' \| 'per_app_shared' \| 'system_wide' |  | `'per_user'` | Where the credential lives: ``per_user`` means each user has their own (default), ``per_app_shared`` means one credential for all users of this app, ``system_wide`` means daemon-level config (admin only). |
| `required` | bool |  | `True` | Whether the app refuses to run without this provider filled. |
| `icon` | str |  | `''` | Logo URL shown in the form. |
| `docs_url` | str |  | `''` | Link to the provider's docs / 'where do I get this?' |
| `fields` | list[[CredentialFieldConfig](CredentialFieldConfig.md)] |  | `[]` | Fields the user must fill. |
| `oauth_provider` | str |  | `''` | For ``type: oauth2``: the key of the OAuth provider registered on the daemon (notion, google, github, slack). The daemon's client_id / client_secret for this provider must be configured by the admin. |
| `oauth_scopes` | list[str] |  | `[]` | OAuth scopes to request during the flow. |
| `transport` | 'stdio' \| 'http' \| 'ws' \| '' |  | `''` | For ``type: mcp_server``: stdio / http / ws. |
| `command` | list[str] |  | `[]` | For stdio MCP servers: command + args to spawn. |
| `url` | str |  | `''` | For http/ws MCP servers: the server URL. |
| `env_template` | dict[str, str] |  | `{}` | For MCP servers: extra env vars to inject into the spawned process. Supports ``{{field.X}}`` substitution pulling from the filled credential fields. |
| `health_check` | dict[str, any] |  | `{}` | For MCP servers: how to check the server is alive. e.g. ``{method: tools/list, timeout_s: 5}``. |
| `test` | dict[str, any] |  | `{}` | Optional live-connection test declaration. For api_key: ``{method, url, auth_header, expected_status}``. For connection_string: ``{test_query}``. |

## Linked models
- [CredentialFieldConfig](CredentialFieldConfig.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
