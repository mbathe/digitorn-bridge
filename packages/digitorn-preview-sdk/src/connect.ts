/**
 * Socket.IO connection logic — hidden from the developer.
 *
 * Rules (hard-learned):
 * - Always use transports: ["websocket"] — polling returns 400 on auth
 * - Token MUST be in the URL query string — extraHeaders crash in browsers
 * - Never use extraHeaders for WebSocket — browsers don't support custom WS headers
 * - The daemon's own origin (http://host:port) must be in CORS — auto-added since v1.1
 */

import { io, type Socket } from "socket.io-client";
import type { SessionInfo } from "./types.js";
import type { ReducerAction } from "./reducer.js";

type Dispatch = (action: ReducerAction) => void;

export function createConnection(
  session: SessionInfo,
  dispatch: Dispatch,
  seqRef: { current: number },
  maxReconnectMs: number,
): Socket {
  // Token in query param — only safe method for WebSocket in browsers
  let socketUrl = `${session.baseUrl}/events`;
  if (session.token) {
    socketUrl += `?token=${encodeURIComponent(session.token)}`;
  }

  const socket = io(socketUrl, {
    transports: ["websocket"],
    auth: session.token ? { token: session.token } : {},
    forceNew: true,
    reconnectionDelay: 500,
    reconnectionDelayMax: maxReconnectMs,
  });

  socket.on("connect", () => {
    dispatch({ type: "connected" });

    socket.emit(
      "join_session",
      { app_id: session.appId, session_id: session.sessionId, since: seqRef.current },
      (ack: Record<string, unknown>) => {
        if (ack && !ack.ok) {
          console.error("[digitorn/preview-sdk] join_session failed:", ack.error);
        }
      },
    );
  });

  socket.on("disconnect", () => dispatch({ type: "disconnected" }));

  socket.on("connect_error", (err: Error) => {
    console.error("[digitorn/preview-sdk] connect_error:", err.message);
    dispatch({ type: "disconnected" });
  });

  socket.on("event", (envelope: Record<string, unknown>) => {
    const eventType = envelope.type as string;
    const seq = envelope.seq as number;
    const payload = (envelope.payload ?? {}) as Record<string, unknown>;

    if (seq > seqRef.current) seqRef.current = seq;

    // ── Preview events ──────────────────────────────────────────
    if (eventType.startsWith("preview:")) {
      const previewType = eventType.slice("preview:".length);
      const previewSeq = (payload.preview_seq as number) ?? seq;

      if (previewType === "snapshot") {
        dispatch({ type: "snapshot", payload: payload as any });
      } else {
        dispatch({
          type: "preview_delta",
          payload: { seq: previewSeq, event_type: previewType, data: payload, timestamp: Date.now() },
        });
      }
      return;
    }

    // ── Agent events ────────────────────────────────────────────
    switch (eventType) {
      case "token":
      case "out_token":
        dispatch({ type: "agent_token", content: (payload.content as string) ?? "" });
        break;

      case "thinking":
      case "thinking_started":
        dispatch({ type: "agent_thinking" });
        break;

      case "tool_start":
        dispatch({
          type: "agent_tool_start",
          tool: (payload.tool as string) ?? "",
          params: (payload.params as Record<string, unknown>) ?? {},
        });
        break;

      case "tool_call":
        dispatch({
          type: "agent_tool_done",
          tool: (payload.tool as string) ?? "",
          result: (payload.result as Record<string, unknown>) ?? {},
        });
        break;

      case "turn_complete":
      case "stream_done":
        dispatch({ type: "agent_turn_complete" });
        break;

      case "abort":
        dispatch({ type: "agent_abort" });
        break;

      case "approval_request":
        dispatch({
          type: "approval_request",
          request: {
            request_id: payload.request_id as string,
            tool: payload.tool as string,
            params: (payload.params as Record<string, unknown>) ?? {},
          },
        });
        break;
    }
  });

  return socket;
}
