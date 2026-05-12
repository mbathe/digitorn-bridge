import { defineConfig, devices } from "@playwright/test";

/**
 * SDK e2e regression suite.
 *
 * The tests drive a live lovable bundle hosted by digitorn_web. They
 * assume the full dev stack is up:
 *
 *   - daemon at 127.0.0.1:8000 serving the lovable bundle
 *   - digitorn_web Next.js dev server at localhost:3000
 *   - latest SDK build synced into ~/.digitorn/packages/digitorn-lovable
 *
 * Run with:
 *
 *   npm run test:e2e
 *
 * Each test exercises one of the critical SDK flows: gallery render,
 * modal open/close, kind dispatch, empty → workspace transition.
 */
// Auto-start a fresh ``vite preview`` of the lovable bundle so the
// tests don't depend on digitorn_web auth + the full daemon stack.
// We pin the port high (5179) to avoid clashing with dev servers a
// human might already have running on 5173-5175.
const LOVABLE_WEB_DIR = "../digitorn/builtins/digitorn-lovable/web";
const PREVIEW_PORT = 5179;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  webServer: {
    command: `npx vite preview --port ${PREVIEW_PORT} --strictPort`,
    cwd: LOVABLE_WEB_DIR,
    url: `http://localhost:${PREVIEW_PORT}/`,
    timeout: 30_000,
    reuseExistingServer: true,
    stdout: "ignore",
    stderr: "pipe",
  },
  use: {
    baseURL: `http://localhost:${PREVIEW_PORT}`,
    trace: "retain-on-failure",
    viewport: { width: 1280, height: 800 },
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
