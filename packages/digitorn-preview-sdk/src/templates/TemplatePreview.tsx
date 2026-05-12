/**
 * Live in-browser preview of a template seed.
 *
 * Pass a ``TemplateSeed`` (or just ``files`` + ``entry``) and the
 * component bundles the files via esbuild-wasm and renders the
 * result in a sandboxed iframe. The bundler is keyed off the
 * stringified file list so re-renders without source changes don't
 * trigger redundant rebuilds.
 *
 * Premium chrome:
 *   - ``chrome="browser"`` adds a mac-style top bar (3 dots + URL
 *     pill) above the iframe. Opt-in: thumbnails leave it off, the
 *     modal / workspace preview turn it on for a real "device" feel.
 *   - Loading state shows a subtle shimmer skeleton, not blank white.
 *   - Errors render in a clean panel with a heading + monospaced
 *     details (apps can still pass ``errorSlot`` to override).
 *   - Content fades in (200 ms) when the bundle becomes ready —
 *     no flash of empty/old.
 *
 * When the template carries a ``previewUrl`` instead of a seed, use
 * a plain ``<iframe src={previewUrl}>`` from the consuming app —
 * this component is for the ``seed`` path only.
 */

import {
  createElement,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";

import { bundleFiles } from "./bundler.js";
import { HtmlPreview } from "./HtmlPreview.js";
import {
  getPreviewKind,
  registerPreviewKind,
  type PreviewChrome as RegistryPreviewChrome,
} from "./registry.js";
import { TemplateSandbox } from "./Sandbox.js";
import type { TemplateBundleError, TemplateBundleStatus, TemplateSeed } from "./types.js";

export type PreviewChrome = RegistryPreviewChrome;

export interface TemplatePreviewProps {
  seed: TemplateSeed;
  /**
   * Renderer kind. Default ``"react"`` (esbuild-wasm pipeline).
   * Set to ``"html"``, ``"markdown"``, or any kind registered via
   * ``registerPreviewKind`` to delegate rendering to a custom renderer.
   * The dispatcher in this component looks up the renderer and forwards
   * all other props — non-React renderers ignore React-specific options
   * like ``onStatus`` / ``errorSlot``.
   */
  kind?: string;
  /** Optional callback fired on every status transition. */
  onStatus?: (status: TemplateBundleStatus) => void;
  /** Wrapper style. */
  style?: CSSProperties;
  /** Wrapper className. */
  className?: string;
  /** Render slot for the loading state (overrides the default skeleton). */
  loadingSlot?: ReactNode;
  /** Render slot for the error state (overrides the default panel). */
  errorSlot?: (errors: TemplateBundleError[]) => ReactNode;
  /**
   * Adds chrome around the iframe. ``"browser"`` paints a mac-style
   * top bar (3 dots + URL pill). ``"none"`` (default) keeps the
   * surface bare — used by thumbnails and any host that already
   * provides its own framing.
   */
  chrome?: PreviewChrome;
  /**
   * URL text shown inside the chrome's address pill. Cosmetic only.
   * Default: empty (just renders an icon hint).
   */
  chromeUrl?: string;
  /**
   * Background of the iframe surface. Defaults to white so light-theme
   * seeds don't show the host surface during the bundle phase. Switch
   * to ``"transparent"`` when the seed is known to paint its own bg.
   */
  background?: string;
}

const _DEFAULT_ENTRY = "src/main.tsx";
const _STYLE_ID = "_digi_preview_styles";

// Inject the keyframes + transitions once per document. Idempotent —
// same id means the browser dedupes the rule set.
function _ensureStyles() {
  if (typeof document === "undefined") return;
  if (document.getElementById(_STYLE_ID)) return;
  const el = document.createElement("style");
  el.id = _STYLE_ID;
  el.textContent = `
@keyframes _digi_shimmer {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(100%); }
}
@keyframes _digi_fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.digi-preview-fade { animation: _digi_fade-in 200ms ease-out both; }
.digi-preview-shimmer-bar {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
}
.digi-preview-shimmer-bar::before {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.04) 50%,
    transparent 100%
  );
  animation: _digi_shimmer 1.6s ease-in-out infinite;
}
`;
  document.head.appendChild(el);
}

/**
 * Public dispatcher — picks the renderer based on ``kind`` and
 * forwards all other props. Default kind is ``"react"`` so existing
 * consumers (lovable + everyone who imports ``<TemplatePreview>``
 * without thinking about kind) keep getting the esbuild-wasm pipeline.
 *
 * ``createElement(renderer, props)`` (instead of calling the renderer
 * inline) is what makes React treat each kind as its own component
 * instance — its hooks live in a separate Fiber, so the dispatcher
 * itself stays hook-free and can flip between kinds without
 * tripping "rendered fewer hooks than expected".
 */
export function TemplatePreview(props: TemplatePreviewProps) {
  const kind = props.kind ?? "react";
  if (kind !== "react") {
    const renderer = getPreviewKind(kind);
    if (renderer) {
      const { errorSlot: _, onStatus: __, ...rest } = props;
      void _;
      void __;
      // ``createElement`` accepts any function component; the cast
       // narrows our generic renderer signature to React's FC shape.
      const Renderer = renderer as (p: typeof rest & { kind: string }) => ReactNode;
      return createElement(Renderer, { ...rest, kind });
    }
    // No registered renderer — warn so devs notice the typo / missing
    // setup, then fall through to React so the iframe paints something
    // instead of going blank. Apps registering custom kinds at boot
    // never hit this path; misconfigured ones now have a console
    // breadcrumb instead of silent failure.
    if (typeof console !== "undefined") {
      console.warn(
        `[@digitorn/preview-sdk] No renderer registered for kind "${kind}". ` +
        `Falling back to ReactPreview. Call registerPreviewKind("${kind}", ...) at boot.`,
      );
    }
  }
  return createElement(ReactPreview, props);
}

/**
 * React renderer — esbuild-wasm bundles the seed and iframes the
 * result. Exported separately so apps that want the React surface
 * specifically (without going through the kind dispatcher) can use
 * it directly.
 */
export function ReactPreview({
  seed,
  onStatus,
  style,
  className,
  loadingSlot,
  errorSlot,
  chrome = "none",
  chromeUrl = "",
  background = "#ffffff",
}: TemplatePreviewProps) {
  useEffect(() => { _ensureStyles(); }, []);

  // Fast path: when the Vite plugin pre-bundled the seed, just iframe
  // the resulting page directly. Skips esbuild-wasm entirely. We
  // still track ``onLoad`` to overlay a skeleton until the bundled
  // page actually finishes painting — otherwise the user sees a
  // brief white flash while the JS chunk fetches and React mounts.
  if (seed.bundleUrl) {
    return createElement(_FastPathPreview, {
      bundleUrl: seed.bundleUrl,
      chrome,
      chromeUrl,
      background,
      style,
      className,
      loadingSlot,
    });
  }

  const fileMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const [k, v] of Object.entries(seed.files || {})) {
      if (typeof v === "string") m.set(k, v);
    }
    return m;
  }, [seed.files]);

  const entry: string = seed.entry || _DEFAULT_ENTRY;

  const [status, setStatus] = useState<TemplateBundleStatus>({ status: "idle" });
  const lastUrlRef = useRef<string | null>(null);

  useEffect(() => {
    if (fileMap.size === 0) return;
    let cancelled = false;
    setStatus({ status: "bundling" });
    onStatus?.({ status: "bundling" });

    const t = setTimeout(async () => {
      const result = await bundleFiles(fileMap, entry);
      if (cancelled) {
        if (result.ok) URL.revokeObjectURL(result.url);
        return;
      }
      if (result.ok) {
        if (lastUrlRef.current) {
          URL.revokeObjectURL(lastUrlRef.current);
        }
        lastUrlRef.current = result.url;
        const next: TemplateBundleStatus = {
          status: "ready",
          bundleUrl: result.url,
          bytes: result.size,
          durationMs: result.durationMs,
        };
        setStatus(next);
        onStatus?.(next);
      } else {
        const next: TemplateBundleStatus = {
          status: "error",
          errors: result.errors,
        };
        setStatus(next);
        onStatus?.(next);
      }
    }, 100);

    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [fileMap, entry, onStatus]);

  useEffect(() => {
    return () => {
      if (lastUrlRef.current) {
        URL.revokeObjectURL(lastUrlRef.current);
        lastUrlRef.current = null;
      }
    };
  }, []);

  let body: ReactNode;
  if (status.status === "error") {
    body = errorSlot
      ? errorSlot(status.errors)
      : _renderErrorPanel(status.errors);
  } else if (status.status !== "ready") {
    body = loadingSlot ?? _renderSkeleton();
  } else {
    body = createElement(TemplateSandbox, {
      bundleUrl: status.bundleUrl,
      className: "digi-preview-fade",
    });
  }

  return _renderShell({ chrome, chromeUrl, background, style, className, body });
}

