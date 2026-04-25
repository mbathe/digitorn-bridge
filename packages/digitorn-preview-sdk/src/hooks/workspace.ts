import { useCallback, useEffect, useState } from "react";
import { useDigiPreview } from "../DigiPreview.js";
import type {
  WorkspaceSnapshotEnvelope,
  WorkspaceImportResult,
  WorkspaceForkResult,
} from "../types.js";

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

export interface UseWorkspaceSnapshotApi {
  /** Download the current session's workspace as a portable envelope. */
  exportSnapshot: () => Promise<WorkspaceSnapshotEnvelope>;
  /** Replace (default) or merge a snapshot envelope into the current session. */
  importSnapshot: (
    envelope: WorkspaceSnapshotEnvelope | { state?: any; resources?: any; seq?: number },
    opts?: { replace?: boolean },
  ) => Promise<WorkspaceImportResult>;
  /** Fork the current session into a brand new session with the same workspace. */
  forkSession: (opts?: { targetSessionId?: string; title?: string }) => Promise<WorkspaceForkResult>;
  /** Download the snapshot as a .json file for the user. */
  downloadSnapshot: (filename?: string) => Promise<void>;
  /** Read a .json file picked by the user and import it. */
  importFromFile: (file: File, opts?: { replace?: boolean }) => Promise<WorkspaceImportResult>;
  /** True while any of the ops above is in flight. */
  busy: boolean;
  /** Last error, if any. */
  error: Error | null;
}

/**
 * High-level workspace checkpoint / fork API bound to the current session.
 *
 * ```tsx
 * const ws = useWorkspaceSnapshot();
 * <button onClick={() => ws.downloadSnapshot()}>Save a copy</button>
 * <button onClick={() => ws.forkSession({ title: "Fork v2" })}>Fork</button>
 * <input type="file" onChange={e => e.target.files?.[0] && ws.importFromFile(e.target.files[0])} />
 * ```
 */
export function useWorkspaceSnapshot(): UseWorkspaceSnapshotApi {
  const preview = useDigiPreview();
  // Session info lives in the provider's URL params — grab it via a lightweight re-read.
  const [{ appId, sessionId, baseUrl, token }] = useState(() => {
    const sessionFromCtx = (preview as unknown as { session?: {
      appId: string; sessionId: string; baseUrl: string; token: string | null;
    }}).session;
    if (sessionFromCtx) return sessionFromCtx;
    const params = new URLSearchParams(window.location.search);
    const pathMatch = window.location.pathname.match(/\/api\/apps\/([^/]+)\//);
    return {
      appId: pathMatch?.[1] ?? "unknown",
      sessionId: params.get("session_id") ?? "_dev_",
      token: params.get("token"),
      baseUrl: window.location.origin,
    };
  });

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const exportSnapshot = useCallback(async () => {
    setBusy(true); setError(null);
    try {
      return await _request<WorkspaceSnapshotEnvelope>(
        baseUrl,
        `/api/apps/${appId}/sessions/${sessionId}/workspace/export`,
        token,
      );
    } catch (e) { setError(e as Error); throw e; }
    finally { setBusy(false); }
  }, [appId, sessionId, baseUrl, token]);

  const importSnapshot = useCallback(
    async (envelope: any, opts?: { replace?: boolean }) => {
      setBusy(true); setError(null);
      try {
        return await _request<WorkspaceImportResult>(
          baseUrl,
          `/api/apps/${appId}/sessions/${sessionId}/workspace/import`,
          token,
          {
            method: "POST",
            body: JSON.stringify({
              snapshot: envelope,
              replace: opts?.replace ?? true,
            }),
          },
        );
      } catch (e) { setError(e as Error); throw e; }
      finally { setBusy(false); }
    },
    [appId, sessionId, baseUrl, token],
  );

  const forkSession = useCallback(
    async (opts?: { targetSessionId?: string; title?: string }) => {
      setBusy(true); setError(null);
      try {
        return await _request<WorkspaceForkResult>(
          baseUrl,
          `/api/apps/${appId}/sessions/${sessionId}/workspace/fork`,
          token,
          {
            method: "POST",
            body: JSON.stringify({
              target_session_id: opts?.targetSessionId ?? null,
              title: opts?.title ?? null,
            }),
          },
        );
      } catch (e) { setError(e as Error); throw e; }
      finally { setBusy(false); }
    },
    [appId, sessionId, baseUrl, token],
  );

  const downloadSnapshot = useCallback(async (filename?: string) => {
    const envelope = await exportSnapshot();
    const blob = new Blob([JSON.stringify(envelope, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || `${appId}-${sessionId}-workspace.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [exportSnapshot, appId, sessionId]);

  const importFromFile = useCallback(
    async (file: File, opts?: { replace?: boolean }) => {
      const text = await file.text();
      const envelope = JSON.parse(text);
      return importSnapshot(envelope, opts);
    },
    [importSnapshot],
  );

  return {
    exportSnapshot,
    importSnapshot,
    forkSession,
    downloadSnapshot,
    importFromFile,
    busy,
    error,
  };
}

/**
 * Small diagnostic hook — returns persistence liveness info so UIs can show
 * an "Auto-saving…" or "Saved ✓" indicator. Backed by the last preview_delta
 * seq the client has observed; diffs from the last flush seq indicate pending
 * writes. This hook does NOT poll — it just reacts to the live seq counter.
 */
export function useWorkspacePersistence(): {
  lastSeq: number;
  lastSavedAt: number | null;
  hasPendingWrites: boolean;
} {
  const { seq } = useDigiPreview();
  const [lastSavedAt, setLastSavedAt] = useState<number | null>(null);
  const [lastPersistedSeq, setLastPersistedSeq] = useState(0);

  useEffect(() => {
    if (seq === 0) return;
    const t = setTimeout(() => {
      setLastPersistedSeq(seq);
      setLastSavedAt(Date.now());
    }, 800); // daemon debounce is 500ms + network/render margin
    return () => clearTimeout(t);
  }, [seq]);

  return {
    lastSeq: seq,
    lastSavedAt,
    hasPendingWrites: seq > lastPersistedSeq,
  };
}
