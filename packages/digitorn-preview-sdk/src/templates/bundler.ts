/**
 * In-browser bundler powered by esbuild-wasm.
 *
 * Receives a ``Map<path, content>`` of source files, bundles them
 * starting from an entry point, and returns a Blob URL pointing at
 * the compiled JS. ``<TemplatePreview>`` writes the bundled URL into
 * a sandboxed iframe via ``<script type="module">`` and imports
 * resolve through the iframe's importmap.
 *
 * Imports inside the source are resolved in this priority order:
 *   1. Relative paths (``./Foo``, ``../bar``) → other entries in the file map.
 *   2. Bare imports (``react``, ``framer-motion``) → esm.sh CDN.
 *
 * Apps `import { bundleFiles } from "@digitorn/preview-sdk"` and own
 * the lifecycle (re-bundle on file change, revoke Blob URLs). The
 * library never spawns workers without ``ensureEsbuildReady`` being
 * called first.
 *
 * Pure utility — no React, no DOM. Apps that want a higher-level
 * surface use ``<TemplatePreview seed={...} />`` which wraps this.
 */

import type { TemplateBundleError } from "./types.js";

// Note: esbuild-wasm is a peerDependency of the SDK so the consuming
// app installs it once and the type information lines up. We import
// from the package, not a CDN, because esbuild's API isn't stable
// across versions and we want type safety at SDK build time.
import * as esbuild from "esbuild-wasm";

let _initPromise: Promise<void> | null = null;

const ESM_CDN = "https://esm.sh";

export async function ensureEsbuildReady(): Promise<void> {
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    // Pin the wasm URL to the installed JS host version. esbuild
    // refuses to start when the two diverge ("Host version X does
    // not match binary version Y"). Reading ``esbuild.version`` at
    // runtime keeps the two locked even when the host package is
    // bumped.
    await esbuild.initialize({
      wasmURL: `${ESM_CDN}/esbuild-wasm@${esbuild.version}/esbuild.wasm`,
      worker: true,
    });
  })();
  return _initPromise;
}

export interface BundleResult {
  ok: true;
  /** Object URL of the bundled JS. Caller MUST ``URL.revokeObjectURL`` it. */
  url: string;
  /** Total bytes of the bundle (gzip not measured here). */
  size: number;
  /** Wall-clock duration of the build in ms. */
  durationMs: number;
}

export interface BundleFailure {
  ok: false;
  errors: TemplateBundleError[];
  durationMs: number;
}

export type BundleOutcome = BundleResult | BundleFailure;

