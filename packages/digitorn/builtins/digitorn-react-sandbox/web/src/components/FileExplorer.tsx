interface FileEntry {
  content: string;
  language: string;
  size: number;
  updated_at: number;
}

interface Props {
  files: Map<string, FileEntry>;
  selected: string | null;
  onSelect: (path: string) => void;
}

export default function FileExplorer({ files, selected, onSelect }: Props) {
  const sorted = Array.from(files.entries()).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        background: "#0f172a",
        borderRight: "1px solid #1e293b",
        overflow: "auto",
        height: "100%",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          fontSize: 9,
          textTransform: "uppercase",
          letterSpacing: 0.6,
          color: "#64748b",
          borderBottom: "1px solid #1e293b",
        }}
      >
        Files ({sorted.length})
      </div>
      <div style={{ flex: 1, padding: "4px 0" }}>
        {sorted.length === 0 ? (
          <div style={{ padding: "20px 12px", color: "#475569", fontSize: 11, textAlign: "center" }}>
            No files yet. Ask the agent to build something.
          </div>
        ) : (
          sorted.map(([path, entry]) => (
            <button
              key={path}
              onClick={() => onSelect(path)}
              style={{
                width: "100%",
                padding: "4px 12px",
                background: selected === path ? "#1e293b" : "transparent",
                border: "none",
                borderLeft: selected === path ? "2px solid #61dafb" : "2px solid transparent",
                color: selected === path ? "#e2e8f0" : "#94a3b8",
                fontSize: 11,
                fontFamily: "ui-monospace, monospace",
                textAlign: "left",
                cursor: "pointer",
                display: "flex",
                gap: 6,
                alignItems: "center",
              }}
            >
              <span style={{ fontSize: 9, color: "#64748b" }}>{iconFor(entry.language)}</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {path}
              </span>
              <span style={{ fontSize: 9, color: "#475569" }}>{(entry.size / 1024).toFixed(1)}k</span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function iconFor(language: string): string {
  switch (language) {
    case "tsx":
    case "jsx":
      return "⚛";
    case "ts":
    case "js":
      return "𝗝𝗦";
    case "css":
      return "🎨";
    case "html":
      return "🌐";
    case "json":
      return "{}";
    case "md":
      return "📝";
    default:
      return "📄";
  }
}
