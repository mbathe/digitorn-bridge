---
id: channels
title: Channels Module
sidebar_label: channels
description: Unified bidirectional channels - inbound (webhooks, cron, email, file watch, RSS, queue) + outbound (Slack, Telegram, email, Discord, webhook, ...) through one YAML providers block.
---

# channels

The **channels** module is Digitorn's unified bidirectional I/O layer. One
YAML `providers:` block declares every input adapter (webhook, cron, email,
file watcher, RSS, queue) **and** every output channel (Slack, Telegram,
Discord, email, webhook, log). Events arriving through any inbound adapter
are run through an activation pipeline that starts an agent turn; agents
can send replies or broadcasts back through the same or a different
provider.

| Property | Value |
|----------|-------|
| **Module ID** | `channels` |
| **Version** | `1.0.0` |
| **Platform** | All |
| **Actions exposed to LLM** | 11 |
| **Activation pipeline** | `modules/channels/pipeline.py::ActivationPipeline` |

---

## Provider types

Registered in `modules/channels/adapters/__init__.py::_BUILTIN_ADAPTERS`:

| Adapter | Inbound | Outbound | Typical use |
|---------|---------|----------|-------------|
| `webhook` | yes | yes | HTTP POST in / HTTP POST out |
| `cron` | yes | - | Scheduled activations |
| `file_watcher` | yes | - | Trigger on filesystem changes |
| `email` | yes | yes | IMAP in / SMTP out |
| `rss` | yes | - | Poll feeds |
| `log` | - | yes | Write to logger |
| `queue` | yes | yes | Read/write a named queue |
| `telegram` | yes | yes | Telegram bot API |
| `discord` | yes | yes | Discord bot |
| `slack` | yes | yes | Slack bot |
| `voice` | yes | yes | Voice adapter |

Register custom adapters at runtime via
`modules.channels.adapters.register_adapter(type_name, cls)`.

---

## Configuration

From `ChannelsModuleConfig`:

```yaml
modules:
  channels:
    config:
      default_agent: ""            # agent id for activations (empty = entry agent)
      max_turns: 30                # per-activation turn cap
      timeout: 120.0               # seconds per activation
      history_limit: 200           # event records kept in memory
      secret_filter_enabled: true  # strip secrets from outbound text

      providers:
        notify_slack:              # provider id used by send/reply
          adapter: slack
          enabled: true
          max_concurrent: 5        # max concurrent activations
          config:                   # adapter-specific
            bot_token: "${SLACK_BOT_TOKEN}"
            default_channel: "#alerts"
          activation:               # inbound pipeline (ignored for outbound-only)
            agent: ""
            session: per_event      # per_event | shared | template
            message: "{{event.message}}"
            context: ""
            expose_data: false
            reply: auto             # auto | none | explicit
            filter: []              # drop events that don't match
            prepare: []             # pre-activation tool calls
            route: null             # dynamic agent routing
```
### `activation` - the inbound pipeline

When an inbound event hits an adapter, it's pushed through
`ActivationPipeline.process_event(event, provider)`:

1. **Filter.** Drop events that don't match all `filter[]` conditions
   (`equals`, `not_equals`, `contains`, `gt`, `lt` on a dot-path field).
2. **Prepare.** Call tools via the service bus and stash results under
   `as: <name>` - later available as `{{prepared.<name>}}` in templates.
3. **Route.** If `route:` is set, pick an agent by matching `field` against
   `rules[].match` (falls back to `rules[*].default`).
4. **Session.** Pick or create a session based on `session:`
   (`per_event`, `shared`, or a `{{template}}` that resolves to a key).
5. **Activate.** Render `message` + `context` templates, call
   `agent_turn` with `max_turns` + `timeout` from the module config.
6. **Reply.** If `reply: auto`, the first agent reply is sent back through
   the originating adapter via `adapter.send_reply(reply_ctx, text)`.
   If `explicit`, the agent must call `channels.reply`. If `none`, no reply.

### Session strategies

| Value | Behavior |
|-------|----------|
| `per_event` | One fresh session per inbound event |
| `shared` | One session for the whole provider (survives daemon restart via DB restore) |
| `{{template}}` | Custom routing key (e.g. `user:{{event.user_id}}`, `thread:{{event.thread_id}}`) |

Shared sessions survive daemon crashes: `ChannelSessionManager.restore_active_sessions()`
reloads them from the database on `start_listeners()`.

### Prepare step

```yaml
activation:
  prepare:
    - action: database.fetch_results
      params:
        user_id: "{{event.headers.X-User-Id}}"
      as: user_profile
  message: "Hi {{prepared.user_profile.name}}, your ticket is ready."
```
### Route rules

