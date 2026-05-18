---
id: index
title: Digitorn Documentation
slug: /
---

# Digitorn

A declarative framework for building AI agent applications.
Define what your agents do, how they think, and what tools
they use, entirely in YAML.

```yaml
app:
  app_id: hello
  name: "Hello"

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen25-7b-gpu:latest
      backend: openai_compat
      config:
        base_url: http://localhost:11434/v1
        api_key: ollama
    system_prompt: "Reply with exactly one word: pong."
```

```bash
digitorn dev deploy hello.yaml
digitorn dev chat hello -m "ping"
# → pong
```

That YAML compiles, deploys, runs, and answers a chat turn. The rest of
the documentation is layers added on top of this same shape.

---

## Where to start

If you are new to Digitorn, work through the [Tutorial](/docs/tutorial/);
it walks from a hello-world to something close to a real app, in
order.

When you are writing a YAML, the [Language reference](/docs/language/) is
the v1 grammar, frozen.

When you need a specific tool, the [Module reference](/docs/reference/modules/)
has one page per shipped module (23 of them).

For UI clients, see [Client SDKs](/docs/reference/client-sdks/) (web,
React, Python testing). For [Deployment](/docs/deployment/), [How-tos](/docs/howtos/)
and the architecture rationale, [Concepts](/docs/concepts/) and the
[glossary](/docs/concepts/glossary) are the entry points.

---

## The 8-block YAML

A Digitorn app is one YAML file with up to eight top-level blocks.
Each field has one canonical home; legacy flat YAMLs (modules at the
root, `execution:` block, etc.) are rewritten by the alias pass
before validation runs.

| Block | Purpose | Reference |
|-------|---------|-----------|
| `app:` | Identity (id, name, version, icon, ...). | [language/app](language/02-app-config.md#app---identity) |
| `runtime:` | Lifecycle and execution policy. | [language/runtime](language/02-app-config.md#runtime---lifecycle-and-execution-policy) |
| `agents:` | Brains, system prompts, sub-agent pools. | [language/agents](language/03-agents.md) |
| `tools:` | Modules, capabilities, channels. | [language/tools](language/04-tools.md) |
| `security:` | Behavior + sandbox + credentials schema. | [language/security](language/11-security.md) |
| `ui:` | Theme, widgets, workspace, preview. Daemon never reads. | [language/ui](language/02-app-config.md#ui---display-layer-daemon-never-reads) |
| `dev:` | Skills, variables, includes. Dev-time only. | [language/dev](language/02-app-config.md#dev---developer-affordances) |
| `flow:` | Optional declarative orchestration graph. | [language/flow](language/07-flows.md) |

The root schema is `AppDefinition`; each block has a dedicated
Pydantic model with `extra: "forbid"`.

For a formal grammar of the YAML language, see
[language/grammar.md](language/grammar.md).

---

## Documentation policy

Claims in this documentation are cross-checked against the source
code, and YAML examples are deployed against a live daemon before
they ship. If you spot a divergence between what's written here and
how the running system behaves, treat the doc as the bug and open
an issue.

The verification path is automated. The YAML is written to a temp
file, `digitorn dev deploy -d <daemon> <file>` runs the compile and
bootstrap path, then `client.send_live(session, "<prompt>")` from
the Python testing SDK opens a Socket.IO stream and waits for
`message_done`. If the assertions don't match, the doc build fails;
a YAML that can't reach step 3 doesn't ship.

---

## Stability

The 8-block YAML is the **v1 language**. Once an app declares it, the
YAML keeps parsing across every minor and patch release of a v1
daemon. The detail (which fields can change, deprecation timing,
default-value policy) lives in [versioning.md](versioning.md).

---

License: MIT

The daemon is a Python 3.12 FastAPI / Uvicorn process. The
front-end clients (web, Next.js) are separate repos that
talk to the daemon over REST + Socket.IO; they are documented
under [Client SDKs](/docs/reference/client-sdks/).
