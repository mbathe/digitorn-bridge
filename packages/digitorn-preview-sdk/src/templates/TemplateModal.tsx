/**
 * Detail modal opened when the user picks a template card.
 *
 * 1:1 layout port of Lovable.dev's template detail page (route at
 * ``/templates/<category>/<id>``). Lovable runs it as a full page;
 * we render it inside a floating modal because the host shell is a
 * chat surface — the user keeps their context.
 *
 * Layout (Lovable spec, inspected against their live DOM):
 *
 *   ┌─────────────────────────────────────────────────────────────┐
 *   │ × close                                              [CTA]  │
 *   ├─────────────────────────────────────┬───────────────────────┤
 *   │                                     │ Title (30/600/-0.025) │
 *   │       Live preview / cover          │ Description (14/400)  │
 *   │       (aspect-video, rounded 12)    │                       │
 *   │                                     │ ─────────────         │
 *   │                                     │ Prompt preview        │
 *   │                                     │ (mono, 13)            │
 *   │                                     │                       │
 *   │                                     │ Tags                  │
 *   │                                     │ [tag] [tag] [tag]     │
 *   └─────────────────────────────────────┴───────────────────────┘
 *
 * The CTA is the primary "Use this template" action. Click anywhere
 * outside the frame closes; Esc closes; the close (×) button closes.
 */

import {
  createElement,
  useEffect,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";

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
  useEffect(() => {
    if (template == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [template, onClose, busy]);

  if (template == null) return null;

  const overlay: CSSProperties = {
    position: "fixed",
    inset: 0,
    zIndex: 60,
    background: `color-mix(in oklab, ${tokens.background} 92%, black)`,
    display: "flex",
    flexDirection: "column",
  };

  const frame: CSSProperties = {
    flex: 1,
    display: "flex",
    flexDirection: "column",
    margin: 24,
    background: tokens.surface,
    border: `1px solid ${tokens.border}`,
    borderRadius: 14,
    overflow: "hidden",
    boxShadow: `0 32px 64px -24px ${tokens.shadow}`,
  };

  return createElement(
    "div",
    {
      role: "dialog",
      "aria-modal": true,
      "aria-label": template.title,
      style: overlay,
      onClick: (e: ReactMouseEvent<HTMLDivElement>) => {
        if (e.target === e.currentTarget && !busy) onClose();
      },
    },
    createElement(
      "div",
      { style: frame },
      _renderTopBar({ onClose, onConfirm, template, busy, ctaLabel, closeLabel, tokens }),
      _renderBody({ template, tokens }),
    ),
  );
}

// Top bar: just close (×) on the left and the primary CTA on the right.
// Title moved INTO the right-column body to mirror Lovable's detail
// page where the title hugs the metadata sidebar.
function _renderTopBar({
  onClose,
  onConfirm,
  template,
  busy,
  ctaLabel,
  closeLabel,
  tokens,
}: {
  onClose: () => void;
  onConfirm: (t: Template) => void | Promise<void>;
  template: Template;
  busy: boolean;
  ctaLabel: string;
  closeLabel: string;
  tokens: ModalTokens;
}) {
  return createElement(
    "header",
    {
      style: {
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "12px 16px",
        borderBottom: `1px solid ${tokens.border}`,
        background: tokens.surface,
        flex: "0 0 auto",
      },
    },
    createElement(
      "button",
      {
        type: "button",
        onClick: onClose,
        disabled: busy,
        "aria-label": closeLabel,
        style: {
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
        },
      },
      "×",
    ),
    createElement("div", { style: { flex: 1 } }),
    createElement(
      "button",
      {
        type: "button",
        onClick: () => {
          if (!busy) void onConfirm(template);
        },
        disabled: busy,
        style: {
          all: "unset",
          cursor: busy ? "wait" : "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "10px 18px",
          background: tokens.textBright,
          color: tokens.background,
          borderRadius: 8,
          fontFamily: "var(--font-sans, system-ui)",
          fontSize: 14,
          fontWeight: 500,
          letterSpacing: "-0.005em",
          opacity: busy ? 0.7 : 1,
          transition: "opacity 120ms ease",
        },
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

function _renderBody({ template, tokens }: { template: Template; tokens: ModalTokens }) {
  const body: CSSProperties = {
    flex: 1,
    display: "flex",
    minHeight: 0,
    background: tokens.background,
  };

  const previewCol: CSSProperties = {
    flex: 1,
    position: "relative",
    minWidth: 0,
    padding: 24,
    display: "flex",
    alignItems: "stretch",
    justifyContent: "stretch",
  };

  const previewFrame: CSSProperties = {
    width: "100%",
    height: "100%",
    borderRadius: 12,
    overflow: "hidden",
    border: `1px solid ${tokens.border}`,
    background: tokens.surfaceAlt,
    position: "relative",
  };

  const sidebar: CSSProperties = {
    width: 360,
    flexShrink: 0,
    borderLeft: `1px solid ${tokens.border}`,
    padding: "28px 28px 24px",
    background: tokens.surface,
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    gap: 24,
  };

  const titleStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 28,
    fontWeight: 600,
    color: tokens.textBright,
    letterSpacing: "-0.025em",
    lineHeight: 1.15,
    margin: 0,
  };

  const descStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 14,
    fontWeight: 400,
    color: tokens.textMuted,
    lineHeight: 1.55,
    marginTop: 6,
    margin: 0,
  };

  const sectionLabel: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 10.5,
    fontWeight: 600,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
    color: tokens.textMuted,
    marginBottom: 10,
  };

  const promptStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 13,
    color: tokens.textBright,
    lineHeight: 1.6,
    background: tokens.surfaceAlt,
    border: `1px solid ${tokens.border}`,
    borderRadius: 10,
    padding: "12px 14px",
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word",
  };

  const tagsRow: CSSProperties = {
    display: "flex",
    flexWrap: "wrap",
    gap: 6,
  };

  const tagPill: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 12,
    color: tokens.textMuted,
    padding: "5px 10px",
    borderRadius: 6,
    background: tokens.surfaceAlt,
    border: `1px solid ${tokens.border}`,
  };

  return createElement(
    "div",
    { style: body },
    createElement(
      "div",
      { style: previewCol },
      createElement(
        "div",
        { style: previewFrame },
        _renderPreviewSurface(template),
      ),
    ),
    createElement(
      "aside",
      { style: sidebar },
      createElement(
        "div",
        null,
        createElement("h1", { style: titleStyle }, template.title),
        template.description.length > 0
          ? createElement("p", { style: descStyle }, template.description)
          : null,
      ),
      template.prompt && template.prompt.length > 0
        ? createElement(
            "div",
            null,
            createElement("div", { style: sectionLabel }, "Starter prompt"),
            createElement("div", { style: promptStyle }, template.prompt),
          )
        : null,
      template.tags && template.tags.length > 0
        ? createElement(
            "div",
            null,
            createElement("div", { style: sectionLabel }, "Tags"),
            createElement(
              "div",
              { style: tagsRow },
              template.tags.map((tag) =>
                createElement("span", { key: tag, style: tagPill }, tag),
              ),
            ),
          )
        : null,
    ),
  );
}

function _renderPreviewSurface(template: Template) {
  if (template.previewUrl) {
    return createElement("iframe", {
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
    });
  }
  if (template.seed) {
    return createElement(TemplatePreview, {
      seed: template.seed,
      style: { position: "absolute", inset: 0 },
    });
  }
  return createElement(
    "div",
    {
      style: {
        position: "absolute",
        inset: 0,
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
