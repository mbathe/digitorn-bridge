// Provider + context
export { DigiPreview, useDigiPreview, readSession } from "./DigiPreview.js";
export type { DigiPreviewProps } from "./DigiPreview.js";

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

// Workspace checkpoint / fork
export {
  useWorkspaceSnapshot,
  useWorkspacePersistence,
} from "./hooks/workspace.js";
export type { UseWorkspaceSnapshotApi } from "./hooks/workspace.js";

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
  DigiPreviewContextValue,
  ResourceMap,
} from "./types.js";
