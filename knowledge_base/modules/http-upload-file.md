---
id: http-upload-file
title: "http.upload_file (HttpUploadFile)"
type: module-action
module: http
action: upload_file
fqn: http.upload_file
short_name: HttpUploadFile
keywords: [http, upload_file, httpuploadfile, upload, file, telecharger_vers, envoyer_fichier]
permissions: [net.http, fs.read]
risk_level: medium
irreversible: false
require_approval: false
---

# http.upload_file (HttpUploadFile)

## Description
Upload a file via multipart/form-data POST. The file must exist on the local filesystem.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `url` | string | ✓ | — | Upload endpoint URL. |
| `file_path` | string | ✓ | — | Local file path to upload. |
| `field_name` | string |  | `file` | Multipart form field name for the file. |
| `extra_fields` | object |  | — | Additional form fields. |
| `headers` | object |  | — | Custom request headers. |
| `timeout` | number |  | `120.0` | Upload timeout in seconds. |
| `verify_tls` | boolean |  | `True` | Verify TLS certificates. |
| `max_upload_bytes` | integer |  | `100000000` | Max file size to upload (default 100 MB). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: http
      actions: [upload_file]
```

## Aliases
`telecharger_vers`, `envoyer_fichier`, `upload`

## Safety
- Required permissions: `net.http`, `fs.read`
- Risk level: **medium**
