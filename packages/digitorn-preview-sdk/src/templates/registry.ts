/**
 * Preview renderer registry — the extensibility seam that lets apps
 * teach the SDK how to preview anything beyond React.
 *
 * The SDK ships ``react`` (esbuild-wasm bundle), ``html`` (raw iframe)
 * and ``markdown`` (rendered Markdown). Apps building LaTeX editors,
 * slide decks, canvas games, Jupyter-style notebooks etc. register
 * their own kind once at boot:
 *
 * ```tsx
 * import { registerPreviewKind } from "@digitorn/preview-sdk";
 * import { LatexPreview } from "./latex-preview";
 *
 * registerPreviewKind("latex", LatexPreview);
 * ```
 *
 * Templates with ``kind: "latex"`` then flow through the SAME
 * gallery, modal, confirm-flow and host-bridge pipeline — only the
 * inner rendering is custom.
 */

import type { CSSProperties, ReactNode } from "react";

import type { TemplateBundleStatus, TemplateSeed } from "./types.js";

export type PreviewChrome = "browser" | "none";

/**
 * Common props every renderer must accept. Renderers ignore the keys
 * they don't need. ``kind`` is included so a single renderer can
 * handle multiple kinds if it wants (e.g. a fenced-language renderer
 * that does HTML and SVG with the same iframe pipeline).
 */
export interface PreviewRendererProps {
  seed: TemplateSeed;
  kind: string;
  onStatus?: (status: TemplateBundleStatus) => void;
  style?: CSSProperties;
  className?: string;
  loadingSlot?: ReactNode;
  chrome?: PreviewChrome;
  chromeUrl?: string;
  background?: string;
}

export type PreviewRenderer = (props: PreviewRendererProps) => ReactNode;

const _previewKinds = new Map<string, PreviewRenderer>();

/**
 * Register a renderer for a specific template kind. Last call wins,
 * so apps can override built-in kinds (e.g. swap ``react`` for their
 * own faster bundler).
 */
export function registerPreviewKind(kind: string, renderer: PreviewRenderer): void {
  _previewKinds.set(kind, renderer);
}

/** Look up the renderer for ``kind``. Returns ``undefined`` if unset. */
export function getPreviewKind(kind: string): PreviewRenderer | undefined {
  return _previewKinds.get(kind);
}

/** List all registered kinds — useful for debugging / docs. */
export function listPreviewKinds(): string[] {
  return Array.from(_previewKinds.keys());
}
