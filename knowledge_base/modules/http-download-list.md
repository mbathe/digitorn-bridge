---
id: http-download-list
title: "http.download_list (HttpDownloadList)"
type: module-action
module: http
action: download_list
fqn: http.download_list
short_name: HttpDownloadList
keywords: [http, download_list, httpdownloadlist, download, list, liste_telechargements, dl_list]
permissions: [sys.info]
risk_level: low
irreversible: false
require_approval: false
---

# http.download_list (HttpDownloadList)

## Description
List all background downloads (active and completed) with their status, progress, and speed.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [download_list]
```

## Aliases
`liste_telechargements`, `dl_list`

## Safety
- Required permissions: `sys.info`
- Risk level: **low**
