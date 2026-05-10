/**
 * Live mini-render of a template seed — used as the gallery card cover
 * when the template doesn't ship an explicit ``cover`` PNG.
 *
 * The component mounts a ``<TemplatePreview>`` at a fixed virtual
 * viewport (1280×720 by default) and CSS-scales it down to fit the
 * card's aspect-ratio slot. The result is byte-identical to what the
 * modal opens on click — same bundling pipeline, same iframe, just
 * smaller. No PNG to maintain, no build step, no script.
 *
 * Cost: one esbuild-wasm bundle per visible thumbnail at mount time.
 * For galleries with many templates, wrap the consumer in an
 * IntersectionObserver so only on-screen cards bundle.
 */

import {
  createElement,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";

import { TemplatePreview } from "./TemplatePreview.js";
import type { TemplateSeed } from "./types.js";

export interface TemplateThumbnailProps {
  seed: TemplateSeed;
  /**
   * Width of the virtual viewport the seed renders into. Bigger = more
   * detail but more memory. Default 1280 matches a typical desktop
   * preview.
   */
  virtualWidth?: number;
  /** Height of the virtual viewport. Default 720 (16:9). */
  virtualHeight?: number;
  /**
   * Background of the wrapper. Defaults to white so light-theme seeds
   * don't show the underlying card surface during the bundle phase.
   */
  background?: string;
  className?: string;
}

const _DEFAULT_W = 1280;
const _DEFAULT_H = 720;

export function TemplateThumbnail({
  seed,
  virtualWidth = _DEFAULT_W,
  virtualHeight = _DEFAULT_H,
  background = "#ffffff",
  className,
}: TemplateThumbnailProps) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [scale, setScale] = useState(1);

  // Recompute the scale on mount + whenever the wrapper resizes. We
  // pin the inner virtual viewport at ``virtualWidth × virtualHeight``
  // and let CSS shrink it to fit the card.
  useEffect(() => {
    const wrapper = wrapperRef.current;
    if (!wrapper) return;
    const compute = () => {
      const rect = wrapper.getBoundingClientRect();
      if (rect.width <= 0) return;
      // Match width — the gallery card is aspect-ratio 16/9 so height
      // follows naturally. Pinning to width keeps the layout stable
      // even when the host's height transitions during animations.
      setScale(rect.width / virtualWidth);
    };
    compute();
    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(compute);
      ro.observe(wrapper);
    } else {
      window.addEventListener("resize", compute);
    }
    return () => {
      if (ro) ro.disconnect();
      else window.removeEventListener("resize", compute);
    };
  }, [virtualWidth]);

  const wrapperStyle: CSSProperties = {
    position: "absolute",
    inset: 0,
    overflow: "hidden",
    background,
    pointerEvents: "none",
  };

  const innerStyle: CSSProperties = {
    width: virtualWidth,
    height: virtualHeight,
    transform: `scale(${scale})`,
    transformOrigin: "top left",
    // Lock the layout so the inner viewport never overflows the card
    // before the scale has been computed (first render frame).
    willChange: "transform",
  };

  return createElement(
    "div",
    { ref: wrapperRef, className, style: wrapperStyle },
    createElement(
      "div",
      { style: innerStyle },
      createElement(TemplatePreview, { seed }),
    ),
  );
}
