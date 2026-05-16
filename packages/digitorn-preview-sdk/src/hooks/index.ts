import { useEffect, useMemo, useRef, useState } from "react";
import { useDigiPreview, useDigiPreviewSocket } from "../DigiPreview.js";
import type { TurnEnricher } from "../DigiPreview.js";
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

/**
 * True once the SDK has applied the initial ``snapshot`` payload.
 * Distinguishes "session state at mount" from "deltas after mount".
 */
export function useHydrated(): boolean {
  return useDigiPreview().hydrated;
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

// ── Resource lifecycle ────────────────────────────────────────────────
//
// Channel-agnostic primitives that fire create / update / delete callbacks
// whenever the agent (or any producer) mutates the preview state via
// ``preview.set_resource`` / ``patch_resource`` / ``delete_resource`` /
// ``bulk_set_resources`` on ANY channel.
//
// Why this exists: ``useResource(channel, id)`` is great for re-render-on-
// change, but apps that want to REACT to a new resource (toast on
// arrival, auto-open the new form, kick off downstream processing) had
// to wire ``useRef + diff`` themselves. This bakes the bookkeeping into
// the SDK so any consumer gets it for free.
//
// Match patterns (string form, on the resource id):
//   - exact         ``"briefing.md"``        — single resource
//   - prefix        ``"audio_overview/"``     — trailing slash = prefix
//   - glob          ``"forms/*.json"``        — ``*`` matches any chars except ``/``
//   - predicate     ``(id) => id.endsWith(".json")``
//
// Reference equality is used to detect "updated" — the SDK reducer
// replaces the payload object on every ``resource_set`` / ``patched``,
// so an unchanged payload reuses the previous reference.

export type ResourceEventKind = "create" | "update" | "delete";

export interface ResourceEvent<T = Record<string, unknown>> {
  kind: ResourceEventKind;
  channel: string;
  /** Resource id. For the ``files`` channel, this is the file path. */
  id: string;
  /** Current payload. Present on ``create`` and ``update``. */
  payload?: T;
  /** Previous payload. Present on ``update`` and ``delete``. */
  prev?: T;
}

export interface UseResourceLifecycleOptions<T> {
  /** Resource channel name (``files``, ``nodes``, ``forms``, ...). Required. */
  channel: string;
  /** Restrict which ids fire callbacks. Defaults to all ids on the channel. */
  match?: string | ((id: string) => boolean);
  onCreate?: (event: ResourceEvent<T> & { kind: "create"; payload: T }) => void;
  onUpdate?: (event: ResourceEvent<T> & { kind: "update"; payload: T; prev: T }) => void;
  onDelete?: (event: ResourceEvent<T> & { kind: "delete"; prev: T }) => void;
  /**
   * Fire ``onCreate`` for resources that ALREADY exist when the
   * component first observes them. Useful for apps that want to react
   * to current state on mount (e.g. page reload landing on a session
   * with files already written). Default: ``true``.
   */
  fireForInitial?: boolean;
}

function _compileMatcher(
  match: string | ((id: string) => boolean) | undefined,
): (id: string) => boolean {
  if (match === undefined) return _matchAll;
  if (typeof match === "function") return match;
  if (match.includes("*")) {
    const escaped = match
      .replace(/[.+?^${}()|[\]\\]/g, "\\$&")
      .replace(/\*/g, "[^/]*");
    const re = new RegExp(`^${escaped}$`);
    return (id) => re.test(id);
  }
  if (match.endsWith("/")) {
    const prefix = match;
    return (id) => id.startsWith(prefix);
  }
  const exact = match;
  return (id) => id === exact;
}

function _matchAll(): boolean { return true; }

/**
 * Subscribe to lifecycle events on a preview resource channel.
 *
 * The hook itself does NOT cause re-renders when resources change — it
 * only invokes the supplied callbacks. Pair with local component state
 * (e.g. ``useState``) if you want to drive rendering from the events.
 *
 * Callback identity does not need to be stable; the hook reads the
 * latest callbacks via a ref each time it dispatches, so inline arrows
 * don't cause double-fires.
 *
 * @example Generic
 * ```tsx
 * useResourceLifecycle<FormSchema>({
 *   channel: "forms",
 *   match: "*.json",
 *   onCreate: (e) => setForm(e.payload),
 *   onUpdate: (e) => setForm(e.payload),
 *   onDelete: (e) => setForm(null),
 * });
 * ```
 *
 * @example Files channel + prefix
 * ```tsx
 * useResourceLifecycle({
 *   channel: "files",
 *   match: "audio_overview/",
 *   onCreate: (e) => toast(`New segment: ${e.id}`),
 * });
 * ```
 */
export function useResourceLifecycle<T = Record<string, unknown>>(
  options: UseResourceLifecycleOptions<T>,
): void {
  const { channel, match, fireForInitial = true } = options;
  const resources = useResources<T>(channel);
  const hydrated = useHydrated();

  const matcher = useMemo(() => _compileMatcher(match), [match]);

  // Stash the LATEST callbacks in a ref so changing their identity
  // (e.g. inline arrows) doesn't trigger an extra diff pass.
  const callbacksRef = useRef({
    onCreate: options.onCreate,
    onUpdate: options.onUpdate,
    onDelete: options.onDelete,
  });
  callbacksRef.current = {
    onCreate: options.onCreate,
    onUpdate: options.onUpdate,
    onDelete: options.onDelete,
  };

  // ``null`` = no baseline yet. We defer baseline capture until the
  // SDK reports ``hydrated``, otherwise the snapshot's resources would
  // look like a flurry of creates the moment they land. Once baseline
  // is captured, every subsequent change is a real delta.
  const previousRef = useRef<Map<string, T> | null>(null);

  useEffect(() => {
    if (!hydrated) return;

    const previous = previousRef.current;
    const cb = callbacksRef.current;
    const next = new Map<string, T>();

    if (previous === null) {
      // First observation after hydration. Snapshot the current state
      // as baseline; optionally fire onCreate for each matched entry.
      for (const [id, payload] of resources) {
        if (!matcher(id)) continue;
        next.set(id, payload);
        if (fireForInitial) {
          cb.onCreate?.({ kind: "create", channel, id, payload });
        }
      }
      previousRef.current = next;
      return;
    }

    // Diff vs previous snapshot.
    for (const [id, payload] of resources) {
      if (!matcher(id)) continue;
      next.set(id, payload);
      const prev = previous.get(id);
      if (prev === undefined) {
        cb.onCreate?.({ kind: "create", channel, id, payload });
      } else if (prev !== payload) {
        cb.onUpdate?.({ kind: "update", channel, id, payload, prev });
      }
    }
    for (const [id, prev] of previous) {
      if (!next.has(id)) {
        cb.onDelete?.({ kind: "delete", channel, id, prev });
      }
    }

    previousRef.current = next;
  }, [resources, matcher, channel, fireForInitial, hydrated]);
}

export interface UseResourceEventsOptions {
  channel: string;
  match?: string | ((id: string) => boolean);
  /** Maximum events retained in the returned array. Default: 100. */
  maxBuffer?: number;
  /** Whether to seed the buffer from current state on mount. Default: ``true``. */
  fireForInitial?: boolean;
}

/**
 * Stream-style counterpart to ``useResourceLifecycle``: returns a
 * bounded array of past events on the channel, oldest first. Each
 * mutation appends a new entry and re-renders the consumer.
 *
 * Useful for rendering activity logs, debugging the agent's actions,
 * or driving an undo/redo stack.
 */
export function useResourceEvents<T = Record<string, unknown>>(
  options: UseResourceEventsOptions,
): ResourceEvent<T>[] {
  const { channel, match, maxBuffer = 100, fireForInitial = true } = options;
  const [events, setEvents] = useState<ResourceEvent<T>[]>([]);

  const push = useMemo(
    () => (event: ResourceEvent<T>) =>
      setEvents((prev) =>
        prev.length >= maxBuffer
          ? [...prev.slice(prev.length - maxBuffer + 1), event]
          : [...prev, event],
      ),
    [maxBuffer],
  );

  useResourceLifecycle<T>({
    channel,
    match,
    fireForInitial,
    onCreate: push,
    onUpdate: push,
    onDelete: push,
  });

  return events;
}

// ── Turn enrichment: one-turn system prompt injection ─────────────────
//
// Apps need to slip ephemeral context to the agent BEFORE a user turn
// fires - "user just dropped X into the iframe", "current selection is
// page 12", "an attachment landed under attachments/Y". The chat
// composer is owned by the host, so the app can't append to the user
// message; injecting into the conversation history would be visible to
// the user.
//
// The SDK collects these one-shot signals through two complementary
// primitives:
//
//   * ``useTurnEnricher(fn)`` - register a function that the SDK calls
//     once per ``useChat().send()``. Return a string to contribute it
//     to ``system_addendum`` for THIS turn; return ``null`` to skip.
//     Stateless (re-evaluated every send), good for "derive a hint
//     from current state on the fly".
//   * ``usePendingHints()`` - returns a stable ``addHint(text)`` that
//     queues a hint. The queue is drained into ``system_addendum`` on
//     the next ``send`` and cleared. Stateful, good for "fire-and-
//     forget when a resource lifecycle event lands".
//
// Both contribute to the SAME envelope field (``system_addendum`` on
// the ``send_message`` payload); the SDK joins their outputs with
// blank-line separators. The daemon frames the joined text as a one-
// turn system message at the head of the agent's context and clears
// it after the turn completes, so nothing leaks into follow-up turns.

/**
 * Register a functional contributor to the next ``send`` envelope's
 * ``system_addendum``.
 *
 * The function is called once per ``useChat().send()`` invocation; its
 * return value (a string) is concatenated with other enrichers and any
 * pending hints, then sent as a one-turn system prompt fragment.
 *
 * Return ``null`` or ``undefined`` to skip the current send (the most
 * common case - you only have something to say when state changed).
 *
 * ```tsx
 * const files = useFilesByPrefix("sources/");
 * const lastSeenCount = useRef(files.length);
 * useTurnEnricher(() => {
 *   const fresh = files.length - lastSeenCount.current;
 *   lastSeenCount.current = files.length;
 *   if (fresh <= 0) return null;
 *   return `${fresh} new source(s) added since last turn.`;
 * });
 * ```
 *
 * Identity-stable callbacks aren't required - the hook re-registers
 * when ``fn`` changes. The unregister happens automatically on
 * unmount. Errors thrown by the enricher are caught and logged; they
 * never break a send.
 */
export function useTurnEnricher(fn: TurnEnricher): void {
  const { turn } = useDigiPreviewSocket();
  useEffect(() => {
    return turn.registerEnricher(fn);
  }, [turn, fn]);
}

export interface UsePendingHintsApi {
  /** Queue a hint to be sent on the NEXT ``useChat().send()``. The
   *  queue is drained after that send, so this is one-shot per call. */
  addHint: (text: string) => void;
}

/**
 * Stateful one-shot hint queue. Push hints from resource-lifecycle
 * callbacks or any other side-effect; the next ``useChat().send()``
 * folds them into ``system_addendum`` and clears the queue.
 *
 * Pairs naturally with ``useResourceLifecycle``:
 *
 * ```tsx
 * const { addHint } = usePendingHints();
 * useResourceLifecycle({
 *   channel: "files",
 *   match: "attachments/",
 *   fireForInitial: false,
 *   onCreate: (e) => addHint(`User uploaded ${e.id} via paperclip.`),
 * });
 * ```
 *
 * The hint queue is per-``DigiPreview`` instance and lives across
 * renders of every consumer (it's not React state) - so calling
 * ``usePendingHints()`` from two components both push into the same
 * queue.
 */
export function usePendingHints(): UsePendingHintsApi {
  const { turn } = useDigiPreviewSocket();
  return useMemo(() => ({ addHint: turn.addHint }), [turn]);
}
