#!/usr/bin/env node
/**
 * Post-build sync: mirror `web/dist/` into the daemon's staging dir so
 * the running daemon serves the fresh bundle without needing a restart
 * or a `bootstrap upgrade`.
 *
 * Background: `bootstrap_builtins` copies source -> staging at daemon
 * boot, then serves from the staging copy. If the daemon was started
 * before our latest `vite build`, the staging dist stays stale (our
 * April-19 ghost bundle). Running this after every build keeps the
 * staging copy in lockstep with the source build output.
 *
 * No-op when the staging dir doesn't exist (i.e. fresh checkout, no
 * daemon ever booted, CI). We never CREATE the staging tree -- only
 * refresh it if the daemon already provisioned it.
 */
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");

const SRC = path.resolve(__dirname, "..", "dist");

// Staging path mirrors the source tree under the daemon's local cache.
// We're at: <repo>/packages/digitorn/builtins/digitorn-builder/web/scripts/
// Staging: ~/.digitorn/packages/.tmp/local-/packages/digitorn/builtins/digitorn-builder/web/dist/
const STAGING = path.join(
  os.homedir(),
  ".digitorn",
  "packages",
  ".tmp",
  "local-",
  "packages",
  "digitorn",
  "builtins",
  "digitorn-builder",
  "web",
  "dist",
);

function exists(p) {
  try { fs.accessSync(p); return true; } catch { return false; }
}

function copyTree(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isDirectory()) copyTree(s, d);
    else fs.copyFileSync(s, d);
  }
}

if (!exists(SRC)) {
  console.warn(`[sync-staging] no dist at ${SRC} -- skip`);
  process.exit(0);
}

// Don't auto-create the staging path -- if the daemon never installed
// the builtin locally, there's nothing to refresh.
const stagingParent = path.dirname(STAGING);
if (!exists(stagingParent)) {
  console.warn(`[sync-staging] staging not provisioned (${stagingParent}) -- skip`);
  process.exit(0);
}

// Wipe the old assets/ to drop the stale hashed bundles (Vite emits
// new hashes per build; orphaned files would otherwise pile up).
const oldAssets = path.join(STAGING, "assets");
if (exists(oldAssets)) fs.rmSync(oldAssets, { recursive: true, force: true });

// Copy fresh dist -> staging.
copyTree(SRC, STAGING);

console.log(`[sync-staging] dist -> ${STAGING}`);
