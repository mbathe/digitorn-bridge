import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import {
  useResourceEvents,
  useResourceLifecycle,
  type ResourceEvent,
  type UseResourceLifecycleOptions,
} from "../../src/hooks/index.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

/**
 * useResourceLifecycle + useResourceEvents — channel-agnostic create /
 * update / delete callbacks driven by the real Socket.IO round-trip.
 */

interface LogEntry {
  kind: "create" | "update" | "delete";
  id: string;
}

function LifecycleProbe({
  log,
  options,
}: {
  log: LogEntry[];
  options: Omit<UseResourceLifecycleOptions<{ content?: string }>, "onCreate" | "onUpdate" | "onDelete">;
}) {
  useResourceLifecycle<{ content?: string }>({
    ...options,
    onCreate: (e) => log.push({ kind: "create", id: e.id }),
    onUpdate: (e) => log.push({ kind: "update", id: e.id }),
    onDelete: (e) => log.push({ kind: "delete", id: e.id }),
  });
  return null;
}

function EventsProbe({
  sink,
  channel,
  match,
  maxBuffer,
  fireForInitial,
}: {
  sink: { value: ResourceEvent[] };
  channel: string;
  match?: string | ((id: string) => boolean);
  maxBuffer?: number;
  fireForInitial?: boolean;
}) {
  const events = useResourceEvents({ channel, match, maxBuffer, fireForInitial });
  sink.value = events;
  return null;
}

