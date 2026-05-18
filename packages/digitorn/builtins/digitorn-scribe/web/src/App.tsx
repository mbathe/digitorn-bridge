import { useEffect, useMemo, useRef, useState } from "react";
import {
  useResources,
  useWorkspaceFiles,
} from "@digitorn/preview-sdk";

/**
 * Scribe iframe — PDF preview only.
 *
 *   ┌─────────────────────────────────┐
 *   │ top bar: title · compile status │
 *   ├─────────────────────────────────┤
 *   │                                 │
 *   │         <iframe pdf>            │
 *   │                                 │
 *   ├─────────────────────────────────┤
 *   │ diagnostics rail (tectonic)     │
 *   └─────────────────────────────────┘
 *
 * The user drives Scribe through the chat panel — the agent writes
 * the .tex files, this iframe just renders the compiled output.
 *
 * Wire path:
 *   - useResource("files","main.pdf") fires on every tectonic compile
 *   - useWorkspaceFiles().readFile("main.pdf", { raw: true }) → Blob
 *   - Blob → ObjectURL → <iframe src=...>
 *   - useResources("files") gives us the `lint` field on the entry
 *     .tex so we can surface the latest diagnostics rail.
 */
export default function App() {
  const fs = useWorkspaceFiles();
  const allFiles = useResources("files");

  // Pick the entry .tex (default main.tex; fallback to first .tex)
  // We don't render the source — but its resource entry fires on
  // every agent write, and that's our cue to re-fetch the PDF.
  const entryPath = useMemo(() => {
    if (allFiles?.["main.tex"]) return "main.tex";
    const list = Object.keys(allFiles || {});
    return list.find((p) => p.endsWith(".tex")) || "main.tex";
  }, [allFiles]);

  // Triggers the PDF re-fetch:
  // (1) entry .tex changes (any agent write fires resource_patched)
  // (2) main.pdf appears in the resource channel (rare — tectonic
  //     writes the PDF as a compile side-effect, workspace doesn't
  //     track it natively, so we don't depend on this)
  // (3) initial mount
  const texEntry = allFiles?.[entryPath] as
    | { updated_at?: number; size?: number; lint?: unknown[] }
    | undefined;
  const pdfEntry = allFiles?.["main.pdf"] as
    | { updated_at?: number; size?: number }
    | undefined;

  // --- PDF blob → ObjectURL, refreshed on every tectonic compile.
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfErr, setPdfErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Track the byte-size of the last PDF we showed so the retry
  // can decide "is the new fetch actually different?" cheaply.
  const lastPdfSizeRef = useRef<number>(-1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    const fetchPdf = async (reason: "initial" | "retry") => {
      try {
        // Workspace readFile with raw=true read-throughs to disk, so
        // it returns main.pdf even when the resource channel doesn't
        // track it (tectonic writes the PDF as a compile side-effect,
        // outside workspace.write).
        const blob = await fs.readFile("main.pdf", { raw: true });
        if (cancelled) return false;
        if (blob.size === 0) {
          if (reason === "initial") throw new Error("empty pdf");
          return false; // retry got an empty pdf → keep current
        }
        // Skip the URL swap if the bytes are identical to what's on
        // screen (avoids iframe reload flicker when the agent does a
        // no-op write or when the retry returns the same compile).
        if (blob.size === lastPdfSizeRef.current) {
          return false;
        }
        lastPdfSizeRef.current = blob.size;
        const url = URL.createObjectURL(blob);
        setPdfUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return url;
        });
        setPdfErr(null);
        return true;
      } catch (err: unknown) {
        if (cancelled) return false;
        // 404 / not-yet-compiled is expected on a fresh session.
        // Keep the existing PDF visible if we already had one.
        const msg = (err as Error).message || String(err);
        if (
          reason === "initial"
          && !msg.includes("404")
          && !msg.toLowerCase().includes("not found")
        ) {
          setPdfErr(msg);
        }
        return false;
      }
    };

    // Race: the workspace fires the ``files`` channel event for the
    // .tex update BEFORE ``_run_lint`` (which runs tectonic and
    // produces the new main.pdf) completes. So the first read may
    // catch the PREVIOUS compile's output. We retry on a short delay
    // to pick up the fresh PDF once tectonic has finished. The
    // server now ships ``no-store`` on the raw endpoint, so the
    // retry actually re-fetches instead of returning the cached
    // stale snapshot.
    (async () => {
      await fetchPdf("initial");
      if (!cancelled) setLoading(false);
    })();
    const t1 = setTimeout(() => { if (!cancelled) fetchPdf("retry"); }, 2500);
    const t2 = setTimeout(() => { if (!cancelled) fetchPdf("retry"); }, 6000);

    return () => {
      cancelled = true;
      clearTimeout(t1);
      clearTimeout(t2);
    };
    // Re-run on every .tex change (agent writes fire this) AND when
    // main.pdf metadata changes (rare but supported).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    texEntry?.updated_at,
    texEntry?.size,
    pdfEntry?.updated_at,
    pdfEntry?.size,
  ]);

  // --- Diagnostics from latest workspace lint for the entry file.
  const diagnostics = useMemo(() => {
    const entry = (allFiles || {})[entryPath] as
      | { lint?: Array<{ line?: number; column?: number; severity?: string; message?: string; source?: string; code?: string }> }
      | undefined;
    return entry?.lint ?? [];
  }, [allFiles, entryPath]);

  const errorCount = diagnostics.filter((d) => d.severity === "error").length;
  const warnCount = diagnostics.filter((d) => d.severity === "warning").length;
  const pdfSize = pdfEntry?.size;

  return (
    <div className="scribe-app">
      <header className="scribe-topbar">
        <div className="scribe-title">
          <span className="icon">📜</span>
          <span>Scribe</span>
          <span className="title-sub">
            {entryPath}
          </span>
        </div>
        <div className="scribe-status">
          <div
            className={`status-pill ${errorCount ? "err" : warnCount ? "warn" : "ok"}`}
            title="Diagnostics from the latest tectonic compile"
          >
            {errorCount > 0 ? (
              <>● {errorCount} erreur{errorCount > 1 ? "s" : ""}</>
            ) : warnCount > 0 ? (
              <>● {warnCount} warning{warnCount > 1 ? "s" : ""}</>
            ) : (
              <>● compile clean</>
            )}
          </div>
          <div
            className="status-pill"
            title="PDF artifact written by tectonic"
          >
            {loading
              ? "📄 recompile…"
              : pdfUrl
                ? `📄 pdf · ${pdfSize ? `${Math.round(pdfSize / 1024)} KB` : "ok"}`
                : "📄 pas de pdf"}
          </div>
        </div>
      </header>

      <section className="scribe-pdf-area">
        {pdfUrl ? (
          <iframe
            key={pdfUrl}
            src={pdfUrl}
            className="pdf-frame"
            title="main.pdf"
          />
        ) : (
          <div className="empty-pdf">
            <div className="empty-icon">📄</div>
            <div className="empty-title">Aucun PDF compilé</div>
            <div className="empty-hint">
              {pdfErr ? (
                <>Erreur de lecture : <code>{pdfErr}</code></>
              ) : (
                <>Demande à l'agent de scaffolder ou d'écrire un document.<br />
                Le PDF apparaîtra ici après le premier <code>compile</code>.</>
              )}
            </div>
          </div>
        )}
      </section>

      {diagnostics.length > 0 ? (
        <aside className="diag-list">
          <div className="diag-header">
            <span>Diagnostics tectonic · {entryPath}</span>
            <span className="diag-count">
              {errorCount} erreur{errorCount !== 1 ? "s" : ""} · {warnCount} warning{warnCount !== 1 ? "s" : ""}
            </span>
          </div>
          {diagnostics.slice(0, 20).map((d, i) => (
            <div className="diag-row" key={i}>
              <span className={`sev ${d.severity || ""}`}>
                {d.severity === "error" ? "ERR" : d.severity === "warning" ? "WARN" : "INFO"}
              </span>
              <span className="loc">
                L{d.line ?? "?"}:{d.column ?? 1}
              </span>
              <span className="msg">
                {d.message}
                {d.source ? (
                  <span className="src"> · {d.source}</span>
                ) : null}
              </span>
            </div>
          ))}
          {diagnostics.length > 20 ? (
            <div className="diag-more">
              + {diagnostics.length - 20} de plus…
            </div>
          ) : null}
        </aside>
      ) : null}
    </div>
  );
}
