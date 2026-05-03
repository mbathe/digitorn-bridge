/**
 * React hooks for host-iframe communication.
 *
 * These wrap the imperative ``host.ts`` primitives in a React-friendly
 * API. The most important one is ``useHostTheme`` — it reads the
 * initial theme from the URL AND subscribes to live theme changes
 * pushed by the host via postMessage, so the iframe automatically
 * re-renders when the user toggles dark/light in the parent client.
 */

import { useEffect, useState } from "react";
import {
  type ClientBoundMessage,
  type HostTheme,
  onHostMessage,
  readHostTheme,
} from "../host.js";

// ── Theme ──────────────────────────────────────────────────────────────

/**
 * Live host theme. Reads the initial value from URL query params,
 * then subscribes to ``digi:theme-change`` and ``digi:locale-change``
 * postMessages to stay in sync when the host toggles dark/light.
 *
 * Apps typically pass the result to a theme provider:
 *
 * ```tsx
 * function App() {
 *   const theme = useHostTheme();
 *   return <ThemeProvider mode={theme.mode} accent={theme.accent}>...</ThemeProvider>;
 * }
 * ```
 */
export function useHostTheme(): HostTheme {
  const [theme, setTheme] = useState<HostTheme>(() => readHostTheme());

  useEffect(() => {
    const offTheme = onHostMessage("digi:theme-change", (msg) => {
      setTheme(msg.theme);
    });
    const offLocale = onHostMessage("digi:locale-change", (msg) => {
      setTheme((prev) => ({ ...prev, locale: msg.locale }));
    });
    return () => {
      offTheme();
      offLocale();
    };
  }, []);

  return theme;
}

// ── Generic message subscription ──────────────────────────────────────

/**
 * Subscribe to a typed host message. The handler is re-bound when its
 * identity changes - wrap it in ``useCallback`` if you need a stable
 * reference (otherwise you'll get a fresh subscription per render).
 *
 * ```tsx
 * useHostMessage("digi:abort", useCallback((msg) => {
 *   console.log("Host requested abort:", msg.reason);
 *   cleanup();
 * }, []));
 * ```
 */
export function useHostMessage<T extends ClientBoundMessage["type"]>(
  type: T,
  handler: (msg: Extract<ClientBoundMessage, { type: T }>) => void,
): void {
  useEffect(() => {
    return onHostMessage(type, handler);
  }, [type, handler]);
}
