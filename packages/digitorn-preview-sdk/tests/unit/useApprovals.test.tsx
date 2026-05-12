import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import { useApprovals, type UseApprovalsApi } from "../../src/hooks/approvals.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

interface ProbeHandle {
  api: UseApprovalsApi | null;
}

function Probe({ handle }: { handle: ProbeHandle }) {
  const api = useApprovals();
  handle.api = api;
  return <span data-testid="count">{api.pending.length}</span>;
}

describe("useApprovals", () => {
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
    renderWithDaemon(daemon.baseUrl, <Probe handle={probe} />);
    await daemon.waitForJoin();
    await waitFor(() => expect(probe.api).toBeTruthy());
  }

  it("surfaces the pending queue from approval_request events", async () => {
    await mount();
    daemon.emitBatch([
      {
        type: "approval_request",
        payload: {
          request_id: "r1",
          tool_name: "Write",
          tool_params: { path: "a.ts" },
          risk_level: "medium",
        },
      },
      {
        type: "approval_request",
        payload: {
          request_id: "r2",
          tool_name: "Bash",
          tool_params: { cmd: "rm -rf" },
          risk_level: "high",
        },
      },
    ]);
    await waitFor(() => {
      expect(probe.api!.pending).toHaveLength(2);
      expect(probe.api!.pending[0].request_id).toBe("r1");
      expect(probe.api!.pending[1].tool_name).toBe("Bash");
    });
  });

  it("approve() emits resolve_approval with approved=true", async () => {
    const captured: any[] = [];
    daemon.onClientEmit("resolve_approval", (data, ack) => {
      captured.push(data);
      ack?.({ ok: true, approved: true, payload_received: true });
    });
    await mount();
    daemon.emit({
      type: "approval_request",
      seq: 1,
      payload: { request_id: "r1", tool_name: "Write" },
    });
    await waitFor(() => expect(probe.api!.pending).toHaveLength(1));

    const ack = await probe.api!.approve("r1", "looks good");
    expect(ack.approved).toBe(true);
    expect(captured[0]).toMatchObject({
      request_id: "r1",
      approved: true,
      message: "looks good",
    });
  });

  it("reject() emits resolve_approval with approved=false", async () => {
    const captured: any[] = [];
    daemon.onClientEmit("resolve_approval", (data, ack) => {
      captured.push(data);
      ack?.({ ok: true, approved: false });
    });
    await mount();
    daemon.emit({
      type: "approval_request",
      seq: 1,
      payload: { request_id: "r2", tool_name: "Bash" },
    });
    await waitFor(() => expect(probe.api!.pending).toHaveLength(1));

    await probe.api!.reject("r2", "too risky");
    expect(captured[0]).toMatchObject({
      request_id: "r2",
      approved: false,
      message: "too risky",
    });
  });

  it("approval_resolved with explicit id removes only that entry", async () => {
    await mount();
    daemon.emitBatch([
      { type: "approval_request", payload: { request_id: "r1", tool_name: "X" } },
      { type: "approval_request", payload: { request_id: "r2", tool_name: "Y" } },
    ]);
    await waitFor(() => expect(probe.api!.pending).toHaveLength(2));

    daemon.emit({
      type: "approval_resolved",
      seq: 99,
      payload: { request_id: "r1" },
    });
    await waitFor(() => {
      expect(probe.api!.pending).toHaveLength(1);
      expect(probe.api!.pending[0].request_id).toBe("r2");
    });
  });

  it("surfaces ack errors via the error field", async () => {
    daemon.onClientEmit("resolve_approval", (_d, ack) => {
      ack?.({ ok: false, error: "stale request" });
    });
    await mount();
    daemon.emit({
      type: "approval_request",
      seq: 1,
      payload: { request_id: "r1", tool_name: "X" },
    });
    await waitFor(() => expect(probe.api!.pending).toHaveLength(1));

    await expect(probe.api!.approve("r1")).rejects.toThrow(/stale request/);
    await waitFor(() => expect(probe.api!.error?.message).toMatch(/stale/));
  });
});
