---

id: architecture
title: Architecture Overview
sidebar_position: 1
format: md
---



# LLMOS Bridge - Architecture Overview

LLMOS Bridge is a distributed daemon that bridges Large Language Models to the operating system through a structured protocol called IML (Instruction Markup Language). It receives declarative plans from an LLM agent, validates them against security policies, schedules actions through a dependency-aware DAG executor, dispatches them to typed modules across one or multiple nodes, and returns structured results back to the agent.

The system is designed for production use at scale: thousands of users, plugin-based extensibility, multi-layered security, multi-node distribution, and real-time observability.

---


## System Context

```
                          LLM Agent (Claude, GPT, Gemini, Ollama)
                                    |
                          LangChain SDK (langchain-llmos)
                                    |
                            HTTP / WebSocket / SSE
                                    |
            +---------------------------------------------------+
            |            LLMOS Bridge Orchestrator               |
            |            (FastAPI, localhost:40000)               |
            +---------------------------------------------------+
            |  Protocol  | Security  | Identity    | Dashboard   |
            |  Orchestration         | Smart Routing              |
            |  Modules   | Events (local + Redis)                |
            |  Perception | Triggers | Recording                  |
            +---------------------------------------------------+
                    |                          |
        +-----------+-----------+    +--------+---------+
        |   Local Execution     |    |   Remote Nodes   |
        |   (ModuleRegistry)    |    |   (HTTP bridge)  |
        +-----------------------+    +------------------+
                    |                          |
        Operating System / Hardware / Network / IoT
```

The daemon supports three deployment modes:

- **Standalone** (default): Single machine, all modules local. Binds to `127.0.0.1`, zero distributed overhead.
- **Orchestrator**: Coordinates local + remote nodes. Routes actions based on capabilities, load, and affinity.
- **Node**: Worker node that registers with an orchestrator and executes delegated actions.

---


## Layer Architecture

LLMOS Bridge is organized into ten distinct layers, each with clear responsibilities and boundaries. Dependencies flow downward only - upper layers depend on lower layers, never the reverse.

```
Layer 9 ─── Dashboard
             Next.js 14 admin UI, cluster visualization
             Real-time monitoring via React Query + SSE/WebSocket

Layer 8 ─── API / CLI
             FastAPI REST + WebSocket + SSE endpoints, Typer CLI
             Cluster API, Admin API, Identity API

Layer 7 ─── Identity / Applications
             Multi-tenant identity (apps, agents, sessions)
             Role-based access, token authentication

Layer 6 ─── Memory / Events
             SQLite key-value store, ChromaDB vector search
             EventBus (topic-routed): local, Redis Streams, Fanout

Layer 5 ─── Triggers / Recording
             Reactive automation (cron, filesystem, process, IoT)
             Workflow recording and replay

Layer 4 ─── Modules
             18 built-in modules, 235+ actions
             BaseModule ABC, ModuleManifest, ModuleRegistry, Hub

Layer 3 ─── Orchestration / Distribution
             DAG scheduler, PlanExecutor, state machine
             Smart routing, node-level fallback, load tracking
             NodeRegistry, RemoteNode, CapabilityRouter

Layer 2 ─── Security
             4 profiles, 6 decorators, PermissionGuard
             Scanner pipeline, IntentVerifier, AuditLogger

Layer 1 ─── Protocol
             IML v2 models, parser, validator, template engine
             Schema registry, repair, migration, compatibility

Layer 0 ─── Perception
             Screenshot capture, OCR, OmniParser vision
             Scene graph, caching, speculative prefetch
```

---


## Component Map

### Layer 1: Protocol

The protocol layer defines the canonical data shapes for all communication. Every plan submitted to LLMOS Bridge is an `IMLPlan` containing one or more `IMLAction` objects.

