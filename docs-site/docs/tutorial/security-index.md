---
id: security-index
title: Security tutorials
sidebar_label: Security (intro)
---

The basic tutorials gave you an app that runs. The advanced
tutorials gave you primitives that scale (sub-agent isolation,
discovery, behaviour engine). The **security tutorials** focus on
the layer that decides **what an agent is permitted to do** and
**when a human is in the loop**.

Read them after the seven base tutorials. Each one ends with a
real live transcript showing the security layer firing - either
by blocking, by pausing for approval, or by filtering tools out
of the agent's view before it ever gets the chance to call them.

| Tutorial                                                                       | What it teaches                                                              |
|--------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| [Security 1 - Human-in-the-loop approval](security-01-approval.md)             | The `approve` capability policy: pause + supervisor confirms                 |
| [Security 2 - The seven security gates](security-02-gates.md)                  | Gate 0-6 sequence; live demo of `max_risk_level` filtering                   |
| [Security 3 - Custom behaviour rule that blocks](security-03-custom-rule.md)   | Project-specific invariants enforced at runtime, with `action: block`        |
| [Security 4 - Hidden actions vs deny](security-04-hidden-vs-deny.md)           | When to hide an action from the LLM vs deny it everywhere                    |
| [Security 5 - Credentials vault and scopes](security-05-credentials.md)        | Four scopes, compile-time validation, encryption + audit chain               |
| [Security 6 - OS sandbox profiles](security-06-sandbox.md)                     | Kernel-level isolation: Landlock + seccomp on Linux, Job Objects on Windows  |
| [Security 7 - Audit log and observability](security-07-audit.md)               | Reading the persistent event log to debug rejections and trace decisions     |

## Where security lives in a Digitorn app

A production app stacks **three independent surfaces**, each with
its own block in the YAML and its own enforcement layer:

| Block                            | Layer                | What it controls                                              |
|----------------------------------|----------------------|---------------------------------------------------------------|
| `tools.capabilities`             | Application gate     | Which actions the agent can call (grant / approve / deny)    |
| `security.behavior`              | Runtime rule engine  | Rule-based corrections injected during the agent loop        |
| `security.sandbox`               | OS kernel            | Process-level isolation (Landlock, seccomp, Job Objects)     |
| `security.credentials_schema`    | Vault contract       | Which credentials the app expects, with typed fields         |

The first three layers compose. A call passes only if every
layer says yes. Capabilities catch wrong actions, the behaviour
engine catches wrong **patterns of use**, the sandbox catches
the case where the first two were bypassed by a bug.

## What "security" actually means here

Two threat models, two sets of tools.

**The agent itself is the threat**. The model misinterprets the
user, hallucinates a tool call, gets prompt-injected through a
file it just read. Defences: capability policy (gate 4), the
risk-level cap (gate 2), behaviour rules
([Advanced 4](advanced-04-behavior.md)), per-agent module
restriction ([Advanced 1](advanced-01-sub-agent-isolation.md)).

**A specific tool call is the threat**. A `Bash` invocation that
asks for `rm -rf`, an `http.post` to `169.254.169.254`, a
`workspace.delete` on the user's project. Defences: approval
flow ([Security 1](security-01-approval.md)), classification
gates (gate 5), sandbox (Landlock filesystem rules,
network egress controls).

The seven security gates ([Security 2](security-02-gates.md)) are
the dispatch sequence both threat models share.

## Going further

- The full security architecture, with every gate and every
  policy field documented:
  [Security architecture](../language/11-security.md).
- The OS sandbox layer (per-platform): on Linux it's Landlock +
  seccomp; on macOS it's Seatbelt; on Windows it's Job Objects:
  [OS Sandbox](../language/35-sandbox.md).
- Credentials vault, encrypted at rest with envelope-wrapped
  per-row DEKs, hash-chained audit log:
  [Credentials](../reference/runtime/credentials.md).
- The behaviour engine reference (every built-in rule and the
  custom-rule schema):
  [Behavior Engine](../language/43-behavior.md).

For complementary reading: the
[advanced tutorials](advanced-index.md) cover sub-agent
isolation and the behaviour engine - both directly tied to the
security story.
