/**
 * `?digitorn-seed` is a virtual module suffix resolved at build time
 * by the SDK's Vite plugin (`@digitorn/preview-sdk/vite`). The plugin
 * returns a `TemplateSeed` payload (`{ bundleUrl, files, entry }`).
 * TS doesn't know about Vite query suffixes, so we declare the
 * wildcard module here to keep `tsc` happy.
 */
declare module "*?digitorn-seed" {
  import type { TemplateSeed } from "@digitorn/preview-sdk";
  const seed: TemplateSeed;
  export default seed;
}