| Component | File | Responsibility |
|-----------|------|----------------|
| **IMLPlan** | `protocol/models.py` | Top-level plan model with validation |
| **IMLAction** | `protocol/models.py` | Single executable action with params, dependencies, error policy |
| **Parser** | `protocol/parser.py` | JSON to IMLPlan deserialization |
| **Validator** | `protocol/validator.py` | Structural and semantic validation |
| **Template Engine** | `protocol/template.py` | `{{result.X.Y}}`, `{{memory.key}}`, `{{env.VAR}}` resolution |
| **Schema Registry** | `protocol/schema.py` | JSON Schema generation for all actions |
| **IML Repair** | `protocol/repair.py` | Fuzzy JSON recovery for malformed LLM output |
| **Migration Pipeline** | `protocol/migration.py` | Protocol version migration (v1.0 to v2.0) |
| **Compatibility Checker** | `protocol/compat.py` | PEP-440 module version validation |
| **Typed Parameters** | `protocol/params/` | Pydantic models for every module action |

**Key design decision**: All 235+ action parameter sets are defined as Pydantic v2 models in `protocol/params/`. This provides compile-time type safety, automatic JSON Schema generation, and clear documentation for LLM agents.

### Layer 2: Security

Security operates at three distinct speeds:

```
Input arrives
    |
    v
[Layer 1.3] Scanner Pipeline ──── <1ms, heuristic patterns + optional ML
    |
    v
[Layer 1.5] IntentVerifier ────── ~200ms, LLM-based semantic analysis
    |
    v
[Layer 2.0] PermissionGuard ───── <1ms, profile + permission checks
    |
    v
[Decorators] Per-action enforcement ── runtime checks on decorated methods
```

| Component | File | Responsibility |
|-----------|------|----------------|
| **PermissionGuard** | `security/guard.py` | Single enforcement point for all permission checks |
| **Security Profiles** | `security/profiles.py` | 4 built-in profiles: `readonly`, `local_worker`, `power_user`, `unrestricted` |
| **Permission System** | `security/permissions.py` | 26+ permission constants, PermissionStore (SQLite) |
| **SecurityManager** | `security/managers.py` | Aggregates PermissionManager + RateLimiter + AuditLogger |
| **Decorators** | `security/decorators.py` | 6 composable decorators for action methods |
| **Scanner Pipeline** | `security/scanners/pipeline.py` | Orchestrates input scanners (heuristic + ML) |
| **HeuristicScanner** | `security/scanners/heuristic.py` | 35 regex patterns, 9 threat categories |
| **IntentVerifier** | `security/intent_verifier.py` | LLM-based semantic intent analysis |
| **OutputSanitizer** | `security/sanitizer.py` | Scrubs module output before LLM injection |
| **AuditLogger** | `security/audit.py` | All security events routed through EventBus |

**Key design decision**: Security decorators are metadata-only in Phase 1 - they set function attributes (`_required_permissions`, `_risk_level`, etc.) without wrapping. Runtime enforcement only activates when `enable_decorators=True` in configuration. This allows gradual adoption without breaking existing modules.

### Layer 3: Orchestration & Distribution

The orchestration layer drives the full lifecycle of a plan from submission to completion, and routes actions across local and remote nodes.

| Component | File | Responsibility |
|-----------|------|----------------|
| **PlanExecutor** | `orchestration/executor.py` | Main execution engine with smart routing |
| **DAG Scheduler** | `orchestration/dag.py` | networkx-based dependency graph scheduling |
| **State Machine** | `orchestration/state.py` | Plan and action status tracking |
| **Approval Gate** | `orchestration/approval.py` | Rich decisions: approve/reject/skip/modify/approve_always |
| **Rollback Engine** | `orchestration/rollback.py` | Compensating actions on failure |
| **Plan Groups** | `orchestration/plan_group.py` | Parallel plan execution |
| **ActionStream** | `orchestration/stream.py` | Real-time progress streaming from actions |
| **Streaming Decorators** | `orchestration/streaming_decorators.py` | `@streams_progress` marker |
| **NodeRegistry** | `orchestration/nodes.py` | Node routing table (local + remote) |
| **LocalNode** | `orchestration/nodes.py` | In-process execution via ModuleRegistry |
| **RemoteNode** | `orchestration/remote_node.py` | HTTP bridge to remote LLMOS daemons |
| **CapabilityRouter** | `orchestration/routing.py` | Filters nodes by module capability |
| **NodeSelector** | `orchestration/routing.py` | Strategy-based node selection (4 strategies) |
| **NodeQuarantine** | `orchestration/routing.py` | Excludes unreliable nodes after failures |
| **ActiveActionCounter** | `orchestration/routing.py` | Tracks in-flight actions per node |
| **NodeDiscoveryService** | `orchestration/discovery.py` | Static + mDNS node discovery |
| **NodeHealthMonitor** | `orchestration/node_health.py` | Background heartbeat + latency tracking |

