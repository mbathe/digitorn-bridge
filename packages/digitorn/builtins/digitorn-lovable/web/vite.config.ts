import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { digitornTemplateSeeds } from "@digitorn/preview-sdk/vite";
import { TEMPLATES_SEEDS_DIR } from "@digitorn/templates/vite";

// ``digitornTemplateSeeds`` discovers ``<dir>/<id>/App.tsx`` folders
// across multiple ``seedsDirs`` and emits one pre-built page per seed
// under ``dist/seeds/<id>/``. At runtime, ``<TemplatePreview>`` iframes
// those pages directly (~30 ms mount).
//
// We feed the shared ``@digitorn/templates`` library FIRST, then any
// app-specific seeds under ``src/templates/seeds`` LAST so a local id
// collision overrides the library default.
export default defineConfig({
  plugins: [
    react(),
    digitornTemplateSeeds({
      seedsDirs: [TEMPLATES_SEEDS_DIR, "src/templates/seeds"],
    }),
  ],
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
  },
  optimizeDeps: {
    exclude: ["esbuild-wasm"],
  },
});
