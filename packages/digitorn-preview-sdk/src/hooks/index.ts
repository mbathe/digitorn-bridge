import { useMemo } from "react";
import { useDigiPreview } from "../DigiPreview.js";
import type {
  WorkspaceFile,
  PreviewNode,
  PreviewEdge,
  PreviewEvent,
  AgentStatus,
  ToolCall,
  ApprovalRequest,
  ResourceMap,
} from "../types.js";

// ── Connection ─────────────────────────────────────────────────────────

/** true when Socket.IO is connected to the daemon */
export function useConnection(): boolean {
  return useDigiPreview().connected;
}

// ── Preview resources ──────────────────────────────────────────────────

/** All resources in a channel as a Map<id, payload> */
export function useResources<T = Record<string, unknown>>(channel: string): Map<string, T> {
  const { resources } = useDigiPreview();
  return (resources.get(channel) as unknown as Map<string, T>) ?? new Map();
}

/** Single resource by id */
export function useResource<T = Record<string, unknown>>(channel: string, id: string): T | undefined {
  const { resources } = useDigiPreview();
  return resources.get(channel)?.get(id) as T | undefined;
}

// ── Scalar state ───────────────────────────────────────────────────────

/** Watch a single state key (set via workspace or preview.set_state) */
export function usePreviewState<T = unknown>(key: string, defaultValue?: T): T | undefined {
  const { state } = useDigiPreview();
  return (state[key] as T | undefined) ?? defaultValue;
}

// ── Workspace files ────────────────────────────────────────────────────

/** All workspace files as a Map<path, WorkspaceFile> */
export function useFiles(): Map<string, WorkspaceFile> {
  return useResources<WorkspaceFile>("files");
}

/** Raw content of a single file */
export function useFile(path: string): string | undefined {
  return useResource<WorkspaceFile>("files", path)?.content;
}

/** Parse a single JSON file — undefined if missing or invalid JSON */
export function useFileJson<T = unknown>(path: string): T | undefined {
  const content = useFile(path);
  return useMemo(() => {
    if (content === undefined) return undefined;
    try { return JSON.parse(content) as T; } catch { return undefined; }
  }, [content]);
}

/** All files whose path starts with prefix, sorted by path */
export function useFilesByPrefix(prefix: string): Array<WorkspaceFile & { path: string }> {
  const files = useFiles();
  return useMemo(() => {
    const out: Array<WorkspaceFile & { path: string }> = [];
    for (const [path, file] of files) {
      if (path.startsWith(prefix)) out.push({ ...file, path });
    }
    return out.sort((a, b) => a.path.localeCompare(b.path));
  }, [files, prefix]);
}

/** Parse all JSON files under prefix — skips invalid JSON silently */
export function useFilesJsonByPrefix<T = unknown>(prefix: string): Array<{ path: string; data: T }> {
  const raw = useFilesByPrefix(prefix);
  return useMemo(() => {
    const out: Array<{ path: string; data: T }> = [];
    for (const { path, content } of raw) {
      try { out.push({ path, data: JSON.parse(content) as T }); } catch { /* skip */ }
    }
    return out;
  }, [raw]);
}

// ── File stats (global tracking) ───────────────────────────────────────

export interface FileStats {
  /** Total files in workspace */
  fileCount: number;
  /** Files added in this session */
  added: number;
  /** Files modified in this session */
  modified: number;
  /** Files deleted in this session */
  deleted: number;
  /** Cumulative lines inserted across all files */
  totalInsertions: number;
  /** Cumulative lines deleted across all files */
  totalDeletions: number;
}

/** Global file change stats for the entire session */
export function useFileStats(): FileStats {
  const files = useFiles();
  return useMemo(() => {
    let added = 0, modified = 0, deleted = 0;
    let totalInsertions = 0, totalDeletions = 0;
    for (const file of files.values()) {
      if (file.status === "added") added++;
      else if (file.status === "modified") modified++;
      else if (file.status === "deleted") deleted++;
      totalInsertions += file.total_insertions ?? 0;
      totalDeletions += file.total_deletions ?? 0;
    }
    return { fileCount: files.size, added, modified, deleted, totalInsertions, totalDeletions };
  }, [files]);
}

// ── Canvas (nodes / edges) ─────────────────────────────────────────────

/** All canvas nodes sorted by updated_at */
export function useNodes(): PreviewNode[] {
  const nodes = useResources<PreviewNode>("nodes");
  return useMemo(
    () => Array.from(nodes.values()).sort((a, b) => (a.updated_at ?? 0) - (b.updated_at ?? 0)),
    [nodes],
  );
}

/** All canvas edges */
export function useEdges(): PreviewEdge[] {
  const edges = useResources<PreviewEdge>("edges");
  return useMemo(() => Array.from(edges.values()), [edges]);
}

// ── Agent ──────────────────────────────────────────────────────────────

/** Current agent status: idle | thinking | working | done | error */
export function useAgentStatus(): AgentStatus {
  return useDigiPreview().agentStatus;
}

/** Accumulated text of the current agent turn (resets between turns) */
export function useAgentStream(): string {
  return useDigiPreview().agentStream;
}

/** Last N tool calls made by the agent */
export function useToolCalls(): ToolCall[] {
  return useDigiPreview().toolCalls;
}

// ── Approval ───────────────────────────────────────────────────────────

/** Non-null when the agent is waiting for user confirmation */
export function useApprovalRequest(): ApprovalRequest | null {
  return useDigiPreview().approvalRequest;
}

// ── Event log ─────────────────────────────────────────────────────────

/** Raw event log, optionally filtered */
export function useEvents(filter?: string | ((e: PreviewEvent) => boolean)): PreviewEvent[] {
  const { events } = useDigiPreview();
  return useMemo(() => {
    if (!filter) return events.slice(-100);
    const pred = typeof filter === "string"
      ? (e: PreviewEvent) => e.event_type === filter
      : filter;
    return events.filter(pred).slice(-100);
  }, [events, filter]);
}