// ── Fast-path iframe with skeleton-until-loaded ──────────────────────
//
// When a pre-bundled URL is available we'd usually just slap an iframe
// in. The catch: the iframe fetches its HTML + JS chunks asynchronously
// and paints white in the meantime. On a cold cache / first-open this
// flash is jarring. We overlay the same skeleton the slow path uses,
// listen for ``onLoad``, then fade the iframe in + the skeleton out.
//
// ``forceShow`` after 2.5s catches the rare case where ``onLoad`` never
// fires (cross-origin redirects, dev-server proxy hiccups) — better to
// reveal the iframe than to stay stuck on the skeleton forever.
interface _FastPathProps {
  bundleUrl: string;
  chrome: PreviewChrome;
  chromeUrl: string;
  background: string;
  style?: CSSProperties;
  className?: string;
  loadingSlot?: ReactNode;
}

function _FastPathPreview({
  bundleUrl,
  chrome,
  chromeUrl,
  background,
  style,
  className,
  loadingSlot,
}: _FastPathProps) {
  useEffect(() => { _ensureStyles(); }, []);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setReady(false);
    const fallback = window.setTimeout(() => setReady(true), 2500);
    return () => window.clearTimeout(fallback);
  }, [bundleUrl]);

  const body = createElement(
    "div",
    {
      style: {
        position: "relative" as const,
        width: "100%",
        height: "100%",
        background,
      },
    },
    createElement("iframe", {
      src: bundleUrl,
      title: "preview",
      sandbox:
        "allow-scripts allow-same-origin allow-modals allow-forms allow-popups",
      onLoad: () => setReady(true),
      style: {
        width: "100%",
        height: "100%",
        border: "none",
        background,
        display: "block",
        opacity: ready ? 1 : 0,
        transition: "opacity 240ms ease-out",
      },
    }),
    ready
      ? null
      : createElement(
          "div",
          {
            style: {
              position: "absolute" as const,
              inset: 0,
              pointerEvents: "none" as const,
              transition: "opacity 240ms ease-out",
            },
          },
          loadingSlot ?? _renderSkeleton(),
        ),
  );

  return _renderShell({ chrome, chromeUrl, background, style, className, body });
}

