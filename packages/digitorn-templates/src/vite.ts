/**
 * `@digitorn/templates/vite` — path helper consumers feed into the
 * SDK's Vite plugin so it discovers the package's seeds alongside
 * the app's own.
 *
 * Usage:
 *
 * ```ts
 * // vite.config.ts
 * import { digitornTemplateSeeds } from "@digitorn/preview-sdk/vite";
 * import { TEMPLATES_SEEDS_DIR } from "@digitorn/templates/vite";
 *
 * export default {
 *   plugins: [
 *     digitornTemplateSeeds({
 *       seedsDirs: [
 *         TEMPLATES_SEEDS_DIR,   // shared library
 *         "src/templates/seeds", // app-specific overrides / additions
 *       ],
 *     }),
 *   ],
 * };
 * ```
 *
 * The plugin discovers `seeds/<id>/App.tsx` in every dir, indexed by
 * id. App-specific seeds with the same id as a library seed override
 * the library version (consumer-first ordering wins).
 */

import { fileURLToPath } from "node:url";
import * as path from "node:path";

const _here = path.dirname(fileURLToPath(import.meta.url));

/**
 * Absolute path to the package's `src/seeds/` directory. Resolves
 * correctly whether the package is installed via `npm`, `pnpm`,
 * `yarn` or workspace-linked.
 */
// In dev: dist/vite.js → ../seeds = packages/digitorn-templates/seeds.
// In published / npm-installed: dist/vite.js → ../seeds = node_modules/@digitorn/templates/seeds.
// Same path math both ways thanks to keeping dist/ as a sibling of seeds/.
export const TEMPLATES_SEEDS_DIR = path.resolve(_here, "..", "seeds");
