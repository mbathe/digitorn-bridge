---
id: grammar
title: YAML grammar
---

# YAML grammar (v1)

The formal grammar of the v1 Digitorn YAML language. This page is
the **structural** companion to [App Configuration](02-app-config.md);
it lists every block, every accepted shape, and every type, in
canonical form. For per-field semantics and worked examples, follow
the per-block links.

## Top level

```ebnf
AppDefinition  ::= {
                     [ schema_version: 1 | 2 ],
                     app: AppMeta,
                     [ runtime: RuntimeBlock ],
                     [ agents: [AgentDefinition, ...] ],
                     [ tools: ToolsBlock ],
                     [ security: SecurityBlock ],
                     [ ui: UIBlock ],
                     [ dev: DevBlock ],
                     [ flow: FlowConfig ]
                   }
```

`extra: forbid` on the root: any unknown top-level key is a
compile error.

`app:` is the only required block. The other 7 default to empty
or to a default-instance model.

## `app:` AppMeta

```ebnf
AppMeta  ::= {
               app_id: string,                        # required
               name: string,                          # required
               version: string,                       # default "1.0"
               schema_version: string,                # default "1"
               description: string,                   # default ""
               author: string,                        # default ""
               tags: [string, ...],                   # default []
               icon: string,                          # default ""
               color: string,                         # default ""
               category: string,                      # default "general"
               quick_prompts: [QuickPrompt, ...]      # default []
             }
QuickPrompt  ::= { label: string, message: string, [icon: string] }
```

