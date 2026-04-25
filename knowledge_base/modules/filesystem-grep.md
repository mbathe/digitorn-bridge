---
id: filesystem-grep
title: "filesystem.grep (Grep)"
type: module-action
module: filesystem
action: grep
fqn: filesystem.grep
short_name: Grep
keywords: [filesystem, grep]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# filesystem.grep (Grep)

## Description
Search file contents for a regex pattern.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `pattern` | string | ✓ | — | The regular expression pattern to search for in file contents. |
| `path` | string |  | `.` | File or directory to search in. Defaults to current working directory. |
| `glob` | string |  | — | Glob filter. Example: '*.py', '*.{ts,tsx}'. |
| `context` | integer |  | `0` | Lines of context before and after each match. |
| `type` | string |  | — |  |
| `recursive` | boolean |  | `True` |  |
| `max_results` | integer |  | `2000` |  |
| `case_sensitive` | boolean |  | `True` |  |
| `output_mode` | string |  | `content` |  |
| `multiline` | boolean |  | `False` |  |
| `offset` | integer |  | `0` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [grep]
```

## Tool usage instructions
```
Search file contents by regex pattern. Your primary discovery tool.

## When to use — ALWAYS search before reading
- Finding where a function/class/variable is defined — Grep('def function_name')
- Finding all usages of a symbol — Grep('import.*module_name')
- Tracing errors — Grep('error message text')
- Understanding how something is used across the codebase
- ALWAYS Grep before Read — find the exact location, then read that section

## When NOT to use
- Finding files by NAME — use Glob instead
- Reading a file you already know the path of — use Read directly

## Smart search strategy
1. Start broad: Grep('function_name') with output_mode='files_with_matches'
2. Narrow down: Grep('function_name', path='src/', glob='*.py')
3. Read context: Grep('function_name', context=5) to see surrounding lines
4. Then Read the specific section with offset/limit

## Parameters
- pattern: regex (e.g. 'def verify', 'import.*auth', 'TODO|FIXME|HACK')
- path: file or directory (default: workspace root)
- glob: filter files (e.g. '*.py', '**/*.ts') — combine with path for precision
- output_mode: 'content' (matching lines), 'files_with_matches' (paths only), 'count'
- context: lines before/after each match (0-20)
- multiline: true for patterns spanning multiple lines
```

## Safety
- Risk level: **low**
