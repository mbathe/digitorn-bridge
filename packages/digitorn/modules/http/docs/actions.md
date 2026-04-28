# HTTP Module - Actions Reference

## Core HTTP

### `request`
Make an HTTP request with full control over method, headers, body, query params.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | - | Target URL (http:// or https://) |
| `method` | string | no | `GET` | HTTP method |
| `headers` | dict | no | null | Custom request headers |
| `body` | string | no | null | Raw request body |
| `json_body` | dict/list | no | null | JSON body (auto-sets Content-Type) |
| `query_params` | dict | no | null | URL query parameters |
| `timeout` | float | no | 30.0 | Request timeout (1–300s) |
| `follow_redirects` | bool | no | true | Follow HTTP redirects |
| `max_redirects` | int | no | 10 | Max redirect hops (0–20) |
| `verify_tls` | bool | no | true | Verify TLS certificates |
| `max_response_bytes` | int | no | 5000000 | Max response body (up to 50 MB) |

**Risk:** medium · **Permissions:** `net.http`

---

### `get`
HTTP GET - fetch a URL and auto-parse the response.

**Parameters:** `url`, `headers`, `query_params`, `timeout`, `verify_tls`, `max_response_bytes`

**Risk:** low · **Permissions:** `net.http`

---

### `post`
HTTP POST - send data to a URL.

**Parameters:** `url`, `headers`, `json_body`, `body`, `timeout`, `verify_tls`

**Risk:** medium · **Permissions:** `net.http`

---

### `put`
HTTP PUT - replace a resource.

**Parameters:** `url`, `headers`, `json_body`, `body`, `timeout`, `verify_tls`

**Risk:** medium · **Permissions:** `net.http`

---

### `patch`
HTTP PATCH - partially update a resource.

**Parameters:** `url`, `headers`, `json_body`, `body`, `timeout`, `verify_tls`

**Risk:** medium · **Permissions:** `net.http`

---

### `delete`
HTTP DELETE - remove a resource. **Irreversible.**

**Parameters:** `url`, `headers`, `query_params`, `timeout`, `verify_tls`

**Risk:** medium · **Permissions:** `net.http`

---

### `head`
HTTP HEAD - retrieve headers without the body.

**Parameters:** `url`, `headers`, `timeout`, `verify_tls`

**Risk:** low · **Permissions:** `net.http`

---

### `options`
HTTP OPTIONS - discover allowed methods and CORS.

**Parameters:** `url`, `headers`, `timeout`, `verify_tls`

**Risk:** low · **Permissions:** `net.http`

---

## Convenience

### `json_api`
Call a JSON API endpoint with auto JSON handling and Bearer auth.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | - | API endpoint URL |
| `method` | string | no | `GET` | HTTP method |
| `data` | dict/list | no | null | Request payload (auto-serialized) |
| `headers` | dict | no | null | Custom headers |
| `query_params` | dict | no | null | URL query parameters |
| `auth_bearer` | string | no | null | Bearer token |
| `timeout` | float | no | 30.0 | Timeout |
| `verify_tls` | bool | no | true | Verify TLS |

**Risk:** medium · **Permissions:** `net.http`

---

### `submit_form`
Submit an HTML form (application/x-www-form-urlencoded).

**Parameters:** `url`, `fields` (required dict), `method`, `headers`, `timeout`, `verify_tls`

**Risk:** medium · **Permissions:** `net.http`

---

### `upload_file`
Upload a file via multipart/form-data.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | - | Upload endpoint |
| `file_path` | string | yes | - | Local file to upload |
| `field_name` | string | no | `file` | Form field name |
| `extra_fields` | dict | no | null | Additional form fields |
| `headers` | dict | no | null | Custom headers |
| `timeout` | float | no | 120.0 | Upload timeout (up to 600s) |
| `verify_tls` | bool | no | true | Verify TLS |
| `max_upload_bytes` | int | no | 100000000 | Max file size (100 MB) |

**Risk:** medium · **Permissions:** `net.http`, `fs.read`

---

### `fetch_page`
Fetch a web page and extract readable text from HTML.

Strips scripts, styles, navigation. Returns text, title, and links.

**Parameters:** `url`, `headers`, `timeout`, `verify_tls`, `max_response_bytes`, `extract_links`, `max_text_length`

**Risk:** low · **Permissions:** `net.http`

---

## Background Downloads

### `download`
Start a streaming background download. Returns a `download_id` immediately.

The system waits ~300ms to detect immediate failures (DNS error, 404). Uses
chunked streaming - handles files of any size without memory pressure.
Supports downloads lasting hours.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `url` | string | yes | - | File URL |
| `destination` | string | yes | - | Local save path |
| `headers` | dict | no | null | Custom headers |
| `timeout` | float | no | 3600.0 | Total timeout (up to 24h) |
| `verify_tls` | bool | no | true | Verify TLS |
| `overwrite` | bool | no | false | Overwrite existing file |
| `chunk_size` | int | no | 65536 | Chunk size (4 KB–1 MB) |

**Risk:** medium · **Permissions:** `net.http`, `fs.write`

---

### `download_status`
Check progress: bytes, speed, ETA, percentage.

**Parameters:** `download_id` (required)

**Risk:** low · **Permissions:** `sys.info`

---

### `download_cancel`
Cancel a running download. Partial file is deleted.

**Parameters:** `download_id` (required)

**Risk:** low · **Permissions:** `sys.info`

---

### `download_list`
List all downloads (active first, then by start time).

**Parameters:** none

**Risk:** low · **Permissions:** `sys.info`