**Smart routing pipeline**:

```
Action needs dispatch (target_node=None)
    |
    v
CapabilityRouter.find_capable_nodes(module_id)
    → Checks LocalNode._registry.is_available(module_id)
    → Checks RemoteNode._capabilities (populated by heartbeat)
    |
    v
NodeQuarantine.filter(candidates)
    → Excludes nodes with N+ consecutive failures
    → Auto-expires after configurable duration
    |
    v
NodeSelector.select(candidates, module_id, load_tracker)
    → local_first: prefer local, fallback to remote (default)
    → round_robin: distribute evenly across nodes
    → least_loaded: pick node with fewest active actions
    → affinity: use module_affinity map (e.g. vision → gpu-node)
    |
    v
Dispatch to selected node
    → LocalNode: module.execute(action, params) - in-process
    → RemoteNode: POST /plans to remote daemon - HTTP bridge
    |
    v
On NodeUnreachableError:
    → record_failure() → quarantine if threshold reached
    → Node-level fallback: retry on alternate capable node
      (up to max_node_retries, separate from module fallback)
```

**Execution pipeline** (for each plan):

```
1. Validate module version requirements (PEP-440)
2. Run Scanner Pipeline (Layer 1.3, <1ms)
3. Launch IntentVerifier (Layer 1.5, background)
4. Run PermissionGuard.check_plan() (concurrent with step 3)
5. Build DAG from action dependencies
6. Await IntentVerifier result
7. For each wave of ready actions:
   a. Skip cascaded failures
   b. Resolve {{templates}} in params
   c. Per-action security check
   d. Pause for approval if required
   e. Capture perception (before)
   f. Smart route to best node (capability → quarantine → strategy)
   g. Dispatch to node (local or remote)
   h. Track load (increment/decrement ActiveActionCounter)
   i. On failure: quarantine node, try alternate (node-level fallback)
   j. Capture perception (after)
   k. Write to memory (optional)
   l. Update state, emit events
8. Error handling: abort | continue | retry | rollback | skip
9. Final state: COMPLETED or FAILED
```

**Standalone zero-cost invariant**: When `node.mode="standalone"` (default), no routing components are instantiated. `resolve()` always returns LocalNode. The distributed layer is a strict no-op - zero memory, zero latency overhead.

### Layer 4: Modules

Modules are the executable units of LLMOS Bridge. Each module exposes a set of typed actions that the executor dispatches.

| Component | File | Responsibility |
|-----------|------|----------------|
| **BaseModule** | `modules/base.py` | Abstract base class with lifecycle hooks |
| **ModuleManifest** | `modules/manifest.py` | Machine-readable capability declaration |
| **ModuleRegistry** | `modules/registry.py` | Register, discover, load, unload modules |
| **PlatformGuard** | `modules/platform.py` | Platform compatibility enforcement |
| **ModuleLifecycle** | `modules/lifecycle.py` | State machine for module lifecycle |

**18 built-in modules** across 8 categories:

| Category | Modules |
|----------|---------|
| **System** | `filesystem`, `os_exec`, `module_manager`, `security`, `recording`, `triggers` |
| **Network** | `api_http` |
| **Automation** | `browser`, `gui`, `computer_control`, `window_tracker` |
| **Database** | `database`, `db_gateway` |
| **Document** | `excel`, `word`, `powerpoint` |
| **Perception** | `vision` (OmniParser) |
| **Hardware** | `iot` |

**Action dispatch convention**: The executor calls `module.execute("read_file", params)`, which routes to `module._action_read_file(params)` by naming convention. No registration files, no mapping tables.

### Layer 5: Triggers / Recording

