/**
 * Sandboxed iframe that renders a bundled-JS module.
 *
 * Internal building block of ``<TemplatePreview>``. Public API
 * surface should usually go through ``<TemplatePreview>`` instead;
 * this lower-level component is exported for apps that want to feed
 * a bundled URL directly (e.g. served by a build step at deploy
 * time, not bundled at runtime).
 *
 * Sandbox flags MUST include ``allow-same-origin`` because the
 * parent writes IFRAME_HTML into ``contentDocument`` via
 * ``doc.open()``. Without it, the iframe gets an opaque origin and
 * the parent can't poke the document — the inner doc stays blank.
 */

import {
  createElement,
  useEffect,
  useRef,
  type CSSProperties,
} from "react";

import { TEMPLATE_IFRAME_HTML } from "./bundler.js";

export interface TemplateSandboxProps {
  /**
   * Object URL pointing at the bundled JS to load. Caller (typically
   * ``<TemplatePreview>``) is responsible for revoking it.
   */
  bundleUrl: string | null;
  /** Optional ARIA title for accessibility. */
  title?: string;
  /** Inline style override for the iframe element. */
  style?: CSSProperties;
}

export function TemplateSandbox({
  bundleUrl,
  title = "preview",
  style,
}: TemplateSandboxProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    if (!bundleUrl) return;
    const iframe = iframeRef.current;
    if (!iframe) return;
    const doc = iframe.contentDocument;
    if (!doc) return;
    doc.open();
    doc.write(TEMPLATE_IFRAME_HTML);
    doc.close();
    // Microtask delay: doc.write closes synchronously but the
    // browser's parser hasn't yet attached the importmap script.
    // 30 ms is the smallest interval that's reliable across
    // Chromium / Firefox / Safari for this pattern; below ~16 ms
    // we sometimes inject before the importmap registers.
    const t = setTimeout(() => {
      const docNow = iframe.contentDocument;
      if (!docNow) return;
      const old = docNow.getElementById("entry");
      if (old) old.remove();
      const script = docNow.createElement("script");
      script.type = "module";
      script.id = "entry";
      script.src = bundleUrl;
      docNow.body.appendChild(script);
    }, 30);

    return () => {
      clearTimeout(t);
      // Note: we deliberately don't revoke ``bundleUrl`` here. The
      // bundle producer (``<TemplatePreview>``) owns its lifecycle
      // — revoking too eagerly here would race a fast re-bundle
      // and leave the iframe with a broken src.
    };
  }, [bundleUrl]);

  const finalStyle: CSSProperties = {
    width: "100%",
    height: "100%",
    border: "none",
    background: "white",
    ...style,
  };

  return createElement("iframe", {
    ref: iframeRef,
    title,
    // ``allow-same-origin`` is REQUIRED for the doc.write trick.
    // Adding ``allow-scripts`` on the same iframe is normally a
    // sandbox-defeating combination, but template previews are
    // showing the consuming app's OWN seed — the threat model is
    // identical to running the app directly.
    sandbox: "allow-scripts allow-same-origin allow-modals allow-forms allow-popups",
    style: finalStyle,
  });
}