See [App Configuration -> app](02-app-config.md#app--identity) for
field semantics.

## `runtime:` RuntimeBlock

```ebnf
RuntimeBlock ::= {
                   [ mode: ("one_shot" | "conversation"
                            | "background" | "pipeline") ],   # default "conversation"
                   [ entry_agent: string ],
                   [ max_turns: int >= 1 ],                   # default 50
                   [ timeout: float > 0 ],                    # default 300.0
                   [ session_mode: ("mono" | "multi") ],      # default "mono"
                   [ max_sessions_per_user: int >= 0 ],       # default 10 (0 = unlimited)
                   [ max_concurrent_activations: int >= 1 ],  # default 20
                   [ workdir: string ],                       # default ""
                   [ workdir_mode: ("none" | "required"
                                   | "fixed" | "auto") ],     # default "auto"
                   [ project_memory: string ],                # default "auto"
                   [ direct_modules: [string, ...] ],         # default []
                   [ tool_injection: ("direct" | "compact_direct"
                                     | "discovery" | null) ], # default null (auto)
                   [ context: ContextConfig ],                # default-instance
                   [ hooks: [HookConfig, ...] ],              # default []
                   [ watchers: bool ],                        # default false
                   [ scheduler: bool ],                       # default false (requires watchers)
                   [ default_channel: string ],               # default "llm_notification"
                   [ middleware: [dict, ...] ],               # default []
                   [ pipeline: [PipelineStep, ...] ],         # mode=pipeline only
                   [ triggers: [TriggerConfig, ...] ],
                   [ input: InputConfig ],                    # mode=one_shot only
                   [ output: OutputConfig ],                  # mode=one_shot only
                   [ payload_schema: PayloadSchemaConfig ]    # mode=background only
                 }
ContextConfig ::= {
                    [ max_tokens: int [0, 2_000_000] ],       # default 0 (auto)
                    [ output_reserved: int ],                  # default 4096
                    [ strategy: ("truncate" | "summarize") ],  # default "summarize"
                    [ keep_recent: int ],                      # default 10
                    [ compression_trigger: float [0, 1] ],     # default 0.75
                    [ summary_max_tokens: int ],               # default 1024
                    [ auto_compact: bool ],                    # default true
                    [ summary_brain: AgentBrain ]              # default null
                  }
```

See [App Configuration -> runtime](02-app-config.md#runtime--lifecycle-and-execution-policy).

## `agents:` AgentDefinition list

```ebnf
AgentDefinition ::= {
                      id: string,                              # required
                      [ role: string ],                        # default "worker"
                      brain: AgentBrain,                       # required
                      [ system_prompt: string ],               # default ""
                      [ plan_first: bool ],                    # default true
                      [ specialty: string ],                   # default ""
                      [ delegate_to: [string, ...] ],          # default []
                      [ skills: string ],                      # default ""  (path to .md)
                      [ capabilities: [string, ...] ],         # default []  (skill names)
                      [ modules: [(string | dict), ...] ],     # default []  (per-agent restriction)
                      [ pool: AgentPoolConfig ],
                      [ coordination: CoordinationBlock ],
                      [ instructions: InstructionsBlock ],
                      [ hooks: [HookConfig, ...] ]
                    }
AgentBrain ::= {
                 [ provider_id: string ],                       # reference mode
                 [ provider: string ],                          # validated set, see Agents
                 [ model: string ],
                 [ backend: ("openai_compat" | "anthropic"
                            | "github_copilot") ],              # default "openai_compat"
                 [ config: dict ],
                 [ credential: (string | dict) ],
                 [ temperature: float ],
                 [ max_tokens: int ],
                 [ top_p: float ],
                 [ timeout: float ],
                 [ native_tool_use: bool ],
                 [ context: ContextConfig ],
                 [ fallback: AgentBrain ],                      # recursive
                 [ vision: bool ],
                 [ image_generation: bool ],                    # default false
                 [ image_detail: ("auto" | "low" | "high") ],   # default "auto"
                 [ max_images_per_turn: int [0, 100] ]          # default 5
               }
```

See [Agents](03-agents.md) for the validated provider hints,
backend selection, and the credential vs inline-config tradeoff.

## `tools:` ToolsBlock

```ebnf
ToolsBlock ::= {
                 [ modules: { string -> ModuleBlock } ],
                 [ capabilities: CapabilitiesConfig ],
                 [ channels: { string -> ChannelInstanceConfig } ]
               }
ModuleBlock ::= {
                  [ config: dict ],                            # default {}
                  [ setup: [SetupStep, ...] ],                 # default []
                  [ constraints: dict ],                       # default {}
                  [ middleware: [dict, ...] ],                 # default []
                  [ credential: (string | dict) ]              # default null
                }
SetupStep ::= { action: string, [ params: dict ] }
CapabilitiesConfig ::= {
                         [ default_policy: ("auto" | "approve" | "block") ],   # default "approve"
                         [ max_risk_level: ("low" | "medium" | "high") ],      # default "medium"
                         [ grant: [CapabilityGrant, ...] ],
                         [ approve: [CapabilityGrant, ...] ],
                         [ deny: [CapabilityGrant, ...] ],
                         [ approval_timeout: int [30, 3600] ],                 # default 300
                         [ hidden_modules: [string, ...] ],
                         [ hidden_actions: [CapabilityGrant, ...] ]
                       }
CapabilityGrant ::= { module: string, [ actions: [string, ...] ], [ reason: string ] }
```

See [App Configuration -> tools](02-app-config.md#tools--modules-capabilities-channels).

## `security:` SecurityBlock

```ebnf
SecurityBlock ::= {
                    [ behavior: BehaviorConfig ],              # see Behavior Engine
                    [ sandbox: SandboxConfig ],                # see OS Sandbox
                    [ credentials_schema: CredentialsSchemaConfig ]
                  }
```

Each sub-block has its own grammar covered in
[Behavior Engine](43-behavior.md), [OS Sandbox](35-sandbox.md),
and [Credentials](../reference/runtime/credentials.md).

## `ui:` UIBlock

`extra: forbid`. Two layers (legacy + chat layout/behaviour). See
[App Configuration -> ui](02-app-config.md#ui--display-layer-daemon-never-reads)
for the full list of sub-blocks (theme, features, workspace,
widgets, slash_commands, quick_prompts, greeting, layout, density,
thinking, tool_calls, composer, visual, activity).

## `dev:` DevBlock

```ebnf
DevBlock ::= {
               [ skills: [SkillEntry, ...] ],                   # default []
               [ variables: { string -> string } ],             # default {}
               [ include: IncludeBlock ]
             }
SkillEntry ::= { command: string, [ description: string ], path: string }
IncludeBlock ::= {
                   [ agents: (string | [string, ...]) ],        # dir path OR list of YAMLs
                   [ hooks:  (string | [string, ...]) ]
                 }
```

See [Skills System](21-skills.md) and [Bundle namespaces](38-bundle-namespaces.md).

## `flow:` FlowConfig

```ebnf
FlowConfig ::= {
                 id: string,                                    # required
                 entry: string,                                 # required
                 [ description: string ],
                 [ max_iterations: int >= 0 ],                  # required > 0 if cyclic
                 nodes: [FlowNode, ...]                         # required, len >= 1
               }
FlowNode  ::= AgentNode | ToolNode | ParallelNode
            | ApprovalNode | DecisionNode | TerminalNode
```

See [Flows](07-flows.md) for the per-node-type fields and the
route expression syntax.

## Templates

The compiler resolves `{{...}}` templates recursively across every
string in the YAML. Six namespaces, plus a fallback operator. See
[App Configuration -> Variables](02-app-config.md#variables).

```ebnf
Template     ::= "{{" Expr "}}"
Expr         ::= DotPath [ Fallback ]
Fallback     ::= "??" String
DotPath      ::= ( UserVar | EnvVar | SecretVar | SysVar
                 | AppVar  | BundleNs | RuntimeVar )
UserVar      ::= identifier                  # dev.variables
EnvVar       ::= "env."     identifier
SecretVar    ::= "secret."  identifier
SysVar       ::= "sys."     identifier
AppVar       ::= "app."     identifier
BundleNs     ::= ( "prompt." | "skill." | "behavior." | "asset." ) identifier
RuntimeVar   ::= identifier "." identifier { "." identifier }
```

Six namespaces are resolved at compile time
(`UserVar`, `EnvVar`, `SecretVar`, `SysVar`, `AppVar`, `BundleNs`).
`RuntimeVar` is left verbatim and resolved by the consuming module
at run time (typically the channels module's `prepare` pipeline).
