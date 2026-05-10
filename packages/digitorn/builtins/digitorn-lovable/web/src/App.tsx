/**
 * Lovable canvas — pure SDK consumer.
 *
 * No Socket.IO connection logic, no esbuild plumbing, no iframe
 * wiring lives here anymore. The SDK provides:
 *   - <DigiPreview>          parent connection + state (in main.tsx)
 *   - useFiles, useChat, ... session-bound hooks
 *   - <TemplateGallery>,
 *     <TemplateModal>,
 *     <TemplatePreview>      the empty-state UX + live sandbox
 *
 * The lovable bundle's job is to wire those primitives together with
 * its own template list (``./templates``) and a small file-explorer +
 * code-viewer layout. Adding a template = drop a folder under
 * ``src/templates/seeds/<id>/`` + an entry in ``./templates``. No
 * daemon work, no YAML changes.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type Template,
  TemplateGallery,
  TemplateModal,
  TemplatePreview,
  sendToHost,
  useChat,
  useFiles,
  usePreviewState,
  useSessionMeta,
  useTemplates,
  useWorkspaceFiles,
} from "@digitorn/preview-sdk";

import FileExplorer from "./components/FileExplorer";
import CodeViewer from "./components/CodeViewer";
import { TEMPLATES } from "./templates";

export default function App() {
  const session = useSessionMeta();
  const files = useFiles();
  const chat = useChat();
  const ws = useWorkspaceFiles();
  const tpls = useTemplates(TEMPLATES);
  const entry =
    usePreviewState<string>("entry_file", "src/main.tsx") ?? "src/main.tsx";

  const [selected, setSelected] = useState<string | null>(null);

  const isEmpty = files.size === 0 && chat.messages.length === 0;

  const fileSources = useMemo(() => {
    const out: Record<string, string> = {};
    for (const [path, wf] of files) out[path] = wf.content;
    return out;
  }, [files]);

  const seed = useMemo(
    () => ({ files: fileSources, entry }),
    [fileSources, entry],
  );

  useEffect(() => {
    if (selected !== null || files.size === 0) return;
    const preferred = files.has(entry)
      ? entry
      : Array.from(files.keys()).sort()[0];
    if (preferred) setSelected(preferred);
  }, [files, entry, selected]);

  // Auto-resize: report the document's true content height to the
  // host on every layout change so the host iframe grows to match
  // and the page-level scroll takes over (no internal scrollbar).
  // Only runs in the empty state — once the workspace canvas is
  // mounted (3-col layout) we want the iframe to fill the height
  // the host gives us, no auto-grow.
  useEffect(() => {
    if (!isEmpty) return;
    const root = document.documentElement;
    let last = -1;
    const send = () => {
      const h = Math.max(root.scrollHeight, document.body?.scrollHeight ?? 0);
      if (h === last) return;
      last = h;
      sendToHost({ type: "digi:content-resize", height: h });
    };
    send();
    const ro = new ResizeObserver(send);
    ro.observe(root);
    if (document.body) ro.observe(document.body);
    return () => ro.disconnect();
  }, [isEmpty]);

  const onConfirmTemplate = useCallback(
    async (template: Template) => {
      // Two paths depending on the embedding context:
      //
      // 1. Embedded inside a host (iframe under digitorn_web's chat
      //    panel) — the host owns session creation. We post the
      //    template payload up; the host (which has the bearer token,
      //    the chat store, the queue) seeds the workspace and
      //    dispatches the first message through its existing
      //    ``sendMessage`` pipeline. Single source of truth for the
      //    session lifecycle.
      //
      // 2. Standalone (no parent / Flutter desktop with a real
      //    session bound at iframe load) — fall back to the SDK
      //    primitives. The host bridge in ``sendToHost`` no-ops when
      //    there's no parent, so we always emit the message and let
      //    the host pick it up if it cares.
      sendToHost({
        type: "digi:template-pick",
        template_id: template.id,
        prompt: template.prompt ?? "",
        seed_files: template.seed?.files ?? {},
      });

      // Standalone fallback: only run when the iframe is the root
      // (no host listening) AND we have a real session bound. The
      // ``_dev_`` session id has no daemon counterpart so writeFile
      // / chat.send would 404; skip in that case.
      const isStandalone =
        typeof window !== "undefined" && window.parent === window;
      const hasRealSession = session.sessionId !== "_dev_";
      if (isStandalone && hasRealSession) {
        if (template.seed?.files) {
          for (const [path, content] of Object.entries(template.seed.files)) {
            try {
              await ws.writeFile(path, content, { autoApprove: true });
            } catch (e) {
              console.warn("[lovable] seed writeFile failed", path, e);
            }
          }
        }
        if (template.prompt) {
          try {
            await chat.send(template.prompt);
          } catch (e) {
            console.warn("[lovable] chat.send failed", e);
          }
        }
      }

      tpls.dismiss();
    },
    [ws, chat, tpls, session.sessionId],
  );

  if (isEmpty) {
    return (
      <div
        style={{
          // No ``minHeight: 100vh`` and no ``overflow: auto``: when the
          // host iframes us with auto-height, those rules trap the
          // content inside an internal scrollbar. The host scrolls
          // naturally; we just provide our intrinsic height.
          background: "transparent",
          color: "#e2e8f0",
          fontFamily: "system-ui, -apple-system, sans-serif",
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "center",
          padding: "16px 20px 32px",
        }}
      >
        <div style={{ width: "100%", maxWidth: 1200 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              marginBottom: 18,
            }}
          >
            <span
              style={{
                fontFamily: "var(--font-sans, system-ui)",
                fontSize: 13,
                fontWeight: 500,
                color: "var(--text-bright, #f5f5f5)",
                letterSpacing: "-0.005em",
              }}
            >
              Templates
            </span>
            <a
              href="#"
              onClick={(e) => e.preventDefault()}
              style={{
                fontFamily: "var(--font-sans, system-ui)",
                fontSize: 12.5,
                fontWeight: 400,
                color: "var(--text-muted, #a3a3a3)",
                textDecoration: "none",
                letterSpacing: "-0.005em",
              }}
            >
              Browse all →
            </a>
          </div>
          <TemplateGallery
            templates={tpls.list}
            onPick={tpls.pick}
            sectionLabel=""
            minCardWidth={200}
            style={{ paddingTop: 0, paddingBottom: 0 }}
          />
        </div>
        <TemplateModal
          template={tpls.selected}
          onClose={tpls.dismiss}
          onConfirm={onConfirmTemplate}
        />
      </div>
    );
  }

  const selectedContent =
    selected != null ? files.get(selected)?.content ?? null : null;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        overflow: "hidden",
        color: "#e2e8f0",
        background: "#0b1120",
        fontFamily: "system-ui, -apple-system, sans-serif",
      }}
    >
      <header
        style={{
          flex: "0 0 auto",
          padding: "10px 16px",
          borderBottom: "1px solid #1e293b",
          display: "flex",
          alignItems: "center",
          gap: 16,
          background: "#0b1120",
        }}
      >
        <span style={{ fontWeight: 600, fontSize: 14 }}>⚛️ Lovable</span>
        <span
          style={{
            fontFamily: "ui-monospace, monospace",
            fontSize: 11,
            color: "#94a3b8",
            padding: "2px 6px",
            background: "#1e293b",
            borderRadius: 4,
          }}
        >
          session: {session.sessionId.slice(0, 8)}
        </span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 10, color: "#64748b" }}>
          esbuild-wasm · in-browser bundler · zero server cost
        </span>
      </header>

      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "200px 1fr 1fr",
          minHeight: 0,
        }}
      >
        <FileExplorer
          files={files}
          selected={selected}
          onSelect={setSelected}
        />
        <CodeViewer path={selected} content={selectedContent} />
        <TemplatePreview seed={seed} />
      </div>
    </div>
  );
}
