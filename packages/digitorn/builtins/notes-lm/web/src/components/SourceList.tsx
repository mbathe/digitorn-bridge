import { useMemo } from "react";
import type { WorkspaceFile } from "@digitorn/preview-sdk";
import type { Selection } from "../App";
import { FileIcon, LinkIcon } from "../lib/icons";

interface Group {
  label: string;
  prefix: string;
  iconFor: (path: string) => JSX.Element;
}

const GROUPS: Group[] = [
  {
    label: "Sources",
    prefix: "sources/",
    iconFor: () => <LinkIcon className="source-icon" size={16} />,
  },
  {
    label: "Attachments",
    prefix: "attachments/",
    iconFor: () => <FileIcon className="source-icon" size={16} />,
  },
];

export function SourceList({
  files,
  selection,
  onSelect,
}: {
  files: Map<string, WorkspaceFile>;
  selection: Selection;
  onSelect: (path: string) => void;
}) {
  const groups = useMemo(() => {
    return GROUPS.map((g) => {
      const items: { path: string; file: WorkspaceFile }[] = [];
      for (const [path, file] of files) {
        if (path.startsWith(g.prefix)) items.push({ path, file });
      }
      items.sort((a, b) => a.path.localeCompare(b.path));
      return { ...g, items };
    });
  }, [files]);

  const total = groups.reduce((n, g) => n + g.items.length, 0);
  if (total === 0) {
    return (
      <div className="empty">
        <strong style={{ color: "var(--text-muted)" }}>No sources yet.</strong>
        <div style={{ marginTop: 8, fontSize: 12.5 }}>
          Paste a URL or upload a file in the chat to get started.
        </div>
      </div>
    );
  }

  const selectedPath = selection.kind === "file" ? selection.path : null;

  return (
    <>
      {groups.map((g) => {
        if (g.items.length === 0) return null;
        return (
          <section key={g.prefix}>
            <div className="section-label">{g.label}</div>
            <div className="source-list">
              {g.items.map(({ path, file }) => {
                const name = path.slice(g.prefix.length);
                return (
                  <button
                    key={path}
                    type="button"
                    className="source-row"
                    aria-selected={selectedPath === path}
                    onClick={() => onSelect(path)}
                    title={path}
                  >
                    {g.iconFor(path)}
                    <span className="source-name">{name}</span>
                    <span className="source-meta">{file.lines ?? 0}L</span>
                  </button>
                );
              })}
            </div>
          </section>
        );
      })}
    </>
  );
}
