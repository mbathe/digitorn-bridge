import { describe, expect, it, vi } from "vitest";
import { bundleFiles } from "../../src/templates/bundler.js";

/**
 * happy-dom rejects ``fetch("blob:...")`` so we intercept Blob URL
 * creation and keep a reference to the underlying Blob keyed by URL.
 * Tests then read content via the Blob directly.
 */
function setupBlobInterceptor(): { read: (url: string) => Promise<string> } {
  const store = new Map<string, Blob>();
  const origCreate = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;
  vi.spyOn(URL, "createObjectURL").mockImplementation((b: Blob | MediaSource) => {
    const url = `blob:test-${Math.random().toString(36).slice(2)}`;
    if (b instanceof Blob) store.set(url, b);
    return url;
  });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation((u: string) => {
    store.delete(u);
  });
  return {
    read: async (url) => {
      const b = store.get(url);
      if (!b) throw new Error(`Blob URL not found: ${url}`);
      return b.text();
    },
  };
}

/**
 * In-browser bundler tests.
 *
 * Runs via Vitest (Node + happy-dom). The bundler uses ``esbuild-wasm``
 * which works in Node via worker-less initialisation — see
 * ``ensureEsbuildReady``. Tests verify the multi-file resolver, the
 * error path, and the missing-entry guard.
 */

describe("bundleFiles", () => {
  it("resolves relative imports across multiple files", async () => {
    const blobs = setupBlobInterceptor();
    const files = new Map([
      [
        "src/main.tsx",
        `import { App } from "./App";
         import { Btn } from "./components/Btn";
         export const root = { App, Btn };`,
      ],
      [
        "src/App.tsx",
        `import { Btn } from "./components/Btn";
         export const App = () => Btn();`,
      ],
      [
        "src/components/Btn.tsx",
        `export const Btn = () => "clicked";`,
      ],
    ]);

    const out = await bundleFiles(files, "src/main.tsx");
    expect(out.ok).toBe(true);
    if (out.ok) {
      const code = await blobs.read(out.url);
      // All three sources merged into one ESM module.
      expect(code).toContain("clicked");
      expect(code).toMatch(/App\s*=/);
      expect(code).toMatch(/Btn\s*=/);
      URL.revokeObjectURL(out.url);
    }
  });

  it("reports a structured error for syntax-broken sources", async () => {
    const files = new Map([
      ["src/main.tsx", `export const broken = (`],
    ]);
    const out = await bundleFiles(files, "src/main.tsx");
    expect(out.ok).toBe(false);
    if (!out.ok) {
      expect(out.errors.length).toBeGreaterThan(0);
      const first = out.errors[0];
      expect(first.file).toBeDefined();
      expect(typeof first.line).toBe("number");
      expect(first.message).toMatch(/expected|unexpected/i);
    }
  });

  it("returns ok:false when the entry file is missing", async () => {
    const out = await bundleFiles(new Map([["other.tsx", "export {}"]]), "src/main.tsx");
    expect(out.ok).toBe(false);
    if (!out.ok) {
      expect(out.errors[0].message).toMatch(/entry file not found/);
    }
  });

  it("reports an unresolved relative import as a structured error", async () => {
    const files = new Map([
      ["src/main.tsx", `import { x } from "./missing"; export const v = x;`],
    ]);
    const out = await bundleFiles(files, "src/main.tsx");
    expect(out.ok).toBe(false);
    if (!out.ok) {
      expect(out.errors.some((e) => /file not found|cannot resolve/.test(e.message))).toBe(true);
    }
  });
});
