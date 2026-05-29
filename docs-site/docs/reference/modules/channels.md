---
id: channels
title: channels Module
sidebar_label: channels
description: Bidirectional I/O - 11 adapters (webhooks, email, Slack, Telegram, Discord, voice, ...) + 11 LLM actions, full activation pipeline.
---

# channels

The **channels** module is Digitorn's unified bidirectional
I/O layer. One YAML `providers:` block declares every input
adapter (webhook, cron, email, file watcher, RSS, queue) and
every output channel (Slack, Telegram, Discord, email,
webhook, log). Inbound events run through an activation
pipeline that starts an agent turn; agents reply or
broadcast through the same or different providers.

| Property | Value |
|----------|-------|
| Module id | `channels` |
| Version | `1.0.0` |
| LLM-exposed actions | 11 |
| Adapter count | 11 built-ins |
| Activation pipeline | |

> **Full reference** (every adapter, full activation pipeline,
> security, custom adapter API, complete IT-support example):
> [Channels](../../language/40-channels.md). This page is
> a quick module-level summary.

## The 11 built-in adapters

 Lazy imports - adding the
optional pip dep enables the adapter at restart.

| Adapter | Inbound | Outbound | Optional dep | Purpose |
|---------|:-------:|:--------:|--------------|---------|
| `webhook` | yes | yes | `aiohttp` (outbound) | HTTP POST in / out. |
| `cron` | yes | - | `croniter` (precise) | Scheduled activations. |
| `file_watcher` | yes | - | none | Trigger on filesystem changes. |
| `email` | yes | yes | none (stdlib `imaplib` / `smtplib`) | IMAP in / SMTP out. |
| `rss` | yes | - | `feedparser` | Poll RSS / Atom feeds. |
| `log` | - | yes | none | Structured Python logging. |
| `queue` | yes | yes | none | Bridge to the `queue` module. |
| `telegram` | yes | yes | `aiohttp` | Bot API (long polling + REST). |
| `discord` | yes | yes | `aiohttp` | WebSocket Gateway + REST. |
| `slack` | yes | yes | `aiohttp` | Socket Mode + Web API. |
| `voice` | yes | yes | `aiohttp` (+ `edge-tts`) | Phone / browser calls (Twilio CR + WebSocket backends). |

Register custom adapters at runtime:

```python
from digitorn.modules.channels.adapters import register_adapter
from my_pkg.kafka_adapter import KafkaAdapter
register_adapter("kafka", KafkaAdapter)
```

## The 11 actions



| Tool | Source | Risk | Purpose |
|------|--------|:----:|---------|
| `channels.send_message` | | medium | Send on a specific provider. |
| `channels.reply` | | medium | Reply on the channel that triggered this activation (uses `reply_context`). |
| `channels.broadcast` | | high | Fan out the same message to many providers. |
| `channels.list_providers` | | low | Configured providers + available adapter catalog. |
| `channels.provider_status` | | low | Status / capabilities of one provider. |
| `channels.pause_provider` | | medium | Pause inbound listener. |
| `channels.resume_provider` | | medium | Resume a paused listener. |
| `channels.provider_history` | | low | Recent inbound + outbound history. |
| `channels.stats` | | low | Aggregate counters across providers. |
| `channels.simulate_event` | | medium | Drop a synthetic inbound event into a provider. |
| `channels.test_send` | | medium | Outbound smoke test. |

Aliases (FR + EN): `envoyer_message`, `repondre`, `diffuser`,
`lister_canaux`, `historique_canaux`, `stats_canaux`,
`pause_canal`, `reprendre_canal`.

## Module-level config

`ChannelsModuleConfig`:

```yaml
tools:
  modules:
    channels:
      config:
        default_agent: ""             # empty → entry agent
        max_turns: 30                 # [1, 200]
        timeout: 120.0                # [5, 3600] seconds per activation
        history_limit: 200            # event records kept in memory
        secret_filter_enabled: true   # mask secrets in outbound text

        providers:
          notify_slack:
            adapter: slack
            enabled: true
            max_concurrent: 5         # [1, 100] concurrent activations
            config:
              bot_token: "{{secret.SLACK_BOT_TOKEN}}"
            activation:
              session: per_event      # per_event | shared | "{{template}}"
              message: "{{event.message}}"
              reply: auto             # auto | none | explicit
              filter: []
              prepare: []
              route: null
```

