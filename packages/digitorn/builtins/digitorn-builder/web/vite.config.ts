import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Direct-connect: the iframe loads the dev server URL straight from
// the browser, no daemon proxy in front. ``base: './'`` keeps asset
// references relative so the bundle works at any URL the daemon
// publishes via PreviewProxy.
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
  },
});
