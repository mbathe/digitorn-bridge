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

  useEffect(() => {
    let cancelled = false;
    const socket = createConnection(session, dispatch, seqRef, maxReconnectMs);
    socketRef.current = socket;

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
    };
  }, [session.appId, session.sessionId, session.baseUrl, session.token, maxReconnectMs]);

  return createElement(DigiPreviewContext.Provider, { value: state }, children);
}