| Component | File | Responsibility |
|-----------|------|----------------|
| **TriggerDaemon** | `triggers/daemon.py` | Event-driven plan firing |
| **TriggerScheduler** | `triggers/scheduler.py` | Cron and interval scheduling |
| **Watchers** | `triggers/watchers/` | Filesystem, process, resource, IoT, composite watchers |
| **WorkflowRecorder** | `recording/recorder.py` | Shadow recording of plan execution |
| **Replayer** | `recording/replayer.py` | Replay recorded workflows |

### Layer 6: Memory / Events

| Component | File | Responsibility |
|-----------|------|----------------|
| **KeyValueStore** | `memory/store.py` | SQLite-backed persistent KV storage |
| **VectorStore** | `memory/vector.py` | ChromaDB semantic search (optional) |
| **ContextBuilder** | `memory/context.py` | System prompt generation with module capabilities |
| **EventBus** | `events/bus.py` | Topic-routed event backbone |
| **NullEventBus** | `events/bus.py` | Zero-overhead default |
| **LogEventBus** | `events/bus.py` | NDJSON file logging |
| **FanoutEventBus** | `events/bus.py` | Broadcast to multiple backends |
| **RedisStreamsBus** | `events/redis_bus.py` | Cross-node events via Redis Streams |
| **EventRebroadcaster** | `events/router.py` | Receives remote events → local bus (no loops) |

**Two-bus architecture** (multi-node mode):

```text
Producer (local module/executor)
    |
    emit(topic, event)
    |
    v
FanoutEventBus
    |
    +--→ local_bus (EventBus)       → SSE, WebSocket, audit, triggers
    |
    +--→ redis_bus (RedisStreamsBus) → Redis XADD → other nodes
                                          |
                                          v
                            EventRebroadcaster (on other nodes)
                                → reads Redis XREAD
                                → emits to local_bus ONLY
                                → prevents infinite loops
```

**Event topics**:

| Topic | Purpose |
|-------|---------|
| `llmos.plans` | Plan lifecycle (submitted, running, completed, failed) |
| `llmos.actions` | Action execution (started, completed, failed, skipped) |
| `llmos.actions.progress` | Streaming progress from `@streams_progress` actions |
| `llmos.actions.results` | Final action results (completion notification) |
| `llmos.security` | Permission denials, approvals, sensitive action invocations |
| `llmos.errors` | Unhandled runtime errors |
| `llmos.perception` | Screenshot/OCR capture events |
| `llmos.permissions` | Permission grant/revoke |
| `llmos.modules` | Module load/unload/state changes |
| `llmos.nodes` | Node health (healthy, unhealthy, recovered), latency |

### Layer 7: Identity / Applications

| Component | File | Responsibility |
|-----------|------|----------------|
| **IdentityContext** | `identity/models.py` | Per-request identity (app_id, agent_id, session_id, role) |
| **IdentityResolver** | `identity/resolver.py` | Resolves identity from request headers |
| **IdentityStore** | `identity/store.py` | SQLite-backed application registry |
| **Applications API** | `api/routes/applications.py` | CRUD for registered applications |

Multi-tenancy is optional. When the identity system is disabled (default), all requests run as the default admin identity. When enabled, each request carries identity headers (`X-LLMOS-App`, `X-LLMOS-Agent`, `X-LLMOS-Session`) resolved into an `IdentityContext` used for per-app plan isolation and audit trails.

### Layer 8: API / CLI

| Component | File | Responsibility |
|-----------|------|----------------|
| **create_app()** | `api/server.py` | FastAPI application factory |
| **Plans API** | `api/routes/plans.py` | CRUD + approval + pending-approvals |
| **Modules API** | `api/routes/modules.py` | Module discovery and schema |
| **Cluster API** | `api/routes/cluster.py` | Cluster info, health, node management |
| **SSE Stream** | `api/routes/stream.py` | Server-Sent Events for real-time plan streaming |
| **WebSocket** | `api/routes/websocket.py` | Bidirectional event streaming |
| **Scanners API** | `api/routes/scanners.py` | Security scanner management |
| **Context API** | `api/routes/context.py` | System prompt for LLM agents |
| **Admin API** | `api/routes/admin_*.py` | Module management, hub, security, system config |
| **CLI** | `cli/main.py` | Typer-based command line interface |

