import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const PROXY_BASE = "/api/apps/digitorn-react-sandbox/preview-server/proxy/";

export default defineConfig({
  plugins: [react()],
  base: PROXY_BASE,
  server: {
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    hmr: false,
  },
  optimizeDeps: {
    exclude: ["esbuild-wasm"],
  },
});