// ── Shell + chrome ────────────────────────────────────────────────────

function _renderShell({
  chrome,
  chromeUrl,
  background,
  style,
  className,
  body,
}: {
  chrome: PreviewChrome;
  chromeUrl: string;
  background: string;
  style?: CSSProperties;
  className?: string;
  body: ReactNode;
}) {
  const outer: CSSProperties = {
    position: "relative",
    width: "100%",
    height: "100%",
    display: "flex",
    flexDirection: "column",
    background,
    overflow: "hidden",
    ...style,
  };

  if (chrome === "none") {
    return createElement("div", { className, style: outer }, body);
  }

  const surface: CSSProperties = {
    position: "relative",
    flex: 1,
    minHeight: 0,
    background,
    overflow: "hidden",
  };

  return createElement(
    "div",
    { className, style: outer },
    _renderBrowserChrome(chromeUrl),
    createElement("div", { style: surface }, body),
  );
}

function _renderBrowserChrome(url: string) {
  const bar: CSSProperties = {
    flex: "0 0 auto",
    display: "flex",
    alignItems: "center",
    gap: 14,
    padding: "11px 16px",
    background: "rgba(15, 18, 24, 0.85)",
    borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
  };
  const dot = (color: string): CSSProperties => ({
    width: 10,
    height: 10,
    borderRadius: 999,
    background: color,
    flex: "0 0 auto",
  });
  const dots: CSSProperties = { display: "flex", gap: 6, flex: "0 0 auto" };
  const pill: CSSProperties = {
    flex: 1,
    minWidth: 0,
    height: 24,
    borderRadius: 6,
    background: "rgba(255, 255, 255, 0.04)",
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "0 10px",
    fontFamily:
      'ui-monospace, "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    fontSize: 11,
    color: "rgba(255, 255, 255, 0.45)",
    letterSpacing: 0,
    overflow: "hidden",
  };
  // No URL = drop the pill entirely. Just the 3 dots remain — clean
  // window-controls-only chrome instead of an empty address-bar shape.
  const showUrl = url && url.length > 0;
  const lockIcon = createElement(
    "svg",
    {
      "aria-hidden": true,
      width: 9,
      height: 11,
      viewBox: "0 0 9 11",
      fill: "none",
      style: { flex: "0 0 auto", opacity: 0.55 },
    },
    createElement("path", {
      d: "M2.25 4.5V3a2.25 2.25 0 1 1 4.5 0v1.5M1.5 4.5h6a.75.75 0 0 1 .75.75V10a.75.75 0 0 1-.75.75h-6A.75.75 0 0 1 .75 10V5.25a.75.75 0 0 1 .75-.75Z",
      stroke: "currentColor",
      strokeWidth: 1,
      strokeLinecap: "round",
      strokeLinejoin: "round",
    }),
  );
  return createElement(
    "div",
    { style: bar },
    createElement(
      "div",
      { style: dots },
      createElement("span", { "aria-hidden": true, style: dot("#ff5f57") }),
      createElement("span", { "aria-hidden": true, style: dot("#febc2e") }),
      createElement("span", { "aria-hidden": true, style: dot("#28c840") }),
    ),
    showUrl
      ? createElement(
          "div",
          { style: pill },
          lockIcon,
          createElement(
            "span",
            {
              style: {
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              },
            },
            url,
          ),
        )
      : null,
  );
}

