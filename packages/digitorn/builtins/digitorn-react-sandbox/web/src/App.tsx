import { useEffect, useMemo, useState } from "react";
import {
  usePreviewResources,
  usePreviewState,
  useSession,
} from "./lib/preview-sdk";
import FileExplorer from "./components/FileExplorer";
import CodeViewer from "./components/CodeViewer";
import SandboxIframe from "./components/SandboxIframe";

interface FileEntry {
  content: string;
  language: string;
  size: number;
  updated_at: number;
}

export default function App() {
  const session = useSession();
  const files = usePreviewResources<FileEntry>("files");
  const entry = usePreviewState<string>("entry_file", "src/main.tsx") ?? "src/main.tsx";
  const [selected, setSelected] = useState<string | null>(null);
  const [counter, setCounter] = useState(0);

  useEffect(() => {
    if (selected === null && files.size > 0) {
      const preferred = files.has(entry)
        ? entry
        : Array.from(files.keys()).sort()[0] || null;
      setSelected(preferred);
    }
  }, [files, entry, selected]);

  const selectedContent = useMemo(() => {
    if (!selected) return null;
    return files.get(selected)?.content ?? null;
  }, [files, selected]);

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
        <span style={{ fontWeight: 600, fontSize: 14 }}>⚛️ Digitorn React Sandbox</span>
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
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            onClick={() => setCounter(counter + 1)}
            style={{
              padding: "4px 12px",
              background: "#334155",
              border: "1px solid #475569",
              borderRadius: 4,
              color: "#e2e8f0",
              fontSize: 12,
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Increment: {counter}
          </button>
          <button
            onClick={() => setCounter(0)}
            style={{
              padding: "4px 8px",
              background: "transparent",
              border: "1px solid #475569",
              borderRadius: 4,
              color: "#94a3b8",
              fontSize: 10,
              cursor: "pointer",
            }}
          >
            Reset
          </button>
        </div>
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
        <FileExplorer files={files} selected={selected} onSelect={setSelected} />
        <CodeViewer path={selected} content={selectedContent} />
        <SandboxIframe files={files} entry={entry} />
      </div>
    </div>
  );
}
