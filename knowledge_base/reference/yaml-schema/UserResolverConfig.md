---
id: yaml-schema-userresolverconfig
title: "UserResolverConfig - YAML schema reference"
type: schema-reference
model: UserResolverConfig
is_root: false
keywords: [userresolverconfig, action, cache_ttl, mapping, module, params]
---

# UserResolverConfig

## Description
Configuration for auto-resolving user-specific delivery targets.

When a channel delivers a notification, the resolver automatically
looks up the user's contact info (email, phone, chat_id, etc.) from
a data source, using the session_id to identify who the user is.

This works like authentication middleware: the system knows who the
user is and adapts. One app serves 10,000 users - no per-user
configuration needed.

Example::

user_resolver:
module: database
action: fetch_results
params:
query: "SELECT phone, email FROM users WHERE session_id = :session_id"
mapping:
to_number: phone
to_address: email
cache_ttl: 300

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `module` | str | ✓ | - | Module ID to query for user info (e.g. 'database', 'http'). Must be declared in the app's modules: block. |
| `action` | str | ✓ | - | Action to call on the module (e.g. 'fetch_results', 'get'). The action should return user-specific data. |
| `params` | dict[str, any] |  | `{}` | Parameters for the action. Use ':session_id' or '{{session_id}}' as a placeholder - it will be replaced with the actual session ID at delivery time. |
| `mapping` | dict[str, str] |  | `{}` | Maps result field names to per-delivery config field names. e.g. {'to_number': 'phone'} means: take the 'phone' column from the query result and pass it as the channel's 'to_number'. |
| `cache_ttl` | float |  | `300.0` | How long to cache resolved results in seconds. 0 = no cache. Default: 300 (5 min). |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
