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

This minimal app deploys, runs, and answers a chat turn end-to-end.
Everything that follows is a layer on top of this same shape.

---

## Where to start

| Audience | Start here |
|----------|-----------|
| New to Digitorn | [Tutorial](tutorial/) - linear path, hello world to a real app. |
| Writing a YAML | [Language reference](language/) - the v1 grammar, frozen. |
| Adding a tool | [Module reference](reference/modules/) - 23 modules, one page each. |
| Building a UI client | [Client SDKs](reference/client-sdks/) - Flutter, React, Python testing. |
| Deploying | [Deployment](deployment/). |
| Solving a specific task | [How-tos](howtos/). |
| Understanding *why* | [Concepts](concepts/) - architecture, mental models, [glossary](concepts/glossary.md). |

---

## The 8-block YAML

Every Digitorn app is a single YAML file with up to 8 top-level
blocks. There is exactly one canonical place to declare each
field; legacy flat YAMLs are auto-rewritten by the alias pass
before validation.

| Block | Purpose | Reference |
|-------|---------|-----------|
| `app:` | Identity (id, name, version, icon, ...). | [language/app](language/02-app-config.md#app--identity) |
| `runtime:` | Lifecycle and execution policy. | [language/runtime](language/02-app-config.md#runtime--lifecycle-and-execution-policy) |
| `agents:` | Brains, system prompts, sub-agent pools. | [language/agents](language/03-agents.md) |
| `tools:` | Modules, capabilities, channels. | [language/tools](language/04-tools.md) |
| `security:` | Behavior + sandbox + credentials schema. | [language/security](language/11-security.md) |
| `ui:` | Theme, widgets, workspace, preview. Daemon never reads. | [language/ui](language/02-app-config.md#ui--display-layer-daemon-never-reads) |
| `dev:` | Skills, variables, includes. Dev-time only. | [language/dev](language/02-app-config.md#dev--developer-affordances) |
| `flow:` | Optional declarative orchestration graph. | [language/flow](language/07-flows.md) |

The schema is defined in `AppDefinition`
(`packages/digitorn/core/app/schema.py`); each block has a dedicated
Pydantic model with `extra: "forbid"`.

For a formal grammar of the YAML language, see
[language/grammar.md](language/grammar.md).

---

## Documentation policy

Every claim in this documentation is cross-checked against the
source code. Every YAML example is deployed against a live
daemon. Every tool call shown in an example has been observed
end-to-end. If you find a divergence between this documentation
and the running system, the documentation is the bug. Open an
issue.

The verification flow is automated:

1. The YAML is saved to a temp file.
2. `digitorn dev deploy -d <daemon> <file>` exercises the compile
   + bootstrap path.
3. `client.send_live(session, "<prompt>")` from the Python
   testing SDK opens a Socket.IO stream and waits for
   `message_done`.
4. The example fails the doc build if the assertions don't match.

A YAML that doesn't pass step 3 doesn't ship.

---

## Stability

The 8-block YAML is the **v1 language**. Once an app declares it,
the YAML keeps parsing across every minor and patch release of
the daemon that supports v1. What that means in practice (no
required field added, no field type narrowed, default values
stable, deprecation policy) is in
[versioning.md](versioning.md).

---

## Source

Source: [github.com/digitorn/digitorn-bridge](https://github.com/)

License: MIT

The daemon is a Python 3.12 FastAPI / Uvicorn process. The
front-end clients (Flutter, Next.js) are separate repos that
talk to the daemon over REST + Socket.IO; they are documented
under [Client SDKs](reference/client-sdks/).
