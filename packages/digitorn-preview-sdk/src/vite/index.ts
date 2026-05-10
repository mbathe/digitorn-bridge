/**
 * @digitorn/preview-sdk/vite — auto-discover and pre-bundle template seeds.
 *
 * Every directory matching ``<seedsDir>/<id>/App.tsx`` is treated as
 * a self-contained mini app. The plugin:
 *
 *   1. Generates a virtual ``main.tsx`` and ``index.html`` for each
 *      seed so the dev only writes ``App.tsx``.
 *   2. Adds each seed as a Rollup input → Vite emits one HTML page +
 *      its hashed JS chunks under ``dist/seeds/<id>/``.
 *   3. Resolves ``import seed from "./seeds/<id>?digitorn-seed"`` to
 *      a JSON object the SDK runtime understands:
 *
 *        { bundleUrl: "/seeds/<id>/",
 *          files: { "src/main.tsx": "...", "src/App.tsx": "..." },
 *          entry: "src/main.tsx" }
 *
 *      ``bundleUrl`` lets ``<TemplatePreview>`` skip esbuild-wasm and
 *      iframe the pre-built page directly (~30 ms mount). ``files``
 *      stays available so the consuming app can still seed an agent
 *      workspace via ``useWorkspaceFiles().writeFile``.
 *
 * Usage:
 *
 *   // vite.config.ts
 *   import { digitornTemplateSeeds } from "@digitorn/preview-sdk/vite";
 *
 *   export default {
 *     plugins: [react(), digitornTemplateSeeds()],
 *     base: "./",
 *   };
 *
 * Convention is everything: drop ``src/templates/seeds/<id>/App.tsx``
 * and the rest is automatic. No Rollup config, no manual entries, no
 * extra ``main.tsx`` / ``index.html`` boilerplate per seed.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { Plugin, ResolvedConfig } from "vite";

export interface DigitornTemplateSeedsOptions {
  /**
   * Directory holding ``<id>/App.tsx`` subfolders, relative to the
   * Vite project root. Default ``src/templates/seeds``. Use
   * ``seedsDirs`` when you want to mix several sources (e.g. SDK
   * library + app-specific overrides).
   */
  seedsDir?: string;
  /**
   * Multiple directories holding ``<id>/App.tsx`` subfolders. Each
   * entry is either an absolute path (e.g. exported by an SDK
   * package) or a path relative to the Vite project root. Seeds are
   * discovered from every dir, indexed by id. **Later entries
   * override earlier ones** when the id collides — feed your
   * app-specific seeds last to override library defaults.
   *
   * Typical setup:
   *
   * ```ts
   * import { TEMPLATES_SEEDS_DIR } from "@digitorn/templates/vite";
   *
   * digitornTemplateSeeds({
   *   seedsDirs: [
   *     TEMPLATES_SEEDS_DIR,    // shared library (lower priority)
   *     "src/templates/seeds",  // app-specific (overrides library)
   *   ],
   * })
   * ```
   */
  seedsDirs?: string[];
  /**
   * Where the built seed pages are written under the Vite output
   * dir. Default ``seeds`` (so ``dist/seeds/<id>/index.html``).
   */
  outDir?: string;
  /**
   * App entry filename inside each seed folder. Default ``App.tsx``.
   * The plugin reads this file's source as the seed's bundled JSX.
   */
  appFile?: string;
}

interface SeedManifest {
  id: string;
  rootDir: string;           // absolute path to seeds/<id>
  appFilePath: string;       // absolute path to seeds/<id>/App.tsx
  appSource: string;         // raw text of App.tsx
  /**
   * Map of workspace-relative path → file source, covering EVERY
   * ``.ts(x)`` / ``.js(x)`` / ``.css`` / ``.json`` / ``.md`` file
   * found under the seed dir. Lets the consuming app seed an agent
   * workspace with the complete project tree (not just App.tsx) when
   * the user clicks "Use this template". Bundling itself is handled
   * by Vite — imports inside App.tsx resolve transitively through
   * Rollup, so multi-file seeds work at preview time without any
   * change to the bundling pipeline.
   */
  files: Record<string, string>;
  virtualHtmlId: string;     // unique id for the virtual index.html
  virtualMainId: string;     // unique id for the virtual main.tsx
}

