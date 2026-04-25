---
id: filesystem-write
title: "filesystem.write (Write)"
type: module-action
module: filesystem
action: write
fqn: filesystem.write
short_name: Write
keywords: [filesystem, write]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# filesystem.write (Write)

## Description
Write a file to the local filesystem.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `file_path` | string | ✓ | — | The absolute path to the file to write. Parent directories are created automatically. |
| `content` | string | ✓ | — | The content to write to the file. |
| `create_dirs` | boolean |  | `True` |  |
| `encoding` | string |  | `utf-8` |  |
| `atomic` | boolean |  | `True` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [write]
```

## Tool usage instructions
```
Create a new file or completely rewrite an existing one.

## When to use
- Creating brand new files that don't exist yet
- Complete rewrites where >50% of the file changes
- Generating config files, test files, or boilerplate from scratch

## When NOT to use — use Edit instead
- Modifying a few lines in an existing file — Edit sends only the diff
- Fixing a bug in one function — Edit is surgical, Write replaces everything
- NEVER Write an existing file you haven't Read first — you'll lose content

## Rules
- Parent directories are created automatically (atomic writes)
- NEVER create documentation files (*.md, README) unless explicitly asked
- NEVER create files that duplicate existing functionality — check with Glob first
- After writing, the file is automatically linted — check the lint result for errors
- Prefer editing existing files over creating new ones — less file bloat

## Parameters
- file_path: absolute or relative (resolved from workspace root)
- content: the complete file content
```

## Safety
- Risk level: **low**
