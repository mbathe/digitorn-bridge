import { describe, expect, it } from "vitest";
import { initialState, reducer, type ReducerAction } from "../../src/reducer.js";
import type {
  ApprovalRequest,
  ChatAssistantMessage,
  ChatUserMessage,
  DigiPreviewContextValue,
  PreviewSnapshot,
} from "../../src/types.js";

/**
 * Reducer regression suite.
 *
 * The reducer is the single source of truth for every state mutation
 * triggered by the daemon. Every Socket.IO event in ``connect.ts``
 * ends up here. A bug here corrupts the rendered UI silently, so the
 * coverage target is "every action × every relevant precondition".
 */

function run(actions: ReducerAction[], from: DigiPreviewContextValue = initialState) {
  return actions.reduce(reducer, from);
}

function snapshot(partial: Partial<PreviewSnapshot> = {}): PreviewSnapshot {
  return {
    state: {},
    resources: {},
    nodes: [],
    edges: [],
    events: [],
    seq: 0,
    ...partial,
  } as PreviewSnapshot;
}

describe("connection lifecycle", () => {
  it("flips connected on connect/disconnect", () => {
    const a = reducer(initialState, { type: "connected" });
    expect(a.connected).toBe(true);
    const b = reducer(a, { type: "disconnected" });
    expect(b.connected).toBe(false);
  });
});

describe("snapshot hydration", () => {
  it("seeds resources from a flat snapshot.resources map", () => {
    const s = reducer(initialState, {
      type: "snapshot",
      payload: snapshot({
        state: { entry_file: "src/main.tsx" },
        resources: {
          files: {
            "src/main.tsx": { content: "export {}", status: "added" },
          },
        },
        seq: 7,
      }),
    });
    expect(s.connected).toBe(true);
    expect(s.state.entry_file).toBe("src/main.tsx");
    expect(s.resources.get("files")?.get("src/main.tsx")).toMatchObject({
      content: "export {}",
      status: "added",
    });
    expect(s.seq).toBe(7);
  });

  it("derives 'nodes' channel from legacy snapshot.nodes when resources empty", () => {
    const s = reducer(initialState, {
      type: "snapshot",
      payload: snapshot({
        nodes: [{ id: "n1", label: "x" } as any],
        edges: [{ id: "e1", source: "n1", target: "n2" } as any],
      }),
    });
    expect(s.resources.get("nodes")?.get("n1")).toMatchObject({ id: "n1", label: "x" });
    expect(s.resources.get("edges")?.get("e1")).toMatchObject({ id: "e1" });
  });

  it("preserves seq when snapshot omits it (HTTP one-shot route)", () => {
    const start = reducer(initialState, {
      type: "preview_delta",
      payload: { seq: 42, event_type: "state_changed", data: { key: "k", value: "v" }, timestamp: 0 },
    });
    expect(start.seq).toBe(42);
    const s = reducer(start, { type: "snapshot", payload: { state: {}, resources: {} } as any });
    expect(s.seq).toBe(42);
  });
});

