/**
 * Lovable canvas — pure SDK consumer.
 *
 * Two states, both built from primitives the SDK ships:
 *
 *   - **Empty**: ``<TemplateEmptyState>`` renders the gallery, the
 *     detail modal, and the standard "host or standalone" confirm
 *     flow. ``useAutoResize()`` keeps the iframe height in sync with
 *     the host. Adding a template = drop a folder under
 *     ``src/templates/seeds/<id>/`` + an entry in ``./templates``.
 *
 *   - **Workspace**: a 3-col layout (file explorer / code viewer /
 *     live preview) wired to the SDK's ``useFiles`` /
 *     ``usePreviewState`` / ``<TemplatePreview>``.
 *
 * Anything that's NOT in this file lives in the SDK.
 */

import { useEffect, useState } from "react";
import {
  TemplateEmptyState,
  TemplatePreview,
  useAutoResize,
  useChat,
  useFiles,
  usePreviewState,
  useSessionMeta,
} from "@digitorn/preview-sdk";

import FileExplorer from "./components/FileExplorer";
import CodeViewer from "./components/CodeViewer";
import { TEMPLATES } from "./templates";

export default function App() {
  const session = useSessionMeta();
  const files = useFiles();
  const chat = useChat();
  const entry =
    usePreviewState<string>("entry_file", "src/main.tsx") ?? "src/main.tsx";

  const [selected, setSelected] = useState<string | null>(null);
  const isEmpty = files.size === 0 && chat.messages.length === 0;

  // Keep the iframe height pinned to the rendered content while we're
  // showing the gallery; once the workspace canvas mounts we want the
  // iframe to fill the host height instead.
  useAutoResize(isEmpty);

  useEffect(() => {
    if (selected !== null || files.size === 0) return;
    const preferred = files.has(entry)
      ? entry
      : Array.from(files.keys()).sort()[0];
    if (preferred) setSelected(preferred);
  }, [files, entry, selected]);

  if (isEmpty) {
    return <TemplateEmptyState templates={TEMPLATES} />;
  }

  const fileSources: Record<string, string> = {};
  for (const [path, wf] of files) fileSources[path] = wf.content;
  const seed = { files: fileSources, entry };
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
