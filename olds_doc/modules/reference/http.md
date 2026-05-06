---
id: http
title: http Module
sidebar_label: http
sidebar_position: 6
description: Full HTTP client — REST verbs, JSON API, multipart upload, background downloads, SSRF-guarded.
---

# http

Full HTTP client for REST APIs and web services. All standard
verbs, JSON-shaped helpers, form submission, multipart upload,
background downloads with progress tracking. Outbound requests
go through the SSRF guard
(`modules/http/security.py`) — same blocklist as the `web`
module.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `http` | `module.py:281` |
| Version | `1.0.0` | `module.py:282` |
| Type | user | |
| Pip deps | `aiohttp` | |

## The 16 actions

`module.py`. Method-specific helpers are easier for the LLM
than a single generic `request`.

| Tool | Risk | Purpose |
|------|:----:|---------|
| `http.request` | medium | Generic call (full control over method / headers / body / params). |
| `http.get` | low | GET, auto-parsed by content type. |
| `http.head` | low | Headers only — no body download. |
| `http.options` | low | Allowed methods + CORS. |
| `http.post` | medium | POST, auto-JSON serialisation. |
| `http.put` | medium | Replace a resource. |
| `http.patch` | medium | Partial update. |
| `http.delete` | medium | Remove a resource. |
| `http.json_api` | medium | JSON API call (auto `Accept: application/json`). |
| `http.submit_form` | medium | `application/x-www-form-urlencoded` POST. |
| `http.upload_file` | medium | `multipart/form-data` upload. |
| `http.fetch_page` | low | Fetch + extract readable text from HTML (lighter than `web.fetch`). |
| `http.download` | medium | Start a background download. Returns a `download_id`. |
| `http.download_status` | low | Bytes downloaded / total / progress %. |
| `http.download_cancel` | low | Cancel a running download. |
| `http.download_list` | low | List active + completed downloads. |

## Egress policy

`module.py:286` `CONSTRAINTS`:

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_hosts` | `string_list` | Allowlist for **write** methods (POST / PUT / PATCH / DELETE). |
| `blocked_hosts` | `string_list` | Blocklist for **every** method. |

### Two modes

| App YAML | Behaviour |
|----------|-----------|
| **No `tools.capabilities:` block** (dev / trusted) | Write methods allowed to any host. |
| **`tools.capabilities:` declared** | Write methods to external hosts **blocked** unless `allowed_hosts` lists them. Loopback (`localhost`, `127.0.0.1`, `::1`) always allowed. |

When blocked, the error includes the exact constraint to add:

```
Host 'api.example.com' not in allowed_hosts.
Add to tools.modules.http.constraints.allowed_hosts.
```

### SSRF guard

Every outbound URL passes through `validate_url()`
(`modules/http/security.py:41-58`) — same private-network
blocklist as the `web` module:

- Loopback: `0.0.0.0/8`, `127.0.0.0/8`, `::1/128`.
- RFC 1918: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`.
- Carrier-grade NAT: `100.64.0.0/10`.
- AWS / GCP metadata: `169.254.0.0/16`.
- IPv6 link-local + ULA: `fe80::/10`, `fc00::/7`.
- Multicast / reserved: `224.0.0.0/4`, `240.0.0.0/4`.

DNS resolved **once** and pinned. The original hostname is
preserved in the `Host` header for SNI / vhost routing.

## Configuration

The module needs no required config. Tunables (when supplied):

```yaml
tools:
  modules:
    http:
      config:
        timeout: 30                   # default request timeout (s)
        max_retries: 3                # transient failures
        user_agent: "MyBot/1.0"       # custom UA header
      constraints:
        allowed_hosts: [api.github.com, api.openai.com]
        blocked_hosts: [evil.example.com]
```

## Cross-references

- App-config block reference (`tools.modules.http`):
  [App Configuration → tools.modules](../../app-language/02-app-config.md#toolsmodules--module-config)
- High-level web search + fetch (built on top of `http`):
  [web reference](web.md)
- SSRF guard + DNS pinning:
  [Production Deployment → SSRF](../../app-language/36-production.md#ssrf-protection)
- Sandbox network filtering (iptables when `allowed_hosts`
  + namespaces are active):
  [OS-Level Sandbox → Network filtering](../../app-language/35-sandbox.md#network-filtering-iptables)