**API endpoints**:

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check |
| POST | `/plans` | Submit IML plan |
| GET | `/plans` | List plans |
| GET | `/plans/{id}` | Get plan status and results |
| DELETE | `/plans/{id}` | Cancel plan |
| POST | `/plans/{id}/actions/{aid}/approve` | Rich approval decision |
| GET | `/plans/{id}/pending-approvals` | List pending approvals |
| GET | `/plans/{id}/stream` | SSE real-time event stream |
| POST | `/plan-groups` | Submit parallel plan group |
| GET | `/modules` | List modules |
| GET | `/modules/{id}` | Module details |
| GET | `/modules/{id}/actions/{action}/schema` | Action JSON Schema |
| GET | `/context` | System prompt for agents |
| WS | `/ws/stream` | WebSocket event stream |
| WS | `/ws/plans/{id}` | WebSocket plan-specific stream |
| GET | `/security/scanners` | List scanners |
| POST | `/security/scanners/scan` | Dry-run security scan |
| GET | `/cluster` | Cluster information |
| GET | `/cluster/health` | Cluster-wide health overview |
| GET | `/nodes` | List registered nodes |
| GET | `/nodes/{id}` | Node detail (latency, load, quarantine) |
| POST | `/nodes` | Register remote node |
| DELETE | `/nodes/{id}` | Unregister node |
| POST | `/nodes/{id}/heartbeat` | Trigger heartbeat |
| GET | `/applications` | List registered applications |

### Layer 9: Dashboard

The admin dashboard is a Next.js 14 application providing real-time visualization of the entire system.

| Component | File | Responsibility |
|-----------|------|----------------|
| **Overview** | `app/(dashboard)/overview/page.tsx` | System overview, key metrics |
| **Plans** | `app/(dashboard)/plans/page.tsx` | Plan list, detail, execution status |
| **Modules** | `app/(dashboard)/modules/page.tsx` | Module list, detail, lifecycle management |
| **Cluster** | `app/(dashboard)/cluster/page.tsx` | Node topology, health, latency, load |
| **Node Detail** | `app/(dashboard)/cluster/[nodeId]/page.tsx` | Per-node detail, heartbeat, unregister |
| **Security** | `app/(dashboard)/security/page.tsx` | Permissions, scanners, audit log |
| **Monitoring** | `app/(dashboard)/monitoring/page.tsx` | Real-time event monitoring |
| **Hub** | `app/(dashboard)/hub/page.tsx` | Module hub browser and install |

**Frontend stack**: Next.js 14 (App Router), React 18, Ant Design 5, TanStack React Query 5, Zustand 5, Tailwind CSS 3.4, TypeScript 5.6.

**Data fetching pattern**: All pages use React Query with configurable `refetchInterval` (5-15s) for near-real-time updates. Mutations invalidate related queries on success. WebSocket and SSE connections provide instant event streaming for plans and node health.

---


## Data Flow

### Plan Submission and Execution

```
SDK submits POST /plans with IMLPlan JSON
    |
    v
Parser deserializes → IMLPlan (Pydantic validation)
    |
    v
Scanner Pipeline screens input (<1ms)
    |                              |
    | PASS                         | REJECT → 403 Forbidden
    v
IntentVerifier analyzes semantics (background, ~200ms)
    |
PermissionGuard.check_plan() validates profile
    |                              |
    | PASS                         | DENY → PermissionDeniedError
    v
DAG Scheduler builds dependency graph (networkx)
    |
    v
Await IntentVerifier completion
    |                              |
    | PASS                         | BLOCK → SecurityError
    v
For each wave of independent actions:
    |
    +--→ Template resolution: {{result.X.Y}} → concrete values
    |
    +--→ Per-action security: profile + approval gate
    |
    +--→ Module.execute(action, params) dispatches to _action_*
    |        |
    |        +--→ Security decorators enforce permissions, rate limits, audit
    |        |
    |        +--→ ActionStream emits progress (if @streams_progress)
    |        |
    |        +--→ Result → OutputSanitizer → execution_results store
    |
    +--→ Perception capture (before/after screenshots + OCR)
    |
    +--→ Memory write (optional KV/vector store)
    |
    +--→ EventBus emit (action completed/failed)
    |
    v
Plan status: COMPLETED or FAILED
    |
    v
SDK polls GET /plans/{id} or streams SSE /plans/{id}/stream
```

