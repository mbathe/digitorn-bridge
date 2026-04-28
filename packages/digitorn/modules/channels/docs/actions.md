# Channels Module - Actions Reference

## Sending Messages

### `send_message`
Send a message through a specific channel provider.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance name from YAML config |
| `text` | string | yes | - | Message text (max 50,000 chars) |
| `recipient` | string | no | `""` | Override recipient (phone, email, channel ID) |
| `subject` | string | no | `""` | Subject/title (email, Slack header) |
| `thread_id` | string | no | `""` | Thread ID for reply threading |
| `metadata` | dict | no | `{}` | Extra metadata passed to adapter |

**Risk:** medium · **Side effects:** `network_io`

---

### `reply`
Reply to the current inbound event on its originating channel.

Only available during a channel-triggered activation. Uses the reply_context
from the inbound event for correct threading (email In-Reply-To, Slack thread_ts).

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `text` | string | yes | - | Reply text (max 50,000 chars) |
| `metadata` | dict | no | `{}` | Extra metadata for the reply |

**Risk:** medium · **Side effects:** `network_io`

---

### `broadcast`
Send the same message to multiple providers simultaneously.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `providers` | list[string] | yes | - | List of provider instance names (min 1) |
| `text` | string | yes | - | Message text (max 50,000 chars) |
| `subject` | string | no | `""` | Subject/title |
| `metadata` | dict | no | `{}` | Extra metadata |

**Risk:** high · **Side effects:** `network_io`

**Returns:** `{ results: [{provider, success, error}], sent: int, failed: int }`

---

## Provider Management

### `list_providers`
List all configured channel providers and their status.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `include_status` | bool | no | `true` | Include runtime status for each provider |

**Risk:** low

**Returns:** `{ providers: [{name, adapter, inbound, outbound, enabled, status, events_received, events_sent, active_activations}] }`

---

### `provider_status`
Get detailed status of a specific provider.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance name |

**Risk:** low

**Returns:** Provider details including capabilities, event counts, last error.

---

### `pause_provider`
Pause a provider's inbound listener. Stops receiving new events.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance name |

**Risk:** medium · **Side effects:** `state_mutation`

---

### `resume_provider`
Resume a paused provider's inbound listener.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance name |

**Risk:** medium · **Side effects:** `state_mutation`

---

## Observability

### `provider_history`
Get recent inbound/outbound event history.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | no | `""` | Filter by provider (empty = all) |
| `limit` | int | no | `20` | Max events to return (1-100) |
| `direction` | string | no | `"all"` | Filter: `"inbound"`, `"outbound"`, or `"all"` |

**Risk:** low

---

### `stats`
Get aggregate statistics for all channel providers.

**Parameters:** None

**Risk:** low

**Returns:** `{ providers_count, active_count, total_events_received, total_events_sent, active_sessions, history_size }`

---

## Testing & Debug

### `simulate_event`
Simulate an inbound event for testing. Runs through the full activation pipeline.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance to simulate on |
| `payload` | dict | no | `{}` | Simulated event payload |
| `source` | string | no | `"test"` | Simulated sender identifier |
| `message` | string | no | `""` | Simulated message text |

**Risk:** medium · **Side effects:** `state_mutation`

---

### `test_send`
Send a test message to verify outbound connectivity.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `provider` | string | yes | - | Provider instance to test |
| `text` | string | no | `"Digitorn test message"` | Test message content |

**Risk:** medium · **Side effects:** `network_io`
