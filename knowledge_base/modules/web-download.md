---
id: web-download
title: "web.download (WebDownload)"
type: module-action
module: web
action: download
fqn: web.download
short_name: WebDownload
keywords: [web, download, webdownload]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# web.download (WebDownload)

## Description
Download a file from a URL to a local path. Supports large files with streaming. Returns the file size in bytes. The download happens in the foreground -- use background tasks for very large files. Example: download(url='https://example.com/data.csv', path='/tmp/data.csv')

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | - | URL to download. |
| `path` | string | ✓ | - | Local file path to save to. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: web
      actions: [download]
```

## Safety
- Risk level: **medium**
