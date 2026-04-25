interface Props {
  path: string | null;
  content: string | null;
}

export default function CodeViewer({ path, content }: Props) {
  if (!path || content === null) {
    return (
      <div
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: "#475569",
          fontSize: 11,
          background: "#0b1120",
        }}
      >
        Select a file to inspect.
      </div>
    );
  }
  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        background: "#0b1120",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          padding: "6px 12px",
          fontSize: 10,
          color: "#94a3b8",
          background: "#0f172a",
          borderBottom: "1px solid #1e293b",
          fontFamily: "ui-monospace, monospace",
        }}
      >
        {path}
      </div>
      <pre
        style={{
          flex: 1,
          margin: 0,
          padding: "12px 16px",
          fontSize: 11,
          fontFamily: "ui-monospace, 'Fira Code', monospace",
          color: "#cbd5e1",
          background: "#0b1120",
          overflow: "auto",
          lineHeight: 1.5,
        }}
      >
        {content}
      </pre>
    </div>
  );
}
