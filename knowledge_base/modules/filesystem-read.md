---
id: filesystem-read
title: "filesystem.read (Read)"
type: module-action
module: filesystem
action: read
fqn: filesystem.read
short_name: Read
keywords: [filesystem, read]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# filesystem.read (Read)

## Description
Read a file from the local filesystem.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `file_path` | string | ✓ | — | The absolute path to the file to read. |
| `offset` | integer |  | — | The line number to start reading from. Only provide if the file is too large to read at once. |
| `limit` | integer |  | — | The number of lines to read. Only provide if the file is too large to read at once. |
| `encoding` | string |  | `utf-8` |  |
| `pages` | string |  | — |  |
| `pattern` | string |  | `` |  |
| `max_binary_size` | integer |  | `1048576` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [read]
```

## Tool usage instructions
```
Read a file from the local filesystem. Returns content with line numbers.

## When to use
- ALWAYS read before editing — Edit will fail on files you haven't read
- Read to understand code before proposing changes — never guess at file contents
- Read config files, READMEs, package.json before starting work on a project
- Read test files to understand expected behavior before fixing bugs
- After editing, read the modified section to verify your changes are correct

## When NOT to use
- Don't read entire large files (1000+ lines) — use offset/limit for specific sections
- Don't read when Grep would be faster — search first, then read matching regions
- Don't read binary files — use Bash('file <path>') instead
- For large codebases, delegate bulk reading to a sub-agent to protect your context

## Smart reading strategy
1. Grep(pattern='function_name') to find the file and line number
2. Read(file_path, offset=line-5, limit=30) to read just that section
3. Never read 10 files sequentially — call multiple Read in parallel

## Parameters
- file_path: absolute or relative (resolved from workspace root)
- offset: start line (1-based). Use to skip to a specific section
- limit: max lines to read. Default 2000. Use smaller values for targeted reads
- Auto-detects images, PDFs, notebooks — returns appropriate metadata
```

## Safety
- Risk level: **low**
