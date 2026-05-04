import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Direct-connect: relative asset base so the bundle works at any
// dev-server URL the daemon publishes via PreviewProxy.
export default defineConfig({
  plugins: [react()],
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
