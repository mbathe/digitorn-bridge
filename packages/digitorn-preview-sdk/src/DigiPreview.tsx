import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import { type Socket } from "socket.io-client";
import { reducer, initialState } from "./reducer.js";
import { createConnection } from "./connect.js";
import type { DigiPreviewContextValue, SessionInfo } from "./types.js";

// ── Session resolution ─────────────────────────────────────────────────

function _readParentSearch(): URLSearchParams | null {
  try {
    if (window.parent && window.parent !== window) {
      return new URLSearchParams(window.parent.location.search);
    }
  } catch { /* cross-origin parent */ }
  return null;
}

export function readSession(): SessionInfo {
  const params = new URLSearchParams(window.location.search);
  const parentParams = _readParentSearch();

  const sessionId =
    params.get("session_id") ?? parentParams?.get("session_id") ?? "_dev_";

  const token =
    params.get("token") ?? parentParams?.get("token") ?? null;

  // Extract appId from URL path: /api/apps/{appId}/preview/
  const pathMatch = window.location.pathname.match(/\/api\/apps\/([^/]+)\//);
  const appId = pathMatch?.[1] ?? "unknown";

  const baseUrl = window.location.origin;

  if (sessionId === "_dev_") {
    console.warn(
      "[digitorn/preview-sdk] No session_id in URL — using '_dev_' fallback. " +
      "Open via /api/apps/{appId}/preview/?session_id={id}&token={jwt}",
    );
  }

  return { appId, sessionId, token, baseUrl };
}

// ── Context ────────────────────────────────────────────────────────────

export const DigiPreviewContext = createContext<DigiPreviewContextValue | null>(null);

export function useDigiPreview(): DigiPreviewContextValue {
  const ctx = useContext(DigiPreviewContext);
  if (ctx === null) throw new Error("Hooks must be used inside <DigiPreview>");
  return ctx;
}

// Internal context: live Socket.IO instance + session info. Exposed so
// hooks that fire imperative actions (resolve_approval, send_message,
// etc.) emit on the SAME persistent connection rather than opening
// fresh HTTP rounds. Kept separate from ``DigiPreviewContext`` so the
// state-consumer hooks don't re-render when the socket reference
// changes (it doesn't, but the boundary is cleaner).

/** Registry of one-turn system-prompt contributors. Mutated imperatively
 *  by ``useTurnEnricher`` / ``usePendingHints``; drained by
 *  ``useChat().send()`` right before the socket emit. Ref-only — no
 *  re-renders. */
export type TurnEnricher = () => string | null | undefined;
export interface TurnEnrichmentRegistry {
  /** Register a functional enricher. Called once per ``send`` on each
   *  message. Returns an unregister function. */
  registerEnricher: (fn: TurnEnricher) => () => void;
  /** Push a hint into the one-shot queue. Drained on the next ``send``. */
  addHint: (text: string) => void;
  /** Internal: build the system_addendum string for this send, then
   *  clear the hint queue. Called by ``useChat().send()`` only. */
  _collectAndDrain: () => string;
}

export interface DigiPreviewSocketHandle {
  socket: Socket | null;
  session: SessionInfo;
  /** Per-DigiPreview turn-enrichment registry. Stable across renders. */
  turn: TurnEnrichmentRegistry;
}

export const DigiPreviewSocketContext =
  createContext<DigiPreviewSocketHandle | null>(null);

export function useDigiPreviewSocket(): DigiPreviewSocketHandle {
  const ctx = useContext(DigiPreviewSocketContext);
  if (ctx === null) {
    throw new Error("useDigiPreviewSocket must be used inside <DigiPreview>");
  }
  return ctx;
}

// ── Provider ───────────────────────────────────────────────────────────

export interface DigiPreviewProps {
  children: ReactNode;
  /** Override session info (useful for testing/Storybook) */
  session?: SessionInfo;
  maxReconnectMs?: number;
}

export function DigiPreview({ children, session: sessionProp, maxReconnectMs = 10_000 }: DigiPreviewProps) {
  const session = useMemo(() => sessionProp ?? readSession(), [sessionProp]);
  const [state, dispatch] = useReducer(reducer, initialState);
  const socketRef = useRef<Socket | null>(null);
  const seqRef = useRef(0);

  // Turn-enrichment registry — stable for the lifetime of this
  // ``DigiPreview`` instance. ``useTurnEnricher`` registers functional
  // contributors, ``usePendingHints`` queues stateful hints, and
  // ``useChat().send()`` drains both into ``system_addendum`` right
  // before emitting ``send_message`` over the socket.
  const turnRegistry = useMemo<TurnEnrichmentRegistry>(() => {
    const enrichers = new Set<TurnEnricher>();
    const hints: string[] = [];
    return {
      registerEnricher(fn: TurnEnricher) {
        enrichers.add(fn);
        return () => { enrichers.delete(fn); };
      },
      addHint(text: string) {
        const trimmed = text.trim();
        if (trimmed) hints.push(trimmed);
      },
      _collectAndDrain() {
        const parts: string[] = [];
        for (const fn of enrichers) {
          try {
            const out = fn();
            if (typeof out === "string" && out.trim()) {
              parts.push(out.trim());
            }
          } catch (err) {
            console.warn("[digitorn/preview-sdk] turn enricher threw:", err);
          }
        }
        if (hints.length > 0) {
          parts.push(...hints);
          hints.length = 0; // drain
        }
        return parts.join("\n\n");
      },
    };
  }, []);

  const [socketHandle, setSocketHandle] = useReducer(
    (_prev: DigiPreviewSocketHandle, next: DigiPreviewSocketHandle) => next,
    { socket: null, session, turn: turnRegistry },
  );

  useEffect(() => {
    let cancelled = false;
    const socket = createConnection(session, dispatch, seqRef, maxReconnectMs);
    socketRef.current = socket;
    setSocketHandle({ socket, session, turn: turnRegistry });

    // HTTP one-shot snapshot. The daemon used to emit ``preview:snapshot``
    // on Socket.IO ``join_session``, but moved that to the HTTP route
    // ``GET /sessions/{sid}/preview`` so the join is non-blocking.
    // Fetch it ourselves so a session reopen rehydrates the canvas
    // before any new agent action lands - otherwise the iframe is
    // empty until the next agent write.
    void (async () => {
      try {
        const headers: Record<string, string> = {};
        if (session.token) {
          headers["Authorization"] = `Bearer ${session.token}`;
        }
        const r = await fetch(
          `${session.baseUrl}/api/apps/${encodeURIComponent(session.appId)}` +
          `/sessions/${encodeURIComponent(session.sessionId)}/preview`,
          { headers },
        );
        if (!r.ok || cancelled) return;
        const body = (await r.json()) as Record<string, unknown>;
        const snap = (body.data ?? body);
        if (!cancelled) {
          dispatch({ type: "snapshot", payload: snap as any });
        }
      } catch (err) {
        // Daemon unreachable / 404 / etc. Socket deltas may still
        // hydrate later as the agent writes new state.
        console.debug("[digitorn/preview-sdk] http snapshot failed:", err);
      }
    })();

    return () => {
      cancelled = true;
      socket.disconnect();
      socketRef.current = null;
      setSocketHandle({ socket: null, session, turn: turnRegistry });
    };
  }, [session.appId, session.sessionId, session.baseUrl, session.token, maxReconnectMs, turnRegistry]);

  return createElement(
    DigiPreviewSocketContext.Provider,
    { value: socketHandle },
    createElement(DigiPreviewContext.Provider, { value: state }, children),
  );
}
