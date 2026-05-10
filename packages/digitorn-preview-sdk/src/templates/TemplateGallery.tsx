/**
 * Card grid that lets the user pick a template.
 *
 * 1:1 port of Lovable.dev's gallery, inspected against their live
 * DOM at https://lovable.dev/templates:
 *
 *   - Grid: ``grid-template-columns: repeat(auto-fill, minmax(345px, 1fr))``
 *     with 20 px gap. Auto-flows from 1 col on mobile to N cols at any
 *     viewport, with no breakpoint maths.
 *   - Card: no background, no border, no shadow. Just the cover image
 *     above and a title row below — the visual signature is the cover.
 *   - Cover: ``aspect-video`` (16:9) + ``rounded-xl`` (12 px) + 1 px
 *     subtle border. ``object-cover object-top`` so screenshots stay
 *     anchored at the top edge of the crop.
 *   - mb-2 (8 px) gap between cover and text.
 *   - Title row: flex justify-between. Left is title (16/400/24) +
 *     description (14/400/21 muted), both ``truncate`` (single line).
 *     Right is an optional tag pill — the FIRST tag if any is set.
 *   - Tag pill: ``rounded-lg bg-muted px-3 py-2 text-xs muted``.
 *   - Hover: cover opacity 1 → 0.80 in 150 ms cubic-bezier(0.4,0,0.2,1).
 *     No transform, no shadow. Chrome stays still — only the image
 *     dims slightly, the way Lovable does it.
 */

import {
  createElement,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from "react";

import { TemplateThumbnail } from "./TemplateThumbnail.js";
import type { Template } from "./types.js";

export interface TemplateGalleryProps {
  templates: Template[];
  onPick: (template: Template) => void;
  /**
   * Header chip label (e.g. ``"Templates"``). Renders as a small
   * pill on the LEFT of the header row above the grid. Pass empty
   * string (or omit) to hide both the chip and the entire header
   * row when no ``onBrowseAll`` is set either.
   */
  sectionLabel?: string;
  /**
   * When provided, a "Browse all →" link is rendered on the RIGHT of
   * the header row (paired with ``sectionLabel``). The handler is
   * called on click — the SDK does not navigate; the consuming app
   * decides what "browse all" means (a dedicated route, an external
   * URL, opening a drawer, etc).
   */
  onBrowseAll?: () => void;
  /** Custom label for the browse-all link. Default ``"Browse all →"``. */
  browseAllLabel?: string;
  /** Wrapper style override (e.g. ``maxWidth: 1200``). */
  style?: CSSProperties;
  /** Wrapper className. */
  className?: string;
  /**
   * CSS color tokens used by the cards. Defaults map to CSS variables
   * the consuming app can override globally; pass literal colors when
   * embedding outside a themed shell.
   */
  tokens?: GalleryTokens;
  /**
   * Minimum card width before the auto-fill grid wraps to a new
   * column. Default 345 px (Lovable's exact value). Drop to 280 for
   * tighter packing on cramped surfaces.
   */
  minCardWidth?: number;
}

export interface GalleryTokens {
  textBright: string;
  textMuted: string;
  surfaceAlt: string;
  border: string;
  accentPrimary: string;
}

const _DEFAULT_TOKENS: GalleryTokens = {
  textBright: "var(--text-bright, #f5f5f5)",
  textMuted: "var(--text-muted, #a3a3a3)",
  surfaceAlt: "var(--surface-alt, #1a1a1a)",
  border: "var(--border, rgba(255,255,255,0.08))",
  accentPrimary: "var(--accent-primary, #14B8A6)",
};

export function TemplateGallery({
  templates,
  onPick,
  sectionLabel = "Templates",
  onBrowseAll,
  browseAllLabel = "Browse all →",
  style,
  className,
  tokens = _DEFAULT_TOKENS,
  minCardWidth = 345,
}: TemplateGalleryProps) {
  if (templates.length === 0) return null;

  const wrapper: CSSProperties = {
    width: "100%",
    paddingTop: 28,
    paddingBottom: 24,
    ...style,
  };

  const grid: CSSProperties = {
    display: "grid",
    gridTemplateColumns: `repeat(auto-fill, minmax(${minCardWidth}px, 1fr))`,
    gap: 20,
    justifyItems: "stretch",
  };

  const showHeader = !!sectionLabel || !!onBrowseAll;

  return createElement(
    "div",
    { className, style: wrapper },
    showHeader
      ? createElement(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginBottom: 18,
            },
          },
          sectionLabel
            ? createElement(
                "span",
                {
                  style: {
                    fontFamily: "var(--font-sans, system-ui)",
                    fontSize: 13,
                    fontWeight: 500,
                    color: tokens.textBright,
                    letterSpacing: "-0.005em",
                  },
                },
                sectionLabel,
              )
            : createElement("span"),
          onBrowseAll
            ? createElement(
                "a",
                {
                  href: "#",
                  onClick: (e: ReactMouseEvent) => {
                    e.preventDefault();
                    onBrowseAll();
                  },
                  style: {
                    fontFamily: "var(--font-sans, system-ui)",
                    fontSize: 12.5,
                    fontWeight: 400,
                    color: tokens.textMuted,
                    textDecoration: "none",
                    letterSpacing: "-0.005em",
                  },
                },
                browseAllLabel,
              )
            : null,
        )
      : null,
    createElement(
      "div",
      { style: grid },
      templates.map((tpl) =>
        createElement(_TemplateCard, {
          key: tpl.id,
          template: tpl,
          onPick: () => onPick(tpl),
          tokens,
        }),
      ),
    ),
  );
}

