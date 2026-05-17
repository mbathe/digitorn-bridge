import type { WorkspaceFile } from "@digitorn/preview-sdk";
import type { Selection } from "../App";
import { ARTEFACTS } from "../lib/artefacts";
import { BookIcon, ClockIcon, DocIcon, GraphIcon, WaveIcon } from "../lib/icons";

const ICONS = {
  doc: DocIcon,
  graph: GraphIcon,
  clock: ClockIcon,
  book: BookIcon,
  wave: WaveIcon,
} as const;

export function Studio({
  files,
  selection,
  onSelect,
}: {
  files: Map<string, WorkspaceFile>;
  selection: Selection;
  onSelect: (id: string) => void;
}) {
  const activeId =
    selection.kind === "artefact" ? selection.id : null;

  const hasSources = Array.from(files.keys()).some(
    (p) => p.startsWith("attachments/"),
  );

  return (
    <div className="studio-grid">
      {ARTEFACTS.map((art) => {
        const file = files.get(art.path);
        const exists = file !== undefined;
        const IconCmp = ICONS[art.icon as keyof typeof ICONS] ?? DocIcon;
        const meta = exists
          ? `${file?.lines ?? 0} lines · updated ${fmtAgo(file?.updated_at)}`
          : hasSources
            ? "Ask the agent to generate it"
            : "Add a source first";
        return (
          <button
            key={art.id}
            type="button"
            className="studio-card"
            aria-pressed={activeId === art.id}
            disabled={!exists}
            onClick={() => onSelect(art.id)}
          >
            <div className="studio-card-header">
              <IconCmp className="icon" size={18} />
              <span>{art.title}</span>
            </div>
            <div
              className={`studio-card-meta ${exists ? "" : "muted"}`}
              title={art.caption}
            >
              {exists ? meta : art.caption}
            </div>
          </button>
        );
      })}
    </div>
  );
}

function fmtAgo(ts?: number): string {
  if (!ts) return "just now";
  const ms = ts > 1e12 ? ts : ts * 1000;
  const diff = Date.now() - ms;
  if (diff < 0 || diff < 30_000) return "just now";
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}
