---

id: api_http
title: API HTTP Module
sidebar_position: 7
format: md
---




# api_http

Full HTTP client with support for all standard methods, file transfer, GraphQL, OAuth2, HTML parsing, email (SMTP/IMAP), webhooks, and persistent session management.

| Property | Value |
|----------|-------|
| **Module ID** | `api_http` |
| **Version** | `1.0.0` |
| **Type** | network |
| **Platforms** | All |
| **Dependencies** | `httpx`, `aiosmtplib` (optional), `beautifulsoup4` (optional) |
| **Declared Permissions** | `network.http`, `file.download`, `network.email` |

---


## Actions

### http_get

Send an HTTP GET request.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Request URL |
| `headers` | object | No | `{}` | Request headers |
| `query_params` | object | No | `{}` | URL query parameters |
| `timeout` | integer | No | `30` | Request timeout in seconds |
| `follow_redirects` | boolean | No | `true` | Follow HTTP redirects |

**Returns**: `{"status_code": 200, "headers": {...}, "body": "...", "elapsed_ms": 150}`

**Security**:
- `@requires_permission(Permission.NETWORK_HTTP)`
- SSRF validation on URL

---


### http_head

Send an HTTP HEAD request (metadata only, no response body).

Same parameters as `http_get`.

**Returns**: `{"status_code": 200, "headers": {...}, "elapsed_ms": 50}`

---


### http_post

Send an HTTP POST request.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Request URL |
| `headers` | object | No | `{}` | Request headers |
| `body` | object/string | No | `null` | Request body (JSON or string) |
| `form_data` | object | No | `null` | Form-encoded data |
| `timeout` | integer | No | `30` | Request timeout |

**Security**:
- `@requires_permission(Permission.NETWORK_HTTP)`
- `@sensitive_action(RiskLevel.MEDIUM)`

---


### http_put / http_patch / http_delete

Standard HTTP methods with the same parameter structure as `http_post`.

**Security**: All require `Permission.NETWORK_HTTP` and are marked as `@sensitive_action(RiskLevel.MEDIUM)`.

---


### download_file

Stream download a file to disk.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | File URL |
| `output_path` | string | Yes | - | Local save path |
| `headers` | object | No | `{}` | Request headers |
| `timeout` | integer | No | `120` | Download timeout |

**Returns**: `{"path": "/tmp/file.zip", "bytes": 1048576, "content_type": "application/zip"}`

**Security**:
- `@requires_permission(Permission.FILE_DOWNLOAD)`

---


### upload_file

Multipart file upload.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Upload endpoint |
| `file_path` | string | Yes | - | Local file to upload |
| `field_name` | string | No | `"file"` | Form field name |
| `form_data` | object | No | `{}` | Additional form fields |
| `headers` | object | No | `{}` | Request headers |

---


### graphql_query

Execute a GraphQL query or mutation.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | GraphQL endpoint |
| `query` | string | Yes | - | GraphQL query string |
| `variables` | object | No | `{}` | Query variables |
| `headers` | object | No | `{}` | Request headers |

**Returns**: `{"data": {...}, "errors": null}`

---


### oauth2_get_token

Acquire an OAuth2 token. Supports all standard grant types.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `token_url` | string | Yes | - | Token endpoint |
| `grant_type` | string | Yes | - | `authorization_code`, `client_credentials`, `password`, `refresh_token` |
| `client_id` | string | Yes | - | OAuth2 client ID |
| `client_secret` | string | No | `null` | Client secret |
| `code` | string | No | `null` | Authorization code (for `authorization_code` grant) |
| `redirect_uri` | string | No | `null` | Redirect URI |
| `username` | string | No | `null` | Username (for `password` grant) |
| `password` | string | No | `null` | Password (for `password` grant) |
| `refresh_token` | string | No | `null` | Refresh token |
| `scope` | string | No | `null` | Requested scopes |

**Returns**: `{"access_token": "...", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "..."}`

---


### parse_html

