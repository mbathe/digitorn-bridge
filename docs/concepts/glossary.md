---
id: glossary
title: Glossary
---

# Glossary

| Term | Definition |
|------|-----------|
| **Action** | A function exposed by a module that an agent can call. Decorated with `@action` in the module's Python source. Has a name, a Pydantic params model, a return type, a `risk_level`, and an optional `tool_prompt` injected into the system prompt. |
| **Agent** | An LLM-powered entity with its own brain (LLM config), system prompt, and tool surface. Declared under `agents:` in the YAML. Can be a coordinator (spawns sub-agents) or specialist / worker. |
| **Alias pass** | The compiler step that reshapes legacy flat YAMLs (`execution:`, `modules:` at the top level) into the canonical 8-block form before Pydantic validates. Lives in `core/app/schema_aliases.py`. |
| **App** | A deployable unit: one `app.yaml` (and optional bundle directory). Gets an `app_id`, deploys under a `(app_id, scope, owner_user_id)` triple, runs in a daemon. |
| **AppDefinition** | The Pydantic root model in `core/app/schema.py`. The canonical 8-block YAML grammar. |
| **Bootstrap** | The daemon-side phase that turns a `CompiledApp` into a `RuntimeApp`. Instantiates modules, pushes configs, runs setup steps, builds the tool index, wires the agent loop. |
| **Brain** | LLM config: provider, model, backend, optional fallback, optional context override. Declared under `agents[].brain`. |
| **Bundle** | The on-disk directory holding an app's compiled artefacts (`app.yaml` + `meta.json` + bundled prompts/skills/behavior/assets). One per `(app_id, scope, owner_user_id)` triple. |
| **Capability** | An action permission expressed in `tools.capabilities`: `grant`, `deny`, `approve`. Compiled into a `SecurityProfile` consulted on every tool call. |
| **Channel** | A bidirectional I/O surface: webhook, cron, file_watcher, email, RSS, queue, slack, discord, telegram, voice. Configured under `tools.channels.<name>`. |
| **Compaction** | Automatic summarisation of old messages when the context window fills up. Configured under `runtime.context.strategy` and `compression_trigger`. |
| **Compiler** | `AppYAMLCompiler` in `core/app/compiler.py`. Reads YAML, runs the alias pass, validates with Pydantic, resolves variables and bundle namespaces, produces a `CompiledApp`. |
| **Context window** | Max tokens an LLM can process per request. Per-brain via `agent.brain.context.max_tokens`, default-detected from the provider hint. |
| **Coordinator** | An agent with `role: coordinator` that can spawn sub-agents via the `Agent` tool. |
| **Credential** | Named external secret (api_key, OAuth token, mTLS pair, ...). Lives in the encrypted vault, referenced by name in YAML. Has a scope (`system_wide`, `per_app_shared`, `per_user`, `per_app_per_user`). |
| **Daemon** | The long-running FastAPI / Uvicorn process. Hosts the daemon HTTP API, the Socket.IO event stream, the agent loop, and the module instances. |
| **Direct mode** | A tool injection mode where every tool's full JSON schema is passed to the LLM up front. Auto-selected for small toolsets. See [Tools](../language/04-tools.md#adaptive-tool-injection). |
| **Discovery mode** | A tool injection mode where the LLM sees only meta-tools (`search_tools`, `get_tool`, ...) and discovers domain tools on demand. Auto-selected for large toolsets. |
| **FQN** | Fully-qualified name. A tool's canonical id: `<module_id>.<action>`, e.g. `shell.bash`, `filesystem.write`. |
| **Hook** | A configurable runtime callback that fires on a specific event (turn_start, tool_start, tool_end, ...) and runs an action (compact_context, inject_message, gate, ...). Fully declarative, lives under `runtime.hooks`. |
| **Manifest** | The client-facing summary of an app: identity, modes, modules, slash commands, theme, etc. Built by `manager.summary()`. |
| **Middleware** | A composable wrapper around tool calls. Runs `before` and `after` the action method. Built-ins: audit, retry, redact, rate_limit, content_filter. |
| **Module** | A self-contained Python package under `packages/digitorn/modules/<id>/`. Subclasses `BaseModule`, declares `CONFIG_MODEL`, `CONSTRAINTS`, action methods. |
| **Provider** | An LLM service backend (DeepSeek, OpenAI, Anthropic, Ollama, ...). Declared under `agents[].brain.provider`, validated against the known set. |
| **Provider hint** | The string in `brain.provider`. Resolves the default `base_url` and the default backend (`openai_compat` vs `anthropic`). |
| **Risk level** | A per-action classification (`low`, `medium`, `high`) used by the capabilities gate. Unset = `medium`. |
| **Role** | The functional role of an agent: `coordinator`, `specialist`, `worker`. Free-form descriptive roles (`assistant`, `analyst`, ...) are also accepted. |
| **Scope** | Where a credential applies. `system_wide` = one for the whole daemon; `per_app_shared` = one per app, shared by all users; `per_user` = one per user, app-agnostic; `per_app_per_user` = one per app per user. |
| **Session** | A per-conversation state container. Has an id, a list of messages, a tool-call history, persisted facts. |
| **Skill** | A reusable workflow markdown file the agent loads on demand via `use_skill`. Declared under `dev.skills`. |
| **Specialist** | A sub-agent with `role: specialist` and a constrained tool surface. Spawned by a coordinator via the `Agent` tool. |
| **Token pressure** | Ratio of used tokens to context window. When it crosses `runtime.context.compression_trigger` (default 0.75), auto-compaction fires. |
| **Tool** | What the LLM calls. Always backed by an `@action` somewhere - either on a module (most common), on `context_builder` (meta-tools), or on `agent_spawn` (sub-agent ops). |
| **Trigger** | An inbound event source under `runtime.triggers` (legacy: `cron`, `watch`, `http`) or via the `channels` module (production: 11 adapters). |
| **Variable** | Compile-time substitution: `{{my_var}}` (under `dev.variables`), `{{env.X}}`, `{{secret.X}}`, `{{sys.X}}`, `{{app.X}}`, bundle namespaces (`{{prompt.X}}`, `{{skill.X}}`, `{{behavior.X}}`, `{{asset.X}}`). |
| **Working memory** | Goal + todos + facts + entities, rendered into the system prompt every turn. Lives in the memory module's session-scoped store. |
| **Workspace (renderer)** | `ui.workspace` - the in-memory virtual filesystem the workspace module mirrors to the client (Lovable-style). NOT the same as `runtime.workdir`. |
| **Workspace (path)** | `runtime.workdir` - the host filesystem path the agent's filesystem and shell modules root themselves at. NOT the same as `ui.workspace`. |
