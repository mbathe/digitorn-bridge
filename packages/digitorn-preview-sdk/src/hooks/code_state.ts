import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDigiPreview } from "../DigiPreview.js";
import { useResources } from "./index.js";
import type {
  Diagnostic,
  DiagnosticsEntry,
  DiagnosticSeverity,
  FileValidation,
  GitStatus,
  WorkspaceFile,
  WorkspaceFileMeta,
} from "../types.js";

function _resolveSession(): {
  appId: string;
  sessionId: string;
  baseUrl: string;
  token: string | null;
} {
  const params = new URLSearchParams(window.location.search);
  const pathMatch = window.location.pathname.match(/\/api\/apps\/([^/]+)\//);
  return {
    appId: pathMatch?.[1] ?? "unknown",
    sessionId: params.get("session_id") ?? "_dev_",
    token: params.get("token"),
    baseUrl: window.location.origin,
  };
}

async function _request<T>(
  baseUrl: string,
  path: string,
  token: string | null,
  init?: RequestInit,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${baseUrl}${path}`, { ...init, headers });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
  }
  const body = (await res.json()) as { success: boolean; data?: T; error?: string };
  if (!body.success) throw new Error(body.error || "request failed");
  return body.data as T;
}

// ── useCodeState ───────────────────────────────────────────────────────

export interface CodeStateEntry extends WorkspaceFileMeta {
  /** Convenience: true when validation === "pending" */
  dirty: boolean;
}

/**
 * VS Code-style tree of workspace files with validation, diff gutters,
 * and git status. Driven by the same Socket.IO `resource_*` deltas as
 * ``useFiles()`` — no separate subscription needed.
 *
 * Returns a stable sorted array (by path). Entries exclude file content
 * to keep the tree light; call ``useFileContent(path)`` to fetch content
 * when the user opens a file.
 */
export function useCodeState(): CodeStateEntry[] {
  const { resources } = useDigiPreview();
  return useMemo(() => {
    const out: CodeStateEntry[] = [];
    const files = resources.get("files") as Map<string, WorkspaceFile> | undefined;
    if (!files) return out;
    for (const [path, payload] of files) {
      const { content: _c, ...meta } = payload;
      out.push({
        path,
        ...(meta as Omit<WorkspaceFile, "content">),
        dirty: (meta as WorkspaceFile).validation !== "approved",
      });
    }
    out.sort((a, b) => a.path.localeCompare(b.path));
    return out;
  }, [resources]);
}

// ── useFileContent ─────────────────────────────────────────────────────

export interface FileContentState {
  content: string | undefined;
  baseline: string | undefined;
  unifiedDiffPending: string | undefined;
  loading: boolean;
  error: Error | null;
  refresh: () => Promise<void>;
}

/**
 * Lazy-load full content + optional baseline for a single file. Cached in
 * component state; re-fetches on ``refresh()`` or when the live Socket.IO
 * stream bumps that file's ``updated_at``.
 */
export function useFileContent(
  path: string,
  opts?: { baseline?: boolean },
): FileContentState {
  const { resources } = useDigiPreview();
  const session = useMemo(_resolveSession, []);
  const [content, setContent] = useState<string | undefined>(undefined);
  const [baseline, setBaseline] = useState<string | undefined>(undefined);
  const [diff, setDiff] = useState<string | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const lastUpdatedAt = useRef<number | undefined>(undefined);

  const fetchOnce = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    setError(null);
    try {
      const q = opts?.baseline ? "?include_baseline=true" : "";
      const data = await _request<{
        path: string;
        payload: WorkspaceFile;
        baseline?: string;
        unified_diff_pending?: string;
      }>(
        session.baseUrl,
        `/api/apps/${session.appId}/sessions/${session.sessionId}/workspace/files/${encodeURIComponent(path)}${q}`,
        session.token,
      );
      setContent(data.payload?.content);
      setBaseline(data.baseline);
      setDiff(data.unified_diff_pending);
      lastUpdatedAt.current = data.payload?.updated_at;
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, [path, session.appId, session.baseUrl, session.sessionId, session.token, opts?.baseline]);

  // Initial fetch
  useEffect(() => {
    void fetchOnce();
  }, [fetchOnce]);

  // Auto-refresh when live stream bumps updated_at
  useEffect(() => {
    const files = resources.get("files") as Map<string, WorkspaceFile> | undefined;
    const live = files?.get(path);
    if (!live) return;
    if (
      live.updated_at !== undefined &&
      live.updated_at !== lastUpdatedAt.current
    ) {
      void fetchOnce();
    }
  }, [resources, path, fetchOnce]);

  return {
    content,
    baseline,
    unifiedDiffPending: diff,
    loading,
    error,
    refresh: fetchOnce,
  };
}

// ── useFileActions ─────────────────────────────────────────────────────

export interface UseFileActionsApi {
  approve: (path: string) => Promise<void>;
  reject: (path: string) => Promise<void>;
  approveAll: () => Promise<number>;
  refreshGitStatus: () => Promise<void>;
  busy: boolean;
  error: Error | null;
}

/** Imperative actions for the SCM panel: approve, reject, refresh git. */
export function useFileActions(): UseFileActionsApi {
  const session = useMemo(_resolveSession, []);
  const code = useCodeState();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const post = useCallback(
    async (path: string, endpoint: "approve" | "reject") => {
      setBusy(true); setError(null);
      try {
        await _request<{}>(
          session.baseUrl,
          `/api/apps/${session.appId}/sessions/${session.sessionId}/workspace/files/${endpoint}`,
          session.token,
          { method: "POST", body: JSON.stringify({ path }) },
        );
      } catch (e) { setError(e as Error); throw e; }
      finally { setBusy(false); }
    },
    [session.appId, session.baseUrl, session.sessionId, session.token],
  );

  const approve = useCallback((path: string) => post(path, "approve"), [post]);
  const reject = useCallback((path: string) => post(path, "reject"), [post]);

  const approveAll = useCallback(async () => {
    const pending = code.filter((f) => f.validation !== "approved");
    setBusy(true); setError(null);
    try {
      for (const f of pending) {
        await _request<{}>(
          session.baseUrl,
          `/api/apps/${session.appId}/sessions/${session.sessionId}/workspace/files/approve`,
          session.token,
          { method: "POST", body: JSON.stringify({ path: f.path }) },
        );
      }
      return pending.length;
    } catch (e) { setError(e as Error); throw e; }
    finally { setBusy(false); }
  }, [code, session.appId, session.baseUrl, session.sessionId, session.token]);

  const refreshGitStatus = useCallback(async () => {
    setBusy(true); setError(null);
    try {
      await _request<{}>(
        session.baseUrl,
        `/api/apps/${session.appId}/sessions/${session.sessionId}/workspace/git-status`,
        session.token,
        { method: "POST", body: JSON.stringify({}) },
      );
    } catch (e) { setError(e as Error); throw e; }
    finally { setBusy(false); }
  }, [session.appId, session.baseUrl, session.sessionId, session.token]);

  return { approve, reject, approveAll, refreshGitStatus, busy, error };
}

// ── useDiagnostics ─────────────────────────────────────────────────────

/**
 * LSP-shape diagnostics for a single file (or all files when omitted).
 * Driven by the `diagnostics` resource channel — agents writing files
 * trigger LSP validation server-side; results flow back here via the
 * same Socket.IO delta stream.
 */
export function useDiagnostics(path: string): DiagnosticsEntry | undefined;
export function useDiagnostics(): Map<string, DiagnosticsEntry>;
export function useDiagnostics(
  path?: string,
): DiagnosticsEntry | Map<string, DiagnosticsEntry> | undefined {
  const all = useResources<DiagnosticsEntry>("diagnostics");
  if (path === undefined) return all;
  return all.get(path);
}

export interface DiagnosticsStats {
  totalErrors: number;
  totalWarnings: number;
  totalInfos: number;
  filesWithErrors: number;
  worstSeverity: DiagnosticSeverity | null;
}

/** Aggregate diagnostics stats for the Problems panel badge. */
export function useDiagnosticsStats(): DiagnosticsStats {
  const all = useResources<DiagnosticsEntry>("diagnostics");
  return useMemo(() => {
    let errors = 0, warnings = 0, infos = 0, filesWithErrors = 0;
    const order: DiagnosticSeverity[] = ["error", "warning", "info", "hint"];
    let worst: DiagnosticSeverity | null = null;
    for (const entry of all.values()) {
      let hasErr = false;
      for (const it of entry.items ?? []) {
        if (it.severity === "error") { errors++; hasErr = true; }
        else if (it.severity === "warning") warnings++;
        else if (it.severity === "info") infos++;
        if (!worst || order.indexOf(it.severity) < order.indexOf(worst)) {
          worst = it.severity;
        }
      }
      if (hasErr) filesWithErrors++;
    }
    return {
      totalErrors: errors, totalWarnings: warnings, totalInfos: infos,
      filesWithErrors, worstSeverity: worst,
    };
  }, [all]);
}

// ── useLspRequest (hover / goto / references / completion / rename) ───

export interface LspRpcResult<T = unknown> {
  server: string;
  method: string;
  result: T;
  request_id?: string;
}

export interface LspCancelResult {
  request_id: string;
  cancelled: boolean;
  already_done?: boolean;
}

/**
 * Imperative API to send LSP requests through the daemon.
 *
 * Any LSP method the underlying language server supports — common ones
 * typed below, but the `request(method, params)` escape hatch accepts
 * any string so new methods work without an SDK upgrade.
 *
 * Example (Monaco hover provider)::
 *
 *   const lsp = useLspRequest();
 *   monaco.languages.registerHoverProvider("typescript", {
 *     provideHover: async (model, position) => {
 *       const r = await lsp.hover(model.uri.path, {
 *         line: position.lineNumber - 1,
 *         character: position.column - 1,
 *       });
 *       if (!r?.result) return null;
 *       return { contents: [{ value: r.result.contents?.value ?? "" }] };
 *     },
 *   });
 */
export interface UseLspRequestApi {
  request: <T = unknown>(
    path: string,
    method: string,
    params?: Record<string, unknown>,
    opts?: {
      timeout?: number;
      requestId?: string;
      supersedePrevious?: boolean;
      abortSignal?: AbortSignal;
    },
  ) => Promise<LspRpcResult<T>>;
  cancel: (requestId: string) => Promise<LspCancelResult | null>;
  hover: (
    path: string,
    position: { line: number; character: number },
  ) => Promise<LspRpcResult | null>;
  definition: (
    path: string,
    position: { line: number; character: number },
  ) => Promise<LspRpcResult | null>;
  references: (
    path: string,
    position: { line: number; character: number },
    opts?: { includeDeclaration?: boolean },
  ) => Promise<LspRpcResult | null>;
  completion: (
    path: string,
    position: { line: number; character: number },
    opts?: { triggerKind?: number; triggerCharacter?: string },
  ) => Promise<LspRpcResult | null>;
  rename: (
    path: string,
    position: { line: number; character: number },
    newName: string,
  ) => Promise<LspRpcResult | null>;
  busy: boolean;
  error: Error | null;
}

export function useLspRequest(): UseLspRequestApi {
  const session = useMemo(_resolveSession, []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const request = useCallback(
    async <T = unknown>(
      path: string,
      method: string,
      params: Record<string, unknown> = {},
      opts?: {
        timeout?: number;
        requestId?: string;
        supersedePrevious?: boolean;
        abortSignal?: AbortSignal;
      },
    ): Promise<LspRpcResult<T>> => {
      setBusy(true); setError(null);
      try {
        const headers: Record<string, string> = {
          "Content-Type": "application/json",
        };
        if (session.token) headers.Authorization = `Bearer ${session.token}`;
        const url = `${session.baseUrl}/api/apps/${session.appId}/sessions/${session.sessionId}/lsp/request`;
        const res = await fetch(url, {
          method: "POST",
          headers,
          signal: opts?.abortSignal,
          body: JSON.stringify({
            path, method, params,
            timeout_seconds: opts?.timeout ?? 10,
            request_id: opts?.requestId,
            supersede_previous: opts?.supersedePrevious ?? true,
          }),
        });
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
        }
        const body = (await res.json()) as { success: boolean; data?: LspRpcResult<T>; error?: string };
        if (!body.success) throw new Error(body.error || "lsp request failed");
        return body.data as LspRpcResult<T>;
      } catch (e) { setError(e as Error); throw e; }
      finally { setBusy(false); }
    },
    [session.appId, session.baseUrl, session.sessionId, session.token],
  );

  const cancel = useCallback(
    async (requestId: string): Promise<LspCancelResult | null> => {
      try {
        return await _request<LspCancelResult>(
          session.baseUrl,
          `/api/apps/${session.appId}/sessions/${session.sessionId}/lsp/cancel`,
          session.token,
          {
            method: "POST",
            body: JSON.stringify({ request_id: requestId }),
          },
        );
      } catch {
        return null;
      }
    },
    [session.appId, session.baseUrl, session.sessionId, session.token],
  );

  const safeCall = useCallback(
    async <T = unknown>(path: string, method: string, params: Record<string, unknown>):
      Promise<LspRpcResult<T> | null> => {
      try {
        return await request<T>(path, method, params);
      } catch {
        return null; // Monaco providers want null on failure, not throw
      }
    },
    [request],
  );

  const hover = useCallback(
    (path: string, position: { line: number; character: number }) =>
      safeCall(path, "textDocument/hover", { position }),
    [safeCall],
  );

  const definition = useCallback(
    (path: string, position: { line: number; character: number }) =>
      safeCall(path, "textDocument/definition", { position }),
    [safeCall],
  );

  const references = useCallback(
    (path: string, position: { line: number; character: number },
     opts?: { includeDeclaration?: boolean }) =>
      safeCall(path, "textDocument/references", {
        position,
        context: { includeDeclaration: opts?.includeDeclaration ?? true },
      }),
    [safeCall],
  );

  const completion = useCallback(
    (path: string, position: { line: number; character: number },
     opts?: { triggerKind?: number; triggerCharacter?: string }) =>
      safeCall(path, "textDocument/completion", {
        position,
        context: {
          triggerKind: opts?.triggerKind ?? 1,
          ...(opts?.triggerCharacter ? { triggerCharacter: opts.triggerCharacter } : {}),
        },
      }),
    [safeCall],
  );

  const rename = useCallback(
    (path: string, position: { line: number; character: number }, newName: string) =>
      safeCall(path, "textDocument/rename", { position, newName }),
    [safeCall],
  );

  return { request, cancel, hover, definition, references, completion, rename, busy, error };
}

// ── useCodeStats ───────────────────────────────────────────────────────

export interface CodeStats {
  total: number;
  pending: number;
  approved: number;
  rejected: number;
  byGit: Record<GitStatus, number>;
  totalInsertionsPending: number;
  totalDeletionsPending: number;
}

/** Aggregate stats for the explorer header / SCM badge. */
export function useCodeStats(): CodeStats {
  const code = useCodeState();
  return useMemo(() => {
    const byGit = {
      staged: 0, unstaged: 0, untracked: 0,
      committed: 0, conflict: 0, ignored: 0,
    } as Record<GitStatus, number>;
    let pending = 0, approved = 0, rejected = 0;
    let ins = 0, del = 0;
    for (const f of code) {
      const v = (f.validation ?? "pending") as FileValidation;
      if (v === "pending") pending++;
      else if (v === "approved") approved++;
      else rejected++;
      if (f.git_status) byGit[f.git_status]++;
      ins += f.insertions_pending ?? 0;
      del += f.deletions_pending ?? 0;
    }
    return {
      total: code.length,
      pending, approved, rejected, byGit,
      totalInsertionsPending: ins,
      totalDeletionsPending: del,
    };
  }, [code]);
}
