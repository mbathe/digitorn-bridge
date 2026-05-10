/**
 * `kind: "html"` renderer — drops the seed's HTML entry straight into
 * a sandboxed iframe via ``srcdoc``. No bundler, no React runtime, no
 * Tailwind injection (the seed brings whatever it wants).
 *
 * Seed contract:
 *   - ``seed.entry``: relative path to the HTML file (default ``index.html``).
 *   - ``seed.files[entry]``: the HTML source, must be self-contained
 *     (inline ``<style>`` / ``<script>``, or external URLs the browser
 *     can fetch from inside the sandbox).
 *
 * Use when:
 *   - You're previewing a static landing built by hand or another tool.
 *   - The seed is a one-page Reveal.js / impress.js deck the consumer
 *     ships pre-rendered.
 *   - You're prototyping in raw HTML/CSS/JS without a build step.
 */

import { createElement, type CSSProperties } from "react";

import type { PreviewRendererProps } from "./registry.js";

const _DEFAULT_ENTRY = "index.html";

export function HtmlPreview({
  seed,
  style,
  className,
  background = "#ffffff",
}: PreviewRendererProps) {
  const entry = seed.entry ?? _DEFAULT_ENTRY;
  const html = seed.files?.[entry] ?? "<!doctype html><body></body>";

  const wrapper: CSSProperties = {
    width: "100%",
    height: "100%",
    background,
    ...style,
  };

  return createElement(
    "div",
    { className, style: wrapper },
    createElement("iframe", {
      srcDoc: html,
      title: "preview",
      sandbox: "allow-scripts allow-same-origin allow-modals allow-forms allow-popups",
      style: {
        width: "100%",
        height: "100%",
        border: "none",
        background,
        display: "block",
      },
    }),
  );
}
