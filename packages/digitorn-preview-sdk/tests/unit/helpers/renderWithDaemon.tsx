import { render, type RenderResult } from "@testing-library/react";
import { type ReactElement } from "react";
import { DigiPreview } from "../../../src/DigiPreview.js";
import type { SessionInfo } from "../../../src/types.js";

/**
 * Mount any subtree under a ``<DigiPreview>`` provider wired to the
 * mock daemon. Each test gets its own session id so two suites
 * running in parallel can't cross-talk on the same Socket.IO server.
 */
export function renderWithDaemon(
  baseUrl: string,
  ui: ReactElement,
  opts: { sessionId?: string; appId?: string; token?: string | null } = {},
): RenderResult {
  const session: SessionInfo = {
    appId: opts.appId ?? "test-app",
    sessionId: opts.sessionId ?? `s-${Math.random().toString(36).slice(2, 10)}`,
    token: opts.token ?? null,
    baseUrl,
  };
  return render(<DigiPreview session={session}>{ui}</DigiPreview>);
}

/**
 * Resolve once the next macrotask boundary has run. Lets a single
 * socket.io ``event`` round-trip through Node's event loop, the
 * socket.io-client decoder, our reducer, and React's scheduler.
 */
export function tick(ms = 0): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Poll an assertion until it passes or the deadline expires. Avoids
 * arbitrary ``await tick(N)`` sprinkled around socket round-trips.
 */
export async function waitFor(
  assertion: () => void | Promise<void>,
  opts: { timeout?: number; interval?: number } = {},
): Promise<void> {
  const timeout = opts.timeout ?? 1_500;
  const interval = opts.interval ?? 15;
  const start = Date.now();
  let lastError: unknown;
  while (Date.now() - start < timeout) {
    try {
      await assertion();
      return;
    } catch (err) {
      lastError = err;
      await tick(interval);
    }
  }
  throw lastError ?? new Error(`waitFor timed out after ${timeout}ms`);
}
