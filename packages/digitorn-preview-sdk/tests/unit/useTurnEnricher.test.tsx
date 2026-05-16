import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import {
  usePendingHints,
  useTurnEnricher,
} from "../../src/hooks/index.js";
import { useChat } from "../../src/hooks/chat.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

/**
 * useTurnEnricher + usePendingHints — verify that the SDK collects
 * one-turn system-prompt fragments and ships them as ``system_addendum``
 * on the next ``send_message`` envelope. Drains the hint queue after.
 */

interface CapturedSend {
  message: string;
  system_addendum?: string;
}

interface Handle {
  send: (text: string) => Promise<void>;
  addHint: (text: string) => void;
}

function Harness({
  enrichers,
  handleRef,
}: {
  enrichers: Array<() => string | null>;
  handleRef: { current: Handle | null };
}) {
  for (const fn of enrichers) {
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useTurnEnricher(fn);
  }
  const { addHint } = usePendingHints();
  const chat = useChat();

  handleRef.current = {
    send: (text: string) => chat.send(text).then(() => undefined),
    addHint,
  };
  return null;
}

describe("useTurnEnricher + usePendingHints", () => {
  let daemon: MockDaemonHandle;
  let captured: CapturedSend[];

  beforeEach(async () => {
    daemon = await createMockDaemon();
    captured = [];
    daemon.onClientEmit("send_message", (payload, ack) => {
      const p = payload as Record<string, unknown>;
      captured.push({
        message: String(p.message ?? ""),
        system_addendum:
          typeof p.system_addendum === "string" ? p.system_addendum : undefined,
      });
      if (ack) {
        ack({ ok: true, accepted: true, correlation_id: "corr", position: 0, queue_depth: 1 });
      }
    });
  });

  afterEach(async () => {
    cleanup();
    await daemon.close();
  });

  async function mount(
    enrichers: Array<() => string | null>,
  ): Promise<Handle> {
    const handleRef = { current: null as Handle | null };
    renderWithDaemon(
      daemon.baseUrl,
      <Harness enrichers={enrichers} handleRef={handleRef} />,
    );
    await daemon.waitForJoin();
    await waitFor(() => expect(handleRef.current).not.toBeNull());
    return handleRef.current!;
  }

  it("ships an enricher's return value as system_addendum", async () => {
    const h = await mount([() => "fresh source X.md uploaded"]);
    await h.send("Brief me");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].message).toBe("Brief me");
    expect(captured[0].system_addendum).toBe("fresh source X.md uploaded");
  });

  it("omits system_addendum when no enricher contributes", async () => {
    const h = await mount([() => null, () => undefined as unknown as null]);
    await h.send("Hi");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBeUndefined();
  });

  it("joins multiple enrichers with a blank-line separator", async () => {
    const h = await mount([
      () => "line A",
      () => null,
      () => "line B",
    ]);
    await h.send("test");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("line A\n\nline B");
  });

  it("flushes pending hints into system_addendum on send", async () => {
    const h = await mount([]);
    h.addHint("hint 1");
    h.addHint("hint 2");
    await h.send("Q");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("hint 1\n\nhint 2");
  });

  it("drains the hint queue after one send", async () => {
    const h = await mount([]);
    h.addHint("one-shot hint");
    await h.send("first");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("one-shot hint");

    // No new hint pushed for the second send → queue is empty.
    await h.send("second");
    await waitFor(() => expect(captured.length).toBe(2));
    expect(captured[1].system_addendum).toBeUndefined();
  });

  it("re-runs enrichers on every send (not one-shot)", async () => {
    let counter = 0;
    const h = await mount([() => `count=${++counter}`]);
    await h.send("first");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("count=1");

    await h.send("second");
    await waitFor(() => expect(captured.length).toBe(2));
    expect(captured[1].system_addendum).toBe("count=2");
  });

  it("survives a throwing enricher (logs + continues)", async () => {
    const h = await mount([
      () => { throw new Error("boom"); },
      () => "still here",
    ]);
    await h.send("Q");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("still here");
  });

  it("combines enrichers AND hints in the same envelope", async () => {
    const h = await mount([() => "from-enricher"]);
    h.addHint("from-hint");
    await h.send("Q");
    await waitFor(() => expect(captured.length).toBe(1));
    expect(captured[0].system_addendum).toBe("from-enricher\n\nfrom-hint");
  });
});