## The activation pipeline (per-provider)

::ActivationPipeline.process_event(event,
provider)`:

1. **Filter** - drop events that don't match every
   `filter[]` condition (`equals` / `not_equals` /
   `contains` / `gt` / `lt` on a dot-path field).
2. **Prepare** - call tools via the ServiceBus, stash
   results under `as: <name>`, available later as
   `{{<name>.X}}`.
3. **Route** - pick an agent by matching `field` against
   `rules[].match` (falls back to the `default: true` rule
   then to `default_agent`).
4. **Session** - pick or create a session keyed by
   `session:` (`per_event` / `shared` / `{{template}}`).
   Shared sessions hold an `asyncio.Lock` per session key
   so concurrent events serialise.
5. **Activate** - render `message` + `context` templates,
   call `agent_turn` with `max_turns` + `timeout` from the
   module config.
6. **Reply** - `reply: auto` sends the agent's first reply
   back through the originating adapter via
   `adapter.send_reply(reply_ctx, text)`. `explicit` →
   agent must call `channels.reply`. `none` → no reply.

## Lifecycle (3 phases)

| Phase | Method | What happens |
|-------|--------|--------------|
| 1. Deploy | `on_config_update(cfg)` | Parse config, create adapters, call `adapter.on_start`. Listeners NOT started yet. Providers `status="ready"`. |
| 2. Run | `start_listeners` | Restore shared sessions from DB; launch one `asyncio.Task` per inbound listener. Providers flip to `status="active"`. |
| 3. Stop | `on_stop` | Cancel all listener tasks, await in-flight activations, call `adapter.stop_listener` + `adapter.on_stop`. |

Splitting deploy from run lets the daemon validate config at
deploy time without binding to webhooks / IMAP / Telegram
until the app actually starts (via `run_background` or an
entry-point HTTP activation).

## Security highlights

| # | Guard |
|---|-------|
| 1 | Payload size enforced before JSON parse (webhook). |
| 2 | HMAC SHA-256 with constant-time compare (`hmac.compare_digest`). |
| 3 | API key constant-time compare. |
| 4 | Content-Type whitelist + sanitisation (`__proto__`, `__class__`, ...). |
| 5 | Sensitive header stripping (`Authorization`, `Cookie`, `X-API-Key`, `X-Signature-*`). |
| 6 | Outbound SSRF blocklist (RFC 1918, loopback, link-local, multicast, AWS / GCP metadata). |
| 7 | Outbound secret filtering (OpenAI `sk-*`, Anthropic `sk-ant-*`, GitHub `ghp_*`, AWS `AKIA*`, JWT `eyJ*`, Bearer, Basic, Digitorn `dk_*`). |
| 8 | Header masking in logs (`Authorization`, `Cookie`, `X-API-Key` → `***masked***`). |
| 9 | No eval / exec in templates - single-pass `{{var}}`. |
| 10 | `{{secret.*}}` / `{{env.*}}` blocked at runtime (compile-time only). |
| 11 | Per-provider `max_concurrent` semaphore + per-shared-session lock. |

There is no daemon-wide loopback auth bypass, and channel inbound
endpoints don't add one either - every webhook still goes through
its adapter's HMAC / token / signature auth layer regardless of
source IP.

## Constraints

:

| Name | Type | Default | Purpose |
|------|------|---------|---------|
| `allowed_adapters` | `string_list` | unrestricted | Restrict which adapter types this app can use. |
| `max_providers` | `integer` | `20` | Upper bound on provider instance count. |

## Cross-references

- Full channels reference (every adapter, full pipeline,
  custom-adapter API, complete IT-support example):
  [Channels](../../language/40-channels.md)
- App-config block reference (`tools.modules.channels`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- The `channels` adapter for the legacy `runtime.triggers` /
  background-mode trigger system:
  [Triggers](../../language/09-triggers.md),
  [Background Sessions](../../language/38-background-sessions.md)
- Credentials vault for adapter secrets:
  [credentials.md](../../reference/runtime/credentials.md)
