---
id: http-download
title: "http.download (HttpDownload)"
type: module-action
module: http
action: download
fqn: http.download
short_name: HttpDownload
keywords: [http, download, httpdownload, background, file, telecharger, download_file, telecharger_fichier, dl]
permissions: [net.http, fs.write]
risk_level: medium
irreversible: false
require_approval: false
---

# http.download (HttpDownload)

## Description
Start a background file download and return a download_id. Uses streaming to handle files of any size without memory pressure. The system waits ~300ms to detect immediate failures (DNS error, 404, permission denied). Use download_status to check progress, download_cancel to stop. Supports downloads lasting hours.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | File URL to download. |
| `destination` | string | ✓ | — | Local file path to save to. |
| `headers` | object |  | — | Custom request headers. |
| `timeout` | number |  | `3600.0` | Total download timeout (up to 24h). |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |
| `overwrite` | boolean |  | `False` | Overwrite if file already exists. |
| `chunk_size` | integer |  | `65536` | Download chunk size in bytes. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [download]
```

## Aliases
`telecharger`, `download_file`, `telecharger_fichier`, `dl`

## Safety
- Required permissions: `net.http`, `fs.write`
- Risk level: **medium**
