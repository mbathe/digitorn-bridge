// ── Session ────────────────────────────────────────────────────────────

export interface SessionInfo {
  appId: string;
  sessionId: string;
  token: string | null;
  baseUrl: string;
}

// ── Node / canvas ──────────────────────────────────────────────────────

export type NodeStatus = "idle" | "running" | "done" | "error";

// ── Preview resources ──────────────────────────────────────────────────

export type ResourceMap<T = Record<string, unknown>> = Map<string, T>;

export type FileStatus = "added" | "modified" | "deleted";

export type FileValidation = "pending" | "approved" | "rejected";

export type GitStatus =
  | "staged"
  | "unstaged"
  | "untracked"
  | "committed"
  | "conflict"
  | "ignored";

export interface WorkspaceFile {
  content: string;
  language: string;
  size: number;
  lines: number;
  /** Status of this file in the current session */
  status?: FileStatus;
  /** Last operation: "write" | "edit" | "delete" */
  operation?: string;
  /** Lines inserted in the LAST operation */
  insertions?: number;
  /** Lines deleted in the LAST operation */
  deletions?: number;
  /** Cumulative lines inserted since session start */
  total_insertions?: number;
  /** Cumulative lines deleted since session start */
  total_deletions?: number;
  /** Lines inserted since the last approve() (VS Code gutter count) */
  insertions_pending?: number;
  /** Lines deleted since the last approve() */
  deletions_pending?: number;
  /** Short diff (last edit only) */
  diff?: string;
  /** Unified diff (last edit only) */
  unified_diff?: string;
  /** Timestamp of last change */
  updated_at?: number;
  /** User approval state: pending | approved | rejected */
  validation?: FileValidation;
  /** Baseline line count (last approved version) */
  baseline_lines?: number;
  /** Git source control state (if workspace is a git repo) */
  git_status?: GitStatus;
}

/** Metadata payload for the code-snapshot endpoint — content stripped. */
export type WorkspaceFileMeta = Omit<WorkspaceFile, "content"> & {
  path: string;
};

// ── Diagnostics (LSP shape for Monaco setModelMarkers) ─────────────────

export type DiagnosticSeverity = "error" | "warning" | "info" | "hint";

export interface DiagnosticRange {
  start: { line: number; character: number };
  end: { line: number; character: number };
}

export interface Diagnostic {
  severity: DiagnosticSeverity;
  message: string;
  range: DiagnosticRange;
  code?: string;
  source?: string;
}

/** Payload on the `diagnostics` channel (keyed by file path). */
export interface DiagnosticsEntry {
  file_path: string;
  items: Diagnostic[];
  generation: number;       // monotonic — client ignores payloads with generation < current
  severity_max: DiagnosticSeverity | null;
  updated_at: number;
}

export interface PreviewNode {
  id: string;
  type: string;
  label: string;
  position: { x: number; y: number };
  data: Record<string, unknown>;
  status: "idle" | "running" | "done" | "error";
  updated_at: number;
}

export interface PreviewEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  data: Record<string, unknown>;
}

export interface PreviewEvent {
  seq: number;
  event_type: string;
  data: Record<string, unknown>;
  timestamp: number;
}

export interface PreviewSnapshot {
  session_id: string;
  state: Record<string, unknown>;
  resources: Record<string, Record<string, Record<string, unknown>>>;
  nodes?: PreviewNode[];
  edges?: PreviewEdge[];
  events: PreviewEvent[];
  seq: number;
}

/**
 * Portable envelope returned by GET /workspace/export — can be stored on
 * disk (``.digitorn-workspace.json``), shared cross-user, or re-imported
 * into a new session via POST /workspace/import or /workspace/fork.
 */
export interface WorkspaceSnapshotEnvelope {
  format: "digitorn.workspace.snapshot";
  version: number;
  app_id: string;
  source_session_id: string;
  exported_at: string;
  state: Record<string, unknown>;
  resources: Record<string, Record<string, Record<string, unknown>>>;
  seq: number;
}

export interface WorkspaceImportResult {
  session_id: string;
  imported: true;
  replaced: boolean;
  files: number;
  state_keys: number;
  seq: number;
}

export interface WorkspaceForkResult {
  source_session_id: string;
  session_id: string;
  forked: true;
  files: number;
  seq: number;
}

// ── Agent events ───────────────────────────────────────────────────────

export type AgentStatus = "idle" | "thinking" | "working" | "done" | "error";

export interface AgentToken {
  content: string;
  accumulated: string;
}

export interface ToolCall {
  tool: string;
  params: Record<string, unknown>;
  result?: Record<string, unknown>;
  timestamp: number;
}

export interface ApprovalRequest {
  request_id: string;
  tool: string;
  params: Record<string, unknown>;
}

// ── Context value ──────────────────────────────────────────────────────

export interface DigiPreviewContextValue {
  // Connection
  connected: boolean;

  // Preview resources (workspace files, nodes, edges, custom channels)
  resources: Map<string, ResourceMap>;

  // Scalar state (progress, status flags, etc.)
  state: Record<string, unknown>;

  // Agent status
  agentStatus: AgentStatus;
  agentStream: string;           // accumulated text of current turn
  toolCalls: ToolCall[];         // last N tool calls

  // Approval
  approvalRequest: ApprovalRequest | null;

  // Event log (last 500)
  events: PreviewEvent[];
  seq: number;
}
