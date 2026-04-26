---
id: yaml-schema-index
title: "YAML Schema Reference — Index"
type: schema-index
keywords: [schema, yaml, reference, index, app-definition]
---

# YAML Schema Reference

Every block in a Digitorn `app.yaml` is derived from the `AppDefinition` Pydantic model. This index lists every model in that tree with a one-line summary. Click through for the full field list of each block.

## Top-level blocks

| Block | Required | Type | Summary |
|-------|:--------:|------|---------|
| `app` | ✓ | [AppMeta](AppMeta.md) | Application identity |
| `variables` |  | dict[str, str] | Template variables available as {{name}} in params and constraints |
| `modules` |  | dict[str, [ModuleBlock](ModuleBlock.md)] | Per-module configuration |
| `agents` |  | list[[AgentDefinition](AgentDefinition.md)] | Agent definitions |
| `execution` |  | [ExecutionConfig](ExecutionConfig.md) | Execution mode and runtime parameters |
| `capabilities` |  | [CapabilitiesConfig](CapabilitiesConfig.md) \| null | Application security capabilities (grant/deny) |
| `behavior` |  | [BehaviorConfig](BehaviorConfig.md) \| null | Behavioral enforcement rules |
| `channels` |  | dict[str, [ChannelInstanceConfig](ChannelInstanceConfig.md)] | Named output channel instances |
| `workspace` |  | [WorkspaceBlock](WorkspaceBlock.md) \| null | Workspace config — tells the client this app uses a virtual file workspace streamed via Socket |
| `preview` |  | [PreviewConfig](PreviewConfig.md) \| null | Optional dev-server preview for apps shipping a web UI (Vite, Next, etc |
| `widgets` |  | [WidgetsConfig](WidgetsConfig.md) \| null | Declarative UI widgets rendered by the Flutter client |
| `middleware` |  | list[dict[str, any]] | App-level middleware pipeline |
| `skills` |  | list[dict[str, str]] | App-level skills — reusable command files ( |
| `pipeline` |  | list[[PipelineStep](PipelineStep.md)] | Pipeline of apps to execute in sequence (one_shot mode only) |
| `features` |  | dict[str, bool] | UI feature toggles consumed by the Flutter client |
| `theme` |  | dict[str, str] | Client theme override map |
| `slash_commands` |  | list[dict[str, str]] | Custom /slash palette entries rendered by the client |

## All models in the schema tree

| Model | Summary |
|-------|---------|
| [AgentBrain](AgentBrain.md) | LLM brain configuration for an agent. |
| [AgentDefinition](AgentDefinition.md) | Definition of a single agent in the app YAML. |
| [AppDefinition](AppDefinition.md) | Root model — direct parse target for an app YAML file. |
| [AppMeta](AppMeta.md) | Top-level application identity. |
| [BehaviorConfig](BehaviorConfig.md) | Behavioral enforcement rules — actively monitored at runtime. |
| [BehaviorCustomRule](BehaviorCustomRule.md) | Legacy custom rule format. Kept for backward compatibility. |
| [BehaviorRuleDefinition](BehaviorRuleDefinition.md) | A fully declarative behavioral rule — works for ANY action. |
| [CapabilitiesConfig](CapabilitiesConfig.md) | Application-level security capabilities. |
| [CapabilityGrant](CapabilityGrant.md) | An explicit grant or deny for module actions. |
| [ChannelInstanceConfig](ChannelInstanceConfig.md) | Configuration for a named output channel instance. |
| [ChatSideWidget](ChatSideWidget.md) | Z2 — companion side panel rendered next to the chat. |
| [ClassifierConfig](ClassifierConfig.md) | Configuration for the semantic classifier LLM. |
| [ClassifierContextConfig](ClassifierContextConfig.md) | What context the classifier receives about the agent's state. |
| [ContextConfig](ContextConfig.md) | Context management configuration for the agent loop. |
| [CredentialFieldConfig](CredentialFieldConfig.md) | One field inside a credential provider (e.g. ``api_key``, ``bot_token``). |
| [CredentialProviderConfig](CredentialProviderConfig.md) | One provider entry inside ``credentials_schema.providers``. |
| [CredentialsSchemaConfig](CredentialsSchemaConfig.md) | Declarative credentials schema for a Digitorn app. |
| [ExecutionConfig](ExecutionConfig.md) | Execution mode and runtime parameters. |
| [HookActionConfig](HookActionConfig.md) | Action configuration for an internal hook. |
| [HookConditionConfig](HookConditionConfig.md) | Condition configuration for an internal hook. |
| [HookConfig](HookConfig.md) | An internal hook: condition → action, evaluated during the agent loop. |
| [InlineWidget](InlineWidget.md) | Named inline widget — referenceable by ``ref:`` from agent SSE. |
| [InputConfig](InputConfig.md) | Input contract for one_shot mode. |
| [ModalWidget](ModalWidget.md) | Z4 — modal pushed by ``action: open_modal``. |
| [ModuleBlock](ModuleBlock.md) | Configuration block for a single module in the app YAML. |
| [OutputConfig](OutputConfig.md) | Output contract for one_shot mode. |
| [PayloadFieldConfig](PayloadFieldConfig.md) | One declared field on a background app's session payload metadata. |
| [PayloadFileRuleConfig](PayloadFileRuleConfig.md) | Constraint on the files a user can attach to a session payload. |
| [PayloadSchemaConfig](PayloadSchemaConfig.md) | Declarative description of the user-pre-filled session payload. |
| [PipelineStep](PipelineStep.md) | A single step in a pipeline: call a deployed app with an input. |
| [PreviewConfig](PreviewConfig.md) | Dev server spawned on app deploy and proxied through the daemon. |
| [SandboxConfig](SandboxConfig.md) | OS-level sandbox configuration for per-session isolation. |
| [SetupStep](SetupStep.md) | A single action call to execute during app bootstrap. |
| [StateTrackingConfig](StateTrackingConfig.md) | Configure what the session state tracks — fully declarative. |
| [StateTrackingCounterConfig](StateTrackingCounterConfig.md) | Configure a named counter. |
| [StateTrackingFlagConfig](StateTrackingFlagConfig.md) | Configure a named boolean flag. |
| [StateTrackingSetConfig](StateTrackingSetConfig.md) | Configure a named set that tracks targets per tool. |
| [TriggerConfig](TriggerConfig.md) | A trigger for background mode. |
| [UserResolverConfig](UserResolverConfig.md) | Configuration for auto-resolving user-specific delivery targets. |
| [WidgetNode](WidgetNode.md) | Recursive widget tree node — every primitive shares this base. |
| [WidgetsConfig](WidgetsConfig.md) | Top-level ``widgets:`` block in app.yaml. |
| [WorkspaceBlock](WorkspaceBlock.md) | Top-level ``workspace:`` block in app.yaml. |
| [WorkspaceTabWidget](WorkspaceTabWidget.md) | Z3 — one tab in the workspace 'Widgets' container. |
