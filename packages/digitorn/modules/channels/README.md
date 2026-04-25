# Channels Module

Unified bidirectional I/O for Digitorn applications.

## Overview

The Channels module provides a single, YAML-configured system that can both
**receive** events (webhooks, cron, email, file changes, RSS, queues) and
**send** messages through the same or different channels. It replaces
separate trigger and output channel systems with one composable module.

Each channel provider is backed by an **adapter** — a pluggable transport
layer. Built-in adapters cover the most common transports; custom adapters
can be added with a single Python file.

| Adapter | Direction | Transport |
|---------|-----------|-----------|
| `webhook` | Bidirectional | HTTP POST in/out |
| `email` | Bidirectional | IMAP polling + SMTP send |
| `queue` | Bidirectional | Bridge to QueueModule |
| `cron` | Inbound only | Cron schedule triggers |
| `file_watcher` | Inbound only | Filesystem polling (glob) |
| `rss` | Inbound only | RSS/Atom feed polling |
| `log` | Outbound only | Python logging |

## Key Features

- **Activation pipeline** — filter, prepare (DB lookup, RAG query), route, build messages, agent_turn, reply
- **Session strategies** — per_event (stateless), shared (conversational), template (custom key)
- **Dynamic routing** — pick the right agent based on enriched event data
- **reply: auto** — agent response automatically sent back on originating channel
- **16 security measures** — HMAC, API key, payload sanitization, SSRF, secret filtering, template safety
- **Concurrency control** — per-provider semaphore, session locking
- **Agent actions** — send_message, reply, broadcast, pause/resume, stats, simulate

## Actions (12)

| Action | Description | Risk |
|--------|-------------|------|
| **Sending** | | |
| `send_message` | Send through a specific provider | Medium |
| `reply` | Reply on the originating channel | Medium |
| `broadcast` | Send to multiple providers | High |
| **Management** | | |
| `list_providers` | List providers and status | Low |
| `provider_status` | Detailed provider status | Low |
| `pause_provider` | Pause inbound listener | Medium |
| `resume_provider` | Resume paused listener | Medium |
| **Observability** | | |
| `provider_history` | Recent event history | Low |
| `stats` | Aggregate statistics | Low |
| **Testing** | | |
| `simulate_event` | Simulate an inbound event | Medium |
| `test_send` | Test outbound connectivity | Medium |

## Architecture

```
ChannelsModule (BaseModule)
    │
    ├── Adapters (BaseChannelAdapter → BaseOutputChannel)
    │       ├── WebhookAdapter (HTTP in + out)
    │       ├── CronAdapter (schedule trigger)
    │       ├── FileWatcherAdapter (glob polling)
    │       ├── EmailAdapter (IMAP + SMTP)
    │       ├── RssAdapter (feed polling)
    │       ├── LogAdapter (logging output)
    │       └── QueueAdapter (QueueModule bridge)
    │
    ├── ActivationPipeline
    │       filter → prepare → route → build → agent_turn → reply
    │
    ├── ChannelSessionManager
    │       per_event / shared / template sessions
    │
    ├── Security (security.py)
    │       HMAC, API key, sanitization, SSRF, secret filter
    │
    └── Template (template.py)
            Safe {{var}} substitution (no eval)
```

## App YAML Configuration

```yaml
modules:
  channels:
    config:
      providers:
        my_webhook:
          adapter: webhook
          config:
            inbound_path: "/hook/events"
            url: "{{secret.SLACK_WEBHOOK}}"
          activation:
            filter:
              - field: event.payload.status
                equals: "new"
            prepare:
              - action: database.fetch_results
                params: { query: "SELECT * FROM clients WHERE id = {{event.payload.client_id}}" }
                as: client
            route:
              field: client.0.plan
              rules:
                - match: premium
                  agent: vip_support
                - default: true
                  agent: general
            session: "ticket-{{event.payload.id}}"
            context: "Client: {{client.0.name}}"
            reply: auto
      default_agent: general
      max_turns: 30
      timeout: 120
      secret_filter_enabled: true
```

## LLM Usage

```
1. channels.list_providers  →  see available channels
2. channels.send_message    →  send via specific provider
3. channels.reply           →  reply on originating channel
4. channels.broadcast       →  alert multiple channels
5. channels.stats           →  monitor channel activity
```
