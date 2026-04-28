# Filesystem Module - Integration Guide

## Constraint System

The filesystem module supports **constraints** that applications define in their
YAML to restrict what the agent can do. This is how you sandbox an agent to
a workspace directory and limit file sizes.

### Declaring Constraints in App YAML

```yaml
modules:
  - module: filesystem
    actions: [read, write, edit, insert, ls, grep, find, mkdir, rm]
    constraints:
      paths: ["{{workspace}}"]
      max_file_size: "50MB"
```

### How Constraints Work

```
App YAML defines constraint values
        |
Runtime resolves templates ({{workspace}} -> /home/user/project)
        |
ExecutionContext(constraints={"paths": ["/home/user/project"], "max_file_size": "50MB"})
        |
Module.execute() -> each action checks _check_path() / _check_size()
        |
ActionResult(success=False, error="Path '/etc/passwd' is outside allowed paths")
```

### Supported Constraints

| Constraint       | Type          | Scope     | Description                                              |
|------------------|---------------|-----------|----------------------------------------------------------|
| `paths`          | `string_list` | Universal | Allowed path prefixes. All operations are restricted.    |
| `max_file_size`  | `size`        | Module    | Max file size for read/write (e.g. `"50MB"`).            |

**Scope meanings:**

- **Universal** - the runtime enforces the constraint before calling the module.
  Any module that touches paths can reuse this constraint.
- **Module** - the module itself enforces the constraint. Only relevant for this
  specific module.

### No Constraints = Dev Mode

When no `ExecutionContext` or no `constraints` are passed, all operations are
allowed. This is the default for unit tests and development.

## Cross-Module Workflows

### Read, Transform, Write

A typical agent workflow: read a config, modify it, write it back.

```yaml
actions:
  - id: read-config
    module: filesystem
    action: read
    params:
      path: "{{workspace}}/config.yaml"

  - id: update-config
    module: filesystem
    action: edit
    params:
      path: "{{workspace}}/config.yaml"
      old_string: "debug: false"
      new_string: "debug: true"
```

### Search and Fix

Agent searches for a pattern, then edits matching files.

```yaml
actions:
  - id: find-todos
    module: filesystem
    action: grep
    params:
      pattern: "TODO:"
      path: "{{workspace}}/src"
      include: "*.py"

  # Agent processes results and calls edit on each file
```

### Scaffold a Project

Agent creates a directory structure and writes files.

```yaml
actions:
  - id: create-structure
    module: filesystem
    action: mkdir
    params:
      path: "{{workspace}}/src/components"

  - id: write-component
    module: filesystem
    action: write
    params:
      path: "{{workspace}}/src/components/Button.tsx"
      content: |
        export function Button({ label }: { label: string }) {
          return <button>{label}</button>;
        }
```

### Integration with os_exec

Combine filesystem operations with shell commands.

```yaml
modules:
  - module: filesystem
    actions: [read, write, edit, ls, grep, find]
    constraints:
      paths: ["{{workspace}}"]

  - module: os_exec
    actions: [run_command]
    constraints:
      working_directory: "{{workspace}}"
      timeout: "30s"
      rate_limit_per_minute: 10
```

## Agent Workflow Patterns

### Line-Number Aware Editing

The agent reads a file and sees numbered lines:

```
 1│import os
 2│import sys
 3│
 4│def main():
 5│    print("hello")
```

It can then reference exact lines for insertion:

```yaml
- action: insert
  params:
    path: "{{workspace}}/main.py"
    line: 3
    content: "import json"
```

Or surgical replacement:

```yaml
- action: edit
  params:
    path: "{{workspace}}/main.py"
    old_string: '    print("hello")'
    new_string: '    print("hello world")'
```

Both return a **preview** with line numbers so the agent can verify the change
without re-reading the whole file.
