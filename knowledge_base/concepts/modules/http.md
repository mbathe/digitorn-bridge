---
id: module-concept-http
title: "http module — overview"
type: module-concept
module: http
isolation: shared
keywords: [http, http-module, request, get, post, put, patch, delete, head, options, json_api, submit_form, upload_file, fetch_page, download, download_status, download_cancel]
version: 1.0.0
---

# `http` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 16 visible, 0 internal

## Description (from class docstring)

HTTP module — agent-optimized HTTP client with background downloads.

Provides full HTTP capabilities to AI agents: API calls, web scraping,
file downloads with progress tracking, and form submissions.

Security layers:
  - SSRF protection: blocks private/reserved IPs before every request.
  - URL allowlist/blocklist: configurable per-app in YAML constraints.
  - Sensitive header masking: Authorization, Cookie, API keys never returned raw.
  - Response size cap: prevents memory exhaustion from huge responses.
  - TLS verification: enabled by default, configurable override requires allow_insecure_tls.
  - Timeout enforcement on every request.
  - Full audit log: every request recorded with URL, method, status, timestamp.

Background downloads:
  - download:          start a streaming download, get a download_id back.
  - download_status:   check progress (bytes, speed, ETA, percentage).
  - download_cancel:   cancel and clean up partial file.
  - download_list:     list all downloads with status.

## Configuration

Set under `modules.http.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML — the daemon resolves it from the app's workspace/workspace_mode config. |
| `timeout` | int |  | `30` | Default request timeout in seconds |
| `max_response_size` | int |  | `10000000` | Maximum response body size |
| `allow_insecure_tls` | bool |  | `False` | Allow HTTPS requests without certificate verification |
| `user_agent` | str \| None |  | `None` | Custom User-Agent header |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `request` | `HttpRequest` |  | medium | Make an HTTP request with full control over method, headers, body, query params, and authentication. Universal action... |
| `get` | `HttpGet` |  | low | HTTP GET — fetch a URL and auto-parse the response based on content type (JSON, text, HTML). |
| `post` | `HttpPost` |  | medium | HTTP POST — send data to a URL with automatic JSON serialization. |
| `put` | `HttpPut` |  | medium | HTTP PUT — replace a resource at the target URL. |
| `patch` | `HttpPatch` |  | medium | HTTP PATCH — partially update a resource at the target URL. |
| `delete` | `HttpDelete` |  | medium | HTTP DELETE — remove a resource at the target URL. |
| `head` | `HttpHead` |  | low | HTTP HEAD — retrieve response headers without downloading the body. Useful for checking if a URL exists, getting cont... |
| `options` | `HttpOptions` |  | low | HTTP OPTIONS — discover allowed methods and CORS configuration for a URL. |
| `json_api` | `HttpJsonApi` |  | medium | Call a JSON API endpoint. Auto-sends Accept: application/json, parses JSON response, supports Bearer token auth. The ... |
| `submit_form` | `HttpSubmitForm` |  | medium | Submit an HTML form (application/x-www-form-urlencoded). Auto-encodes key-value pairs. |
| `upload_file` | `HttpUploadFile` |  | medium | Upload a file via multipart/form-data POST. The file must exist on the local filesystem. |
| `fetch_page` | `HttpFetchPage` |  | low | Fetch a web page and extract readable text from HTML. Strips scripts, styles, and navigation. Returns text with basic... |
| `download` | `HttpDownload` |  | medium | Start a background file download and return a download_id. Uses streaming to handle files of any size without memory ... |
| `download_status` | `HttpDownloadStatus` |  | low | Check the progress of a background download: bytes downloaded, speed, ETA, and completion percentage. |
| `download_cancel` | `HttpDownloadCancel` |  | low | Cancel a running background download. The partially downloaded file is deleted. |
| `download_list` | `HttpDownloadList` |  | low | List all background downloads (active and completed) with their status, progress, and speed. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: http
      actions: [request, get, post, put, patch, delete, head, options, json_api, submit_form, upload_file, fetch_page, download, download_status, download_cancel, download_list]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {http: [request, get, post, put, patch]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/http-*.md`.
