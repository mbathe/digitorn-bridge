# HTTP Module — Integration Guide

## YAML Configuration

```yaml
tools:
  - module: http
    actions: [get, post, json_api, fetch_page, download, download_status, download_list]
    constraints:
      allowed_hosts: ["api.example.com", "*.github.com"]
```

## Security Integration

### Permissions

The HTTP module uses two permission namespaces:

| Permission | Used by |
|------------|---------|
| `net.http` | All HTTP actions (request, get, post, etc.) |
| `fs.read` | `upload_file` (reads local file) |
| `fs.write` | `download` (writes to disk) |
| `sys.info` | `download_status`, `download_cancel`, `download_list` |

Grant permissions in your YAML capabilities:

```yaml
capabilities:
  grant:
    - net.http
    - fs.write     # if downloads needed
```

### SSRF Protection

Every outbound URL is validated before execution:
1. Scheme check (only `http://` and `https://`)
2. Hostname against blocklist (deny wins)
3. Hostname against allowlist (if configured)
4. DNS resolution → private IP check (blocks 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, etc.)

### Host Filtering

- **`allowed_hosts`**: Whitelist mode — only listed hosts are reachable. Supports globs (`*.github.com`).
- **`blocked_hosts`**: Always denied, even if in allowlist. Supports globs.
- Both empty: all public hosts are reachable (SSRF protection still active).

### TLS

TLS verification is enforced by default. Even if the agent sends `verify_tls: false`,
the module silently re-enables it unless `allow_insecure_tls: true` is set in constraints.

## Background Downloads

Downloads run as asyncio tasks in the same event loop. They:
- Stream chunks to disk (no memory pressure)
- Track speed every second (rolling window)
- Support cancellation at any time
- Clean up partial files on cancel/failure
- Are cleaned up on module stop (`on_stop`)

The 300ms stabilisation delay catches immediate failures (DNS, 404, permissions)
before returning the download_id to the agent.

## Response Handling

- JSON responses are auto-parsed
- HTML text responses are returned as strings
- Binary responses return metadata only (use `download()` for actual content)
- Responses exceeding `max_response_bytes` are truncated with guidance
- Sensitive headers (Authorization, Cookie, API keys) are masked in results

## Audit Log

Every HTTP request is logged via Python `logging`:
```
http_audit action=get method=GET url='https://api.example.com/data' status=200 error=None ts=2026-03-12T10:00:00+00:00
```
