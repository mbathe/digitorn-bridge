/**
 * Drop-in empty-state for any SDK-powered app that ships templates.
 *
 * Bundles three concerns that every consumer was reproducing by hand:
 *
 *   1. The centred container + max-width gutter so the gallery sits
 *      cleanly under a host composer.
 *   2. ``<TemplateGallery>`` + ``<TemplateModal>`` wired together via
 *      the ``useTemplates`` hook (selection state, dismiss, click).
 *   3. The standard "user picks a template" confirm flow:
 *        a. Emit ``digi:template-pick`` to the host (it owns session
 *           creation, seeds workspace files, dispatches the prompt).
 *        b. Standalone fallback (no host listening, real session
 *           bound): seed via ``ws.writeFile`` + send the prompt via
 *           ``chat.send``. Skipped on the ``_dev_`` placeholder
 *           session id (no daemon counterpart).
 *
 * Apps that need a different confirm path can pass ``onConfirm`` to
 * fully override step 3 (e.g. apps without a host bridge, or apps
 * that route templates through their own backend).
 *
 * Minimal usage:
 *
 * ```tsx
 * import { TemplateEmptyState } from "@digitorn/preview-sdk";
 * import { TEMPLATES } from "./templates";
 *
 * export default function App() {
 *   return <TemplateEmptyState templates={TEMPLATES} />;
 * }
 * ```
 */

import { createElement, useCallback, useEffect, useState, type CSSProperties } from "react";

import { sendToHost } from "../host.js";
import { useChat } from "../hooks/chat.js";
import { useSessionMeta, useWorkspaceFiles } from "../hooks/workspace.js";
import { TemplateGallery, type GalleryTokens } from "./TemplateGallery.js";
import { TemplateModal, type ModalTokens } from "./TemplateModal.js";
import type { Template } from "./types.js";
import { useTemplates } from "./useTemplates.js";

export interface TemplateEmptyStateProps {
  /** Templates to render in the gallery. */
  templates: Template[];
  /** Header chip label. Default ``"Templates"``. Empty hides the chip. */
  sectionLabel?: string;
  /** When set, "Browse all →" link appears in the header. */
  onBrowseAll?: () => void;
  /** Custom label for the browse-all link. */
  browseAllLabel?: string;
  /**
   * Min card width before the auto-fill grid wraps to a new column.
   * Default 200 (a tighter pack than Lovable's 345 — fits the chat
   * panel surface most apps embed under).
   */
  minCardWidth?: number;
  /** Container max-width. Default 1200. */
  maxWidth?: number;
  /** Outer wrapper style override. */
  style?: CSSProperties;
  /** Outer wrapper className. */
  className?: string;
  /** Tokens forwarded to the gallery cards. */
  galleryTokens?: GalleryTokens;
  /** Tokens forwarded to the detail modal. */
  modalTokens?: ModalTokens;
  /**
   * Override the entire confirm flow (advanced). When omitted, the
   * default flow is: emit ``digi:template-pick`` to the host, fall
   * back to ``ws.writeFile`` + ``chat.send`` when standalone.
   */
  onConfirm?: (template: Template) => void | Promise<void>;
  /** CTA label inside the detail modal. */
  ctaLabel?: string;
}

const _OUTER: CSSProperties = {
  background: "transparent",
  display: "flex",
  alignItems: "flex-start",
  justifyContent: "center",
  padding: "16px 20px 32px",
};

export function TemplateEmptyState({
  templates,
  sectionLabel,
  onBrowseAll,
  browseAllLabel,
  minCardWidth = 200,
  maxWidth = 1200,
  style,
  className,
  galleryTokens,
  modalTokens,
  onConfirm,
  ctaLabel,
}: TemplateEmptyStateProps) {
  const tpls = useTemplates(templates);
  const session = useSessionMeta();
  const ws = useWorkspaceFiles();
  const chat = useChat();

  // Counter-shift the gallery when the host elevates the iframe to
  // full-canvas modal mode. Without this, the in-flow gallery follows
  // the iframe element upward and visibly jumps. Padding-top equal to
  // the elevation amount keeps it pinned at the same viewport y.
  const [layoutOffset, setLayoutOffset] = useState(0);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onMessage = (e: MessageEvent) => {
      const data = e.data as { type?: string; offset?: number } | null;
      if (!data || data.type !== "digi:layout-shift") return;
      const off = typeof data.offset === "number" ? data.offset : 0;
      setLayoutOffset(off);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const _defaultConfirm = useCallback(
    async (template: Template) => {
      sendToHost({
        type: "digi:template-pick",
        template_id: template.id,
        prompt: template.prompt ?? "",
        seed_files: template.seed?.files ?? {},
      });

      const isStandalone =
        typeof window !== "undefined" && window.parent === window;
      const hasRealSession = session.sessionId !== "_dev_";
      if (isStandalone && hasRealSession) {
        if (template.seed?.files) {
          for (const [path, content] of Object.entries(template.seed.files)) {
            try {
              await ws.writeFile(path, content, { autoApprove: true });
            } catch (e) {
              console.warn("[preview-sdk] seed writeFile failed", path, e);
            }
          }
        }
        if (template.prompt) {
          try {
            await chat.send(template.prompt);
          } catch (e) {
            console.warn("[preview-sdk] chat.send failed", e);
          }
        }
      }
    },
    [ws, chat, session.sessionId],
  );

  const handleConfirm = useCallback(
    async (template: Template) => {
      if (onConfirm) {
        await onConfirm(template);
      } else {
        await _defaultConfirm(template);
      }
      tpls.dismiss();
    },
    [onConfirm, _defaultConfirm, tpls],
  );

  const outerStyle: CSSProperties = {
    ..._OUTER,
    paddingTop: (16 + layoutOffset),
    ...style,
  };

  // While the host has elevated the iframe (layoutOffset > 0), the
  // modal is covering the chat-panel canvas. Hide the gallery
  // entirely so that the SDK's modal fade-out doesn't reveal the
  // gallery momentarily through its decreasing opacity. Visibility
  // flips back to ``visible`` only when the host has fully reset
  // (offset = 0), which it does only AFTER the iframe has shrunk
  // back to its in-flow slot.
  const galleryHidden = layoutOffset > 0;

  return createElement(
    "div",
    { className, style: outerStyle },
    createElement(
      "div",
      {
        style: {
          width: "100%",
          maxWidth,
          visibility: galleryHidden ? "hidden" : "visible",
        },
      },
      createElement(TemplateGallery, {
        templates: tpls.list,
        onPick: tpls.pick,
        sectionLabel,
        onBrowseAll,
        browseAllLabel,
        minCardWidth,
        tokens: galleryTokens,
        style: { paddingTop: 0, paddingBottom: 0 },
      }),
    ),
    createElement(TemplateModal, {
      template: tpls.selected,
      onClose: tpls.dismiss,
      onConfirm: handleConfirm,
      ctaLabel,
      tokens: modalTokens,
    }),
  );
}
