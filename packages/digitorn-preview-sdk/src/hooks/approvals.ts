import { useCallback, useState } from "react";
import { useDigiPreview, useDigiPreviewSocket } from "../DigiPreview.js";
import type { ApprovalRequest } from "../types.js";

// ── Internal helpers ──────────────────────────────────────────────────

interface ResolveAck {
  ok: boolean;
  request_id?: string;
  approved?: boolean;
  payload_received?: boolean;
  error?: string;
}

/** Promise wrapper around ``socket.emit`` with an ack callback. We
 *  could use ``socket.emitWithAck`` (socket.io-client v4.7+), but
 *  the manual pattern keeps the SDK runnable on older clients that
 *  forks of the package may have pinned. */
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

export interface UseApprovalsApi {
  /** All approval requests waiting for THIS user. Updated live via
   *  Socket.IO (``approval_request`` / ``approval_resolved``). */
  pending: ApprovalRequest[];
  /** Approve a request by id. ``message`` is the optional payload the
   *  daemon forwards to the awaiting tool call (interpreted as the
   *  user's reply for ``ask_user``-style requests, or a free-form
   *  reason for plain tool approvals). Resolves to the daemon's
   *  acknowledgement; throws on failure. */
  approve: (requestId: string, message?: string) => Promise<ResolveAck>;
  /** Deny a request by id. Same payload semantics as ``approve``. */
  reject: (requestId: string, message?: string) => Promise<ResolveAck>;
  /** True while any of the imperatives above is in flight. Useful to
   *  disable the modal's buttons during a round-trip. */
  busy: boolean;
  /** Last error from ``approve`` / ``reject``, cleared on the next
   *  successful call. */
  error: Error | null;
}

/**
 * Read the live pending-approvals queue and resolve requests from the
 * iframe. Pairs with ``security.behavior`` and ``capabilities`` policy
 * `approve` so the user (or a custom UI) can gate every risky tool call.
 *
 * Pending state is fed by the ``approval_request`` /
 * ``approval_resolved`` Socket.IO events, so an iframe that joins after
 * a request was already queued catches up automatically through the
 * session bus replay (``join_session`` with ``since: seq``).
 *
 * **Resolution travels over the SAME WebSocket** that delivered the
 * pending request (``resolve_approval`` Socket.IO action). The daemon
 * then resolves the awaiting tool's future on the server side -
 * symmetric IO, no second HTTP round-trip, no extra JWT verification.
 *
 * ```tsx
 * const { pending, approve, reject, busy } = useApprovals();
 *
 * return pending.map(req => (
 *   <div key={req.request_id}>
 *     <p><strong>{req.tool_name}</strong> {req.description}</p>
 *     <pre>{JSON.stringify(req.tool_params, null, 2)}</pre>
 *     <button disabled={busy}
 *             onClick={() => approve(req.request_id)}>Approve</button>
 *     <button disabled={busy}
 *             onClick={() => reject(req.request_id, "looks risky")}>
 *       Reject
 *     </button>
 *   </div>
 * ));
 * ```
 */
export function useApprovals(): UseApprovalsApi {
  const ctx = useDigiPreview();
  const { socket, session } = useDigiPreviewSocket();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const _resolve = useCallback(async (
    requestId: string, approved: boolean, message?: string,
  ): Promise<ResolveAck> => {
    if (!socket) {
      throw new Error("Socket.IO not connected - call again once connected.");
    }
    setBusy(true);
    try {
      const ack = await _emitAck<ResolveAck>(
        (ev, p, cb) => socket.emit(ev, p, cb),
        "resolve_approval",
        {
          app_id: session.appId,
          request_id: requestId,
          approved,
          message: message ?? "",
        },
        10_000,
      );
      if (!ack || ack.ok === false) {
        throw new Error(ack?.error || "approval resolve refused");
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
  }, [socket, session.appId]);

  const approve = useCallback(
    (requestId: string, message?: string) => _resolve(requestId, true, message),
    [_resolve],
  );
  const reject = useCallback(
    (requestId: string, message?: string) => _resolve(requestId, false, message),
    [_resolve],
  );

  return {
    pending: ctx.approvals,
    approve,
    reject,
    busy,
    error,
  };
}
