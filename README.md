# Digitorn

Digitorn is an interpreter for AI agents. Install it once, write a
YAML manifest, run it. Same model as `python` for `.py` or `node`
for `.js`.

```yaml
app:
  app_id: hello

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: "Reply with one short sentence."
```

```bash
digitorn dev deploy hello.yaml --scope user
digitorn dev chat hello -m "ping"
# pong
```

That YAML is the entire app. No Python loop to wire up. No SDK
glue code. No context-window bookkeeping. No tool dispatcher to
implement. The interpreter handles all of it.

## Why declarative

Every layer of the stack has moved from imperative to declarative
because the declarative version scales better, audits cleaner,
and removes glue code:

- Bash scripts → Terraform
- `docker run` → Kubernetes manifests
- jQuery → React
- Manual SQL → ORM and migration specs

AI agents are the next layer. Writing the orchestration loop,
the tool dispatcher, the retry policy, the sandbox, and the cost
cap by hand is the bash-scripts-for-infra of the agent era.
Digitorn makes the manifest the source of truth and runs
everything from it.

## What the interpreter handles

A single `digitorn start` brings every primitive a production
agent needs:

- 23 modules: filesystem, shell, web, http, RAG, vector,
  database, LSP, MCP, channels (webhook / cron / email / slack /
  discord / voice / ...), memory, workspace, widgets, ...
- 16 middleware: secret masking, content filter, RAG injection,
  retry, circuit breaker, dedup, audit, ...
- Multi-agent orchestration with sub-agent spawning
- OS-level sandbox (Landlock / seccomp / Job Objects)
- Credentials vault (OAuth2, API key, mTLS, ...)
- Real-time event stream over Socket.IO
- Behavior engine with declarative rules

Adding a new capability to your agent is one YAML key, not a
`pip install` plus integration code.

## Install

### Windows

```powershell
irm https://digitorn.ai/install.ps1 | iex
```

### macOS / Linux

```bash
curl -fsSL https://digitorn.ai/install.sh | sh
```

The installer fetches Python 3.12 via [uv](https://docs.astral.sh/uv/),
installs the `digitorn` CLI, registers a background service
(Windows Service / launchd / systemd), and starts the
interpreter on `http://127.0.0.1:8000`.

Already have Python 3.12?

```bash
pip install digitorn         # or: uv tool install digitorn
digitorn service install
digitorn service start
```

## First steps

```bash
digitorn doctor                       # check the environment
digitorn init my-app && cd my-app     # scaffold a project
digitorn dev deploy app.yaml          # push to the local interpreter
digitorn dev chat my-app              # interactive chat
```

Full reference: [docs.digitorn.ai](https://docs.digitorn.ai).

## Requirements

- Windows 10+, macOS 12+, or a recent Linux distro
- Python 3.12 (the installer fetches it via uv if missing)
- 2 GB free disk space for the model cache (embeddings, ONNX runtimes)
- Outbound HTTPS for LLM providers and MCP servers you choose to use

## Operating the interpreter

```bash
digitorn service status               # is it running?
digitorn service logs                 # last 50 lines
digitorn service stop                 # stop until next boot
digitorn service start                # start again
digitorn service uninstall            # remove the service
```

Logs:

- Windows: Event Viewer (`Applications and Services Logs > DigitornDaemon`)
- macOS: `~/Library/Logs/digitorn/`
- Linux: `journalctl --user -u digitorn`

The interpreter also writes structured logs to `~/.digitorn/logs/`.

## Development

```bash
git clone https://github.com/mbathe/digitorn-bridge
cd digitorn-bridge
uv sync                               # or: poetry install
uv run digitorn start                 # foreground, with the venv's deps
```

## License

Apache 2.0. See [LICENSE](LICENSE).
