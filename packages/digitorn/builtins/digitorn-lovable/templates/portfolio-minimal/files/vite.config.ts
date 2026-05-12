import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base so the build works under any URL the daemon serves it
// from (e.g. /api/apps/digitorn-lovable/template-assets/.../dist/).
// allowedHosts: "all" so the user's browser can load the dev server
// proxied through digitorn_web without Vite rejecting the Host header.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    host: "0.0.0.0",
    port: 5173,
    allowedHosts: "all",
  },
});
