# Shell Module - Integration Guide

## Constraint System

The shell module supports **constraints** that applications define in their
YAML to restrict what the agent can do. This is how you sandbox an agent
to a workspace directory and limit which actions are available.

### Declaring Constraints in App YAML

```yaml
modules:
  shell:
    constraints:
      allowed_actions: [run, script, which, env]
```

### How Constraints Work

```
App YAML defines constraint values
        |
Runtime resolves templates ({{workspace}} -> /home/user/project)
        |
ExecutionContext(constraints={"allowed_actions": ["run", "script", "which", "env"]})
        |
ShellModule.__init__() -> get_adapter() detects platform
        |
Module.execute() -> each action checks blacklist / workspace confinement
        |
ActionResult(success=False, error="Command rejected: matches forbidden pattern 'rm -rf /'")
```

### Supported Constraints

| Constraint | Type | Scope | Description |
|------------|------|-------|-------------|
| `allowed_actions` | `string_list` | Universal | Actions the agent is allowed to call. |

**Scope meanings:**

- **Universal** - the runtime enforces the constraint before calling the module.
- **Module** - the module itself enforces the constraint internally.

### No Constraints = Dev Mode

When no `ExecutionContext` or no `constraints` are passed, all actions are
allowed. This is the default for unit tests and development.

---

## Platform Adaptation

Actions are registered **dynamically at startup** based on the detected OS.
No platform-specific code is loaded on an unsupported system.

```
ShellModule.__init__()
        |
        └── get_adapter()
              ├── Linux/macOS → UnixAdapter
              │     default_shell = /bin/bash
              │     forbidden_patterns = Unix patterns
              │     actions loaded = run, script, bash, which, env
              │
              └── Windows → WindowsAdapter
                    default_shell = powershell.exe
                    forbidden_patterns = Windows patterns
                    actions loaded = run, script, powershell, which, env
```

---

## Cross-Module Workflows

### Write a script then execute it

A typical agent workflow: write a script with the filesystem module,
then run it with the shell module.

```yaml
modules:
  filesystem:
    constraints:
      allowed_actions: [read, write, mkdir]
  shell:
    constraints:
      allowed_actions: [run, script, which]
```

```yaml
actions:
  - id: write-script
    module: filesystem
    action: write
    params:
      path: "{{workspace}}/scripts/build.sh"
      content: |
        #!/bin/bash
        echo "Building project..."
        pip install -r requirements.txt
        pytest tests/
      create_dirs: true

  - id: run-script
    module: shell
    action: script
    params:
      path: "{{workspace}}/scripts/build.sh"
      timeout: 120.0
```

### Check environment then run

Agent verifies a dependency exists before running a command.

```yaml
actions:
  - id: check-python
    module: shell
    action: which
    params:
      command: "python3"

  # Agent checks result, then proceeds only if python3 was found

  - id: run-tests
    module: shell
    action: run
    params:
      command: "python3 -m pytest tests/ -v"
      cwd: "{{workspace}}"
      timeout: 60.0
```

### Inspect environment before execution

Agent checks env variables before running a deployment script.

```yaml
actions:
  - id: check-env
    module: shell
    action: env
    params:
      filter: "DEPLOY"

  # Agent verifies DEPLOY_TARGET is set, then runs the deploy script

  - id: deploy
    module: shell
    action: script
    params:
      path: "{{workspace}}/scripts/deploy.sh"
      timeout: 180.0
```

### Multi-step Bash workflow (Linux/macOS)

Agent runs a multi-step pipeline in a single Bash block.

```yaml
actions:
  - id: build-and-test
    module: shell
    action: bash
    params:
      cwd: "{{workspace}}"
      timeout: 120.0
      script: |
        echo "Installing dependencies..."
        pip install -r requirements.txt

        echo "Running linter..."
        ruff check src/

        echo "Running tests..."
        pytest tests/ -v

        echo "Done."
```

### Multi-step PowerShell workflow (Windows)

Agent runs a multi-step pipeline in a single PowerShell block.

```yaml
actions:
  - id: build-and-test
    module: shell
    action: powershell
    params:
      cwd: "{{workspace}}"
      timeout: 120.0
      script: |
        Write-Host "Installing dependencies..."
        pip install -r requirements.txt

        Write-Host "Running tests..."
        pytest tests/ -v

        Write-Host "Done."
```

### Read output, write result to file

Agent runs a command and saves its output using the filesystem module.

```yaml
actions:
  - id: get-deps
    module: shell
    action: run
    params:
      command: "pip freeze"
      cwd: "{{workspace}}"

  # Agent takes stdout from get-deps and passes it to write

  - id: save-deps
    module: filesystem
    action: write
    params:
      path: "{{workspace}}/requirements.lock"
      content: "{{steps.get-deps.stdout}}"
```

---

## Security Patterns

### Minimal permissions for read-only inspection

```yaml
modules:
  shell:
    constraints:
      allowed_actions: [which, env]
```

### Allow execution but not environment inspection

```yaml
modules:
  shell:
    constraints:
      allowed_actions: [run, script]
```

### Full access for a trusted automation agent

```yaml
modules:
  shell:
    constraints:
      allowed_actions: [run, script, bash, which, env]
```

> ⚠️ Always keep `capabilities.default_policy: approve` in your app YAML
> when granting shell execution permissions. Every command will require
> explicit confirmation before running.

---

## Agent Workflow Patterns

### Check-then-Act

The agent always verifies a precondition before executing a destructive action.

```
which python3          → check interpreter exists
env filter=VIRTUAL     → check virtual env is active
run: pip install ...   → safe to proceed
```

### Output capture loop

The agent captures command output and uses it to decide next steps.

```
run: git status        → check working tree state
run: git diff          → inspect changes
run: git commit -m ... → commit if changes look correct
```

### Escalating timeout

Start with short timeout, retry with longer if needed.

```yaml
- action: run
  params:
    command: "make build"
    timeout: 30.0       # fast first attempt

- action: run
  params:
    command: "make build"
    timeout: 120.0      # slower retry if first timed out
```