describe("preview_delta", () => {
  const seeded = run([{ type: "connected" }, { type: "snapshot", payload: snapshot({ seq: 0 }) }]);

  it("ignores deltas with seq <= state.seq (out-of-order replay)", () => {
    const s1 = reducer(seeded, {
      type: "preview_delta",
      payload: { seq: 5, event_type: "state_changed", data: { key: "k", value: 1 }, timestamp: 0 },
    });
    const s2 = reducer(s1, {
      type: "preview_delta",
      payload: { seq: 3, event_type: "state_changed", data: { key: "k", value: 99 }, timestamp: 0 },
    });
    expect(s2.state.k).toBe(1);
    expect(s2.seq).toBe(5);
  });

  it("state_patched merges patch shallowly", () => {
    const s = reducer(seeded, {
      type: "preview_delta",
      payload: {
        seq: 1,
        event_type: "state_patched",
        data: { patch: { entry_file: "src/App.tsx", theme: "dark" } },
        timestamp: 0,
      },
    });
    expect(s.state).toEqual({ entry_file: "src/App.tsx", theme: "dark" });
  });

  it("resource_set adds a file to the 'files' channel", () => {
    const s = reducer(seeded, {
      type: "preview_delta",
      payload: {
        seq: 1,
        event_type: "resource_set",
        data: {
          channel: "files",
          id: "src/main.tsx",
          payload: { content: "export {}", status: "added" },
        },
        timestamp: 0,
      },
    });
    expect(s.resources.get("files")?.get("src/main.tsx")).toMatchObject({ status: "added" });
  });

  it("resource_deleted is a no-op when the id isn't present (no spurious channel creation)", () => {
    const s = reducer(seeded, {
      type: "preview_delta",
      payload: {
        seq: 1,
        event_type: "resource_deleted",
        data: { channel: "files", id: "ghost.tsx" },
        timestamp: 0,
      },
    });
    expect(s.resources.has("files")).toBe(false);
  });

  it("resource_bulk_set with replace=true wipes the channel first", () => {
    const s1 = reducer(seeded, {
      type: "preview_delta",
      payload: {
        seq: 1,
        event_type: "resource_set",
        data: { channel: "files", id: "old.tsx", payload: { content: "old" } },
        timestamp: 0,
      },
    });
    const s2 = reducer(s1, {
      type: "preview_delta",
      payload: {
        seq: 2,
        event_type: "resource_bulk_set",
        data: {
          channel: "files",
          items: { "new.tsx": { content: "new" } },
          replace: true,
        },
        timestamp: 0,
      },
    });
    expect(s2.resources.get("files")?.has("old.tsx")).toBe(false);
    expect(s2.resources.get("files")?.has("new.tsx")).toBe(true);
  });

  it("'cleared' wipes both state and resources", () => {
    const s1 = run([
      {
        type: "preview_delta",
        payload: {
          seq: 1,
          event_type: "state_changed",
          data: { key: "k", value: 1 },
          timestamp: 0,
        },
      },
      {
        type: "preview_delta",
        payload: {
          seq: 2,
          event_type: "resource_set",
          data: { channel: "files", id: "f.tsx", payload: {} },
          timestamp: 0,
        },
      },
    ], seeded);
    const s2 = reducer(s1, {
      type: "preview_delta",
      payload: { seq: 3, event_type: "cleared", data: {}, timestamp: 0 },
    });
    expect(s2.state).toEqual({});
    expect(s2.resources.size).toBe(0);
  });

  it("event ring buffer caps at 500", () => {
    let s = seeded;
    for (let i = 1; i <= 510; i += 1) {
      s = reducer(s, {
        type: "preview_delta",
        payload: { seq: i, event_type: "state_changed", data: { key: "x", value: i }, timestamp: 0 },
      });
    }
    expect(s.events.length).toBe(500);
    expect(s.events[0].seq).toBe(11);
    expect(s.events[499].seq).toBe(510);
  });
});

describe("agent streaming", () => {
  it("creates a streaming assistant tail and accumulates text deltas", () => {
    const s = run([
      { type: "agent_token", content: "Hel" },
      { type: "agent_token", content: "lo " },
      { type: "agent_token", content: "world" },
    ]);
    expect(s.agentStatus).toBe("working");
    expect(s.agentStream).toBe("Hello world");
    expect(s.chatMessages).toHaveLength(1);
    const tail = s.chatMessages[0] as ChatAssistantMessage;
    expect(tail.role).toBe("assistant");
    expect(tail.streaming).toBe(true);
    expect(tail.content).toBe("Hello world");
    expect(tail.blocks).toHaveLength(1);
    expect(tail.blocks[0]).toMatchObject({ type: "text", content: "Hello world", streaming: true });
  });

  it("thinking_started opens a thinking block before any text", () => {
    const s = run([
      { type: "agent_thinking_started" },
      { type: "agent_thinking_delta", delta: "Let me think... ", tokens: 5 },
      { type: "agent_thinking_delta", delta: "OK.", tokens: 12 },
    ]);
    const tail = s.chatMessages[0] as ChatAssistantMessage;
    expect(s.agentStatus).toBe("thinking");
    expect(tail.blocks).toHaveLength(1);
    expect(tail.blocks[0]).toMatchObject({
      type: "thinking",
      content: "Let me think... OK.",
      streaming: true,
      tokens: 12,
    });
  });

  it("tool_start then tool_done resolves the matching tool_use block", () => {
    const s = run([
      { type: "agent_token", content: "Reading file..." },
      { type: "agent_tool_start", tool: "Read", params: { path: "a.ts" } },
      { type: "agent_tool_done", tool: "Read", result: { content: "ok" } },
    ]);
    const tail = s.chatMessages[0] as ChatAssistantMessage;
    const toolBlock = tail.blocks.find((b) => b.type === "tool_use") as any;
    expect(toolBlock.status).toBe("done");
    expect(toolBlock.result).toEqual({ content: "ok" });
    expect(s.toolCalls).toHaveLength(1);
    expect(s.toolCalls[0].result).toEqual({ content: "ok" });
  });

  it("tool_done marks the block error when result has an 'error' key", () => {
    const s = run([
      { type: "agent_tool_start", tool: "Read", params: { path: "x" } },
      { type: "agent_tool_done", tool: "Read", result: { error: "boom" } },
    ]);
    const tail = s.chatMessages[0] as ChatAssistantMessage;
    const tb = tail.blocks.find((b) => b.type === "tool_use") as any;
    expect(tb.status).toBe("error");
  });

  it("turn_complete freezes streaming + settles the latest pending user msg", () => {
    const s = run([
      { type: "chat_user_message", content: "hi", correlation_id: "cid-1", pending: true },
      { type: "agent_token", content: "yo" },
      { type: "agent_turn_complete" },
    ]);
    expect(s.agentStatus).toBe("idle");
    expect(s.agentStream).toBe("");
    const [user, assistant] = s.chatMessages as [ChatUserMessage, ChatAssistantMessage];
    expect(user.pending).toBe(false);
    expect(assistant.streaming).toBe(false);
    expect(assistant.blocks[0]).toMatchObject({ streaming: false });
  });

  it("abort drops the streaming tail (partial blocks would mislead)", () => {
    const s = run([
      { type: "chat_user_message", content: "hi", pending: true },
      { type: "agent_token", content: "partial" },
      { type: "agent_abort" },
    ]);
    expect(s.chatMessages).toHaveLength(1);
    expect(s.chatMessages[0].role).toBe("user");
    expect(s.agentStatus).toBe("idle");
  });
});