interface _CardProps {
  template: Template;
  onPick: () => void;
  tokens: GalleryTokens;
}

function _TemplateCard({ template, onPick, tokens }: _CardProps) {
  const [hover, setHover] = useState(false);
  const [coverFailed, setCoverFailed] = useState(false);

  // Cover resolution priority:
  //   1. Explicit ``template.cover`` URL (if provided and not 404)
  //   2. Live ``<TemplateThumbnail>`` rendering ``template.seed``
  //   3. Letter fallback (no cover, no seed → can't render anything)
  const hasCover = !!template.cover && !coverFailed;
  const hasSeed = !!template.seed;

  const cardStyle: CSSProperties = {
    all: "unset",
    cursor: "pointer",
    display: "flex",
    flexDirection: "column",
    width: "100%",
  };

  const coverWrap: CSSProperties = {
    position: "relative",
    aspectRatio: "16 / 9",
    background: tokens.surfaceAlt,
    overflow: "hidden",
    borderRadius: 12,
    marginBottom: 8,
    border: `1px solid ${tokens.border}`,
    opacity: hover ? 0.8 : 1,
    transition: "opacity 150ms cubic-bezier(0.4, 0, 0.2, 1)",
  };

  const titleRow: CSSProperties = {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: 12,
  };

  const textCol: CSSProperties = {
    minWidth: 0,
    flex: 1,
    textAlign: "left",
    overflow: "hidden",
  };

  const titleStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 14,
    fontWeight: 400,
    color: tokens.textBright,
    lineHeight: "20px",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  };

  const descStyle: CSSProperties = {
    fontFamily: "var(--font-sans, system-ui)",
    fontSize: 12.5,
    fontWeight: 400,
    color: tokens.textMuted,
    lineHeight: "18px",
    marginTop: 0,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  };

  return createElement(
    "button",
    {
      type: "button",
      onClick: onPick,
      onMouseEnter: () => setHover(true),
      onMouseLeave: () => setHover(false),
      style: cardStyle,
      "aria-label": template.title,
    },
    createElement(
      "div",
      { style: coverWrap },
      hasCover
        ? createElement("img", {
            src: template.cover,
            alt: "",
            loading: "lazy",
            decoding: "async",
            onError: () => setCoverFailed(true),
            style: {
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              objectFit: "cover",
              objectPosition: "top",
            },
          })
        : hasSeed
        ? createElement(TemplateThumbnail, { seed: template.seed! })
        : createElement(_CoverFallback, { template, tokens }),
    ),
    createElement(
      "div",
      { style: titleRow },
      createElement(
        "div",
        { style: textCol },
        createElement("span", { style: titleStyle }, template.title),
        template.description.length > 0
          ? createElement("span", { style: { ...descStyle, display: "block" } }, template.description)
          : null,
      ),
    ),
  );
}

function _CoverFallback({ template, tokens }: { template: Template; tokens: GalleryTokens }) {
  const letter = template.title.length > 0
    ? template.title.charAt(0).toUpperCase()
    : "?";
  return createElement(
    "div",
    {
      "aria-hidden": true,
      style: {
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: tokens.textMuted,
        fontSize: 28,
        fontWeight: 600,
        letterSpacing: "-0.02em",
        background: `linear-gradient(135deg, color-mix(in oklab, ${tokens.accentPrimary} 18%, ${tokens.surfaceAlt}), ${tokens.surfaceAlt})`,
      },
    },
    letter,
  );
}
