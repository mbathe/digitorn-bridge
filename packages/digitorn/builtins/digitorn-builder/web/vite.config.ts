import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const PROXY_BASE = "/api/apps/digitorn-builder/preview-server/proxy/";

// Allow standalone testing without the daemon proxy by setting VITE_BASE=/
// (used by tools/.claude/launch.json for canvas iteration).
const BASE = process.env.VITE_BASE ?? PROXY_BASE;

export default defineConfig({
  plugins: [react()],
  base: BASE,
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    // HMR disabled: when Vite is iframed behind the daemon proxy at
    // a non-root base, its HMR client URL = base + hmr.path which
    // produces a duplicated path the daemon can't route. The trade-
    // off: editing builder code requires a manual refresh in the
    // iframe. The canvas itself updates live via the Socket.IO
    // preview events which are independent of HMR.
    hmr: false,
  },
});
