import { useMemo } from "react";
import type { WorkspaceFile } from "@digitorn/preview-sdk";
import type { Selection } from "../App";
import { DocIcon, FileIcon } from "../lib/icons";

interface Group {
  label: string;
  prefix: string;
  icon: typeof FileIcon;
  emptyHint?: string;
}

const GROUPS: Group[] = [
  {
    label: "Sources",
    prefix: "attachments/",
    icon: FileIcon,
    emptyHint:
      "Use the + Add button to paste text, drop a file or fetch a URL. The chat composer paperclip works too.",
  },
  {
    label: "Forms",
    prefix: "forms/",
    icon: DocIcon,
    emptyHint:
      "Ask the agent for a form, e.g. 'build me a feedback form'.",
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
    const primary = groups[0];
    return (
      <div className="empty">
        <strong style={{ color: "var(--text-muted)" }}>No sources yet.</strong>
        <div style={{ marginTop: 8, fontSize: 12.5 }}>
          {primary.emptyHint}
        </div>
      </div>
    );
  }

  const selectedPath = selection.kind === "file" ? selection.path : null;

  return (
    <>
      {groups.map((g) => {
        if (g.items.length === 0) return null;
        const Icon = g.icon;
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
                    <Icon className="source-icon" size={16} />
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
