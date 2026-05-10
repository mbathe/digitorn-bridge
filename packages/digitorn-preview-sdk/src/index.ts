// Provider + context
export { DigiPreview, useDigiPreview, readSession } from "./DigiPreview.js";
export type { DigiPreviewProps } from "./DigiPreview.js";

// Host ↔ iframe protocol (postMessage + URL-bootstrapped theme).
// Apps that want the bare-bones imperative API import these directly;
// most use the hooks below.
export {
  readHostTheme,
  sendToHost,
  onHostMessage,
  requestOpenFile,
  requestFocusLine,
  requestToast,
  notifyReady,
  DEFAULT_HOST_THEME,
} from "./host.js";
export type {
  HostTheme,
  ThemeMode,
  ClientBoundMessage,
  HostBoundMessage,
} from "./host.js";

// Host hooks - thin React wrappers over the protocol above.
export { useHostTheme, useHostMessage } from "./hooks/host.js";

// All hooks
export {
  useConnection,
  useResources,
  useResource,
  usePreviewState,
  useFiles,
  useFile,
  useFileJson,
  useFilesByPrefix,
  useFilesJsonByPrefix,
  useFileStats,
  useNodes,
  useEdges,
  useAgentStatus,
  useAgentStream,
  useToolCalls,
  useApprovalRequest,
  useEvents,
} from "./hooks/index.js";
export type { FileStats } from "./hooks/index.js";

// Workspace checkpoint / fork + file-level mutations + lifecycle
export {
  useWorkspaceSnapshot,
  useWorkspacePersistence,
  useWorkspaceFiles,
  useSessionMeta,
  useSessionLifecycle,
} from "./hooks/workspace.js";
export type {
  UseWorkspaceSnapshotApi,
  UseWorkspaceFilesApi,
  SessionMeta,
  SessionLifecycleHandlers,
} from "./hooks/workspace.js";

// Approvals - imperative resolve + live pending list
export { useApprovals } from "./hooks/approvals.js";
export type { UseApprovalsApi } from "./hooks/approvals.js";

// Chat - send / abort / retry + live transcript over Socket.IO
export { useChat } from "./hooks/chat.js";
export type {
  UseChatApi,
  ChatSendOptions,
  ChatSendResult,
  ChatAbortOptions,
  ChatImageInput,
} from "./hooks/chat.js";

// Structured streaming - typed content blocks (thinking / text / tool_use / citation)
export { useStream } from "./hooks/stream.js";
export type { UseStreamApi } from "./hooks/stream.js";

// Code state (VS Code-like editor hooks)
export {
  useCodeState,
  useFileContent,
  useFileActions,
  useCodeStats,
  useDiagnostics,
  useDiagnosticsStats,
  useLspRequest,
} from "./hooks/code_state.js";
export type {
  CodeStateEntry,
  FileContentState,
  UseFileActionsApi,
  CodeStats,
  DiagnosticsStats,
  LspRpcResult,
  LspCancelResult,
  UseLspRequestApi,
} from "./hooks/code_state.js";

// Templates — declarative starter UX. Apps export a Template[] and
// the SDK provides the gallery card grid, the detail modal, and a
// live in-browser preview iframe powered by esbuild-wasm. The
// daemon is unaware of templates; everything lives in the app
// bundle.
export {
  TemplateGallery,
  TemplateModal,
  TemplatePreview,
  TemplateSandbox,
  TemplateThumbnail,
  bundleFiles,
  ensureEsbuildReady,
  TEMPLATE_IFRAME_HTML,
  useTemplates,
} from "./templates/index.js";
export type {
  Template,
  TemplateSeed,
  TemplateBundleStatus,
  TemplateBundleError,
  TemplateGalleryProps,
  GalleryTokens,
  TemplateModalProps,
  ModalTokens,
  TemplatePreviewProps,
  TemplateSandboxProps,
  TemplateThumbnailProps,
  BundleResult,
  BundleFailure,
  BundleOutcome,
  UseTemplatesApi,
} from "./templates/index.js";

// Types
export type {
  SessionInfo,
  NodeStatus,
  FileStatus,
  FileValidation,
  GitStatus,
  Diagnostic,
  DiagnosticsEntry,
  DiagnosticRange,
  DiagnosticSeverity,
  WorkspaceFile,
  WorkspaceFileMeta,
  PreviewNode,
  PreviewEdge,
  PreviewEvent,
  PreviewSnapshot,
  WorkspaceSnapshotEnvelope,
  WorkspaceImportResult,
  WorkspaceForkResult,
  AgentStatus,
  AgentToken,
  ToolCall,
  ApprovalRequest,
  ChatMessage,
  ChatUserMessage,
  ChatAssistantMessage,
  ChatToolMessage,
  ContentBlock,
  ThinkingBlock,
  TextBlock,
  ToolUseBlock,
  CitationBlock,
  DigiPreviewContextValue,
  ResourceMap,
} from "./types.js";
