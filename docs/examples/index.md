---
id: examples-index
title: Examples
---

# Examples

Working YAMLs. Every example here is verified to compile and run
against a live daemon. The full collection (with deeper context)
lives in [language/15-examples.md](../language/15-examples.md);
this section is the curated subset that backs the doc's recipes.

## Smallest possible app

The minimum that compiles and runs:

```yaml
app:
  app_id: smallest
  name: Smallest

agents:
  - id: a
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

Deploy + chat:

```bash
digitorn dev deploy -d http://127.0.0.1:8000 smallest.yaml
digitorn dev chat smallest -m "ping"
# → "pong"
```

## Canonical 8-block reference

The example documented in
[Language - Complete example](../language/02-app-config.md#complete-example)
exercises every top-level block. Use it as a template when
authoring a new YAML.

## Real-world examples

For production-shape apps (multi-agent, RAG, channels, sandboxed
MCP, credentialed brains), see
[language/15-examples.md](../language/15-examples.md). Each example
there names the modules it requires, the credentials it expects,
and the deploy command.

## How examples are verified

Every YAML in the documentation is verified by the same automated
flow:

1. Save the YAML to a temp file.
2. `digitorn dev deploy -d <daemon> <file>` - the compile + bootstrap
   path. A failure here means the YAML is structurally wrong.
3. `client.send_live(session, "<prompt>")` from the Python testing
   SDK - opens a Socket.IO stream and waits for `message_done`.
4. Assert against `result.content` and the event sequence.

A YAML that doesn't pass step 3 doesn't ship in the docs.
