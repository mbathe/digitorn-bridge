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
      case "user_message": {
        // Daemon emits this when a user turn is queued (POST /messages
        // or socket ``send_message``). Used to drive the chat history
        // even if the iframe was launched mid-flight - the SDK adds
        // its own optimistic entry on ``send()``, so we de-dup by
        // ``correlation_id`` in the reducer.
        const content = (payload.content as string) ?? "";
        const images = Array.isArray(payload.images)
          ? (payload.images as unknown[]).map(String)
          : undefined;
        dispatch({
          type: "chat_user_message",
          content,
          images,
          correlation_id: (payload.correlation_id as string | undefined),
          pending: Boolean(payload.pending ?? true),
        });
        break;
      }

      case "token":
      case "out_token":
        dispatch({ type: "agent_token", content: (payload.content as string) ?? "" });
        break;

      case "thinking_started":
        // Open a typed thinking block at the current tail. The
        // ``agent_thinking_started`` reducer also flips ``agentStatus``
        // to "thinking" for status-only consumers.
        dispatch({ type: "agent_thinking_started" });
        break;

      case "thinking_delta": {
        const delta = (payload.delta as string)
          ?? (payload.content as string)
          ?? "";
        const tokens = typeof payload.count === "number"
          ? payload.count : undefined;
        if (delta) {
          dispatch({ type: "agent_thinking_delta", delta, tokens });
        }
        break;
      }

      case "thinking":
        // Final thinking summary (full block + token count). We've
        // already streamed every delta, so just keep the status flag
        // so the UI shows "Reasoning..." until token / turn_complete.
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

      case "approval_request": {
        // The daemon's ``ApprovalRequest.to_dict()`` carries
        // ``tool_name`` / ``tool_params`` (canonical) plus risk + desc
        // + identity fields. Older SDK code read ``tool`` / ``params``
        // - we mirror them so the legacy fields keep working.
        const toolName =
          (payload.tool_name as string | undefined) ??
          (payload.tool as string | undefined) ??
          "";
        const toolParams =
          (payload.tool_params as Record<string, unknown> | undefined) ??
          (payload.params as Record<string, unknown> | undefined) ??
          {};
        dispatch({
          type: "approval_request",
          request: {
            request_id: (payload.request_id as string) ?? "",
            tool_name: toolName,
            tool_params: toolParams,
            risk_level: (payload.risk_level as string) ?? "medium",
            description: (payload.description as string) ?? "",
            agent_id: (payload.agent_id as string) ?? "",
            user_id: (payload.user_id as string) ?? "",
            app_id: (payload.app_id as string) ?? "",
            session_id: (payload.session_id as string) ?? "",
            created_at: Number(payload.created_at ?? 0),
            // legacy aliases
            tool: toolName,
            params: toolParams,
          },
        });
        break;
      }

      case "approval_resolved":
        dispatch({
          type: "approval_resolved",
          request_id: (payload.request_id as string | undefined),
        });
        break;
    }
  });

  return socket;
}