// ── Default loading + error states ────────────────────────────────────

function _renderSkeleton(): ReactNode {
  const inner: CSSProperties = {
    position: "absolute",
    inset: 0,
    display: "flex",
    flexDirection: "column",
    gap: 14,
    padding: 28,
    background:
      "linear-gradient(180deg, rgba(255,255,255,0.02) 0%, rgba(255,255,255,0) 100%), #111827",
  };
  const blockBase: CSSProperties = {
    background: "rgba(255, 255, 255, 0.04)",
    borderRadius: 8,
    border: "1px solid rgba(255, 255, 255, 0.04)",
  };
  const titleBlock: CSSProperties = { ...blockBase, height: 22, width: "60%", borderRadius: 6 };
  const lineBlock = (w: string): CSSProperties => ({ ...blockBase, height: 12, width: w, borderRadius: 4 });
  const card: CSSProperties = { ...blockBase, height: 96, marginTop: 10 };
  return createElement(
    "div",
    { style: inner },
    createElement("div", { style: titleBlock }),
    createElement("div", { style: lineBlock("85%") }),
    createElement("div", { style: lineBlock("70%") }),
    createElement("div", { style: card }),
    createElement("div", { className: "digi-preview-shimmer-bar" }),
  );
}

function _renderErrorPanel(errors: TemplateBundleError[]): ReactNode {
  const wrap: CSSProperties = {
    position: "absolute",
    inset: 0,
    display: "flex",
    flexDirection: "column",
    background: "#0F1218",
    color: "#e2e8f0",
    fontFamily: "var(--font-sans, system-ui)",
  };
  const header: CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "14px 18px",
    borderBottom: "1px solid rgba(255, 255, 255, 0.06)",
    flex: "0 0 auto",
  };
  const badge: CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: 22,
    height: 22,
    borderRadius: 999,
    background: "rgba(248, 113, 113, 0.14)",
    color: "#fca5a5",
    fontSize: 13,
    fontWeight: 600,
  };
  const title: CSSProperties = {
    fontSize: 13,
    fontWeight: 500,
    color: "#f5f5f5",
    letterSpacing: "-0.005em",
  };
  const subtitle: CSSProperties = {
    fontSize: 12,
    color: "#94a3b8",
    marginLeft: "auto",
    fontFamily:
      'ui-monospace, "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
  };
  const body: CSSProperties = {
    flex: 1,
    minHeight: 0,
    overflow: "auto",
    padding: "12px 18px 18px",
    fontFamily:
      'ui-monospace, "JetBrains Mono", "SFMono-Regular", Menlo, monospace',
    fontSize: 12,
    lineHeight: 1.55,
    whiteSpace: "pre-wrap",
    color: "#cbd5e1",
  };
  const errorRow: CSSProperties = { marginBottom: 14 };
  const errLoc: CSSProperties = {
    color: "#fca5a5",
    fontWeight: 500,
    marginBottom: 4,
  };
  return createElement(
    "div",
    { className: "digi-preview-fade", style: wrap },
    createElement(
      "div",
      { style: header },
      createElement("span", { style: badge }, "!"),
      createElement("span", { style: title }, "Bundle failed"),
      createElement(
        "span",
        { style: subtitle },
        `${errors.length} error${errors.length === 1 ? "" : "s"}`,
      ),
    ),
    createElement(
      "div",
      { style: body },
      errors.map((er, i) =>
        createElement(
          "div",
          { key: i, style: errorRow },
          createElement(
            "div",
            { style: errLoc },
            `${er.file}:${er.line}:${er.column}`,
          ),
          createElement("div", null, er.message),
        ),
      ),
    ),
  );
}


// ── Built-in kind registration ───────────────────────────────────────
//
// Side-effect import — ensures the two built-in renderers (``react``
// and ``html``) are available the moment any code touches the SDK.
// Apps register custom kinds (``latex``, ``slides``, etc.) on top of
// these via ``registerPreviewKind`` at boot time.

registerPreviewKind("react", ReactPreview as unknown as Parameters<typeof registerPreviewKind>[1]);
registerPreviewKind("html", HtmlPreview as unknown as Parameters<typeof registerPreviewKind>[1]);
