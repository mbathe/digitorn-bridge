import { useCallback, useState } from "react";
import { useDigiPreview, useDigiPreviewSocket } from "../DigiPreview.js";
import type { AgentStatus, ChatMessage } from "../types.js";

// ── Internal helpers ──────────────────────────────────────────────────

interface SendAck {
  ok: boolean;
  accepted?: boolean;
  correlation_id?: string;
  position?: number;
  queue_depth?: number;
  error?: string;
}

interface AbortAck {
  ok: boolean;
  was_active?: boolean;
  task_cancelled?: boolean;
  pending_approvals_cancelled?: number;
  queue_purged?: number;
  error?: string;
}

function _emitAck<T>(
  emit: (event: string, payload: unknown, cb: (resp: T) => void) => void,
  event: string,
  payload: unknown,
  timeoutMs: number,
): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`socket emit '${event}' timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    emit(event, payload, (resp: T) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(resp);
    });
  });
}

// ── Public API ────────────────────────────────────────────────────────

export interface ChatImageInput {
  /** Base64-encoded image bytes (without the ``data:`` prefix). */
  data: string;
  /** MIME type, e.g. ``image/png``. Defaults to ``image/png`` daemon-side. */
  mime?: string;
  /** Display name for the alt-text + tool prompt. */
  name?: string;
}

export interface ChatSendOptions {
  /** Inline images to attach to the user message. The daemon stores
   *  them, hydrates them in the LLM payload as vision parts (when
   *  the brain supports it), and ages them across turns. */
  images?: ChatImageInput[];
  /** ``"async"`` (default) returns immediately with a correlation id
   *  while the turn runs in the background. ``"wait"`` blocks the ack
   *  until the turn produces its first event - useful in tests. */
  queue_mode?: "async" | "wait";
  /** Idempotency key the daemon uses to deduplicate retries. */
  client_message_id?: string;
}

export interface ChatSendResult {
  correlation_id: string;
  /** Position in the per-session queue (0 = running now). */
  position: number;
  /** Total queue depth after this enqueue. */
  queue_depth: number;
}

export interface ChatAbortOptions {
  /** ``true`` drops every queued message in addition to cancelling the
   *  running turn. Default ``false`` (cancel only the current turn,
   *  keep the queue draining). */
  purgeQueue?: boolean;
}

export interface UseChatApi {
  /** Live chat history (user / assistant / tool messages). Updates as
   *  events arrive on Socket.IO. */
  messages: ChatMessage[];
  /** Send a message. Returns the daemon's correlation id + queue
   *  position. The optimistic user entry lands in ``messages``
   *  immediately - de-duped against the ``user_message`` event when
   *  the daemon broadcasts it. */
  send: (text: string, opts?: ChatSendOptions) => Promise<ChatSendResult>;
  /** Cancel the running turn. Default keeps queued messages so the
   *  next one drains automatically. */
  abort: (opts?: ChatAbortOptions) => Promise<AbortAck>;
  /** Re-send the last user message. No-op when no user message is
   *  in history yet. */
  retry: (opts?: ChatSendOptions) => Promise<ChatSendResult | null>;
  /** True while a ``send`` ack is in flight or the agent is producing
   *  tokens / calling tools. */
  busy: boolean;
  /** Coarse agent state - drives loading indicators. */
  status: AgentStatus;
  /** Accumulating text of the current turn. Same value as
   *  ``useAgentStream()``; mirrored here so a single hook covers the
   *  classic "typing indicator + transcript" UI. */
  stream: string;
  /** Last error from ``send`` / ``abort``, cleared on the next
   *  successful call. */
  error: Error | null;
}

/**
 * Drive the chat conversation from the iframe. Send messages, cancel
 * runs, retry, and read the live transcript - all over the same
 * Socket.IO connection that delivers the agent's tokens.
 *
 * The hook is intentionally thin: state lives in the SDK reducer and
 * is fed by the daemon's session events (``user_message``, ``token``,
 * ``tool_call``, ``turn_complete``, ``abort``). The imperatives map 1:1
 * to Socket.IO actions:
 *
 * - ``send``  → ``send_message``  (daemon enqueues, runs turn)
 * - ``abort`` → ``abort_turn``    (cancels current task + approvals)
 * - ``retry`` → looks up the last user message, calls ``send``
 *
 * ```tsx
 * function ChatPanel() {
 *   const chat = useChat();
 *   const [draft, setDraft] = useState("");
 *
 *   return (
 *     <div>
 *       <ul>
 *         {chat.messages.map((m, i) => (
 *           <li key={i} data-role={m.role}>
 *             {m.role === "user" ? "🙋 " : m.role === "assistant" ? "🤖 " : "🔧 "}
 *             {m.content}
 *             {m.role === "assistant" && m.streaming && <span>▍</span>}
 *           </li>
 *         ))}
 *       </ul>
 *       <form onSubmit={async e => {
 *         e.preventDefault();
 *         if (!draft.trim() || chat.busy) return;
 *         await chat.send(draft);
 *         setDraft("");
 *       }}>
 *         <input value={draft} onChange={e => setDraft(e.target.value)} />
 *         <button type="submit" disabled={chat.busy}>Send</button>
 *         {chat.busy && (
 *           <button type="button" onClick={() => chat.abort()}>Stop</button>
 *         )}
 *       </form>
 *     </div>
 *   );
 * }
 * ```
 */
export function useChat(): UseChatApi {
  const ctx = useDigiPreview();
  const { socket, session } = useDigiPreviewSocket();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const send = useCallback(async (
    text: string, opts?: ChatSendOptions,
  ): Promise<ChatSendResult> => {
    if (!socket) {
      throw new Error("Socket.IO not connected - call again once connected.");
    }
    setBusy(true);
    try {
      const ack = await _emitAck<SendAck>(
        (ev, p, cb) => socket.emit(ev, p, cb),
        "send_message",
        {
          app_id: session.appId,
          session_id: session.sessionId,
          message: text,
          images: opts?.images,
          queue_mode: opts?.queue_mode,
          client_message_id: opts?.client_message_id,
        },
        20_000,
      );
      if (!ack || ack.ok === false) {
        throw new Error(ack?.error || "send_message refused");
      }
      setError(null);
      return {
        correlation_id: ack.correlation_id ?? "",
        position: ack.position ?? 0,
        queue_depth: ack.queue_depth ?? 0,
      };
    } catch (exc) {
      const err = exc instanceof Error ? exc : new Error(String(exc));
      setError(err);
      throw err;
    } finally {
      setBusy(false);
    }
  }, [socket, session.appId, session.sessionId]);

  const abort = useCallback(async (
    opts?: ChatAbortOptions,
  ): Promise<AbortAck> => {
    if (!socket) {
      throw new Error("Socket.IO not connected.");
    }
    setBusy(true);
    try {
      const ack = await _emitAck<AbortAck>(
        (ev, p, cb) => socket.emit(ev, p, cb),
        "abort_turn",
        {
          app_id: session.appId,
          session_id: session.sessionId,
          purge_queue: opts?.purgeQueue ?? false,
        },
        10_000,
      );
      if (!ack || ack.ok === false) {
        throw new Error(ack?.error || "abort_turn refused");
      }
      setError(null);
      return ack;
    } catch (exc) {
      const err = exc instanceof Error ? exc : new Error(String(exc));
      setError(err);
      throw err;
    } finally {
      setBusy(false);
    }
  }, [socket, session.appId, session.sessionId]);

  const retry = useCallback(async (
    opts?: ChatSendOptions,
  ): Promise<ChatSendResult | null> => {
    // Find the most recent user message and replay it. Keeps tool /
    // assistant entries untouched - the daemon will see a fresh user
    // turn and decide what to do.
    for (let i = ctx.chatMessages.length - 1; i >= 0; i -= 1) {
      const m = ctx.chatMessages[i];
      if (m.role === "user") {
        return send(m.content, opts);
      }
    }
    return null;
  }, [ctx.chatMessages, send]);

  // ``busy`` reflects two states: SDK round-trip in progress AND
  // agent producing tokens / calling tools. This single boolean is
  // what most chat UIs gate their "Send" button on.
  const liveBusy = busy
    || ctx.agentStatus === "thinking"
    || ctx.agentStatus === "working";

  return {
    messages: ctx.chatMessages,
    send,
    abort,
    retry,
    busy: liveBusy,
    status: ctx.agentStatus,
    stream: ctx.agentStream,
    error,
  };
}
