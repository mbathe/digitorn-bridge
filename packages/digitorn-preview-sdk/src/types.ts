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

// ── Structured streaming blocks ──────────────────────────────────────
//
// The daemon's response stream isn't a single text channel - it
// interleaves several distinct types of output (chain-of-thought,
// final answer text, tool calls + results, citations). We surface
// them as an ORDERED LIST of typed blocks so chat UIs can render each
// kind differently (collapsible thinking, inline tool widgets,
// clickable citations) instead of flattening everything to a string.
//
// Each block carries ``streaming: true`` while its content is still
// growing, then flips to false on ``turn_complete``. ``timestamp`` is
// epoch ms set by the SDK when the first delta lands.

export interface ThinkingBlock {
  type: "thinking";
  content: string;
  streaming?: boolean;
  /** Total token count across the thinking block as reported by the
   *  daemon's ``thinking_delta`` payload (when available). */
  tokens?: number;
  timestamp: number;
}

export interface TextBlock {
  type: "text";
  content: string;
  streaming?: boolean;
  timestamp: number;
}

export interface ToolUseBlock {
  type: "tool_use";
  /** FQN of the tool (``shell.bash``, ``filesystem.write``, ...). */
  tool: string;
  params: Record<string, unknown>;
  /** Set when the tool returns. ``undefined`` while running. */
  result?: unknown;
  /** ``running`` while the call is in flight, ``done`` on success,
   *  ``error`` when the daemon flagged the result as an exception. */
  status: "running" | "done" | "error";
  timestamp: number;
}

export interface CitationBlock {
  type: "citation";
  /** URL or doc-id the citation points to. */
  source: string;
  /** Optional quoted span the model attributed to that source. */
  quote?: string;
  timestamp: number;
}

export type ContentBlock =
  | ThinkingBlock | TextBlock | ToolUseBlock | CitationBlock;

// ── Chat history ─────────────────────────────────────────────────────

export interface ChatUserMessage {
  role: "user";
  content: string;
  /** Image refs the user attached, if any. ``id``/``ref`` style strings
   *  resolvable via ``GET /sessions/{sid}/images/{id}``. */
  images?: string[];
  /** Epoch milliseconds. Set by the SDK when the event lands. */
  timestamp: number;
  /** Correlation id assigned by the daemon's queue. Use it to match the
   *  resulting assistant message + tool calls. */
  correlation_id?: string;
  /** True until the daemon emits ``message_done`` for this entry.
   *  Useful for greying out the bubble while it's being processed. */
  pending?: boolean;
}

export interface ChatAssistantMessage {
  role: "assistant";
  /** Concatenation of every ``text`` block in ``blocks`` - convenience
   *  for plain UIs that don't care about the typed structure. */
  content: string;
  timestamp: number;
  /** Correlation id of the user message that triggered this turn. */
  correlation_id?: string;
  /** Tool calls the assistant issued during this turn (chronological).
   *  Mirrors the ``tool_use`` blocks; kept for backward compat with
   *  the older ``useToolCalls()`` based UIs. */
  tool_calls?: ToolCall[];
  /** True while tokens are still streaming. Flips to false on
   *  ``turn_complete``. */
  streaming?: boolean;
  /** Ordered, typed view of the assistant's output. Each entry is one
   *  of ``ThinkingBlock | TextBlock | ToolUseBlock | CitationBlock``.
   *  Build rich chat UIs (collapsible thinking, inline tool widgets,
   *  clickable citations) by mapping over this instead of ``content``. */
  blocks: ContentBlock[];
}

export interface ChatToolMessage {
  role: "tool";
  /** Matches a ``tool_calls[].id`` on the preceding assistant message. */
  tool_call_id: string;
  /** Short label of the tool (``filesystem.write``, ``Bash``, ...). */
  tool: string;
  /** Tool result content (stringified for display). */
  content: string;
  timestamp: number;
}

export type ChatMessage =
  | ChatUserMessage | ChatAssistantMessage | ChatToolMessage;

export interface ApprovalRequest {
  /** Stable identifier the daemon assigned. Doubles as ``op_id`` on the
   *  session event bus so request + resolve share a correlation id. */
  request_id: string;
  /** FQN of the tool awaiting approval (e.g. ``shell.bash``). */
  tool_name: string;
  /** Params the agent intends to pass to the tool. */
  tool_params: Record<string, unknown>;
  /** Risk level resolved by the capabilities engine
   *  (``low | medium | high | critical``). */
  risk_level: string;
  /** Free-form description suitable for display in a modal. */
  description: string;
  /** Agent that initiated the call (``main`` for the entry agent). */
  agent_id: string;
  /** Owner of the session - server-side enforced when resolving. */
  user_id: string;
  /** App + session the request belongs to. Set by the daemon, useful
   *  when an iframe juggles multiple sessions. */
  app_id: string;
  session_id: string;
  /** Epoch seconds when the request was queued. Drive a "waiting
   *  for X seconds" badge from this. */
  created_at: number;

  // ── Legacy aliases (the original SDK shipped these names). Kept
  //    as optional mirrors of the canonical fields so apps that read
  //    ``request.tool`` / ``request.params`` keep working until they
  //    migrate to ``tool_name`` / ``tool_params``.
  /** @deprecated alias of ``tool_name``. */
  tool?: string;
  /** @deprecated alias of ``tool_params``. */
  params?: Record<string, unknown>;
}

// ── Context value ──────────────────────────────────────────────────────

export interface DigiPreviewContextValue {
  // Connection
  connected: boolean;

  /**
   * True once the SDK has applied an initial ``snapshot`` payload from
   * the daemon (HTTP ``GET /sessions/{sid}/preview`` or Socket.IO
   * ``preview:snapshot``). Useful for distinguishing "pre-existing
   * session state" (everything visible at hydration) from "new deltas
   * the agent just produced" — see ``useResourceLifecycle``.
   */
  hydrated: boolean;

  // Preview resources (workspace files, nodes, edges, custom channels)
  resources: Map<string, ResourceMap>;

  // Scalar state (progress, status flags, etc.)
  state: Record<string, unknown>;

  // Agent status
  agentStatus: AgentStatus;
  agentStream: string;           // accumulated text of current turn
  toolCalls: ToolCall[];         // last N tool calls

  // Chat history - chronological list of every user / assistant / tool
  // message the daemon emitted on this session. Updated live as the
  // ``user_message`` / ``token`` / ``tool_call`` / ``turn_complete``
  // events arrive on the bus. ``useChat()`` is the typed wrapper.
  chatMessages: ChatMessage[];

  // Approvals - all currently pending requests for this user. The
  // legacy ``approvalRequest`` mirror points at ``approvals[0]`` so
  // existing single-modal UIs keep working unchanged. New code should
  // read ``approvals`` (full list) and call ``useApprovals()`` for
  // imperative resolve.
  approvals: ApprovalRequest[];
  /** @deprecated alias of ``approvals[0] ?? null``. Use ``approvals``
   *  + ``useApprovals()`` for the full list and resolve actions. */
  approvalRequest: ApprovalRequest | null;

  // Event log (last 500)
  events: PreviewEvent[];
  seq: number;
}
