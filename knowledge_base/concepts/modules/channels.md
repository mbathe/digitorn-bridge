---
id: module-concept-channels
title: "channels module - overview"
type: module-concept
module: channels
isolation: shared
keywords: [channels, channels-module, send_message, reply, broadcast, list_providers, provider_status, pause_provider, resume_provider, provider_history, stats, simulate_event, test_send]
version: 1.0.0
---

# `channels` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 11 visible, 0 internal

## Description (from class docstring)

Unified bidirectional channels module.

Provides inbound event reception (webhooks, cron, email, file watch, RSS, queue)
and outbound message delivery through the same or different channels. Everything
is configured via YAML in the ``providers:`` block.

Security is enforced at every layer:
- Inbound: payload size limits, HMAC/API-key auth, content-type whitelist,
  payload sanitization, per-source rate limiting.
- Outbound: SSRF protection, secret filtering, header masking.
- Templates: no eval/exec, no runtime secret access, single-pass only.
- Isolation: each adapter gets its own config copy, no cross-adapter access.

## Configuration

Set under `modules.channels.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `providers` | dict |  | `{}` | Named provider instances - each is a free-form adapter config. |
| `default_agent` | str |  | `''` |  |
| `max_turns` | int |  | `30` |  |
| `timeout` | float |  | `120.0` |  |
| `history_limit` | int |  | `200` |  |
| `secret_filter_enabled` | bool |  | `True` |  |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `send_message` | `ChannelsSendMessage` |  | medium | Send a message through a specific channel provider. |
| `reply` | `ChannelsReply` |  | medium | Reply to the current inbound event on its originating channel. |
| `broadcast` | `ChannelsBroadcast` |  | high | Broadcast a message to multiple channel providers. |
| `list_providers` | `ChannelsListProviders` |  | low | List all configured channel providers and their status. |
| `provider_status` | `ChannelsProviderStatus` |  | low | Get detailed status of a specific provider. |
| `pause_provider` | `ChannelsPauseProvider` |  | medium | Pause a provider's inbound listener. |
| `resume_provider` | `ChannelsResumeProvider` |  | medium | Resume a paused provider's inbound listener. |
| `provider_history` | `ChannelsProviderHistory` |  | low | Get recent event history for channels. |
| `stats` | `ChannelsStats` |  | low | Get aggregate statistics for all channel providers. |
| `simulate_event` | `ChannelsSimulateEvent` |  | medium | Simulate an inbound event for testing purposes. |
| `test_send` | `ChannelsTestSend` |  | medium | Send a test message to verify outbound connectivity. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: channels
      actions: [send_message, reply, broadcast, list_providers, provider_status, pause_provider, resume_provider, provider_history, stats, simulate_event, test_send]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {channels: [send_message, reply, broadcast, list_providers, provider_status]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/channels-*.md`.