### Event Flow

```text
Producer (Module/Executor/Security)
    |
    emit(topic, event)
    |
    v
EventBus (or FanoutEventBus in multi-node mode)
    |
    +--→ _stamp(topic, event)     Add _topic, _timestamp
    |
    +--→ _recent_events ring buffer (last 500)
    |
    +--→ _dispatch_to_listeners(topic, event)
    |        |
    |        +--→ SSE endpoint listener → client queue → HTTP stream
    |        +--→ WebSocket listener → broadcast to connected clients
    |        +--→ AuditLogger listener → NDJSON file
    |        +--→ Custom listeners (triggers, dashboard, etc.)
    |
    +--→ Backend-specific persistence
             |
             +--→ NullEventBus: discard (zero overhead)
             +--→ LogEventBus: append to NDJSON file
             +--→ FanoutEventBus: local_bus + redis_bus (cross-node)
             +--→ RedisStreamsBus: XADD/XREAD for multi-node events
```

---


## Configuration

LLMOS Bridge uses a layered configuration system:

```
Priority (highest to lowest):
1. Environment variables (LLMOS_*)
2. User config (~/.llmos/config.yaml)
3. System config (/etc/llmos-bridge/config.yaml)
4. Built-in defaults
```

**Key configuration sections**:

| Section | Purpose | Key Settings |
|---------|---------|-------------|
| `server` | HTTP daemon | host, port, rate_limit, max_result_size |
| `security` | IML security | profile, approval rules, sandbox_paths |
| `module` | Module loading | enabled, disabled, fallback chains |
| `memory` | State storage | state_db_path, vector_enabled |
| `perception` | Vision | enabled, ocr_enabled, format |
| `trigger` | Reactive automation | enabled, types, max_chain_depth |
| `intent_verifier` | LLM analysis | provider, model, timeout |
| `security_advanced` | OS permissions | enable_decorators, auto_grant_low_risk |
| `scanner_pipeline` | Input screening | enabled, thresholds, patterns |
| `recording` | Workflow capture | enabled, db_path |
| `hub` | Module distribution | enabled, registry_url |
| `isolation` | Sandboxing | enabled, venv management |
| `node` | Deployment mode | mode, node_id, cluster_name, location |
| `routing` | Smart routing | strategy, fallback, quarantine, affinity |
| `redis` | Distributed events | url, stream_prefix |
| `identity` | Multi-tenancy | enabled, default_role |

---


## Startup Sequence

When `create_app()` is called, the following initialization occurs in order:

```
1.  Load Settings (YAML + env vars)
2.  Create ModuleRegistry
3.  Register all enabled built-in modules
4.  Initialize PlanStateStore (SQLite)
5.  Initialize KeyValueStore (SQLite)
6.  Build PermissionGuard from profile
7.  Create EventBus (Null, Log, or Fanout)
8.  Create AuditLogger (delegates to EventBus)
9.  Create OutputSanitizer
10. Initialize PermissionStore (SQLite)
11. Create PermissionManager
12. Create ActionRateLimiter
13. Create IntentVerifier (LLM-based, optional)
14. Assemble SecurityManager
15. Create Scanner Pipeline (heuristic + optional ML)
16. Inject SecurityManager into all modules (if decorators enabled)
17. Register SecurityModule
18. Audit undecorated action methods (warnings)
19. Initialize ServiceBus (inter-module communication)
20. Initialize ModuleLifecycleManager
21. Start all modules (on_start lifecycle)
22. Register ModuleManagerModule (if enabled)
23. Initialize Module Hub (if enabled)
24. Create NodeRegistry + LocalNode
25. If mode != standalone:
    a. Create NodeDiscoveryService (static + mDNS)
    b. Create NodeHealthMonitor (background heartbeat)
    c. Build routing components (CapabilityRouter, NodeSelector, NodeQuarantine, ActiveActionCounter)
    d. Create RedisStreamsBus + FanoutEventBus + EventRebroadcaster
26. Create PlanExecutor with all dependencies (including routing_config)
27. Initialize Identity system (if enabled): IdentityStore, IdentityResolver
28. Mount all API routes (plans, modules, cluster, admin, applications)
29. Apply middleware stack (CORS, rate limit, request ID, logging)
30. Start NodeHealthMonitor + EventRebroadcaster (if multi-node)
```

