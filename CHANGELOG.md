# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0] - 2026-03-18

First production release. Declarative YAML framework for AI agent applications.

### Core Platform

- **App lifecycle**: compile, bootstrap, deploy, run, undeploy, reload
- **Agent loop**: multi-turn tool calling with streaming, context compaction, and emergency recovery
- **YAML compiler**: full app schema with validation, variable interpolation, macros, expressions
- **Multi-agent**: agent spawning, delegation, parallel execution
- **Execution modes**: one-shot, conversation, background (daemon-managed)
- **Session management**: per-user/per-app sessions with TTL, persistence, fork, resume
- **Approval queue**: async approval workflow for sensitive tool calls (CLI + API + SSE)
- **Scheduler**: cron-based job scheduling with atomic persistence and at-least-once delivery
- **Middleware pipeline**: app-level middleware (before/after hooks, short-circuit, content filtering)

### Modules (14)

- **filesystem** - read, write, find, grep, ls (with path constraints)
- **database** - SQLite, PostgreSQL, MySQL (parameterized queries, schema introspection)
- **shell** - sandboxed command execution with allowlist/blocklist
- **git** - status, diff, commit, branch, log (workspace-scoped)
- **http** - outbound HTTP with egress policy and allowed hosts
- **web** - web search and page fetch
- **mcp** - Model Context Protocol client (stdio, SSE, streamable-HTTP transports)
- **memory** - working memory with goals, todos, key facts, session snapshots
- **index** - semantic workspace indexing (FastEmbed + Qdrant embedded)
- **context_builder** - meta-tool discovery, adaptive injection (direct vs discovery mode)
- **llm_provider** - multi-provider support (OpenAI, Anthropic, DeepSeek, Ollama, any OpenAI-compatible)
- **notebook** - Jupyter notebook read/edit/execute
- **hello** - demo/test module
- **agent_spawn** - dynamic agent creation and delegation

### Tool Discovery

- Meta-tool system: search_tools, get_tool, execute_tool, list_categories, browse_category
- Semantic search via FastEmbed (paraphrase-multilingual-MiniLM-L12-v2, 384 dims, ~50 languages)
- Hybrid scoring: semantic (x10) + keyword boost
- Adaptive injection: direct mode (small toolsets) vs discovery mode (large toolsets)
- Module-declared aliases (FR/EN) indexed at build time

### Security

- **Auth**: JWT (access + refresh tokens) and API key authentication, enabled by default
- **Rate limiting**: sliding window per-app/per-user, atomic counters (DiskCache/Redis)
- **Input validation**: regex on all app_id/session_id params across all API routes
- **SQL injection prevention**: parameterized queries, strict identifier validation
- **Path traversal protection**: symlink checks, path resolution, constraint validation
- **Secret redaction**: structlog processor masks API keys, tokens, JWTs in all log output
- **Security profiles**: grant/approve/deny capabilities per module per app
- **MCP risk inference**: auto-classify MCP tools as low/medium/high risk
- **HTTP egress policy**: allowed hosts enforcement with security profiles

### Infrastructure

- **KV backends**: DiskCache (zero-config, SQLite-backed) and Redis (multi-host production)
- **Resilient Redis**: circuit breaker with automatic DiskCache fallback
- **Multi-worker**: uvicorn `--workers N` (1-16), shared backends across workers
- **Database migrations**: Alembic configured, auto-run on daemon startup
- **Graceful shutdown**: drains active HTTP requests and agent turns (30s timeout each)
- **Structured logging**: structlog with context vars (request_id, session_id, app_id)

### Observability

- **Health endpoints**: `/health`, `/healthz`, `/readyz` (liveness + readiness)
- **Metrics**: in-process counters, gauges, histograms with cardinality limit (5000)
- **Prometheus**: `/api/metrics/prometheus` text exposition format
- **JSON metrics**: `/api/metrics` snapshot endpoint
- **Tracing**: in-process span hierarchy with context propagation

### Resilience

- **LLM circuit breaker**: fast-fail after 5 consecutive provider failures (30s recovery)
- **MCP reconnect backoff**: exponential backoff (1s-30s) with up to 5 retries
- **Context overflow recovery**: auto-detect + emergency compaction + context reminder re-injection
- **Tool result truncation**: smart JSON array truncation capped at 50% of context window
- **Per-session locking**: asyncio.Lock per session prevents concurrent modification races
- **Session TTL hard check**: enforced on every get(), independent of backend soft TTL
- **Metrics cardinality limit**: prevents OOM from unbounded label combinations
- **MCP health task cleanup**: asyncio.wait_for prevents task leaks on shutdown
- **Global exception handler**: structured JSON envelope for all unhandled errors (500, 422, HTTP)

### API

- **80+ REST endpoints** across apps, sessions, auth, config, modules, MCP, security
- **SSE streaming**: `/chat/stream` with token-by-token output and tool call events
- **Socket.IO**: real-time event bus for session subscriptions
- **OpenAPI**: auto-generated Swagger (`/docs`) and ReDoc (`/redoc`)
- **Per-app quota API**: GET/PUT/DELETE `/api/apps/{app_id}/quota`
- **MCP management**: connect, disconnect, reconnect, health, OAuth flow

### CLI

- `digitorn run <app.yaml> [message]` - standalone execution
- `digitorn start/stop/status` - daemon lifecycle
- `digitorn app deploy/undeploy/list/validate/schema` - app management
- `digitorn doctor` - system diagnostics

### Documentation

- 29 app-language guides (getting started, config, agents, tools, security, API, examples...)
- 14 module reference docs with actions, parameters, constraints, examples
- Professional README with quick start, feature matrix, architecture overview

### Testing

- **2573 tests** - unit, integration, e2e, security, module tests
- All critical paths covered: auth, sessions, rate limiting, MCP, agent loop, compiler, database
- Security test suite: injection, path traversal, secret masking, middleware pipeline
