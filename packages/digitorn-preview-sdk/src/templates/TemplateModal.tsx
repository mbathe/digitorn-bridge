/**
 * Full-canvas template detail.
 *
 * Layout:
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │  ×    Title · short description           [Use this →]  │  ← toolbar
 *   ├──────────────────────────────────────────────────────────┤
 *   │                                                          │
 *   │              [live template preview]                     │
 *   │                                                          │
 *   └──────────────────────────────────────────────────────────┘
 *
 * Important behaviour:
 *
 *   - When mounted the modal posts ``digi:modal-open`` to the host.
 *     The host (digitorn_web chat-panel + Flutter PreviewIframe etc.)
 *     elevates the iframe to ``position: fixed; inset: 0`` so the
 *     modal covers the host's composer / hero, not just the gallery
 *     slot. ``digi:modal-close`` on unmount restores normal layout.
 *
 *   - There is no dim-tint overlay. The modal IS the canvas: it paints
 *     the digitorn slate directly so the eye doesn't bounce off a
 *     darker dim layer + a panel surface.
 *
 *   - Starter prompt + tags moved out of this view by design: the
 *     user wants the preview to take centre stage. Title +
 *     description live as a thin label inside the toolbar.
 */

import {
  createElement,
  useEffect,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";

import { sendToHost } from "../host.js";
import { TemplatePreview } from "./TemplatePreview.js";
import type { Template } from "./types.js";

export interface TemplateModalProps {
  /** Template to render. ``null`` hides the modal. */
  template: Template | null;
  onClose: () => void;
  onConfirm: (template: Template) => void | Promise<void>;
  /** Disables the CTA + close while the app is processing the pick. */
  busy?: boolean;
  /** Custom label for the CTA. */
  ctaLabel?: string;
  /** Custom aria-label for the close button. */
  closeLabel?: string;
  /** Color tokens for the chrome. */
  tokens?: ModalTokens;
}

export interface ModalTokens {
  background: string;
  surface: string;
  surfaceAlt: string;
  border: string;
  textBright: string;
  textMuted: string;
  accentPrimary: string;
  accentForeground: string;
  shadow: string;
}

const _DEFAULT_TOKENS: ModalTokens = {
  background: "var(--bg, #0B0F14)",
  surface: "var(--surface, #111827)",
  surfaceAlt: "var(--surface-alt, #1a1a1a)",
  border: "var(--border, rgba(255,255,255,0.08))",
  textBright: "var(--text-bright, #f5f5f5)",
  textMuted: "var(--text-muted, #a3a3a3)",
  accentPrimary: "var(--accent-primary, #14B8A6)",
  accentForeground: "var(--on-accent, #0a0a0a)",
  shadow: "var(--shadow, rgba(0,0,0,0.4))",
};

export function TemplateModal({
  template,
  onClose,
  onConfirm,
  busy = false,
  ctaLabel = "Use this template",
  closeLabel = "Close",
  tokens = _DEFAULT_TOKENS,
}: TemplateModalProps) {
  // Tell the host to elevate us to a full-canvas overlay while the
  // detail view is open. Pair the open/close calls so a fast
  // open→close sequence doesn't leave the iframe stuck fullscreen.
  useEffect(() => {
    if (template == null) return;
    sendToHost({ type: "digi:modal-open" });
    return () => sendToHost({ type: "digi:modal-close" });
  }, [template != null]);

  useEffect(() => {
    if (template == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [template, onClose, busy]);

  if (template == null) return null;

  // Outer canvas — paints the digitorn slate so the panel below
  // floats over a subtly darker surface. ``align-items: stretch``
  // lets the panel fill the available height; ``justify-content:
  // center`` centres it horizontally inside its bounded ``maxWidth``.
  const root: CSSProperties = {
    position: "fixed",
    inset: 0,
    zIndex: 60,
    background: tokens.background,
    display: "flex",
    alignItems: "stretch",
    justifyContent: "center",
    padding: "24px 28px 28px",
  };

  // Centred floating panel — bounded width + breathing room on every
  // side, so the modal never feels like a full takeover. ``--surface``
  // is one notch lighter than the canvas to give the panel its
  // "elevated card" feel without resorting to a dark overlay tint.
  const panel: CSSProperties = {
    width: "100%",
    maxWidth: 1200,
    minHeight: 0,
    display: "flex",
    flexDirection: "column",
    background: tokens.surface,
    border: `1px solid ${tokens.border}`,
    borderRadius: 16,
    overflow: "hidden",
    boxShadow: `0 24px 64px -32px ${tokens.shadow}`,
  };

  return createElement(
    "div",
    {
      role: "dialog",
      "aria-modal": true,
      "aria-label": template.title,
      style: root,
      onClick: (e: ReactMouseEvent<HTMLDivElement>) => {
        // Clicking the canvas around the panel dismisses, mirroring
        // the standard modal contract. Inner clicks bubble through
        // ``e.target === e.currentTarget`` so panel/toolbar/canvas
        // clicks don't trip it.
        if (e.target === e.currentTarget && !busy) onClose();
      },
    },
    createElement(
      "div",
      { style: panel },
      _renderToolbar({ template, onClose, onConfirm, busy, ctaLabel, closeLabel, tokens }),
      _renderCanvas({ template }),
    ),
  );
}

function _renderToolbar({
  template,
  onClose,
  onConfirm,
  busy,
  ctaLabel,
  closeLabel,
  tokens,
}: {
  template: Template;
  onClose: () => void;
  onConfirm: (t: Template) => void | Promise<void>;
  busy: boolean;
  ctaLabel: string;
  closeLabel: string;
  tokens: ModalTokens;
}) {
  const bar: CSSProperties = {
    flex: "0 0 auto",
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "12px 18px",
    borderBottom: `1px solid ${tokens.border}`,
    background: tokens.background,
    minHeight: 56,
  };
  const closeBtn: CSSProperties = {
    all: "unset",
    cursor: busy ? "wait" : "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    width: 32,
    height: 32,
    borderRadius: 8,
    color: tokens.textMuted,
    fontSize: 18,
    opacity: busy ? 0.5 : 1,
    flex: "0 0 auto",
  };
  const titleBlock: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    minWidth: 0,
    flex: 1,
  };
  const titleStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 14,
    fontWeight: 500,
    color: tokens.textBright,
    letterSpacing: "-0.005em",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  };
  const descStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 12,
    fontWeight: 400,
    color: tokens.textMuted,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    lineHeight: 1.4,
  };
  const ctaBtn: CSSProperties = {
    all: "unset",
    cursor: busy ? "wait" : "pointer",
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "8px 16px",
    background: tokens.textBright,
    color: tokens.background,
    borderRadius: 8,
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 13,
    fontWeight: 500,
    letterSpacing: "-0.005em",
    opacity: busy ? 0.7 : 1,
    transition: "opacity 120ms ease",
    flex: "0 0 auto",
  };
  return createElement(
    "header",
    { style: bar },
    createElement(
      "button",
      {
        type: "button",
        onClick: onClose,
        disabled: busy,
        "aria-label": closeLabel,
        style: closeBtn,
      },
      "×",
    ),
    createElement(
      "div",
      { style: titleBlock },
      createElement("span", { style: titleStyle }, template.title),
      template.description.length > 0
        ? createElement("span", { style: descStyle }, template.description)
        : null,
    ),
    createElement(
      "button",
      {
        type: "button",
        onClick: (e: ReactMouseEvent) => {
          e.preventDefault();
          if (!busy) void onConfirm(template);
        },
        disabled: busy,
        style: ctaBtn,
      },
      ctaLabel,
      createElement(
        "span",
        { "aria-hidden": true, style: { display: "inline-block", marginLeft: 4 } },
        "→",
      ),
    ),
  );
}

function _renderCanvas({ template }: { template: Template }) {
  const canvas: CSSProperties = {
    flex: 1,
    minHeight: 0,
    position: "relative",
    overflow: "hidden",
  };
  if (template.previewUrl) {
    return createElement(
      "div",
      { style: canvas },
      createElement("iframe", {
        src: template.previewUrl,
        title: template.title,
        sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
        style: {
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          border: "none",
        },
      }),
    );
  }
  if (template.seed) {
    return createElement(
      "div",
      { style: canvas },
      createElement(TemplatePreview, {
        seed: template.seed,
        style: { position: "absolute", inset: 0 },
      }),
    );
  }
  return createElement(
    "div",
    {
      style: {
        ...canvas,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
      },
    },
    createElement("img", {
      src: template.cover,
      alt: template.title,
      style: {
        maxWidth: "100%",
        maxHeight: "100%",
        objectFit: "contain",
      },
    }),
  );
}