---


## Design Principles

### Convention over Configuration
Modules use `_action_<name>` naming convention for action dispatch. No XML files, no registration decorators, no mapping tables. If a method exists named `_action_read_file`, the action `read_file` is automatically routable.

### Dependency Injection
Every component is wired through constructor injection in `create_app()`. Tests replace any component with mocks. No global singletons, no service locators.

### Pluggable Architecture
Swap any subsystem by injecting a different implementation:

- `EventBus`: NullEventBus (default) to LogEventBus to RedisStreamsBus to FanoutEventBus
- `IntentVerifier`: Null (disabled) to OpenAI to Anthropic to Ollama to custom
- `VisionBackend`: OmniParser to Ultra (advanced SoM) to custom
- `SecurityPipeline`: Heuristic-only to + LLMGuard to + PromptGuard
- `NodeSelector`: local_first, round_robin, least_loaded, affinity (or custom strategy)

### Secure by Default
- Default profile is `local_worker` (read + write, no delete, no kill)
- Sandbox paths enforced with symlink resolution
- Commands always `list[]`, never `shell=True`
- Output sanitizer scrubs all module output before LLM sees it
- Scanner pipeline screens all input in under 1ms

### Temporal Decoupling
The EventBus allows producers and consumers to operate independently. The executor emits events; dashboards, audit loggers, rollback engines, and SSE clients consume them on their own schedule.

### Cascade Semantics
When an action fails with `on_error=ABORT`, all transitive descendants in the DAG are immediately marked `SKIPPED`. No orphaned `PENDING` actions that can never run.

---


## SDK Integration

The LangChain SDK (`langchain-llmos`) provides the agent-side integration:

```
LLM (Claude/GPT/Gemini)
    |
    v
ReactivePlanLoop (agent loop)
    |
    +--→ Provider (Anthropic/OpenAI/Gemini/Ollama)
    |
    +--→ LLMOSClient (HTTP client for daemon)
    |        |
    |        +--→ POST /plans (submit)
    |        +--→ GET /plans/{id} (poll)
    |        +--→ GET /plans/{id}/stream (SSE)
    |
    +--→ Safeguards (pre-submission validation)
    |
    +--→ Observation builder (structured feedback to LLM)
```

The SDK supports multiple LLM providers through a pluggable provider registry, with built-in support for Anthropic, OpenAI, Google Gemini, and Ollama.

---


## Production Considerations

### Performance
- Scanner pipeline: <1ms per input (heuristic patterns)
- Action dispatch: <1ms overhead (method lookup)
- OmniParser vision: ~4s per screen parse (GPU), ~12s (CPU)
- Speculative prefetch: background parse after each action saves ~4s/iteration
- Perception cache: MD5-based LRU with configurable TTL

### Scalability
- DAG scheduler handles parallel action waves (networkx)
- Plan groups allow concurrent independent plans
- Module concurrency configurable per-module (ResourceConfig)
- EventBus scales with backend (file to Redis Streams)
- Multi-node distribution: actions routed to capable remote nodes
- Smart routing: load balancing, capability filtering, node quarantine
- Standalone zero-cost: no routing overhead in single-machine mode

### Observability
- Structured logging (structlog)
- Request ID tracing (middleware)
- Audit trail for all security events
- Real-time SSE and WebSocket streaming
- Module health checks and metrics

### Extensibility
- Community modules via `llmos-module-template`
- Hub integration for discovery and installation
- Custom security scanners (subclass `InputScanner`)
- Custom event bus backends (subclass `EventBus`)
- Custom LLM providers for intent verification
