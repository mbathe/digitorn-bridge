/**
 * Lovable canvas — pure SDK consumer.
 *
 * Two states, both built from primitives the SDK ships:
 *
 *   - **Empty**: `<TemplateEmptyState>` renders the gallery, the
 *     detail modal, and the standard "host or standalone" confirm
 *     flow. `useAutoResize()` keeps the iframe height in sync with
 *     the host.
 *
 *   - **Workspace**: a 3-col layout (`<WorkspaceFileTree>` /
 *     `<WorkspaceCodeViewer>` / `<TemplatePreview>`) wired to the
 *     SDK's `useFiles` / `useFile` / `usePreviewState`. ZERO local
 *     UI components — every primitive lives in the SDK so any other
 *     app builder gets the same surface for free.
 */

import { useEffect, useState } from "react";
import {
  TemplateEmptyState,
  TemplatePreview,
  WorkspaceCodeViewer,
  WorkspaceFileTree,
  useAutoResize,
  useChat,
  useFile,
  useFiles,
  usePreviewState,
  useSessionMeta,
} from "@digitorn/preview-sdk";

import { TEMPLATES } from "./templates";

export default function App() {
  const session = useSessionMeta();
  const files = useFiles();
  const chat = useChat();
  const entry =
    usePreviewState<string>("entry_file", "src/main.tsx") ?? "src/main.tsx";

  const [selected, setSelected] = useState<string | null>(null);
  const isEmpty = files.size === 0 && chat.messages.length === 0;

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
  const selectedFile = selected ? files.get(selected) : undefined;
  const selectedContent = useFile(selected ?? "") ?? null;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        overflow: "hidden",
        color: "var(--text-bright, #f1f3f6)",
        background: "var(--bg, #0b0e13)",
        fontFamily: "var(--font-sans, 'IBM Plex Sans', system-ui, sans-serif)",
      }}
    >
      <header
        style={{
          flex: "0 0 auto",
          padding: "10px 18px",
          borderBottom: "1px solid var(--border, rgba(255,255,255,0.06))",
          display: "flex",
          alignItems: "center",
          gap: 16,
          background: "var(--surface, #11151b)",
        }}
      >
        <span style={{ fontWeight: 500, fontSize: 14, letterSpacing: "-0.015em" }}>
          ⚛ Lovable
        </span>
        <span
          style={{
            fontFamily: "ui-monospace, 'JetBrains Mono', monospace",
            fontSize: 11,
            color: "var(--text-muted, #9099a8)",
            padding: "2px 8px",
            background: "var(--bg, #0b0e13)",
            border: "1px solid var(--border, rgba(255,255,255,0.06))",
            borderRadius: 6,
          }}
        >
          session: {session.sessionId.slice(0, 8)}
        </span>
        <div style={{ flex: 1 }} />
        <span style={{ fontSize: 11, color: "var(--text-dim, #5d6573)" }}>
          esbuild-wasm · in-browser bundler · zero server cost
        </span>
      </header>

      <div
        style={{
          flex: 1,
          display: "grid",
          gridTemplateColumns: "240px 1fr 1fr",
          minHeight: 0,
        }}
      >
        <WorkspaceFileTree
          files={files}
          selected={selected}
          onSelect={setSelected}
        />
        <WorkspaceCodeViewer
          path={selected}
          content={selectedContent}
          status={selectedFile?.status as "added" | "modified" | "deleted" | undefined}
          insertions={selectedFile?.total_insertions}
          deletions={selectedFile?.total_deletions}
        />
        <TemplatePreview seed={seed} />
      </div>
    </div>
  );
}
