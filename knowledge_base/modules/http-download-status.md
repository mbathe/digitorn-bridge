---
id: http-download-status
title: "http.download_status (HttpDownloadStatus)"
type: module-action
module: http
action: download_status
fqn: http.download_status
short_name: HttpDownloadStatus
keywords: [http, download_status, httpdownloadstatus, download, status, statut_telechargement, dl_status, download_progress]
permissions: [sys.info]
risk_level: low
irreversible: false
require_approval: false
---

# http.download_status (HttpDownloadStatus)

## Description
Check the progress of a background download: bytes downloaded, speed, ETA, and completion percentage.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `download_id` | string | ✓ | — | The download ID returned by download. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [download_status]
```

## Aliases
`statut_telechargement`, `dl_status`, `download_progress`

## Safety
- Required permissions: `sys.info`
- Risk level: **low**
