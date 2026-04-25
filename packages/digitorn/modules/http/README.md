# HTTP Module

Agent-optimized HTTP client with SSRF protection, background downloads,
and web scraping.

## Overview

The HTTP module gives AI agents full network capabilities: API calls, form
submissions, file uploads/downloads, and web page extraction. Every request
passes through SSRF protection (private IP blocking + DNS resolution checks)
before execution.

Background downloads run as streaming asyncio tasks — files of any size
download without memory pressure, with real-time progress, speed, and ETA.

## Actions

### Core HTTP

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `request` | Full-control HTTP request (any method) | Medium | `net.http` |
| `get` | HTTP GET with auto-parse | Low | `net.http` |
| `post` | HTTP POST with JSON serialization | Medium | `net.http` |
| `put` | HTTP PUT — replace resource | Medium | `net.http` |
| `patch` | HTTP PATCH — partial update | Medium | `net.http` |
| `delete` | HTTP DELETE — remove resource | Medium | `net.http` |
| `head` | HTTP HEAD — headers only | Low | `net.http` |
| `options` | HTTP OPTIONS — discover methods/CORS | Low | `net.http` |

### Convenience

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `json_api` | JSON API call with Bearer auth support | Medium | `net.http` |
| `submit_form` | HTML form submission (URL-encoded) | Medium | `net.http` |
| `upload_file` | Multipart file upload | Medium | `net.http`, `fs.read` |
| `fetch_page` | Fetch web page and extract readable text | Low | `net.http` |

### Background Downloads

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `download` | Start streaming background download | Medium | `net.http`, `fs.write` |
| `download_status` | Check download progress | Low | `sys.info` |
| `download_cancel` | Cancel and clean up download | Low | `sys.info` |
| `download_list` | List all downloads with status | Low | `sys.info` |

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_hosts` | `string_list` | Whitelist mode — only these hosts are reachable. |
| `blocked_hosts` | `string_list` | Blocklist — these hosts are always denied (overrides allowlist). |
| `allow_insecure_tls` | `bool` | Allow disabling TLS verification (default: false). |

Example:

```yaml
- module: http
  actions: [get, post, json_api, fetch_page, download, download_status, download_list]
  constraints:
    allowed_hosts: ["api.example.com", "*.github.com"]
    blocked_hosts: ["internal.corp.net"]
```

## Security

- **SSRF protection**: DNS resolution check blocks private/reserved IPs (127.0.0.0/8, 10.0.0.0/8, 192.168.0.0/16, etc.)
- **Header masking**: Authorization, Cookie, API keys are masked in responses
- **TLS enforcement**: Enabled by default, insecure requires explicit `allow_insecure_tls`
- **Response size cap**: Prevents memory exhaustion (default 5 MB, configurable per-request)
- **Audit log**: Every request logged with URL, method, status, timestamp

## Requirements

- `httpx` (HTTP/1.1 and HTTP/2 async client)

## Platform Support

| Platform | Status |
|----------|--------|
| Linux | Supported |
| macOS | Supported |
| Windows | Supported |
