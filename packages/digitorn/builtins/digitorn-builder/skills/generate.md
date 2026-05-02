---
version: 1
description: How to generate app YAML - discovery-first, never hallucinate
---

## Generate skill

### The discovery-first rule

BEFORE writing ANY YAML block, verify it exists:

```
# List all modules
http.get(url="http://127.0.0.1:8000/api/discovery/modules")

# Get exact actions for a module
http.get(url="http://127.0.0.1:8000/api/discovery/modules/<module_id>")

# List all trigger types
http.get(url="http://127.0.0.1:8000/api/discovery/triggers")

# List credential providers
http.get(url="http://127.0.0.1:8000/api/credentials/providers")
```

### From template (preferred)

1. Fetch the template YAML:
   ```
   http.get(url="http://127.0.0.1:8000/api/discovery/templates/<id>")
   ```
2. Read it carefully
3. Adapt fields: app_id, name, modules, triggers, system_prompt
4. Compile and iterate

### From scratch

Query RAG for each concept you need:
```
rag.query(knowledge_base="digitorn_concepts", query="<topic>", top_k=3)
rag.query(knowledge_base="digitorn_modules", query="<module name> actions params", top_k=5)
```

### YAML structure (ALL possible blocks)

```yaml
app:
  app_id: my-app
  name: My App
  version: 1.0.0
  icon: emoji or URL
  color: '#hex'
  category: general
runtime:
  mode: conversation
  entry_agent: main
  max_turns: 30
  timeout: 300
  triggers: []
  session_mode: mono
  payload_schema: null
  workdir_mode: auto
agents:
- id: main
  role: coordinator
  brain:
    provider: anthropic
    model: claude-sonnet-4-5
    config:
      api_key: claude-code
    temperature: 0.2
    max_tokens: 8192
    fallback:
      provider: anthropic
      model: claude-haiku-4-5
      config:
        api_key: claude-code
    context:
      max_tokens: 200000
      strategy: summarize
      auto_compact: true
  system_prompt: 'Short prompt here.  For external prompt files put them in prompts/
    and reference by filename.

    '
tools:
  modules:
    module_id:
      config: {}
      setup:
      - action: connect
        params:
          connection_id: main
      constraints: {}
  capabilities:
    default_policy: block
    max_risk_level: medium
    grant:
    - module: filesystem
      actions:
      - read
      - write
      - edit
      - glob
      - grep
    - module: memory
      actions:
      - set_goal
      - remember
      - recall
  channels:
    channel_name:
      type: webhook
      config:
        url: '{{secret.SLACK_WEBHOOK}}'
ui:
  greeting: Hello!
  workspace:
    render_mode: react
    entry_file: src/App.tsx
    title: My App
  preview:
    enabled: false
dev:
  variables:
    key: value
```

### Brain provider config

For **claude-code** (free, uses local OAuth token):
```yaml
brain:
  provider: anthropic
  model: claude-sonnet-4-5
  config:
    api_key: "claude-code"
```

For **DeepSeek**:
```yaml
brain:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  config:
    api_key: "{{secret.DEEPSEEK_API_KEY}}"
    base_url: "https://api.deepseek.com/v1"
```

For **OpenAI**:
```yaml
brain:
  provider: openai
  model: gpt-4o
  config:
    api_key: "{{secret.OPENAI_API_KEY}}"
```

### Multi-agent pattern

```yaml
runtime:
  entry_agent: coordinator
agents:
- id: coordinator
  role: coordinator
  brain:
    provider: anthropic
    model: claude-sonnet-4-5
    config:
      api_key: claude-code
  system_prompt: You orchestrate specialists...
- id: researcher
  role: specialist
  brain:
    provider: anthropic
    model: claude-sonnet-4-5
    config:
      api_key: claude-code
tools:
  capabilities:
    grant:
    - module: agent_spawn
      actions:
      - spawn_agent
      - agent_wait
      - agent_wait_all
      - agent_result
```

### Background app with triggers

```yaml
runtime:
  mode: background
  entry_agent: worker
  triggers:
  - id: hourly
    type: cron
    schedule: 0 * * * *
    message: Time to run
    routing: broadcast
  session_mode: multi
  payload_schema:
    prompt:
      required: true
      label: What to search for?
```

### Background app with webhook channel

```yaml
tools:
  channels:
    inbox:
      config:
        default_agent: handler
        providers:
          github_pr:
            adapter: webhook
            config:
              inbound_path: /hook/github
              auth: signature
              signature_secret: '{{secret.WEBHOOK_SECRET}}'
            activation:
              session: pr-{{event.payload.pull_request.id}}
              message: 'PR: {{event.payload.pull_request.title}}'
              reply: none
```

### Write it live

Write app.yaml to workspace so the preview canvas updates:
```
workspace.write(path="app.yaml", content=<yaml_string>)
```

Use workspace.edit for small changes:
```
workspace.edit(path="app.yaml",
  old_string="temperature: 0.7",
  new_string="temperature: 0.2")
```
