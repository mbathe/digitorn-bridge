import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup } from "@testing-library/react";
import { useConnection, usePreviewState, useFiles } from "../../src/hooks/index.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

/**
 * Smoke test: prove that <DigiPreview> + the mock daemon roundtrip a
 * snapshot and a live delta end-to-end through Socket.IO. If this
 * test breaks, every hook test in the suite is moot — the bridge is
 * down.
 */

function Probe() {
  const connected = useConnection();
  const entry = usePreviewState<string>("entry_file");
  const files = useFiles();
  const filesArr = Array.from(files.keys()).sort().join(",");
  return (
    <div>
      <span data-testid="connected">{connected ? "yes" : "no"}</span>
      <span data-testid="entry">{entry ?? ""}</span>
      <span data-testid="files">{filesArr}</span>
    </div>
  );
}

describe("DigiPreview ↔ daemon roundtrip", () => {
  let daemon: MockDaemonHandle;

  beforeEach(async () => {
    daemon = await createMockDaemon();
  });

  afterEach(async () => {
    cleanup();
    await daemon.close();
  });

  it("connects to the socket namespace and receives the HTTP snapshot", async () => {
    daemon.setSnapshot({
      state: { entry_file: "src/main.tsx" },
      resources: { files: { "src/main.tsx": { content: "export {}", status: "added" } } },
      seq: 5,
    });

    const { getByTestId } = renderWithDaemon(daemon.baseUrl, <Probe />, {
      sessionId: "smoke-1",
    });

    const join = await daemon.waitForJoin();
    expect(join.session_id).toBe("smoke-1");
    expect(join.app_id).toBe("test-app");

    await waitFor(() => {
      expect(getByTestId("connected").textContent).toBe("yes");
      expect(getByTestId("entry").textContent).toBe("src/main.tsx");
      expect(getByTestId("files").textContent).toBe("src/main.tsx");
    });
  });

  it("applies a live preview:resource_set delta", async () => {
    const { getByTestId } = renderWithDaemon(daemon.baseUrl, <Probe />, {
      sessionId: "smoke-2",
    });
    await daemon.waitForJoin();
    await waitFor(() => expect(getByTestId("connected").textContent).toBe("yes"));

    daemon.emit({
      type: "preview:resource_set",
      seq: 1,
      payload: {
        channel: "files",
        id: "src/App.tsx",
        payload: { content: "export {}", status: "added" },
      },
    });

    await waitFor(() => expect(getByTestId("files").textContent).toBe("src/App.tsx"));
  });

  it("applies a live preview:state_changed delta", async () => {
    const { getByTestId } = renderWithDaemon(daemon.baseUrl, <Probe />, {
      sessionId: "smoke-3",
    });
    await daemon.waitForJoin();
    await waitFor(() => expect(getByTestId("connected").textContent).toBe("yes"));

    daemon.emit({
      type: "preview:state_changed",
      seq: 1,
      payload: { key: "entry_file", value: "src/Other.tsx" },
    });

    await waitFor(() => expect(getByTestId("entry").textContent).toBe("src/Other.tsx"));
  });
});
