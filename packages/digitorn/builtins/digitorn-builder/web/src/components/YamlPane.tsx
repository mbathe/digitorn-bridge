/**
 * Live YAML preview pane.
 *
 * Toggle from the toolbar — opens a right-side splitter showing the
 * current YAML text as the user edits the canvas. Read-only by default.
 * Toggling the "Edit raw" mode swaps to a textarea for direct editing,
 * and on blur the new YAML flows back through `onChange` so the canvas
 * re-renders.
 */
import { useMemo, useState } from "react";
import { X, FileCode, Edit3, Eye, Copy, Check, GitCompare } from "lucide-react";
import clsx from "clsx";

interface Props {
  open: boolean;
  yaml: string;
  /** Original (unedited) YAML — when set + different, the user can
   *  toggle a diff view. */
  sourceYaml?: string | null;
  onChange?: (yaml: string) => void;
  onClose: () => void;
}

interface DiffLine {
  kind: "context" | "add" | "del";
  oldNo?: number;
  newNo?: number;
  text: string;
}

/** Naive line-based diff — sufficient for human eyeballing edits.
 *  Walks both files in parallel, marking insertions / deletions when
 *  they don't match, with a small look-ahead to keep alignment. */
function lineDiff(a: string, b: string): DiffLine[] {
  const aL = a.split("\n");
  const bL = b.split("\n");
  const out: DiffLine[] = [];
  let i = 0, j = 0;
  while (i < aL.length || j < bL.length) {
    if (i >= aL.length) {
      out.push({ kind: "add", newNo: j + 1, text: bL[j] });
      j++;
      continue;
    }
    if (j >= bL.length) {
      out.push({ kind: "del", oldNo: i + 1, text: aL[i] });
      i++;
      continue;
    }
    if (aL[i] === bL[j]) {
      out.push({ kind: "context", oldNo: i + 1, newNo: j + 1, text: aL[i] });
      i++; j++;
      continue;
    }
    // Look ahead in b to see if aL[i] reappears (= insertion in b)
    const lookB = bL.slice(j, j + 6).indexOf(aL[i]);
    if (lookB > 0) {
      for (let k = 0; k < lookB; k++) {
        out.push({ kind: "add", newNo: j + 1, text: bL[j] });
        j++;
      }
      continue;
    }
    // Look ahead in a (= deletion in b)
    const lookA = aL.slice(i, i + 6).indexOf(bL[j]);
    if (lookA > 0) {
      for (let k = 0; k < lookA; k++) {
        out.push({ kind: "del", oldNo: i + 1, text: aL[i] });
        i++;
      }
      continue;
    }
    // Replace
    out.push({ kind: "del", oldNo: i + 1, text: aL[i] });
    out.push({ kind: "add", newNo: j + 1, text: bL[j] });
    i++; j++;
  }
  return out;
}

export default function YamlPane({ open, yaml, sourceYaml, onChange, onClose }: Props) {
  const [editMode, setEditMode] = useState(false);
  const [diffMode, setDiffMode] = useState(false);
  const [draft, setDraft] = useState(yaml);
  const [copied, setCopied] = useState(false);

  const hasDiff = sourceYaml != null && sourceYaml !== yaml;
  const diff = useMemo(
    () => (diffMode && hasDiff && sourceYaml != null) ? lineDiff(sourceYaml, yaml) : [],
    [diffMode, hasDiff, sourceYaml, yaml],
  );
  const diffStats = useMemo(() => {
    let adds = 0, dels = 0;
    for (const d of diff) {
      if (d.kind === "add") adds++;
      else if (d.kind === "del") dels++;
    }
    return { adds, dels };
  }, [diff]);

  if (!open) return null;
  return (
    <aside className="w-[420px] flex-shrink-0 border-l border-border-subtle bg-surface-1 flex flex-col">
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border-subtle">
        <FileCode className="w-3.5 h-3.5 text-accent" />
        <span className="text-[10px] uppercase tracking-wider font-bold text-accent">
          Live YAML
        </span>
        <span className="text-[10px] font-mono text-ink-dim">
          {yaml.split("\n").length} lines
        </span>
        <div className="flex-1" />
        <button
          onClick={() => {
            navigator.clipboard.writeText(yaml).then(() => {
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            });
          }}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[10px] text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Copy to clipboard"
        >
          {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
          {copied ? "Copied" : "Copy"}
        </button>
        {hasDiff && (
          <button
            onClick={() => setDiffMode((d) => !d)}
            className={clsx(
              "inline-flex items-center gap-1 h-7 px-2 rounded-md text-[10px]",
              diffMode ? "bg-accent/20 text-accent" : "text-ink-muted hover:text-ink hover:bg-surface-2",
            )}
            title="Show line-level diff vs source"
          >
            <GitCompare className="w-3 h-3" />
            Diff
            <span className="font-mono text-[9px] text-status-ok">+{diffStats.adds}</span>
            <span className="font-mono text-[9px] text-status-error">-{diffStats.dels}</span>
          </button>
        )}
        {onChange && !diffMode && (
          <button
            onClick={() => {
              if (editMode) {
                onChange(draft);
              } else {
                setDraft(yaml);
              }
              setEditMode((m) => !m);
            }}
            className={clsx(
              "inline-flex items-center gap-1 h-7 px-2 rounded-md text-[10px]",
              editMode ? "bg-accent text-surface-0" : "text-ink-muted hover:text-ink hover:bg-surface-2",
            )}
            title={editMode ? "Apply changes" : "Edit raw YAML"}
          >
            {editMode ? <><Check className="w-3 h-3" /> Apply</> : <><Edit3 className="w-3 h-3" /> Edit</>}
          </button>
        )}
        <button
          onClick={onClose}
          className="text-ink-muted hover:text-ink"
          title="Close"
        >
          <X className="w-4 h-4" />
        </button>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        {diffMode ? (
          <pre className="text-[11px] font-mono whitespace-pre overflow-x-auto">
            {diff.map((d, i) => (
              <div
                key={i}
                className={clsx(
                  "px-3 leading-tight",
                  d.kind === "add" && "bg-status-ok/10 text-status-ok",
                  d.kind === "del" && "bg-status-error/10 text-status-error",
                  d.kind === "context" && "text-ink-muted",
                )}
              >
                <span className="inline-block w-7 text-right pr-2 opacity-50 select-none">{d.oldNo ?? ""}</span>
                <span className="inline-block w-7 text-right pr-2 opacity-50 select-none">{d.newNo ?? ""}</span>
                <span className="inline-block w-3 select-none">
                  {d.kind === "add" ? "+" : d.kind === "del" ? "-" : " "}
                </span>
                {d.text}
              </div>
            ))}
          </pre>
        ) : editMode ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-full h-full p-3 bg-surface-0 text-[11px] font-mono text-ink resize-none focus:outline-none"
            style={{ minHeight: "100%" }}
            spellCheck={false}
          />
        ) : (
          <pre className="p-3 text-[11px] font-mono text-ink whitespace-pre overflow-x-auto">
            {yaml || "# (no YAML loaded)"}
          </pre>
        )}
      </div>
      <div className="px-3 py-2 border-t border-border-subtle text-[10px] text-ink-dim leading-snug">
        {editMode
          ? "Editing raw YAML. Click Apply to flow changes back into the canvas."
          : "Read-only preview. Edit fields in the Inspector or click Edit to modify the raw YAML."}
      </div>
    </aside>
  );
}
