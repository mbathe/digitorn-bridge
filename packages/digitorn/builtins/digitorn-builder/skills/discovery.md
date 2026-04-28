---
version: 1
description: How to discover available modules, actions, triggers, MCP servers, and credentials
---

## Discovery skill

### Rule: NEVER guess - ALWAYS verify

Before writing any YAML block, query the live daemon to know what exists.

### Available modules

```
http.get(url="http://127.0.0.1:8000/api/discovery/modules")
```
Returns ALL loaded modules with their IDs and action counts.

### Module details (actions + params)

```
http.get(url="http://127.0.0.1:8000/api/discovery/modules/<module_id>")
```
Returns the exact action names, parameter names, types, and descriptions.

### Trigger types

```
http.get(url="http://127.0.0.1:8000/api/discovery/triggers")
```
Returns ALL trigger types with their config schema.

### Templates

```
http.get(url="http://127.0.0.1:8000/api/discovery/templates")
http.get(url="http://127.0.0.1:8000/api/discovery/templates/<id>")
```
Returns starter templates with their full YAML.

### Credential providers

```
http.get(url="http://127.0.0.1:8000/api/credentials/providers")
```
Returns available LLM providers (anthropic, openai, deepseek, etc.)
with their fields (api_key, base_url, etc.).

### MCP servers

```
http.get(url="http://127.0.0.1:8000/api/mcp/catalog")
http.get(url="http://127.0.0.1:8000/api/mcp/search?q=<keyword>")
http.get(url="http://127.0.0.1:8000/api/mcp/servers")
```

### Deployed apps (avoid collisions)

```
http.get(url="http://127.0.0.1:8000/api/apps")
```

### Installed packages

```
http.get(url="http://127.0.0.1:8000/api/packages")
```

### RAG knowledge bases

```
rag.query(knowledge_base="digitorn_concepts", query="<topic>", top_k=5)
rag.query(knowledge_base="digitorn_modules", query="<module action params>", top_k=5)
rag.query(knowledge_base="digitorn_examples", query="<use case>", top_k=3)
```

### The verification chain

1. User says "I want module X" → query /api/discovery/modules
2. X exists → query /api/discovery/modules/X for exact actions
3. X doesn't exist → tell user honestly, suggest alternatives
4. Write the module block in YAML
5. Compile via /api/discovery/compile to catch remaining errors

### When RAG vs Discovery API

- **RAG concepts** → HOW things work (patterns, best practices, architecture)
- **RAG modules** → detailed action documentation with examples
- **RAG examples** → starter templates to adapt
- **Discovery API** → WHAT exists right now (exact names, exact params)

Never rely on training data alone. The daemon is the source of truth.
