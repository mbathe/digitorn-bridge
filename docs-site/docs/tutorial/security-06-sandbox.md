---
id: security-06-sandbox
title: "Security 6 - OS sandbox profiles"
sidebar_label: "Security 6: Sandbox"
---

The previous five security tutorials covered **application-level**
defences: capabilities decide which actions the agent can call,
the behaviour engine intercepts patterns of use, the credentials
vault encrypts secrets. They all run in the same Python process
as the agent loop. A bug in any of them - or an exploit chain that
defeats them all - reaches the kernel without further checks.

The **OS sandbox** is the layer **below** every application
defence. It runs the agent's worker process inside a kernel-level
isolation primitive (Landlock, seccomp, namespaces on Linux;
Seatbelt on macOS; Job Objects on Windows) so even if everything
else fails, the syscalls the worker can issue are bounded by the
kernel.

This is defence-in-depth. The application gates are 99 % of the
real-world story; the sandbox is the 1 % that matters when the 99
breaks.

## The four levels

| Level     | Linux                                                                | macOS                          | Windows                            |
|-----------|----------------------------------------------------------------------|--------------------------------|------------------------------------|
| `off`     | No sandbox; agent runs as-is                                         | Same                           | Same                               |
| `standard`| Landlock (FS rules) + seccomp (syscall allowlist) + cgroups          | Seatbelt sandbox-exec          | Job Objects (process kill, mem cap, CPU cap) |
| `strict`  | + warm pool + user/PID namespaces + capability drop + MDWE           | Requires extra entitlements    | Advisory only (no kernel FS isolation) |
| `maximum` | + network namespace + seccomp-notify audit + workspace snapshot      | Requires extra entitlements    | Advisory only                      |

The level chooses **what** runs; the daemon picks the right
implementation based on the host kernel. On Linux, `standard`
enables Landlock if the kernel supports it (5.13+) and falls back
to a seccomp-only profile on older kernels.

## The YAML

Save this as `sandbox-bot.yaml`. The interesting line is
`security.sandbox.level: standard`.

```yaml
app:
  app_id: sandbox-bot
  name: Sandbox Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 60

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: deepseek_main
        scope: per_user
        provider: deepseek
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      temperature: 0
      max_tokens: 200
    system_prompt: |
      You can use Bash and filesystem tools. Be concise. If a
      tool or syscall is denied at the OS level, report what
      was rejected.

tools:
  modules:
    shell: {}
    filesystem: {}
  capabilities:
    default_policy: auto
    max_risk_level: high
    grant:
      - module: shell
        actions: [bash]
      - module: filesystem
        actions: [read, glob, grep]

security:
  sandbox:
    level: standard
    pool_size: 2                        # warm workers ready to accept sessions
    pool_max: 4                         # cap under load
    workspace_snapshot: false           # CoW snapshots (Linux maximum-level only)
```

`pool_size` controls how many sandbox workers stay warm. New
sessions check out a worker from the pool instead of spawning one
each time - the spawn cost (Landlock setup, namespace
construction) only happens at warm-up. `pool_max` bounds the total
under load. `workspace_snapshot` enables copy-on-write snapshots
of the workspace per session - Linux-only, requires `maximum`
level.

## Live - the YAML compiles and the app runs normally

The sandbox is a wrapper around the worker process; from the
agent's perspective, granted tools work as usual. Real session
captured against the daemon:

```text
> Run Bash: echo SANDBOX_OK
SANDBOX_OK
```

`tool_calls_count: 1`, the Bash call ran, the agent reported the
output unchanged. The sandbox sat between the Python worker and
the OS without changing what `echo` does.

On Linux 5.13+, this same Bash with the same `level: standard`
would be running inside Landlock rules that **deny filesystem
access outside the workspace** (the workspace path is whitelisted
read/write; everything else is read-only at best, often denied
entirely). On macOS, Seatbelt enforces a similar rule. On Windows,
Job Objects bound CPU/memory but the FS is not enforced at the
kernel layer.

## What each level adds

`standard` is what most production apps run. The unsafe-by-default
syscalls are blocked (`ptrace`, `mount`, `pivot_root`, raw
sockets, `/proc/sys/kernel/*` writes). The agent's filesystem
access is restricted to its workspace and read-only system paths.
A misbehaving Bash `rm -rf /` would land on the workspace boundary
and stop there, regardless of permissions on the runtime user.

`strict` adds **process isolation** via user and PID namespaces.
The worker sees its own PID space; even if it tries to `kill -9`
another worker, it can't see the other PIDs. Capability drop strips
`CAP_SYS_ADMIN` and friends. **MDWE** (memory deny write+execute)
prevents JIT'd code from running, useful for blocking
shellcode-style exploits.

`maximum` adds the **network namespace** - the worker has its own
loopback and no default route. Outbound HTTP must go through a
declared proxy (configured at the daemon). seccomp-notify audit
logs every blocked syscall for forensics. Workspace snapshots use
overlayfs CoW so each session sees its own copy of the workspace
without paying the full disk-copy cost.

## What about Windows?

Job Objects do CPU caps, memory caps, and process-tree kill
nicely. They do not do filesystem isolation at the kernel layer.
Apps that run on Windows daemons get **advisory** sandboxing for
`strict` and `maximum`: the YAML compiles, the app deploys, the
worker runs, but the FS rules are enforced only by the
application-level capability gate, not the kernel.

The daemon reports its **effective** sandbox capability per worker
in the diagnostics endpoint. If your app needs hard FS isolation,
deploy on Linux; on Windows treat the sandbox levels as a defence
hardening signal, not a guarantee.

## Composing with the application gates

The sandbox does not replace the seven security gates - it
backstops them. A typical production app stacks:

1. **Capability layer** (gates 0-6): the agent can only call
   actions the YAML grants. (See
   [tutorials 1-2](security-01-approval.md).)
2. **Behaviour engine**: rules intercept patterns of use even
   for granted actions. (See
   [tutorials 3](security-03-custom-rule.md).)
3. **OS sandbox**: kernel enforces the surface even if the first
   two are bypassed.

The sandbox is the only one of the three that doesn't depend on
the daemon's own code paths being correct. A bug in the
application gate could let an action through; the kernel rules
still hold. Real defence-in-depth means assuming each layer above
the kernel is breakable and building accordingly.

## Going further

- The full sandbox reference (per-platform availability matrix,
  fine-tuning each level, troubleshooting Landlock kernel
  versions):
  [OS Sandbox](../language/35-sandbox.md).
- Per-platform daemon deployment (which kernels Landlock
  requires, how to enable user namespaces in non-root containers,
  Job Objects entitlements on Windows):
  [Production Deployment](../language/36-production.md).
- The **MCP server sandbox** (a related but distinct
  primitive that isolates external MCP processes the agent
  connects to):
  [MCP module](../reference/modules/mcp.md).
