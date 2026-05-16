import { useCallback, useContext, useEffect, useMemo, useState } from "react";
import { DigiPreviewSocketContext, useDigiPreview } from "../DigiPreview.js";
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

/** Resolve session info from the iframe URL when no provider is in tree. */
function _readSessionFromUrl(): {
  appId: string; sessionId: string; baseUrl: string; token: string | null;
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

/**
 * Resolve session info, preferring the ``<DigiPreview session={...}>``
 * override over URL params. Hosts that mount the SDK with a custom
 * session prop (tests, embedded preview surfaces) need session-aware
 * hooks to agree with the provider — otherwise ``useSessionMeta`` and
 * the socket connection point at different sessions.
 */
function _useResolvedSession() {
  const ctx = useContext(DigiPreviewSocketContext);
  if (ctx) return ctx.session;
  return _readSessionFromUrl();
}

// ── File-level workspace API ───────────────────────────────────────────

/** Where an ingested file lands in the workspace. */
export type IngestTargetDir = "sources" | "attachments";

/** Result of a successful ``ingestFile`` call. */
export interface IngestResult {
  /** Workspace-relative path the extracted text landed at. */
  path: string;
  /** Line count of the extracted text. */
  lines: number;
  /** Character count of the extracted text. */
  size: number;
  /** MIME type the daemon detected (after magic-byte sniffing). */
  mime: string;
  /** Canonical format (``.pdf``, ``.docx``, ...). */
  format: string;
  /** Whether the file landed in ``sources/`` or ``attachments/``. */
  target_dir: IngestTargetDir;
}

export interface UseWorkspaceFilesApi {
  /** Read a single file's content from the workspace. */
  readFile: (path: string) => Promise<{ content: string; size: number; lines: number }>;
  /** Write or overwrite a text file. ``autoApprove`` skips the
   *  pending-validation step so the file lands as approved instantly. */
  writeFile: (
    path: string,
    content: string,
    opts?: { autoApprove?: boolean },
  ) => Promise<unknown>;
  /** Upload a binary blob (image, archive, audio) to a known path.
   *  The daemon writes the bytes 1:1 - no transcoding, no extraction.
   *  For PDF/DOCX/etc. that need text extraction, use ``ingestFile``
   *  instead. ``autoApprove`` like ``writeFile``. */
  uploadFile: (
    path: string,
    blob: Blob,
    opts?: { autoApprove?: boolean },
  ) => Promise<unknown>;
  /**
   * Ingest a file with **server-side text extraction**. The daemon
   * sniffs the format (magic bytes + filename hint), routes PDF through
   * pymupdf, DOCX through python-docx, spreadsheets through openpyxl,
   * etc., and writes the plain-text result to the workspace under
   * ``<targetDir>/<sanitised_name>``. For pure-text inputs the
   * extractor returns the raw bytes unchanged.
   *
   * Use this whenever the iframe wants to accept files the agent should
   * be able to *read* — PDFs, Word docs, slides, spreadsheets, etc. —
   * without forcing the user through the chat composer paperclip.
   *
   * ``targetDir`` defaults to ``"sources"`` (manual curation). Pass
   * ``"attachments"`` to mirror the chat-composer destination instead.
   *
   * ```tsx
   * const fs = useWorkspaceFiles();
   * <input type="file" onChange={async e => {
   *   const f = e.target.files?.[0];
   *   if (f) await fs.ingestFile(f);
   * }} />
   * ```
   *
   * Errors:
   * - 422 ``no extractable text`` when the file is empty or unsupported
   * - 413 when over the 10 MB upload cap
   * - 400 on invalid target_dir
   */
  ingestFile: (
    file: File | Blob,
    opts?: { targetDir?: IngestTargetDir; filename?: string },
  ) => Promise<IngestResult>;
  /** Delete a file from the workspace. */
  deleteFile: (path: string) => Promise<unknown>;
  /** Mark a file as approved (snapshot the current content as the
   *  baseline so subsequent diffs compare against it). */
  approveFile: (path: string) => Promise<unknown>;
  /** Reject a file's pending changes - reverts to the last approved
   *  baseline. If never approved, deletes the file. */
  rejectFile: (path: string) => Promise<unknown>;
  /** Commit the approved files via git. Set ``push=true`` for git push. */
  commit: (message: string, opts?: { push?: boolean; files?: string[] }) => Promise<unknown>;
  /** True while any of the ops above is in flight. */
  busy: boolean;
  /** Last error, if any. */
  error: Error | null;
}

/**
 * File-level workspace mutation API. Write, delete, upload, approve,
 * reject, commit. Mirrors the agent-side ``Ws*`` tool set so an iframe
 * can manipulate workspace files autonomously (user uploads, manual
 * edits, drag-drop import, ...).
 *
 * ```tsx
 * const fs = useWorkspaceFiles();
 * <input type="file" onChange={async e => {
 *   const f = e.target.files?.[0];
 *   if (f) await fs.uploadFile(`avatars/${f.name}`, f, { autoApprove: true });
 * }} />
 * <button onClick={() => fs.writeFile("notes.md", "# Hello")}>Save</button>
 * <button onClick={() => fs.deleteFile("notes.md")}>Delete</button>
 * ```
 */
export function useWorkspaceFiles(): UseWorkspaceFilesApi {
  const session = _useResolvedSession();
  const { appId, sessionId, baseUrl, token } = session;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const _wrap = async <T,>(fn: () => Promise<T>): Promise<T> => {
    setBusy(true); setError(null);
    try { return await fn(); }
    catch (e) { setError(e as Error); throw e; }
    finally { setBusy(false); }
  };

  const readFile = useCallback(async (path: string) => {
    return _wrap(async () => _request<{ content: string; size: number; lines: number }>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/files/${encodeURI(path)}`,
      token,
    ));
  }, [appId, sessionId, baseUrl, token]);

  const writeFile = useCallback(async (
    path: string, content: string, opts?: { autoApprove?: boolean },
  ) => {
    return _wrap(async () => _request<unknown>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/files/${encodeURI(path)}`,
      token,
      {
        method: "PUT",
        body: JSON.stringify({
          content,
          auto_approve: opts?.autoApprove ?? false,
          source: "user",
        }),
      },
    ));
  }, [appId, sessionId, baseUrl, token]);

  const uploadFile = useCallback(async (
    path: string, blob: Blob, opts?: { autoApprove?: boolean },
  ) => {
    return _wrap(async () => {
      const fd = new FormData();
      fd.append("file", blob);
      fd.append("auto_approve", opts?.autoApprove ? "true" : "false");
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(
        `${baseUrl}/api/apps/${appId}/sessions/${sessionId}/workspace/upload/${encodeURI(path)}`,
        { method: "POST", body: fd, headers },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
      }
      const body = await res.json();
      if (!body.success) throw new Error(body.error || "upload failed");
      return body.data;
    });
  }, [appId, sessionId, baseUrl, token]);

  const ingestFile = useCallback(async (
    file: File | Blob,
    opts?: { targetDir?: IngestTargetDir; filename?: string },
  ): Promise<IngestResult> => {
    return _wrap(async () => {
      const fd = new FormData();
      const fname =
        opts?.filename ?? (file instanceof File ? file.name : "upload");
      // Wrap into a File so the filename round-trips through FormData
      // and ends up on the multipart Content-Disposition header. A bare
      // Blob would default to "blob" in some runtimes.
      const upload =
        file instanceof File && file.name === fname
          ? file
          : new File([file], fname, { type: file.type });
      fd.append("file", upload, fname);
      fd.append("target_dir", opts?.targetDir ?? "sources");
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(
        `${baseUrl}/api/apps/${appId}/sessions/${sessionId}/workspace/ingest-source`,
        { method: "POST", body: fd, headers },
      );
      if (!res.ok) {
        // Daemon errors on this route use the same envelope as the
        // rest of apps_v2: ``{detail: {error, ...}}`` for 4xx, plain
        // text for some 5xx. Surface the daemon's error message
        // verbatim so callers can show it to the user.
        let detail: unknown = null;
        try { detail = (await res.json())?.detail ?? null; } catch { /* */ }
        const msg =
          detail && typeof detail === "object" && "error" in detail
            ? String((detail as { error: string }).error)
            : typeof detail === "string"
              ? detail
              : `${res.status} ${res.statusText}`;
        throw new Error(msg);
      }
      const body = (await res.json()) as {
        success: boolean;
        data?: IngestResult;
        error?: string;
      };
      if (!body.success || !body.data) {
        throw new Error(body.error || "ingest failed");
      }
      return body.data;
    });
  }, [appId, sessionId, baseUrl, token]);

  const deleteFile = useCallback(async (path: string) => {
    return _wrap(async () => _request<unknown>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/files/${encodeURI(path)}`,
      token,
      { method: "DELETE" },
    ));
  }, [appId, sessionId, baseUrl, token]);

  const approveFile = useCallback(async (path: string) => {
    return _wrap(async () => _request<unknown>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/files/approve`,
      token,
      { method: "POST", body: JSON.stringify({ path }) },
    ));
  }, [appId, sessionId, baseUrl, token]);

  const rejectFile = useCallback(async (path: string) => {
    return _wrap(async () => _request<unknown>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/files/reject`,
      token,
      { method: "POST", body: JSON.stringify({ path }) },
    ));
  }, [appId, sessionId, baseUrl, token]);

  const commit = useCallback(async (
    message: string, opts?: { push?: boolean; files?: string[] },
  ) => {
    return _wrap(async () => _request<unknown>(
      baseUrl,
      `/api/apps/${appId}/sessions/${sessionId}/workspace/commit`,
      token,
      {
        method: "POST",
        body: JSON.stringify({
          message,
          push: opts?.push ?? false,
          files: opts?.files ?? null,
        }),
      },
    ));
  }, [appId, sessionId, baseUrl, token]);

  return {
    readFile, writeFile, uploadFile, ingestFile, deleteFile,
    approveFile, rejectFile, commit,
    busy, error,
  };
}

// ── Session lifecycle / metadata ──────────────────────────────────────

export interface SessionMeta {
  sessionId: string;
  appId: string;
  /** Epoch seconds. 0 if unknown. */
  createdAt: number;
  /** Epoch seconds. 0 if unknown. */
  lastActiveAt: number;
  /** Number of (user + assistant) message turns persisted so far. */
  turnCount: number;
  /** True when no user turn has happened AND the workspace state is
   *  empty (no resources, no state keys). Drive ``onFirstVisit`` UX. */
  isFirstVisit: boolean;
  /** Display title set by the daemon (first user message head). */
  title: string;
  /** Convenience: how long ago the session was last touched (seconds).
   *  ``Infinity`` when ``lastActiveAt`` is unknown. */
  idleSeconds: number;
  /** Convenience: how long since the session was first opened (seconds).
   *  ``Infinity`` when ``createdAt`` is unknown. */
  ageSeconds: number;
  /** Daemon-private session dir (``~/.digitorn/workspaces/{app}/{sid}/``).
   *  Where state.json, baselines, ``__sdk__/`` files live. The agent
   *  never operates here. */
  workspace: string;
  /** Agent-facing working directory. User-supplied at session create
   *  OR equal to ``workspace`` when no separate workdir was provided
   *  (legacy single-tree behaviour). The agent's tools read/write here. */
  workdir: string;
}

const _DEFAULT_META: SessionMeta = {
  sessionId: "",
  appId: "",
  createdAt: 0,
  lastActiveAt: 0,
  turnCount: 0,
  isFirstVisit: true,
  title: "",
  idleSeconds: Infinity,
  ageSeconds: Infinity,
  workspace: "",
  workdir: "",
};

/**
 * Read session metadata (created_at, last_active_at, turn count,
 * is_first_visit). Refreshes once on mount; the daemon's snapshot
 * route is the source of truth, see ``GET /sessions/{sid}/preview``.
 *
 * ```tsx
 * const meta = useSessionMeta();
 * if (meta.isFirstVisit) return <FirstVisitWizard />;
 * return <RegularDashboard ageSeconds={meta.ageSeconds} />;
 * ```
 */
export function useSessionMeta(): SessionMeta {
  const session = _useResolvedSession();
  const [meta, setMeta] = useState<SessionMeta>(() => ({
    ..._DEFAULT_META,
    sessionId: session.sessionId,
    appId: session.appId,
  }));

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const headers: Record<string, string> = {};
        if (session.token) headers.Authorization = `Bearer ${session.token}`;
        const r = await fetch(
          `${session.baseUrl}/api/apps/${encodeURIComponent(session.appId)}` +
          `/sessions/${encodeURIComponent(session.sessionId)}/preview`,
          { headers },
        );
        if (!r.ok || cancelled) return;
        const body = await r.json();
        const snap = (body.data ?? body) as Record<string, unknown>;
        const s = (snap.session as Record<string, unknown>) || {};
        const createdAt = Number(s.created_at ?? 0);
        const lastActiveAt = Number(s.last_active_at ?? 0);
        const now = Date.now() / 1000;
        if (!cancelled) {
          setMeta({
            sessionId: String(s.session_id ?? session.sessionId),
            appId: String(s.app_id ?? session.appId),
            createdAt,
            lastActiveAt,
            turnCount: Number(s.turn_count ?? 0),
            isFirstVisit: Boolean(s.is_first_visit ?? true),
            title: String(s.title ?? ""),
            idleSeconds: lastActiveAt > 0 ? now - lastActiveAt : Infinity,
            ageSeconds: createdAt > 0 ? now - createdAt : Infinity,
            workspace: String(s.workspace ?? ""),
            workdir: String(s.workdir ?? s.workspace ?? ""),
          });
        }
      } catch {
        // Daemon unreachable: leave defaults so ``isFirstVisit`` stays
        // true (the safer assumption - showing a wizard once is a
        // smaller UX issue than skipping it forever).
      }
    })();
    return () => { cancelled = true; };
  }, [session.appId, session.sessionId, session.baseUrl, session.token]);

  return meta;
}

export interface SessionLifecycleHandlers {
  /** Fired ONCE the first time the iframe loads with no prior state. */
  onFirstVisit?: (meta: SessionMeta) => void | Promise<void>;
  /** Fired ONCE on a session that has prior state (resume). */
  onResume?: (meta: SessionMeta) => void | Promise<void>;
  /** Fired on every successful mount (after first-visit / resume). */
  onReady?: (meta: SessionMeta) => void | Promise<void>;
}

/**
 * Lifecycle dispatcher: fires ``onFirstVisit`` exactly once when the
 * SDK detects an empty session, ``onResume`` exactly once when the
 * session has prior state, and ``onReady`` after either of those.
 *
 * Idempotent across remounts during the SAME session - we keep a
 * one-shot flag in module-level state keyed by ``sessionId``.
 *
 * ```tsx
 * useSessionLifecycle({
 *   onFirstVisit: async () => {
 *     // First time the user opens this session
 *     await fs.writeFile("__sdk__/welcome-shown", "1", { autoApprove: true });
 *   },
 *   onResume: meta => console.log(`Welcome back, last seen ${meta.idleSeconds}s ago`),
 * });
 * ```
 */
const _firedFirstVisit = new Set<string>();
const _firedResume = new Set<string>();
const _firedReady = new Set<string>();

export function useSessionLifecycle(handlers: SessionLifecycleHandlers): void {
  const meta = useSessionMeta();

  useEffect(() => {
    // Wait until the snapshot has actually arrived (createdAt > 0
    // OR turnCount > 0). Without this, the default state shows
    // ``isFirstVisit=true`` which would fire onFirstVisit on apps
    // we know nothing about yet (transient).
    const hasLoaded = meta.createdAt > 0 || meta.turnCount > 0
      || meta.lastActiveAt > 0;
    if (!hasLoaded || !meta.sessionId) return;

    void (async () => {
      try {
        if (meta.isFirstVisit) {
          if (!_firedFirstVisit.has(meta.sessionId)) {
            _firedFirstVisit.add(meta.sessionId);
            await handlers.onFirstVisit?.(meta);
          }
        } else {
          if (!_firedResume.has(meta.sessionId)) {
            _firedResume.add(meta.sessionId);
            await handlers.onResume?.(meta);
          }
        }
        if (!_firedReady.has(meta.sessionId)) {
          _firedReady.add(meta.sessionId);
          await handlers.onReady?.(meta);
        }
      } catch (err) {
        console.error("[digitorn/preview-sdk] lifecycle handler error:", err);
      }
    })();
  }, [
    meta.sessionId,
    meta.isFirstVisit,
    meta.createdAt,
    meta.turnCount,
    meta.lastActiveAt,
    handlers,
  ]);
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