```yaml
activation:
  route:
    field: event.payload.priority
    rules:
      - match: high
        agent: incident_responder
      - match: low
        agent: support_bot
      - default: true
        agent: triage_bot
```
---

## Actions (11)

| Action | Visible params | Risk | Purpose |
|--------|---------------|------|---------|
| `send_message` | `provider`, `text`, `subject?`, `recipient?`, `metadata?`, `thread_id?` | medium | Send on one provider |
| `reply` | `text`, `metadata?` | medium | Reply to the triggering inbound event (uses stored `_channel_reply_context`) |
| `broadcast` | `providers: list`, `text`, `subject?`, `metadata?` | high | Fan out to many providers |
| `list_providers` | `include_status?: bool` | low | List configured providers (+ status if asked) |
| `provider_status` | `provider` | low | Full status + capability summary for one provider |
| `pause_provider` | `provider` | medium | Stop the inbound listener |
| `resume_provider` | `provider` | medium | Restart the inbound listener |
| `provider_history` | `provider?`, `direction: inbound\|outbound\|all`, `limit` | low | Recent event records |
| `stats` | - | low | Aggregate counters across all providers |
| `simulate_event` | `provider`, `source`, `message`, `payload?` | medium | Debug: push a synthetic inbound event |
| `test_send` | `provider`, `text` | medium | Debug: send a smoke-test outbound message |

Aliases: `envoyer_message`, `repondre`, `diffuser`, `lister_canaux`,
`historique_canaux`, `stats_canaux`, `pause_canal`, `reprendre_canal`.

### Secret filtering

When `secret_filter_enabled: true` (default), every outbound `text` is run
through `security.filter_secrets` before delivery - API keys, tokens, and
other obviously-sensitive patterns are masked.

---

## Lifecycle - three phases

| Phase | Method | What happens |
|-------|--------|-------------|
| 1. Deploy | `on_config_update(cfg)` | Parse config, create adapters, call `adapter.on_start()`. Listeners NOT started yet. Providers are `status="ready"`. |
| 2. Run | `start_listeners()` | Restore shared sessions from DB; launch one `asyncio.Task` per inbound listener. Providers flip to `status="active"`. |
| 3. Stop | `on_stop()` | Cancel all listener tasks, await in-flight activations, call `adapter.stop_listener()` + `adapter.on_stop()`. |

Splitting phases 1 & 2 lets the daemon validate config at deploy time without
actually binding to webhooks / IMAP / Telegram until the app is actually run
(via `run_background` or entry-point HTTP activation).

### Event persistence

Every inbound event is persisted to `ActionExecution` before processing
(status=`started`) and marked `completed` after. This gives at-least-once
delivery semantics across daemon crashes.

### Bounded concurrency

Each provider has an `asyncio.Semaphore(max_concurrent)`. The pipeline
acquires it before activating, so a flood of inbound events won't spawn
unlimited agent turns.

---

## Constraints

From `CONSTRAINTS`:

| Name | Type | Default | Purpose |
|------|------|---------|---------|
| `allowed_adapters` | string_list | - | Restrict which adapter types this app can use |
| `max_providers` | integer | 20 | Upper bound on provider instance count |

---

## Security

- **Payload size limits, HMAC/API-key auth, content-type whitelists** are
  enforced per-adapter (webhook, email).
- **Per-source rate limiting** is applied at the adapter layer.
- **SSRF protection, secret filtering, header masking** on outbound delivery.
- **Isolated adapter configs** - each adapter gets its own copy; no
  cross-adapter leakage.
- **No eval/exec in templates** - single-pass `{{var}}` substitution only,
  no runtime secret access.
- **Loopback auth bypass** (for agent self-calls on `/api/apps/...`) does
  NOT apply to channel providers - every inbound webhook still goes through
  its adapter's auth layer.

---

## Integration notes

- **Not Socket.IO.** Channel events don't hit the preview/widget bus. Inbound
  events flow through the activation pipeline into an agent turn; outbound
  deliveries happen via adapter-specific transports (SMTP, HTTPS, bot APIs).
- **No SSE.** Webhooks respond synchronously with the activation's first
  reply text (or a fire-and-forget ack when `reply: none`).
- **No workbench.** Channel state is visible only through the module's own
  `list_providers` / `provider_history` / `stats` actions.

---

## Related

- `modules/channels/adapter.py` - `BaseChannelAdapter` contract
- `modules/channels/adapters/` - built-in adapter implementations
- `modules/channels/pipeline.py` - `ActivationPipeline`
- `modules/channels/session_manager.py` - `ChannelSessionManager` (shared-session restore)
- `modules/channels/security.py` - `filter_secrets`, `sanitize_payload`,
  `generate_webhook_token`
- `CLAUDE.md` - section *Background Trigger Routing*
