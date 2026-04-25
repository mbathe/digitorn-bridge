import { useEffect, useMemo, useRef, useState } from "react";
import { bundleFiles, type BundleError, type BundleResult } from "../lib/esbuild-bundler";

interface Props {
  files: Map<string, { content: string; language: string; size: number; updated_at: number }>;
  entry: string;
}

const ESM_CDN = "https://esm.sh";

const IFRAME_HTML = `<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
html, body { margin: 0; padding: 0; height: 100%; background: #fff; font-family: system-ui, sans-serif; }
#root { height: 100%; }
.dt-error { padding: 20px; background: #fee; color: #900; font-family: ui-monospace, monospace; white-space: pre-wrap; }
</style>
</head>
<body>
<div id="root"></div>
<script type="importmap">
{
  "imports": {
    "react": "${ESM_CDN}/react@18?bundle&dev",
    "react-dom": "${ESM_CDN}/react-dom@18?bundle&dev",
    "react-dom/client": "${ESM_CDN}/react-dom@18/client?bundle&dev",
    "react/jsx-runtime": "${ESM_CDN}/react@18/jsx-runtime?bundle&dev",
    "react/jsx-dev-runtime": "${ESM_CDN}/react@18/jsx-dev-runtime?bundle&dev"
  }
}
</script>
<script type="module" id="entry"></script>
<script>
window.addEventListener("error", (e) => {
  const root = document.getElementById("root");
  root.innerHTML = '<div class="dt-error">Runtime error: ' + (e.message || e.error?.message || String(e)) + '</div>';
  parent.postMessage({ type: "sandbox-error", message: e.message || String(e) }, "*");
});
window.addEventListener("unhandledrejection", (e) => {
  const root = document.getElementById("root");
  root.innerHTML = '<div class="dt-error">Unhandled promise rejection: ' + (e.reason?.message || String(e.reason)) + '</div>';
  parent.postMessage({ type: "sandbox-error", message: String(e.reason) }, "*");
});
</script>
</body>
</html>`;

export default function SandboxIframe({ files, entry }: Props) {
  const [bundle, setBundle] = useState<BundleResult | BundleError | null>(null);
  const [bundling, setBundling] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const sourceMap = useMemo(() => {
    const out = new Map<string, string>();
    for (const [path, entry] of files) out.set(path, entry.content);
    return out;
  }, [files]);

  useEffect(() => {
    if (sourceMap.size === 0) {
      setBundle(null);
      return;
    }
    let cancelled = false;
    setBundling(true);
    const t = setTimeout(async () => {
      const result = await bundleFiles(sourceMap, entry);
      if (cancelled) return;
      setBundle(result);
      setBundling(false);
    }, 100);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [sourceMap, entry]);

  useEffect(() => {
    if (!bundle || !bundle.ok) return;
    const iframe = iframeRef.current;
    if (!iframe) return;
    const doc = iframe.contentDocument;
    if (!doc) return;
    doc.open();
    doc.write(IFRAME_HTML);
    doc.close();
    setTimeout(() => {
      const win = iframe.contentWindow;
      const docNow = iframe.contentDocument;
      if (!win || !docNow) return;
      const old = docNow.getElementById("entry");
      if (old) old.remove();
      const script = docNow.createElement("script");
      script.type = "module";
      script.id = "entry";
      script.src = bundle.url;
      docNow.body.appendChild(script);
    }, 30);
    return () => {
      URL.revokeObjectURL(bundle.url);
    };
  }, [bundle]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "#0b1120" }}>
      <div
        style={{
          flex: "0 0 auto",
          padding: "6px 12px",
          fontSize: 10,
          fontFamily: "ui-monospace, monospace",
          color: "#94a3b8",
          background: "#0f172a",
          borderBottom: "1px solid #1e293b",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span>📦 {sourceMap.size} files</span>
        <span style={{ color: bundling ? "#f59e0b" : bundle?.ok ? "#10b981" : bundle ? "#ef4444" : "#64748b" }}>
          {bundling
            ? "compiling…"
            : bundle?.ok
            ? `built in ${bundle.durationMs.toFixed(0)}ms (${(bundle.size / 1024).toFixed(1)} KB)`
            : bundle
            ? `${bundle.errors.length} error(s)`
            : "idle"}
        </span>
        <span style={{ flex: 1 }} />
        <span>entry: {entry}</span>
      </div>

      {bundle && !bundle.ok ? (
        <div
          style={{
            flex: 1,
            padding: 16,
            overflow: "auto",
            color: "#fca5a5",
            background: "#0b1120",
            fontFamily: "ui-monospace, monospace",
            fontSize: 11,
            whiteSpace: "pre-wrap",
          }}
        >
          {bundle.errors.map((er, i) => (
            <div key={i} style={{ marginBottom: 12 }}>
              <div style={{ color: "#ef4444", fontWeight: 700 }}>
                {er.file}:{er.line}:{er.column}
              </div>
              <div>{er.message}</div>
            </div>
          ))}
        </div>
      ) : (
        <iframe
          ref={iframeRef}
          title="sandbox"
          sandbox="allow-scripts allow-modals allow-forms allow-popups"
          style={{
            flex: 1,
            border: "none",
            background: "white",
          }}
        />
      )}
    </div>
  );
}
