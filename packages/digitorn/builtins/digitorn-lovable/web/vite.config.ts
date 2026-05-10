import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { digitornTemplateSeeds } from "@digitorn/preview-sdk/vite";

// Direct-connect: relative asset base so the bundle works at any
// dev-server URL the daemon publishes via PreviewProxy.
//
// ``digitornTemplateSeeds`` auto-discovers ``src/templates/seeds/<id>/App.tsx``
// folders and emits one pre-built page per seed under ``dist/seeds/<id>/``.
// At runtime, ``<TemplatePreview>`` iframes those pages directly so the
// gallery cold-mount stays under 100 ms regardless of template count.
export default defineConfig({
  plugins: [react(), digitornTemplateSeeds()],
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