describe("approvals", () => {
  const req = (id: string, tool = "Write"): ApprovalRequest => ({
    request_id: id,
    tool_name: tool,
    tool_params: { path: "x" },
    risk_level: "medium",
    description: "",
    agent_id: "",
    user_id: "",
    app_id: "",
    session_id: "",
    created_at: 0,
    tool,
    params: { path: "x" },
  });

  it("queues requests and exposes the head as approvalRequest", () => {
    const s = run([
      { type: "approval_request", request: req("r1") },
      { type: "approval_request", request: req("r2") },
    ]);
    expect(s.approvals).toHaveLength(2);
    expect(s.approvalRequest?.request_id).toBe("r1");
  });

  it("dedupes by request_id when the same request re-emits", () => {
    const s = run([
      { type: "approval_request", request: req("r1") },
      { type: "approval_request", request: req("r1") },
    ]);
    expect(s.approvals).toHaveLength(1);
  });

  it("resolved with explicit id removes the matching entry only", () => {
    const s1 = run([
      { type: "approval_request", request: req("r1") },
      { type: "approval_request", request: req("r2") },
    ]);
    const s2 = reducer(s1, { type: "approval_resolved", request_id: "r1" });
    expect(s2.approvals).toHaveLength(1);
    expect(s2.approvals[0].request_id).toBe("r2");
    expect(s2.approvalRequest?.request_id).toBe("r2");
  });

  it("resolved without id drops the head (legacy fallback)", () => {
    const s1 = run([
      { type: "approval_request", request: req("r1") },
      { type: "approval_request", request: req("r2") },
    ]);
    const s2 = reducer(s1, { type: "approval_resolved" });
    expect(s2.approvals.map((r) => r.request_id)).toEqual(["r2"]);
  });
});

describe("chat user-message dedup", () => {
  it("merges optimistic + daemon echo by correlation_id", () => {
    const s = run([
      { type: "chat_user_message", content: "ping", correlation_id: "cid-a", pending: true },
      // Daemon echo: same cid, server-decided pending=false
      { type: "chat_user_message", content: "ping", correlation_id: "cid-a", pending: false },
    ]);
    expect(s.chatMessages).toHaveLength(1);
    const u = s.chatMessages[0] as ChatUserMessage;
    expect(u.pending).toBe(false);
  });

  it("appends two distinct entries when correlation_ids differ", () => {
    const s = run([
      { type: "chat_user_message", content: "a", correlation_id: "cid-1" },
      { type: "chat_user_message", content: "b", correlation_id: "cid-2" },
    ]);
    expect(s.chatMessages.map((m) => (m as ChatUserMessage).content)).toEqual(["a", "b"]);
  });

  it("appends without merging when no correlation_id", () => {
    const s = run([
      { type: "chat_user_message", content: "a" },
      { type: "chat_user_message", content: "a" },
    ]);
    expect(s.chatMessages).toHaveLength(2);
  });
});
