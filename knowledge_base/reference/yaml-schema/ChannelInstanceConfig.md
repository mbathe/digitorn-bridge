---
id: yaml-schema-channelinstanceconfig
title: "ChannelInstanceConfig - YAML schema reference"
type: schema-reference
model: ChannelInstanceConfig
is_root: false
keywords: [channelinstanceconfig, config, type, user_resolver]
---

# ChannelInstanceConfig

## Description
Configuration for a named output channel instance.

Each entry in the ``channels:`` block defines a channel instance
with a user-chosen name, a channel type, and type-specific config.

Optionally, a ``user_resolver`` auto-resolves per-user delivery targets
(email, phone, chat_id) from a data source - no manual ``output_config``
needed.

Example::

channels:
slack_alerts:
type: webhook
config:
url: "{{secret.SLACK_WEBHOOK}}"

sms_user:
type: sms
config:
account_sid: "{{env.TWILIO_SID}}"
from_number: "+33600000000"
user_resolver:
module: database
action: fetch_results
params:
query: "SELECT phone FROM users WHERE session_id = :session_id"
mapping:
to_number: phone

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str | ✓ | - | Channel type ID. Built-in: 'llm_notification', 'webhook', 'log'. Plugins: 'slack', 'gmail', 'telegram', 'kafka', 'sms', etc. (via pip install digitorn-channel-<type>) |
| `config` | dict[str, any] |  | `{}` | Channel-specific configuration. Supports {{variables}} and {{secret.X}} / {{env.X}} for credentials. See 'digitorn channel schema <type>' for available fields. |
| `user_resolver` | [UserResolverConfig](UserResolverConfig.md) \| null |  | `None` | Optional user resolver for auto-targeting notifications. When set, the channel automatically looks up the user's delivery address (email, phone, chat_id) from a data source using the session_id. No manual output_config needed. |

## Linked models
- [UserResolverConfig](UserResolverConfig.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