const _SEEDABLE_EXTS = new Set([
  ".ts", ".tsx", ".js", ".jsx", ".css", ".json", ".md", ".html", ".svg",
]);

const SEED_QUERY = "?digitorn-seed";
// File-system-safe virtual ids — Rollup uses these as output chunk
// names on Windows where ``:`` and ``/`` in IDs explode mkdir. The
// ``writeBundle`` hook moves the emitted files to their final
// ``dist/<outDir>/<id>/`` slots.
const VIRTUAL_HTML_PREFIX = "__digitorn_seed_html_";
const VIRTUAL_MAIN_PREFIX = "__digitorn_seed_main_";

const _MAIN_TEMPLATE = `import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "__APP_PATH__";
const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
`;

// Tailwind v3 Play CDN — works out of the box, no config file required.
// Used by ALL seed iframes so any template can drop in utility classes
// without per-app Tailwind setup. The CDN script self-installs a JIT
// compiler that scans the rendered DOM at runtime; classes that aren't
// used in the seed don't ship CSS. Caches well across iframes (same
// URL = browser hits the cache for every thumbnail after the first).
const _TAILWIND_CDN = "https://cdn.tailwindcss.com";

const _HTML_TEMPLATE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__</title>
<script src="${_TAILWIND_CDN}"></script>
<style>html,body,#root{margin:0;padding:0;height:100%;}body{font-family:system-ui,-apple-system,sans-serif;}</style>
</head>
<body>
<div id="root"></div>
<script type="module" src="__MAIN_PATH__"></script>
</body>
</html>
`;

export function digitornTemplateSeeds(
  opts: DigitornTemplateSeedsOptions = {},
): Plugin {
  const outDir = opts.outDir ?? "seeds";
  const appFile = opts.appFile ?? "App.tsx";

  let resolvedConfig: ResolvedConfig | null = null;
  let seedsAbsDirs: string[] = [];
  const seeds = new Map<string, SeedManifest>();

  // Resolve the configured dirs against the Vite project root.
  // Absolute paths pass through unchanged (SDK-exported library
  // dirs); relative paths land inside the consumer project.
  function _resolveDirs(root: string): string[] {
    const dirs: string[] = [];
    if (opts.seedsDirs?.length) {
      for (const d of opts.seedsDirs) dirs.push(path.resolve(root, d));
    }
    if (opts.seedsDir) {
      dirs.push(path.resolve(root, opts.seedsDir));
    } else if (!opts.seedsDirs?.length) {
      // Back-compat default: single "src/templates/seeds" dir.
      dirs.push(path.resolve(root, "src/templates/seeds"));
    }
    return dirs;
  }

  // Discover seed folders across every configured dir + read
  // App.tsx sources. Later dirs override earlier ones when the id
  // collides — the consumer feeds its own dir last to win against
  // the SDK library's defaults.
  function _discoverSeeds(): void {
    seeds.clear();
    for (const dir of seedsAbsDirs) {
      if (!fs.existsSync(dir)) continue;
      for (const name of fs.readdirSync(dir)) {
        if (name.startsWith("_") || name.startsWith(".")) continue;
        const seedDir = path.join(dir, name);
        let stat: fs.Stats;
        try {
          stat = fs.statSync(seedDir);
        } catch {
          continue;
        }
        if (!stat.isDirectory()) continue;
        const appPath = path.join(seedDir, appFile);
        if (!fs.existsSync(appPath)) continue;
        let source = "";
        try {
          source = fs.readFileSync(appPath, "utf-8");
        } catch {
          continue;
        }
        const files = _collectSeedFiles(seedDir);
        // Always include App.tsx under ``src/App.tsx`` so the runtime
        // esbuild-wasm fallback (or the agent workspace seed) finds
        // it under a predictable path.
        files["src/App.tsx"] = source;
        seeds.set(name, {
          id: name,
          rootDir: seedDir,
          appFilePath: appPath,
          appSource: source,
          files,
          virtualHtmlId: `${VIRTUAL_HTML_PREFIX}${name}.html`,
          virtualMainId: `${VIRTUAL_MAIN_PREFIX}${name}.tsx`,
        });
      }
    }
  }

  // Walks every file under ``seedDir`` with a seedable extension and
  // returns a ``src/<relative-path>`` → source map. Skips
  // ``node_modules``, ``dist``, hidden dirs, and files larger than
  // 256 kB (paranoid guard against accidentally committed binaries).
  function _collectSeedFiles(seedDir: string): Record<string, string> {
    const out: Record<string, string> = {};
    const walk = (abs: string, rel: string): void => {
      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(abs, { withFileTypes: true });
      } catch {
        return;
      }
      for (const e of entries) {
        if (e.name.startsWith(".") || e.name === "node_modules" || e.name === "dist") continue;
        const childAbs = path.join(abs, e.name);
        const childRel = rel ? `${rel}/${e.name}` : e.name;
        if (e.isDirectory()) {
          walk(childAbs, childRel);
          continue;
        }
        if (!e.isFile()) continue;
        const ext = path.extname(e.name).toLowerCase();
        if (!_SEEDABLE_EXTS.has(ext)) continue;
        let stat: fs.Stats;
        try {
          stat = fs.statSync(childAbs);
        } catch {
          continue;
        }
        if (stat.size > 256 * 1024) continue;
        try {
          out[`src/${childRel}`] = fs.readFileSync(childAbs, "utf-8");
        } catch {
          /* skip unreadable */
        }
      }
    };
    walk(seedDir, "");
    return out;
  }

  // Hash-stable JSON serialiser for the import payload — keeps source
  // maps lined up across rebuilds and avoids the consumer's tsc
  // tripping on inline object literals.
  function _seedPayload(s: SeedManifest, base: string): string {
    const minimalMain =
      'import React from "react";\n' +
      'import { createRoot } from "react-dom/client";\n' +
      'import { App } from "./App";\n' +
      'createRoot(document.getElementById("root")!).render(<App />);\n';
    // The emitted HTML lives at ``dist/<virtualHtmlBaseName>`` —
    // Vite uses the virtual id's basename for HTML output regardless
    // of the input key. We point ``bundleUrl`` straight at it so the
    // iframe loads the page without any file moves (which would break
    // relative asset paths Vite wires in at build time).
    const htmlBaseName = `${VIRTUAL_HTML_PREFIX}${s.id}.html`;
    // Multi-file seed: every collected source file under the seed dir
    // is exposed under ``src/<relative-path>``. ``src/main.tsx`` is
    // injected as the entry — the agent workspace + the runtime
    // esbuild-wasm fallback both start from this file.
    const obj = {
      bundleUrl: `${base.replace(/\/$/, "")}/${htmlBaseName}`,
      files: {
        "src/main.tsx": minimalMain,
        ...s.files,
      },
      entry: "src/main.tsx",
    };
    return JSON.stringify(obj, null, 2);
  }

  return {
    name: "@digitorn/preview-sdk:template-seeds",
    enforce: "pre",

    config(userConfig) {
      // Resolve the seeds dirs relative to the user's Vite root (which
      // we don't yet have here — fall back to cwd, fixed up in
      // configResolved). Add seed inputs unconditionally; resolveId
      // hooks below short-circuit when no dir holds a matching seed.
      const root = userConfig.root
        ? path.resolve(userConfig.root)
        : process.cwd();
      seedsAbsDirs = _resolveDirs(root);
      _discoverSeeds();

      // Inject every seed's virtual HTML as a Rollup input so Vite
      // builds it as a separate page. The input KEY doubles as the
      // output path under ``dist/`` — using ``<outDir>/<id>/index``
      // makes Rollup write straight to ``dist/<outDir>/<id>/index.html``,
      // so we don't need a ``writeBundle`` move (which would also break
      // the relative asset paths Vite wires into the HTML).
      const inputs: Record<string, string> = {};
      for (const s of seeds.values()) {
        inputs[`${outDir}/${s.id}/index`] = s.virtualHtmlId;
      }
      if (Object.keys(inputs).length === 0) return;

      const existingInput = userConfig.build?.rollupOptions?.input;
      let mergedInput: Record<string, string>;
      if (existingInput == null) {
        // No explicit input → assume default ``index.html`` at root.
        mergedInput = {
          main: path.resolve(root, "index.html"),
          ...inputs,
        };
      } else if (typeof existingInput === "string") {
        mergedInput = { main: existingInput, ...inputs };
      } else if (Array.isArray(existingInput)) {
        mergedInput = Object.fromEntries(
          existingInput.map((p, i) => [`entry-${i}`, p]),
        );
        Object.assign(mergedInput, inputs);
      } else {
        mergedInput = { ...existingInput, ...inputs };
      }

      return {
        build: {
          rollupOptions: { input: mergedInput },
        },
      };
    },

    configResolved(cfg) {
      resolvedConfig = cfg;
      seedsAbsDirs = _resolveDirs(cfg.root);
      _discoverSeeds();
    },

    resolveId(source, importer) {
      // Virtual ids surfaced from this plugin's own emit chain.
      if (source.startsWith(VIRTUAL_HTML_PREFIX)) return source;
      if (source.startsWith(VIRTUAL_MAIN_PREFIX)) return source;

      // Public surface: ``<path>/<id>?digitorn-seed`` resolves to a
      // synthetic JSON module that the SDK runtime consumes. Works
      // for both consumer-relative imports (``./seeds/<id>``) and
      // package-internal imports (``../seeds/<id>`` from a published
      // SDK seeds dist) — we only care about the basename matching a
      // discovered seed id.
      if (!source.endsWith(SEED_QUERY)) return null;
      const bareId = source.slice(0, -SEED_QUERY.length);
      const baseDir = importer
        ? path.dirname(importer)
        : (seedsAbsDirs[0] ?? process.cwd());
      const resolved = path.resolve(baseDir, bareId);
      const seedId = path.basename(resolved);
      if (!seeds.has(seedId)) return null;
      return `\0digitorn-seed-payload:${seedId}`;
    },

    load(id) {
      // Virtual HTML page (Rollup input).
      if (id.startsWith(VIRTUAL_HTML_PREFIX)) {
        const seedId = id.slice(VIRTUAL_HTML_PREFIX.length).replace(/\.html$/, "");
        const s = seeds.get(seedId);
        if (!s) return null;
        return _HTML_TEMPLATE
          .replace("__TITLE__", `${seedId} preview`)
          .replace("__MAIN_PATH__", s.virtualMainId);
      }
      // Virtual main.tsx referenced by the HTML.
      if (id.startsWith(VIRTUAL_MAIN_PREFIX)) {
        const seedId = id.slice(VIRTUAL_MAIN_PREFIX.length).replace(/\.tsx$/, "");
        const s = seeds.get(seedId);
        if (!s) return null;
        const importPath = s.appFilePath
          .replace(/\\/g, "/")
          .replace(/\.tsx$/, "");
        return _MAIN_TEMPLATE.replace("__APP_PATH__", importPath);
      }
      // Public ``?digitorn-seed`` payload.
      if (id.startsWith("\0digitorn-seed-payload:")) {
        const seedId = id.slice("\0digitorn-seed-payload:".length);
        const s = seeds.get(seedId);
        if (!s) return null;
        const base = resolvedConfig?.base ?? "/";
        return `export default ${_seedPayload(s, base)};`;
      }
      return null;
    },

  };
}

// Default export so consumers can do ``import digitornTemplateSeeds from
// "@digitorn/preview-sdk/vite"`` if they prefer.
export default digitornTemplateSeeds;
