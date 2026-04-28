---
id: http
title: HTTP Module
sidebar_label: http
sidebar_position: 6
description: Full HTTP client - GET, POST, PUT, PATCH, DELETE, JSON API, form submission, file upload/download.
---

# http

Full HTTP client for interacting with REST APIs and web services. Supports all methods, JSON APIs, form submission, file upload, and background downloads.

| Property | Value |
|----------|-------|
| **Module ID** | `http` |
| **Version** | `1.0.0` |
| **Type** | user |
| **Dependencies** | `httpx` or `aiohttp` |

---

## Design Philosophy

- **Method-specific actions** - dedicated actions for GET, POST, PUT, PATCH, DELETE instead of a single generic request. Clearer semantics for the agent.
- **Auto-parsing** - responses are automatically parsed based on content type (JSON, text, binary).
- **Background downloads** - large files download in the background with progress tracking.
- **Security-aware** - read-only methods (GET, HEAD, OPTIONS) are low risk. Write methods are medium risk.

---

## Actions (16)

### request
Generic HTTP request with full control. Parameters: `method`, `url`, `headers`, `body`, `params`. **Risk: medium**

### get
HTTP GET with auto-parsed response. Parameters: `url`, `headers`, `params`. **Risk: low**

### post
HTTP POST with JSON serialization. Parameters: `url`, `data`, `headers`. **Risk: medium**

### put
HTTP PUT. Parameters: `url`, `data`, `headers`. **Risk: medium**

### patch
HTTP PATCH. Parameters: `url`, `data`, `headers`. **Risk: medium**

### delete
HTTP DELETE. Parameters: `url`, `headers`. **Risk: medium**

### head
HTTP HEAD - headers only. Parameters: `url`. **Risk: low**

### options
HTTP OPTIONS - CORS and allowed methods. Parameters: `url`. **Risk: low**

### json_api
Call a JSON API endpoint with auto-headers. Parameters: `url`, `method`, `data`. **Risk: medium**

### submit_form
Submit an HTML form (URL-encoded). Parameters: `url`, `data`. **Risk: medium**

### upload_file
Upload a file via multipart POST. Parameters: `url`, `file_path`, `field_name`. **Risk: medium**

### fetch_page
Fetch a web page and extract readable text. Parameters: `url`. **Risk: low**

### download
Start a background file download. Parameters: `url`, `path`. **Risk: medium**

### download_status
Check download progress. Parameters: `download_id`. **Risk: low**

### download_cancel
Cancel a running download. Parameters: `download_id`. **Risk: low**

### download_list
List all downloads with status. **Risk: low**

---

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_hosts` | string_list | Allowed HTTP hosts for write requests (POST/PUT/PATCH/DELETE). |
| `blocked_hosts` | string_list | Blocked HTTP hosts for all requests. |

### Egress Policy

Write methods (POST, PUT, PATCH, DELETE) to external hosts are subject to egress control:

- **Without security profile** (no `capabilities:` block in YAML): POST/PUT/PATCH/DELETE are allowed to any host. This is the default dev/trusted mode.
- **With security profile** (a `capabilities:` block is present): write requests to external hosts are **blocked** unless the host is explicitly listed in `allowed_hosts`. Requests to `localhost`, `127.0.0.1`, and `::1` are always allowed.

If a write request is blocked, the error message tells the agent exactly which constraint to add.

### Example App YAML

```yaml
modules:
  - module: http
    constraints:
      allowed_hosts: [api.github.com, api.openai.com]
      blocked_hosts: [evil.example.com]
```
---

## Configuration

The HTTP module does not require explicit configuration. Constraints are the primary way to control behavior.

```yaml
modules:
  http: {}
```