describe("useResourceLifecycle", () => {
  let daemon: MockDaemonHandle;
  let log: LogEntry[];

  beforeEach(async () => {
    daemon = await createMockDaemon();
    log = [];
  });

  afterEach(async () => {
    cleanup();
    await daemon.close();
  });

  it("fires onCreate for resources in the initial snapshot", async () => {
    daemon.setSnapshot({
      resources: {
        files: {
          "a.md": { content: "a" },
          "b.md": { content: "b" },
        },
      },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "files" }} />,
    );
    await daemon.waitForJoin();

    await waitFor(() => {
      expect(log).toEqual([
        { kind: "create", id: "a.md" },
        { kind: "create", id: "b.md" },
      ]);
    });
  });

  it("skips initial fire when fireForInitial is false", async () => {
    daemon.setSnapshot({
      resources: { files: { "a.md": { content: "a" } } },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "files", fireForInitial: false }} />,
    );
    await daemon.waitForJoin();

    // Give the hydration round-trip room to land.
    await new Promise((r) => setTimeout(r, 50));
    expect(log).toEqual([]);

    // But subsequent CREATEs still fire.
    daemon.emit({
      type: "preview:resource_set",
      seq: 1,
      payload: { channel: "files", id: "b.md", payload: { content: "b" } },
    });
    await waitFor(() => {
      expect(log).toEqual([{ kind: "create", id: "b.md" }]);
    });
  });

  it("fires onCreate on a live resource_set delta", async () => {
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "files", fireForInitial: false }} />,
    );
    await daemon.waitForJoin();

    daemon.emit({
      type: "preview:resource_set",
      seq: 1,
      payload: { channel: "files", id: "fresh.md", payload: { content: "x" } },
    });

    await waitFor(() => {
      expect(log).toEqual([{ kind: "create", id: "fresh.md" }]);
    });
  });

  it("fires onUpdate when the payload changes for an existing id", async () => {
    daemon.setSnapshot({
      resources: { files: { "doc.md": { content: "v1" } } },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "files", fireForInitial: false }} />,
    );
    await daemon.waitForJoin();
    await new Promise((r) => setTimeout(r, 50));

    daemon.emit({
      type: "preview:resource_set",
      seq: 1,
      payload: { channel: "files", id: "doc.md", payload: { content: "v2" } },
    });

    await waitFor(() => {
      expect(log).toEqual([{ kind: "update", id: "doc.md" }]);
    });
  });

  it("fires onDelete on resource_deleted", async () => {
    daemon.setSnapshot({
      resources: { files: { "doc.md": { content: "v1" } } },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "files", fireForInitial: false }} />,
    );
    await daemon.waitForJoin();
    await new Promise((r) => setTimeout(r, 50));

    daemon.emit({
      type: "preview:resource_deleted",
      seq: 1,
      payload: { channel: "files", id: "doc.md" },
    });

    await waitFor(() => {
      expect(log).toEqual([{ kind: "delete", id: "doc.md" }]);
    });
  });

  it("restricts callbacks via a prefix match", async () => {
    daemon.setSnapshot({
      resources: {
        files: {
          "audio_overview/turn_001.mp3": { content: "" },
          "audio_overview/turn_002.mp3": { content: "" },
          "briefing.md": { content: "" },
          "sources/foo.md": { content: "" },
        },
      },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe
        log={log}
        options={{ channel: "files", match: "audio_overview/" }}
      />,
    );
    await daemon.waitForJoin();

    await waitFor(() => {
      expect(log.map((e) => e.id).sort()).toEqual([
        "audio_overview/turn_001.mp3",
        "audio_overview/turn_002.mp3",
      ]);
    });
  });

  it("restricts callbacks via a glob match", async () => {
    daemon.setSnapshot({
      resources: {
        files: {
          "forms/booking.json": { content: "{}" },
          "forms/contact.json": { content: "{}" },
          "forms/draft.md": { content: "" },
          "sources/foo.json": { content: "{}" },
        },
      },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe
        log={log}
        options={{ channel: "files", match: "forms/*.json" }}
      />,
    );
    await daemon.waitForJoin();

    await waitFor(() => {
      expect(log.map((e) => e.id).sort()).toEqual([
        "forms/booking.json",
        "forms/contact.json",
      ]);
    });
  });

  it("restricts callbacks via a predicate match", async () => {
    daemon.setSnapshot({
      resources: {
        files: {
          "a.json": { content: "{}" },
          "b.json": { content: "{}" },
          "c.md": { content: "" },
        },
      },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe
        log={log}
        options={{
          channel: "files",
          match: (id) => id.endsWith(".json"),
        }}
      />,
    );
    await daemon.waitForJoin();

    await waitFor(() => {
      expect(log.map((e) => e.id).sort()).toEqual(["a.json", "b.json"]);
    });
  });

  it("works on a custom (non-files) channel", async () => {
    daemon.setSnapshot({
      resources: {
        forms: {
          booking: { fields: ["from", "to", "date"] },
        },
      },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <LifecycleProbe log={log} options={{ channel: "forms" }} />,
    );
    await daemon.waitForJoin();

    await waitFor(() => {
      expect(log).toEqual([{ kind: "create", id: "booking" }]);
    });
  });
});

describe("useResourceEvents", () => {
  let daemon: MockDaemonHandle;

  beforeEach(async () => {
    daemon = await createMockDaemon();
  });

  afterEach(async () => {
    cleanup();
    await daemon.close();
  });

  it("accumulates events in order", async () => {
    const sink: { value: ResourceEvent[] } = { value: [] };
    daemon.setSnapshot({
      resources: { files: { "a.md": { content: "a" } } },
    });
    renderWithDaemon(
      daemon.baseUrl,
      <EventsProbe sink={sink} channel="files" />,
    );
    await daemon.waitForJoin();
    await waitFor(() => expect(sink.value.length).toBe(1));

    // Emit one at a time with a flush between each so React doesn't
    // collapse them into a single render (which would coalesce the
    // intermediate update with the subsequent delete).
    daemon.emit({
      type: "preview:resource_set",
      seq: 1,
      payload: { channel: "files", id: "b.md", payload: { content: "b" } },
    });
    await waitFor(() => expect(sink.value.length).toBe(2));

    daemon.emit({
      type: "preview:resource_set",
      seq: 2,
      payload: { channel: "files", id: "a.md", payload: { content: "a2" } },
    });
    await waitFor(() => expect(sink.value.length).toBe(3));

    daemon.emit({
      type: "preview:resource_deleted",
      seq: 3,
      payload: { channel: "files", id: "a.md" },
    });

    await waitFor(() => {
      expect(sink.value.map((e) => `${e.kind}:${e.id}`)).toEqual([
        "create:a.md",
        "create:b.md",
        "update:a.md",
        "delete:a.md",
      ]);
    });
  });

  it("caps the buffer at maxBuffer", async () => {
    const sink: { value: ResourceEvent[] } = { value: [] };
    renderWithDaemon(
      daemon.baseUrl,
      <EventsProbe
        sink={sink}
        channel="files"
        maxBuffer={3}
        fireForInitial={false}
      />,
    );
    await daemon.waitForJoin();

    for (let i = 1; i <= 5; i++) {
      daemon.emit({
        type: "preview:resource_set",
        seq: i,
        payload: { channel: "files", id: `f${i}.md`, payload: { content: "" } },
      });
    }

    await waitFor(() => {
      expect(sink.value.map((e) => e.id)).toEqual(["f3.md", "f4.md", "f5.md"]);
    });
  });
});
