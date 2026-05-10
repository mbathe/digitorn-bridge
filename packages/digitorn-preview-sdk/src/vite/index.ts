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
   * Vite project root. Default ``src/templates/seeds``.
   */
  seedsDir?: string;
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
  appFilePath: string;       // absolute path to seeds/<id>/App.tsx
  appSource: string;         // raw text of App.tsx
  virtualHtmlId: string;     // unique id for the virtual index.html
  virtualMainId: string;     // unique id for the virtual main.tsx
}

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

const _HTML_TEMPLATE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__</title>
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
  const seedsDirRel = opts.seedsDir ?? "src/templates/seeds";
  const outDir = opts.outDir ?? "seeds";
  const appFile = opts.appFile ?? "App.tsx";

  let resolvedConfig: ResolvedConfig | null = null;
  let seedsAbsDir = "";
  const seeds = new Map<string, SeedManifest>();

  // Discover seed folders + read App.tsx sources. Called once at
  // configResolved so subsequent hooks have the manifest ready.
  function _discoverSeeds(): void {
    seeds.clear();
    if (!fs.existsSync(seedsAbsDir)) return;
    for (const name of fs.readdirSync(seedsAbsDir)) {
      if (name.startsWith("_") || name.startsWith(".")) continue;
      const dir = path.join(seedsAbsDir, name);
      let stat: fs.Stats;
      try {
        stat = fs.statSync(dir);
      } catch {
        continue;
      }
      if (!stat.isDirectory()) continue;
      const appPath = path.join(dir, appFile);
      if (!fs.existsSync(appPath)) continue;
      let source = "";
      try {
        source = fs.readFileSync(appPath, "utf-8");
      } catch {
        continue;
      }
      seeds.set(name, {
        id: name,
        appFilePath: appPath,
        appSource: source,
        virtualHtmlId: `${VIRTUAL_HTML_PREFIX}${name}.html`,
        virtualMainId: `${VIRTUAL_MAIN_PREFIX}${name}.tsx`,
      });
    }
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
    const obj = {
      bundleUrl: `${base.replace(/\/$/, "")}/${htmlBaseName}`,
      files: {
        "src/main.tsx": minimalMain,
        "src/App.tsx": s.appSource,
      },
      entry: "src/main.tsx",
    };
    return JSON.stringify(obj, null, 2);
  }

  return {
    name: "@digitorn/preview-sdk:template-seeds",
    enforce: "pre",

    config(userConfig) {
      // Resolve the seeds dir relative to the user's Vite root (which
      // we don't yet have here — fall back to cwd, fixed up in
      // configResolved). Add seed inputs unconditionally; resolveId
      // hooks below short-circuit when the dir is missing.
      const root = userConfig.root
        ? path.resolve(userConfig.root)
        : process.cwd();
      seedsAbsDir = path.resolve(root, seedsDirRel);
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
      seedsAbsDir = path.resolve(cfg.root, seedsDirRel);
      _discoverSeeds();
    },

    resolveId(source, importer) {
      // Virtual ids surfaced from this plugin's own emit chain.
      if (source.startsWith(VIRTUAL_HTML_PREFIX)) return source;
      if (source.startsWith(VIRTUAL_MAIN_PREFIX)) return source;

      // Public surface: ``./seeds/<id>?digitorn-seed`` resolves to a
      // synthetic JSON module that the SDK runtime consumes.
      if (!source.endsWith(SEED_QUERY)) return null;
      const bareId = source.slice(0, -SEED_QUERY.length);
      // Resolve relative to the importer (templates/index.ts).
      const baseDir = importer ? path.dirname(importer) : seedsAbsDir;
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
