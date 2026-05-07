---
id: runtime-index
title: Runtime reference
---

# Runtime reference

Cross-cutting subsystems that touch every app at runtime, but
aren't a single module. This is where you find the behavior of
features that are configured in YAML (`runtime.hooks`,
`runtime.middleware`, `security.credentials_schema`) but executed
by the daemon's runtime layer.

| Page | What it covers |
|------|----------------|
| [Configuration](configuration.md) | Daemon `Settings`: server, KV, database, CORS, sandbox, KMS. Loaded from `/etc/digitorn/config.yaml` < `~/.digitorn/config.yaml` < env vars. |
| [Credentials](credentials.md) | The encrypted vault: scopes (`system_wide`, `per_app_shared`, `per_user`, `per_app_per_user`), 19 handler types, OAuth refresh loop, audit log. |
| [Hooks](hooks.md) | The 15 events, 14 conditions, 13 actions; templating with `{{tool.*}}`; piping tool output to other tools or modules. |
| [Middleware](middleware.md) | The middleware pipeline: built-ins (audit, retry, redact, rate_limit, content_filter) and how to author your own. |
| [Multimodal](multimodal.md) | Image input/output handling, image aging, provider conversion. |
| [Voice](voice.md) | Voice transcription pipeline. |
| [Tool chaining](tool-chaining.md) | The `pipe` hook action - route tool output into the next tool. |

## Where these come together

```mermaid
flowchart TD
    llm["LLM tool call"] --> h1["hook · pre_tool_use"]
    h1 --> caps["capabilities gate<br/><i>grant / deny / approve</i>"]
    caps --> b1["behavior · pre_tool_check<br/><i>block / warn / remind</i>"]
    b1 --> mw1["middleware · before"]
    mw1 --> action["module · @action method"]
    action --> mw2["middleware · after"]
    mw2 --> b2["behavior · post_tool_check"]
    b2 --> h2["hook · post_tool_use"]
    h2 --> result["tool result"]
    result --> llm
    classDef gate fill:#0d0d0d,stroke:#A78BFA,stroke-width:1.4px,color:#E6E6E6;
    classDef core fill:#1a1a1a,stroke:#3B82F6,stroke-width:1.6px,color:#E6E6E6;
    class h1,h2,caps,b1,b2,mw1,mw2 gate;
    class llm,action,result core;
```

Every tool call passes through this pipeline, in this order.
Hooks see the result; middleware can transform params and result;
behavior can block before the call or remind after.