function _normalizePath(p: string): string {
  return p.replace(/^\.\//, "").replace(/\\/g, "/");
}

function _resolveRelative(importer: string, target: string): string | null {
  if (!target.startsWith(".")) return null;
  const importerParts = importer.split("/").slice(0, -1);
  const targetParts = target.split("/");
  for (const part of targetParts) {
    if (part === "" || part === ".") continue;
    if (part === "..") importerParts.pop();
    else importerParts.push(part);
  }
  return importerParts.join("/");
}

function _tryExtensions(files: Map<string, string>, base: string): string | null {
  if (files.has(base)) return base;
  const exts = [".tsx", ".ts", ".jsx", ".js", ".mjs", ".json"];
  for (const ext of exts) {
    if (files.has(base + ext)) return base + ext;
  }
  for (const ext of exts) {
    if (files.has(base + "/index" + ext)) return base + "/index" + ext;
  }
  return null;
}

function _virtualFsPlugin(files: Map<string, string>): esbuild.Plugin {
  return {
    name: "digitorn-template-vfs",
    setup(build) {
      build.onResolve({ filter: /.*/ }, (args) => {
        if (args.kind === "entry-point") {
          return { path: _normalizePath(args.path), namespace: "vfs" };
        }
        if (args.path.startsWith(".")) {
          const importer = args.importer || "";
          const importerInVfs = importer.replace(/^vfs:/, "");
          const resolved = _resolveRelative(importerInVfs, args.path);
          if (!resolved) {
            return {
              errors: [{
                text: `cannot resolve relative ${args.path} from ${importer}`,
              }],
            };
          }
          const found = _tryExtensions(files, resolved);
          if (!found) {
            return {
              errors: [{
                text: `file not found: ${resolved} (from ${importer})`,
              }],
            };
          }
          return { path: found, namespace: "vfs" };
        }
        if (args.path.startsWith("/") || args.path.startsWith("http")) {
          return { path: args.path, external: true };
        }
        return { path: `${ESM_CDN}/${args.path}?bundle&dev`, external: true };
      });

      build.onLoad({ filter: /.*/, namespace: "vfs" }, (args) => {
        const content = files.get(args.path);
        if (content === undefined) {
          return { errors: [{ text: `vfs load miss: ${args.path}` }] };
        }
        const loader = args.path.endsWith(".tsx")
          ? "tsx"
          : args.path.endsWith(".ts")
          ? "ts"
          : args.path.endsWith(".jsx")
          ? "jsx"
          : args.path.endsWith(".json")
          ? "json"
          : "js";
        return { contents: content, loader };
      });
    },
  };
}

/**
 * Bundle a virtual filesystem into a single ESM module URL.
 *
 * Caller is responsible for revoking the returned ``url`` once the
 * iframe has consumed it. ``<TemplatePreview>`` does this
 * automatically.
 */
export async function bundleFiles(
  files: Map<string, string>,
  entry: string,
): Promise<BundleOutcome> {
  await ensureEsbuildReady();
  const start = performance.now();
  const normalizedEntry = _normalizePath(entry);
  if (!files.has(normalizedEntry)) {
    return {
      ok: false,
      errors: [{
        file: normalizedEntry, line: 0, column: 0,
        message: `entry file not found: ${normalizedEntry}`,
      }],
      durationMs: performance.now() - start,
    };
  }

  try {
    const result = await esbuild.build({
      entryPoints: [normalizedEntry],
      bundle: true,
      write: false,
      format: "esm",
      target: "es2020",
      jsx: "automatic",
      jsxImportSource: "react",
      sourcemap: "inline",
      plugins: [_virtualFsPlugin(files)],
      logLevel: "silent",
    });

    const out = result.outputFiles?.[0];
    if (!out) {
      return {
        ok: false,
        errors: [{
          file: normalizedEntry, line: 0, column: 0,
          message: "esbuild produced no output",
        }],
        durationMs: performance.now() - start,
      };
    }

    const blob = new Blob(
      [out.contents as BlobPart],
      { type: "application/javascript" },
    );
    return {
      ok: true,
      url: URL.createObjectURL(blob),
      size: out.contents.length,
      durationMs: performance.now() - start,
    };
  } catch (e) {
    const errs = (e as { errors?: Array<{
      text: string;
      location?: { file?: string; line?: number; column?: number };
    }> })?.errors ?? [];
    return {
      ok: false,
      errors: errs.length > 0
        ? errs.map((er) => ({
            file: er.location?.file ?? normalizedEntry,
            line: er.location?.line ?? 0,
            column: er.location?.column ?? 0,
            message: er.text,
          }))
        : [{
            file: normalizedEntry, line: 0, column: 0,
            message: String(e),
          }],
      durationMs: performance.now() - start,
    };
  }
}

/**
 * The minimal HTML the sandboxed iframe loads. The bundled blob URL
 * is injected as ``<script type="module" src=...>`` AFTER this HTML
 * is written, so the importmap is in place by the time the bundle
 * tries to ``import "react"``.
 */
export const TEMPLATE_IFRAME_HTML = `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
html, body { margin: 0; padding: 0; height: 100%; background: #fff; font-family: system-ui, sans-serif; }
#root { height: 100%; }
.dt-error { padding: 20px; background: #fee; color: #900; font-family: ui-monospace, monospace; white-space: pre-wrap; }
/* Premium scrollbar - thin neutral thumb, no track at all. The
   border-clip trick used elsewhere paints the gutter visibly under
   the thumb; here the thumb fills the 5 px column directly so the
   track stays fully invisible. Hover bumps the thumb to ~50% so the
   user knows it is interactive. Works on light and dark template
   themes (mid-grey is readable on both). */
* { scrollbar-width: thin; scrollbar-color: rgba(120, 120, 120, 0.25) transparent; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(120, 120, 120, 0.25); border-radius: 999px; }
::-webkit-scrollbar-thumb:hover { background: rgba(120, 120, 120, 0.5); }
::-webkit-scrollbar-corner { background: transparent; }
</style>
</head>
<body>
<div id="root"></div>
<script type="importmap">
{
  "imports": {
    "react": "${ESM_CDN}/react@18?bundle&dev",
    "react-dom": "${ESM_CDN}/react-dom@18?bundle&dev",
    "react-dom/client": "${ESM_CDN}/react-dom@18/client?bundle&dev",
    "react/jsx-runtime": "${ESM_CDN}/react@18/jsx-runtime?bundle&dev",
    "react/jsx-dev-runtime": "${ESM_CDN}/react@18/jsx-dev-runtime?bundle&dev"
  }
}
</script>
<script type="module" id="entry"></script>
<script>
window.addEventListener("error", (e) => {
  const root = document.getElementById("root");
  if (root) root.innerHTML = '<div class="dt-error">Runtime error: ' + (e.message || (e.error && e.error.message) || String(e)) + '</div>';
  parent.postMessage({ type: "digitorn:template-preview:runtime-error", message: e.message || String(e) }, "*");
});
window.addEventListener("unhandledrejection", (e) => {
  const root = document.getElementById("root");
  if (root) root.innerHTML = '<div class="dt-error">Unhandled promise rejection: ' + ((e.reason && e.reason.message) || String(e.reason)) + '</div>';
  parent.postMessage({ type: "digitorn:template-preview:runtime-error", message: String(e.reason) }, "*");
});
</script>
</body>
</html>`;
