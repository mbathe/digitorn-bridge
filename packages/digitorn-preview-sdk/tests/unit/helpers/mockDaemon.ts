import { createServer, type Server as HttpServer } from "node:http";
import { AddressInfo } from "node:net";
import { Server as IoServer, type Socket as ServerSocket } from "socket.io";

/**
 * MockDaemon — a stand-in for the Digitorn daemon used by hook tests.
 *
 * Implements the exact contract the SDK consumes from ``connect.ts``:
 *
 *   - Socket.IO at ``http://127.0.0.1:<port>/events``
 *   - ``join_session`` ack
 *   - ``event`` envelopes ``{ type, seq, payload }``
 *   - HTTP one-shot ``GET /api/apps/:appId/sessions/:sid/preview`` →
 *     ``{ data: { state, resources } }``
 *
 * Tests script the daemon imperatively: spawn → wait for a join →
 * emit a sequence of events → assert React state. ``onClientEmit``
 * exposes the socket.io ack callback so tests can answer client
 * ``emit-with-ack`` calls (``send_message``, ``abort_turn``, etc).
 *
 * Lifecycle is per-test:
 *
 *   const daemon = await createMockDaemon();
 *   try { ... } finally { await daemon.close(); }
 */

export interface MockDaemonHandle {
  baseUrl: string;
  waitForJoin: () => Promise<JoinPayload>;
  waitForJoins: (count: number) => Promise<JoinPayload[]>;
  emit: (envelope: EventEnvelope) => void;
  emitBatch: (envelopes: EventEnvelopeInput[]) => void;
  setSnapshot: (snap: SnapshotPayload) => void;
  emitUserMessage: (content: string, opts?: { correlation_id?: string; pending?: boolean }) => void;
  /**
   * Register a handler for a client emit. The handler receives the
   * payload, the ack callback (or undefined when the client used
   * fire-and-forget), and the originating socket. Returns an unregister.
   */
  onClientEmit: (
    event: string,
    handler: (
      data: unknown,
      ack: ((response: unknown) => void) | undefined,
      socket: ServerSocket,
    ) => void,
  ) => () => void;
  dropClients: () => void;
  close: () => Promise<void>;
}

export interface JoinPayload {
  app_id?: string;
  session_id?: string;
  since?: number;
}

export interface EventEnvelope {
  type: string;
  seq: number;
  payload: Record<string, unknown>;
}

export interface EventEnvelopeInput {
  type: string;
  seq?: number;
  payload?: Record<string, unknown>;
}

export interface SnapshotPayload {
  state?: Record<string, unknown>;
  resources?: Record<string, Record<string, unknown>>;
  nodes?: unknown[];
  edges?: unknown[];
  events?: unknown[];
  seq?: number;
}

type ClientEmitHandler = (
  data: unknown,
  ack: ((response: unknown) => void) | undefined,
  socket: ServerSocket,
) => void;

export async function createMockDaemon(): Promise<MockDaemonHandle> {
  let snapshot: SnapshotPayload = { state: {}, resources: {} };

  const http = createServer((req, res) => {
    const corsHeaders: Record<string, string> = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };
    if (req.method === "OPTIONS") {
      res.writeHead(204, corsHeaders);
      res.end();
      return;
    }

    const previewMatch = req.url?.match(/^\/api\/apps\/[^/]+\/sessions\/[^/]+\/preview/);
    if (req.method === "GET" && previewMatch) {
      res.writeHead(200, { ...corsHeaders, "Content-Type": "application/json" });
      res.end(JSON.stringify({ data: snapshot }));
      return;
    }
    res.writeHead(404, corsHeaders);
    res.end();
  });

  const io = new IoServer(http, {
    cors: { origin: "*" },
    serveClient: false,
  });

  await new Promise<void>((resolve) => http.listen(0, "127.0.0.1", () => resolve()));
  const port = (http.address() as AddressInfo).port;

  const eventsNs = io.of("/events");
  const joins: JoinPayload[] = [];
  const joinListeners: Array<(j: JoinPayload) => void> = [];
  const clientEmitHandlers = new Map<string, Set<ClientEmitHandler>>();
  const sockets = new Set<ServerSocket>();
  let autoSeq = 0;

  function registerSocketListener(socket: ServerSocket, event: string) {
    // Register one socket.io listener per event-name per socket. The
    // ``ClientEmitHandler`` set is consulted at fire-time so tests can
    // add/remove handlers after the connection is up.
    socket.on(event, (...args: unknown[]) => {
      const last = args[args.length - 1];
      const ack = typeof last === "function" ? (last as (resp: unknown) => void) : undefined;
      const payload = ack ? args[0] : args[0];
      const handlers = clientEmitHandlers.get(event);
      if (!handlers || handlers.size === 0) return;
      for (const h of handlers) h(payload, ack, socket);
    });
  }

  eventsNs.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("disconnect", () => sockets.delete(socket));

    socket.on("join_session", (payload: JoinPayload, ack: (resp: object) => void) => {
      joins.push(payload);
      while (joinListeners.length > 0) {
        const next = joinListeners.shift();
        next?.(payload);
      }
      if (typeof ack === "function") {
        ack({ ok: true });
      }
    });

    // Wire every currently-known event name so tests that registered
    // ``onClientEmit`` before connection still receive the call.
    for (const event of clientEmitHandlers.keys()) {
      registerSocketListener(socket, event);
    }
  });

  function emit(env: EventEnvelope) {
    if (env.seq > autoSeq) autoSeq = env.seq;
    eventsNs.emit("event", env);
  }

  return {
    baseUrl: `http://127.0.0.1:${port}`,
    waitForJoin: () => {
      if (joins.length > 0) {
        return Promise.resolve(joins.shift()!);
      }
      return new Promise<JoinPayload>((resolve) => joinListeners.push(resolve));
    },
    waitForJoins: async (count) => {
      while (joins.length < count) {
        await new Promise<JoinPayload>((resolve) => joinListeners.push(resolve));
      }
      return joins.slice(0, count);
    },
    emit,
    emitBatch: (envelopes) => {
      for (const e of envelopes) {
        const seq = e.seq ?? ++autoSeq;
        emit({ type: e.type, seq, payload: e.payload ?? {} });
      }
    },
    setSnapshot: (snap) => {
      snapshot = snap;
    },
    emitUserMessage: (content, opts = {}) => {
      autoSeq += 1;
      emit({
        type: "user_message",
        seq: autoSeq,
        payload: {
          content,
          correlation_id: opts.correlation_id,
          pending: opts.pending ?? true,
        },
      });
    },
    onClientEmit: (event, handler) => {
      let set = clientEmitHandlers.get(event);
      const isNewEvent = !set;
      if (!set) {
        set = new Set();
        clientEmitHandlers.set(event, set);
      }
      set.add(handler);
      // Late-binding: if a socket connected before this handler was
      // registered, attach the listener now so the next emit reaches us.
      if (isNewEvent) {
        for (const sock of sockets) registerSocketListener(sock, event);
      }
      return () => {
        set?.delete(handler);
      };
    },
    dropClients: () => {
      for (const s of sockets) s.disconnect(true);
      sockets.clear();
    },
    close: async () => {
      io.close();
      await new Promise<void>((resolve) => http.close(() => resolve()));
    },
  };
}
