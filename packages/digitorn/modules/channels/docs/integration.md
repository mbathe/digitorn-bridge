# Channels Module — Integration Guide

## YAML Configuration

```yaml
modules:
  channels:
    config:
      providers:
        my_webhook:
          adapter: webhook
          config:
            inbound_path: "/hook/events"
            auth: signature
            signature_secret: "{{secret.WEBHOOK_SECRET}}"
          activation:
            message: "Event: {{event.payload.action}}"
            reply: auto
    constraints:
      allowed_adapters: [cron, webhook, log]
      max_providers: 10
```

## Security Integration

### Permissions

| Permission | Used by |
|------------|---------|
| `net.http` | Webhook outbound delivery, SSRF-protected |
| `state_mutation` | pause_provider, resume_provider, simulate_event |
| `network_io` | send_message, reply, broadcast, test_send |

Grant permissions via capabilities:

```yaml
capabilities:
  grant:
    - channels.list_providers
    - channels.reply
    - channels.send_message
    - channels.stats
  approve:
    - channels.broadcast
  deny:
    - channels.pause_provider
    - channels.simulate_event
```

### Inbound Security

Every inbound event passes through:

1. **Payload size check** — raw bytes, before JSON parsing (default 1 MB)
2. **Authentication** — HMAC-SHA256 signature or API key (constant-time)
3. **Content-Type whitelist** — JSON, form-urlencoded, text/plain only
4. **Payload sanitization** — strips `__proto__`, `__class__`, `constructor`, all `__*`/`$$*` keys
5. **Header stripping** — removes Authorization, Cookie, X-API-Key from event metadata

### Outbound Security

1. **SSRF protection** — private IP blocklist on all outbound URLs
2. **Secret filtering** — scans messages for API key patterns, replaces with `[REDACTED]`
3. **Header masking** — sensitive headers masked in logs

### Template Safety

- No `eval()`, no Jinja2 — pure string substitution
- `{{secret.*}}` and `{{env.*}}` blocked at runtime
- Single-pass (no recursive expansion)
- 256 KB output limit

## Constraints

| Constraint | Type | Default | Description |
|------------|------|---------|-------------|
| `allowed_adapters` | string_list | all | Restrict adapter types |
| `max_providers` | integer | 20 | Max provider instances |

## Activation Pipeline

When an inbound event arrives:

```
Event → Filter → Prepare → Route → Build → Agent Turn → Reply
```

### Filter

Drop events not matching conditions (AND logic):

```yaml
filter:
  - field: event.payload.status
    equals: "new"
  - field: event.payload.priority
    lt: 3
```

### Prepare

Call tools before agent starts:

```yaml
prepare:
  - action: database.fetch_results
    params:
      query: "SELECT * FROM clients WHERE id = {{event.payload.client_id}}"
    as: client
```

### Route

Dynamic agent selection:

```yaml
route:
  field: client.0.department
  rules:
    - match: "tech"
      agent: tech_support
    - default: true
      agent: general
```

### Sessions

| Mode | YAML | Behavior |
|------|------|----------|
| Per-event | `"per_event"` | Stateless, fresh each time |
| Shared | `"shared"` | Persistent per (provider, source) |
| Template | `"wa-{{event.source}}"` | Custom key from event data |

### Reply

| Mode | Behavior |
|------|----------|
| `"auto"` | Agent response sent back automatically |
| `"none"` | Agent must use `channels.send_message()` explicitly |

## Custom Adapters

Implement `BaseChannelAdapter` and register:

```python
from digitorn.modules.channels.adapters import register_adapter
from my_package import TelegramAdapter

register_adapter("telegram", TelegramAdapter)
```

Then use in YAML:

```yaml
providers:
  telegram:
    adapter: telegram
    config:
      bot_token: "{{secret.TG_TOKEN}}"
```

## Module Dependencies

The channels module has no hard dependencies. Optional integrations:

- **database** — for prepare steps with DB lookups
- **rag** — for prepare steps with knowledge base queries
- **filesystem** — for prepare steps reading files
- **queue** — for the queue adapter bridge
- **memory** — for persistent agent context across shared sessions
