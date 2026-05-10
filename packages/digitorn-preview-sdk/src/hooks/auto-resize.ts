/**
 * `useAutoResize` — make the iframe document grow to fit its content.
 *
 * Without this, an iframe under a host that uses content-driven sizing
 * (digitorn_web's chat panel, Flutter desktop preview, etc.) will
 * either show an internal scrollbar (bad UX) or clip content. This
 * hook reports the document's true scrollHeight to the host on every
 * layout change via the standard ``digi:content-resize`` postMessage.
 *
 * Usage — drop one line at the top of any iframe-embedded SDK app:
 *
 * ```tsx
 * import { useAutoResize } from "@digitorn/preview-sdk";
 *
 * export default function App() {
 *   useAutoResize();
 *   return <YourEmptyState />;
 * }
 * ```
 *
 * Pass ``active={false}`` to disable: useful when the app switches
 * from an empty-state (auto-grow desired) to a full canvas where the
 * iframe should fill the host height instead (e.g. an IDE pane).
 */

import { useEffect } from "react";
import { sendToHost } from "../host.js";

export function useAutoResize(active: boolean = true): void {
  useEffect(() => {
    if (!active || typeof window === "undefined") return;
    const root = document.documentElement;
    let last = -1;
    const send = () => {
      const h = Math.max(root.scrollHeight, document.body?.scrollHeight ?? 0);
      if (h === last) return;
      last = h;
      sendToHost({ type: "digi:content-resize", height: h });
    };
    send();
    const ro = new ResizeObserver(send);
    ro.observe(root);
    if (document.body) ro.observe(document.body);
    return () => ro.disconnect();
  }, [active]);
}
