---
id: filesystem-edit
title: "filesystem.edit (Edit)"
type: module-action
module: filesystem
action: edit
fqn: filesystem.edit
short_name: Edit
keywords: [filesystem, edit]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# filesystem.edit (Edit)

## Description
Find-and-replace in a file.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `file_path` | string | ✓ | - | The absolute path to the file to modify. |
| `old_string` | string |  | - | The text to replace. Copy this from Read output. Must be unique in the file. Not needed if using insert_at_line. |
| `new_string` | string | ✓ | - | The text to replace it with (or insert if using insert_at_line). |
| `replace_all` | boolean |  | `False` | Replace all occurrences of old_string. |
| `insert_at_line` | integer |  | - | Insert new_string at this line number (1-based). Use instead of old_string. |
| `fuzzy_threshold` | number |  | `0.85` |  |
| `max_suggestions` | integer |  | `3` |  |
| `encoding` | string |  | `utf-8` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [edit]
```

## Tool usage instructions
```
Surgical text replacement in a file. Only sends the diff, not the full file.

## When to use
- Fixing bugs - change the broken line(s), leave everything else untouched
- Adding features - insert new code at a specific location
- Refactoring - rename, restructure, or update specific sections
- ANY modification to an existing file - always prefer Edit over Write

## Required workflow
1. Read the file first (or the relevant section) - Edit FAILS on unread files
2. Copy old_string EXACTLY from the Read output - including indentation
3. Make your change in new_string - preserve surrounding indentation
4. After editing, Read the modified section to verify correctness

## Handling failures
- If old_string is not found, the error shows closest matches with line numbers
- Use those line numbers to Read the exact region, then copy the correct text
- Fuzzy matching auto-handles: trailing whitespace, CRLF, indentation differences
- If old_string appears multiple times, add more context to make it unique

## Parameters
- file_path: the file to edit
- old_string: exact text to find (copy from Read output)
- new_string: replacement text
- replace_all: true to replace ALL occurrences (default: false)
- insert_at_line: insert new_string at this line number (no old_string needed)

## Rules
- NEVER rewrite entire files with Edit - use Write for complete rewrites
- Keep edits small and focused - one logical change per Edit call
- Preserve exact indentation - the file's style, not yours
- After each edit, the file is automatically linted - check for errors
```

## Safety
- Risk level: **low**
