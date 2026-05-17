#!/usr/bin/env node
// IndexNow submission script - pushes all sitemap URLs to Bing / Yandex /
// Seznam / Naver in one shot. They typically crawl within 5-15 minutes
// of receiving a valid POST.
//
// Run manually after a content update:
//   node scripts/indexnow-submit.js
//
// Prerequisites:
//   - `npm run build` has been run (script reads build/sitemap.xml)
//   - The key file at static/<KEY>.txt has been deployed to prod
//
// The key value below MUST match the filename + content of the
// static/<KEY>.txt file. Both are deployed together; never rotate
// without redeploying the txt file or IndexNow will start rejecting
// submissions with HTTP 403 (key file mismatch).

"use strict";

const fs = require("fs");
const path = require("path");
const https = require("https");

// ── Config ──────────────────────────────────────────────────────────
const HOST = "docs.digitorn.ai";
const KEY = "c11f8d572b64c0a76384be916e6c850d";
const KEY_LOCATION = `https://${HOST}/${KEY}.txt`;
const ENDPOINT = "https://api.indexnow.org/indexnow";
const SITEMAP_PATH = path.join(__dirname, "..", "build", "sitemap.xml");
// IndexNow accepts up to 10000 URLs per request. Stay under for safety.
const BATCH_SIZE = 5000;

// ── Load sitemap ────────────────────────────────────────────────────
if (!fs.existsSync(SITEMAP_PATH)) {
  console.error(`[indexnow] sitemap not found at ${SITEMAP_PATH}`);
  console.error("[indexnow] run 'npm run build' first.");
  process.exit(1);
}

const xml = fs.readFileSync(SITEMAP_PATH, "utf8");
const urls = Array.from(xml.matchAll(/<loc>([^<]+)<\/loc>/g))
  .map((m) => m[1].trim())
  .filter((u) => u.startsWith(`https://${HOST}/`));

if (urls.length === 0) {
  console.error(`[indexnow] no URLs found for host ${HOST} in sitemap.`);
  console.error("[indexnow] check docusaurus.config.js 'url' field.");
  process.exit(1);
}

console.log(`[indexnow] found ${urls.length} URLs for ${HOST}`);
console.log(`[indexnow] keyLocation: ${KEY_LOCATION}`);

// ── POST a batch ────────────────────────────────────────────────────
function postBatch(urlList) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      host: HOST,
      key: KEY,
      keyLocation: KEY_LOCATION,
      urlList,
    });

    const req = https.request(
      ENDPOINT,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Content-Length": Buffer.byteLength(body),
        },
        timeout: 30000,
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => (chunks += c));
        res.on("end", () => resolve({ status: res.statusCode, body: chunks }));
      },
    );

    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("request timed out"));
    });
    req.write(body);
    req.end();
  });
}

// ── Submit in batches ───────────────────────────────────────────────
(async () => {
  let ok = 0;
  let fail = 0;
  for (let i = 0; i < urls.length; i += BATCH_SIZE) {
    const batch = urls.slice(i, i + BATCH_SIZE);
    try {
      const { status, body } = await postBatch(batch);
      if (status === 200 || status === 202) {
        console.log(`[indexnow] batch ${i / BATCH_SIZE + 1}: ${batch.length} URLs -> HTTP ${status}`);
        ok += batch.length;
      } else {
        console.error(`[indexnow] batch ${i / BATCH_SIZE + 1}: HTTP ${status} | body=${body || "(empty)"}`);
        fail += batch.length;
      }
    } catch (err) {
      console.error(`[indexnow] batch ${i / BATCH_SIZE + 1} failed: ${err.message}`);
      fail += batch.length;
    }
  }
  console.log(`[indexnow] done. accepted=${ok} failed=${fail}`);
  process.exit(fail > 0 ? 1 : 0);
})();
