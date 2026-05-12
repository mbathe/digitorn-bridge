import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import { useChat } from "../../src/hooks/chat.js";
import type { UseChatApi } from "../../src/hooks/chat.js";
import type { ChatAssistantMessage, ChatUserMessage } from "../../src/types.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

/**
 * useChat against the real Socket.IO round-trip. Each test scripts
 * the daemon (ack ``send_message``, push the right event sequence)
 * and asserts the resulting hook state.
 */

interface ProbeHandle {
  api: UseChatApi | null;
}

function Probe({ handle }: { handle: ProbeHandle }) {
  const api = useChat();
  handle.api = api;
  return (
    <div>
      <span data-testid="busy">{api.busy ? "1" : "0"}</span>
      <span data-testid="status">{api.status}</span>
      <span data-testid="count">{api.messages.length}</span>
    </div>
  );
}

describe("useChat", () => {
  let daemon: MockDaemonHandle;
  let probe: ProbeHandle;

  beforeEach(async () => {
    daemon = await createMockDaemon();
    probe = { api: null };
  });

  afterEach(async () => {
    cleanup();
    await daemon.close();
  });

  async function mount() {
    const r = renderWithDaemon(daemon.baseUrl, <Probe handle={probe} />);
    await daemon.waitForJoin();
    await waitFor(() => expect(probe.api).toBeTruthy());
    return r;
  }

  it("send() round-trips through socket.io and returns the daemon ack", async () => {
    const received: Array<{ data: any }> = [];
    daemon.onClientEmit("send_message", (data, ack) => {
      received.push({ data });
      ack?.({ ok: true, correlation_id: "cid-42", position: 0, queue_depth: 1 });
    });

    await mount();

    const result = await probe.api!.send("hello world");
    expect(result.correlation_id).toBe("cid-42");
    expect(result.queue_depth).toBe(1);

    expect(received).toHaveLength(1);
    expect(received[0].data).toMatchObject({
      app_id: "test-app",
      message: "hello world",
    });
  });

  it("surfaces server error from the ack payload", async () => {
    daemon.onClientEmit("send_message", (_data, ack) => {
      ack?.({ ok: false, error: "quota exhausted" });
    });
    await mount();

    await expect(probe.api!.send("hi")).rejects.toThrow(/quota exhausted/);
    await waitFor(() => expect(probe.api!.error?.message).toMatch(/quota/));
  });

  it("delivers a full turn: user echo → thinking → tokens → turn_complete", async () => {
    daemon.onClientEmit("send_message", (_data, ack) => {
      ack?.({ ok: true, correlation_id: "cid-1" });
    });
    await mount();

    await probe.api!.send("explain X");

    // Simulate the daemon's event sequence for the turn.
    daemon.emitBatch([
      { type: "user_message", payload: { content: "explain X", correlation_id: "cid-1", pending: true } },
      { type: "thinking_started", payload: {} },
      { type: "thinking_delta", payload: { delta: "Let me think... " } },
      { type: "thinking_delta", payload: { delta: "OK." } },
      { type: "token", payload: { content: "Answer " } },
      { type: "token", payload: { content: "complete." } },
      { type: "turn_complete", payload: {} },
    ]);

    await waitFor(() => {
      const msgs = probe.api!.messages;
      expect(msgs).toHaveLength(2);
      const user = msgs[0] as ChatUserMessage;
      const ass = msgs[1] as ChatAssistantMessage;
      expect(user.content).toBe("explain X");
      expect(user.pending).toBe(false);
      expect(ass.content).toBe("Answer complete.");
      expect(ass.streaming).toBe(false);
      const thinking = ass.blocks.find((b) => b.type === "thinking") as any;
      expect(thinking?.content).toBe("Let me think... OK.");
      expect(thinking?.streaming).toBe(false);
    });
  });

  it("retry() re-sends the last user message", async () => {
    let sendCount = 0;
    const lastPayload: any[] = [];
    daemon.onClientEmit("send_message", (data, ack) => {
      sendCount += 1;
      lastPayload.push(data);
      ack?.({ ok: true, correlation_id: `cid-${sendCount}` });
    });
    await mount();

    await probe.api!.send("first");
    daemon.emit({
      type: "turn_complete",
      seq: 100,
      payload: {},
    });
    daemon.emitUserMessage("first", { correlation_id: "cid-1", pending: false });

    await waitFor(() => expect(probe.api!.messages.length).toBeGreaterThanOrEqual(1));

    const retried = await probe.api!.retry();
    expect(retried).not.toBeNull();
    expect(sendCount).toBe(2);
    expect(lastPayload[1].message).toBe("first");
  });

  it("retry() returns null when there is no user message in history", async () => {
    await mount();
    const r = await probe.api!.retry();
    expect(r).toBeNull();
  });

  it("abort() emits abort_turn and surfaces the daemon ack", async () => {
    daemon.onClientEmit("abort_turn", (data, ack) => {
      expect((data as any).purge_queue).toBe(true);
      ack?.({ ok: true, was_active: true, task_cancelled: true });
    });
    await mount();

    const r = await probe.api!.abort({ purgeQueue: true });
    expect(r.was_active).toBe(true);
    expect(r.task_cancelled).toBe(true);
  });

  it("busy reflects agent status while a turn streams", async () => {
    daemon.onClientEmit("send_message", (_d, ack) => ack?.({ ok: true, correlation_id: "x" }));
    await mount();

    await probe.api!.send("go");
    daemon.emit({ type: "token", seq: 1, payload: { content: "..." } });

    await waitFor(() => {
      expect(probe.api!.busy).toBe(true);
      expect(probe.api!.status).toBe("working");
    });

    daemon.emit({ type: "turn_complete", seq: 2, payload: {} });
    await waitFor(() => {
      expect(probe.api!.busy).toBe(false);
      expect(probe.api!.status).toBe("idle");
    });
  });

  it("dedupes optimistic + echoed user message by correlation_id", async () => {
    daemon.onClientEmit("send_message", (_d, ack) => {
      ack?.({ ok: true, correlation_id: "cid-dedup" });
    });
    await mount();

    await probe.api!.send("ping");
    daemon.emitUserMessage("ping", { correlation_id: "cid-dedup", pending: false });

    await waitFor(() => {
      expect(probe.api!.messages).toHaveLength(1);
      const u = probe.api!.messages[0] as ChatUserMessage;
      expect(u.pending).toBe(false);
    });
  });
});
