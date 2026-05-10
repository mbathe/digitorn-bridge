/**
 * Template primitives — declarative starter UX for app empty states.
 *
 * Templates are PURE DATA owned by the consuming app. The SDK
 * provides UI surfaces (gallery, modal, preview iframe) and a thin
 * state hook. The daemon is unaware of templates: an app exports a
 * ``Template[]`` array from its bundle and feeds it to
 * ``<TemplateGallery>``.
 *
 * Picking a template usually triggers two effects in the app:
 * (1) seed the workspace with files via the standard write actions,
 * (2) dispatch a first message that adapts those files via
 * ``useChat().send(template.prompt)``. The SDK does not impose either
 * — the consuming app's ``onConfirm`` callback decides.
 */

export interface TemplateSeed {
  /**
   * Pre-built bundle URL — when set, ``<TemplatePreview>`` skips the
   * runtime esbuild-wasm pipeline and iframes this URL directly.
   * Populated automatically by ``@digitorn/preview-sdk/vite`` when
   * the consumer imports a seed via ``?digitorn-seed``. ~30 ms mount
   * vs ~500-1000 ms for the runtime path.
   */
  bundleUrl?: string;
  /**
   * Map of bundle-relative path → raw source string. Used by the
   * runtime esbuild-wasm fallback (when ``bundleUrl`` is absent) AND
   * by the consuming app to seed an agent workspace via
   * ``useWorkspaceFiles().writeFile``. The Vite plugin populates
   * this alongside ``bundleUrl`` so both surfaces are always in sync.
   */
  files?: Record<string, string>;
  /** Entry file the in-browser bundler starts from. Default ``"src/main.tsx"``. */
  entry?: string;
}

export interface Template {
  /** Stable identifier for the template — used as React key, never shown. */
  id: string;
  /** Card title (Lovable: 16 px / weight 400). */
  title: string;
  /** Card subtitle (Lovable: 14 px / weight 400). */
  description: string;
  /**
   * Optional card cover image. URL string, imported asset, or data URI.
   * When omitted, ``<TemplateGallery>`` renders a live mini-render of
   * the seed via ``<TemplateThumbnail>`` — the dev declares a seed and
   * the cover surfaces for free, byte-identical to the modal preview.
   * Pre-shipping a static cover is still useful when the seed is
   * heavy (long bundling) or when consistency matters (marketing
   * pages with hand-tuned covers).
   */
  cover?: string;
  /** First-turn prompt the consuming app can dispatch on confirm. */
  prompt: string;
  /**
   * Inline seed for the live in-browser preview. When present,
   * ``<TemplatePreview>`` bundles ``seed.files`` via esbuild-wasm
   * and renders the result in a sandboxed iframe.
   */
  seed?: TemplateSeed;
  /**
   * Optional URL the modal iframes instead of bundling the seed.
   * Use when the preview lives on an external service (live demo
   * site, Storybook, screenshot tool). When both ``seed`` and
   * ``previewUrl`` are present, ``previewUrl`` wins.
   */
  previewUrl?: string;
  /** Free-form tags shown in the modal sidebar. */
  tags?: string[];
}

export interface TemplateBundleError {
  file: string;
  line: number;
  column: number;
  message: string;
}

export type TemplateBundleStatus =
  | { status: "idle" }
  | { status: "bundling" }
  | { status: "ready"; bundleUrl: string; bytes: number; durationMs: number }
  | { status: "error"; errors: TemplateBundleError[] };
