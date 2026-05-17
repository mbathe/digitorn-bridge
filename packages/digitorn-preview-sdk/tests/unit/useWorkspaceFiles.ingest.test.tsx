import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { useWorkspaceFiles } from "../../src/hooks/workspace.js";
import { createMockDaemon, type MockDaemonHandle } from "./helpers/mockDaemon.js";
import { renderWithDaemon, waitFor } from "./helpers/renderWithDaemon.js";

/**
 * useWorkspaceFiles.ingestFile — server-side extraction primitive.
 * Verified at the HTTP-envelope level by stubbing global.fetch (the
 * mock daemon's HTTP server is preview-only).
 */

interface Handle {
  ingestFile: ReturnType<typeof useWorkspaceFiles>["ingestFile"];
  busy: boolean;
  error: Error | null;
}

function Probe({ handleRef }: { handleRef: { current: Handle | null } }) {
  const fs = useWorkspaceFiles();
  handleRef.current = {
    ingestFile: fs.ingestFile,
    busy: fs.busy,
    error: fs.error,
  };
  return null;
}

describe("useWorkspaceFiles.ingestFile", () => {
  let daemon: MockDaemonHandle;
  let originalFetch: typeof globalThis.fetch;
  let captured: Array<{
    url: string;
    method: string;
    body: FormData | null;
    headers: Record<string, string>;
  }>;

  beforeEach(async () => {
    daemon = await createMockDaemon();
    captured = [];
    originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(
      async (input: string | URL | Request, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        const method = (init?.method ?? "GET").toUpperCase();
        // Only intercept the ingest route; everything else (preview
        // snapshot fetch) falls through to the real implementation
        // so DigiPreview can hydrate normally.
        if (url.includes("/workspace/ingest-source") && method === "POST") {
          const headers: Record<string, string> = {};
          if (init?.headers && typeof init.headers === "object") {
            for (const [k, v] of Object.entries(init.headers)) {
              headers[k] = String(v);
            }
          }
          captured.push({
            url,
            method,
            body: init?.body instanceof FormData ? init.body : null,
            headers,
          });
          return new Response(
            JSON.stringify({
              success: true,
              data: {
                path: "sources/test.md",
                lines: 42,
                size: 1234,
                mime: "application/pdf",
                format: ".pdf",
                target_dir: "sources",
              },
            }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          );
        }
        return originalFetch(input as RequestInfo, init);
      },
    ) as typeof globalThis.fetch;
  });

  afterEach(async () => {
    globalThis.fetch = originalFetch;
    cleanup();
    await daemon.close();
  });

  async function mount(): Promise<Handle> {
    const handleRef = { current: null as Handle | null };
    renderWithDaemon(daemon.baseUrl, <Probe handleRef={handleRef} />);
    await daemon.waitForJoin();
    await waitFor(() => expect(handleRef.current).not.toBeNull());
    return handleRef.current!;
  }

  it("POSTs multipart to /workspace/ingest-source with target_dir=attachments (default)", async () => {
    const h = await mount();
    const file = new File(["hello world"], "notes.md", { type: "text/markdown" });
    const result = await h.ingestFile(file);

    expect(captured).toHaveLength(1);
    expect(captured[0].method).toBe("POST");
    expect(captured[0].url).toMatch(/\/workspace\/ingest-source$/);
    expect(captured[0].body?.get("target_dir")).toBe("attachments");
    const sent = captured[0].body?.get("file");
    expect(sent).toBeInstanceOf(File);
    expect((sent as File).name).toBe("notes.md");
    expect(result.path).toBe("sources/test.md");
    expect(result.lines).toBe(42);
  });

  it("forwards opts.targetDir override to the daemon", async () => {
    const h = await mount();
    const file = new File(["x"], "x.pdf", { type: "application/pdf" });
    await h.ingestFile(file, { targetDir: "sources" });
    expect(captured[0].body?.get("target_dir")).toBe("sources");
  });

  it("uses opts.filename when provided (override)", async () => {
    const h = await mount();
    const blob = new Blob(["binary content"], { type: "application/octet-stream" });
    await h.ingestFile(blob, { filename: "renamed.bin" });
    const sent = captured[0].body?.get("file");
    expect(sent).toBeInstanceOf(Blob);
    expect((sent as File).name).toBe("renamed.bin");
  });

  it("surfaces 422 'no extractable text' as a clean Error", async () => {
    globalThis.fetch = vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/workspace/ingest-source")) {
        return new Response(
          JSON.stringify({
            detail: {
              error: "no extractable text from file",
              format: ".xyz",
              mime: "application/octet-stream",
            },
          }),
          {
            status: 422,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      return originalFetch(input as RequestInfo, input as RequestInit);
    }) as typeof globalThis.fetch;

    const h = await mount();
    const file = new File(["x"], "x.bin", { type: "application/octet-stream" });
    await expect(h.ingestFile(file)).rejects.toThrow(/no extractable text/);
  });
});
