---
id: filesystem-glob
title: "filesystem.glob (Glob)"
type: module-action
module: filesystem
action: glob
fqn: filesystem.glob
short_name: Glob
keywords: [filesystem, glob]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# filesystem.glob (Glob)

## Description
Find files by name pattern.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `pattern` | string | ✓ | - | Glob pattern. Example: '**/*.py', 'src/**/*.ts', '*.md'. |
| `path` | string |  | `.` | Directory to search in. Defaults to current working directory. |
| `type` | string |  | - | Filter by type: 'file' or 'dir'. |
| `max_results` | integer |  | `5000` |  |
| `include_hidden` | boolean |  | `False` |  |
| `follow_symlinks` | boolean |  | `False` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [glob]
```

## Tool usage instructions
```
Find files by name pattern. Use this to discover project structure.

## When to use
- Starting work on a new project - Glob('**/*.py') to see the structure
- Finding files by name - Glob('**/auth*.py') to find auth-related files
- Checking if a file exists before creating it - avoid duplicates
- Discovering test files - Glob('**/test_*.py') or Glob('**/*.test.ts')

## When NOT to use - use Grep instead
- Searching for content INSIDE files (function names, imports, strings)
- Finding which file contains a specific error message

## Common patterns
- '**/*.py' - all Python files recursively
- 'src/**/*.tsx' - all TSX files under src/
- '**/test_*' - all test files
- '*.yaml' - YAML files in current directory only
- '**/*config*' - any file with 'config' in the name

## Parameters
- pattern: glob pattern (* = one level, ** = any depth)
- path: directory to search (default: workspace root)
- Results sorted by modification time (newest first)
```

## Safety
- Risk level: **low**
