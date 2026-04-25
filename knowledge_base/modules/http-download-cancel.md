---
id: http-download-cancel
title: "http.download_cancel (HttpDownloadCancel)"
type: module-action
module: http
action: download_cancel
fqn: http.download_cancel
short_name: HttpDownloadCancel
keywords: [http, download_cancel, httpdownloadcancel, download, cancel, annuler_telechargement, dl_cancel, stop_download]
permissions: [sys.info]
risk_level: low
irreversible: false
require_approval: false
---

# http.download_cancel (HttpDownloadCancel)

## Description
Cancel a running background download. The partially downloaded file is deleted.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `download_id` | string | ✓ | — | The download ID returned by download. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [download_cancel]
```

## Aliases
`annuler_telechargement`, `dl_cancel`, `stop_download`

## Safety
- Required permissions: `sys.info`
- Risk level: **low**