Extract content from HTML using CSS selectors, XPath, or regex.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | No | `null` | URL to fetch (or use `html` param) |
| `html` | string | No | `null` | HTML content to parse |
| `selector` | string | No | `null` | CSS selector |
| `xpath` | string | No | `null` | XPath expression |
| `regex` | string | No | `null` | Regex pattern |
| `attribute` | string | No | `null` | Extract specific attribute |

**Returns**: `{"results": ["...", "..."], "count": 5}`

---


### check_url_availability

Health check for a URL.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | URL to check |
| `timeout` | integer | No | `10` | Timeout |
| `follow_redirects` | boolean | No | `true` | Follow redirects |

**Returns**: `{"available": true, "status_code": 200, "elapsed_ms": 50, "redirect_url": null}`

---


### send_email

Send email via SMTP.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `smtp_host` | string | Yes | - | SMTP server host |
| `smtp_port` | integer | No | `587` | SMTP port |
| `username` | string | Yes | - | SMTP username |
| `password` | string | Yes | - | SMTP password |
| `from_email` | string | Yes | - | Sender address |
| `to` | array | Yes | - | Recipient addresses |
| `subject` | string | Yes | - | Email subject |
| `body` | string | Yes | - | Email body |
| `html` | boolean | No | `false` | Send as HTML |
| `cc` | array | No | `[]` | CC addresses |
| `bcc` | array | No | `[]` | BCC addresses |
| `attachments` | array | No | `[]` | File paths to attach |

**Security**:
- `@requires_permission(Permission.NETWORK_EMAIL)`
- `@sensitive_action(RiskLevel.MEDIUM)`
- `@audit_trail("detailed")`
- `@rate_limited(calls_per_minute=30)`

---


### read_email

Read emails via IMAP.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `imap_host` | string | Yes | - | IMAP server host |
| `imap_port` | integer | No | `993` | IMAP port |
| `username` | string | Yes | - | IMAP username |
| `password` | string | Yes | - | IMAP password |
| `folder` | string | No | `"INBOX"` | Mailbox folder |
| `limit` | integer | No | `10` | Max emails to fetch |
| `unread_only` | boolean | No | `true` | Fetch only unread |

**Security**:
- `@requires_permission(Permission.NETWORK_EMAIL)`
- `@audit_trail("detailed")`
- `@data_classification(DataClassification.SENSITIVE)`

---


### webhook_trigger

Create and send a webhook with HMAC signing and retry logic.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `url` | string | Yes | - | Webhook endpoint |
| `payload` | object | Yes | - | Webhook payload |
| `secret` | string | No | `null` | HMAC signing secret |
| `headers` | object | No | `{}` | Additional headers |
| `max_retries` | integer | No | `3` | Retry count on failure |

---


### set_session / close_session

Manage persistent HTTP client sessions for connection reuse.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `session_id` | string | Yes | - | Session identifier |
| `base_url` | string | No | `null` | Base URL for all requests |
| `headers` | object | No | `{}` | Default headers |
| `timeout` | integer | No | `30` | Default timeout |

---


## Streaming Support

5 long-running actions are decorated with `@streams_progress` and emit real-time events via SSE (`GET /plans/{plan_id}/stream`):

| Action | Status Phases | Progress Pattern |
|--------|---------------|------------------|
| `download_file` | `downloading` | % based on bytes received/content-length |
| `upload_file` | `uploading` | 100% on completion |
| `send_email` | `connecting` → `sending` | 100% on completion |
| `read_email` | `connecting` → `fetching` | 100% on completion |
| `webhook_trigger` | `sending` | % based on attempt/max_retries |

Fast actions (`http_get`, `http_post`, `parse_html`, etc.) are not streaming-enabled as they complete quickly.

See [Decorators Reference - @streams_progress](../../annotators/decorators.md) for SDK consumption details.

---


## Implementation Notes

- Built on `httpx.AsyncClient` for async HTTP
- SSRF validation prevents requests to internal/private IP ranges
- Session management with lazy cleanup
- `aiosmtplib` for async SMTP (with `smtplib` sync fallback)
- `beautifulsoup4` for HTML parsing (regex fallback if not installed)
- Webhook HMAC signing with exponential backoff retry
