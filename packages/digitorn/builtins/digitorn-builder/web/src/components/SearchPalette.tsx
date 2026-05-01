/**
 * cmd+K search palette — fuzzy filter across every node in the
 * canvas, with arrow-key navigation and Enter to jump+select.
 *
 * No external fuzzy library — a simple subsequence match works well
 * for the vocabulary we have (kebab-case ids, kind names, labels).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Search, X, ArrowRight } from "lucide-react";
import clsx from "clsx";
import type { Node as RFNode } from "reactflow";
import type { EnrichedNodeData } from "../lib/enrich-graph";

interface Props {
  open: boolean;
  nodes: RFNode<EnrichedNodeData>[];
  onSelect: (id: string) => void;
  onClose: () => void;
}

type FilterChip =
  | "all"
  | "agent"
  | "module"
  | "hook"
  | "channel"
  | "skill"
  | "errors"
  | "approval";

const CHIPS: Array<{ id: FilterChip; label: string }> = [
  { id: "all", label: "All" },
  { id: "agent", label: "Agents" },
  { id: "module", label: "Modules" },
  { id: "hook", label: "Hooks" },
  { id: "channel", label: "Channels" },
  { id: "skill", label: "Skills" },
  { id: "errors", label: "❗ Errors" },
  { id: "approval", label: "🔒 Approval" },
];

interface Hit {
  node: RFNode<EnrichedNodeData>;
  score: number;
  hl: { label: number[]; kind: number[]; subtitle: number[] };
}

function fuzzyScore(needle: string, hay: string): { score: number; positions: number[] } {
  if (!needle) return { score: 0, positions: [] };
  needle = needle.toLowerCase();
  hay = hay.toLowerCase();
  const positions: number[] = [];
  let h = 0, n = 0, score = 0, lastMatch = -2;
  while (n < needle.length && h < hay.length) {
    if (needle[n] === hay[h]) {
      positions.push(h);
      // bonuses: consecutive match, start of token (after - / _ / .)
      let bonus = 1;
      if (h === lastMatch + 1) bonus += 2;
      if (h === 0 || /[-_./\s]/.test(hay[h - 1])) bonus += 3;
      score += bonus;
      lastMatch = h;
      n++;
    }
    h++;
  }
  if (n < needle.length) return { score: -1, positions: [] };
  return { score, positions };
}

export default function SearchPalette({ open, nodes, onSelect, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [chip, setChip] = useState<FilterChip>("all");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      setChip("all");
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  // ESC closes
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const hits: Hit[] = useMemo(() => {
    if (!open) return [];
    const q = query.trim();
    const out: Hit[] = [];
    for (const n of nodes) {
      const d = n.data;
      // Apply filter chip first
      if (chip !== "all") {
        const kindStr = (d.kind as string) ?? "";
        if (chip === "errors") {
          if (d.validation !== "error" && d.validation !== "warn") continue;
        } else if (chip === "approval") {
          const acts = (d as unknown as { approveActions?: string[] }).approveActions;
          if (!Array.isArray(acts) || acts.length === 0) continue;
        } else if (kindStr !== chip) continue;
      }
      const label = d.label ?? n.id;
      const kind = (d.kind as string) ?? "";
      const subtitle = d.subtitle ?? "";
      if (!q) {
        out.push({ node: n, score: 1, hl: { label: [], kind: [], subtitle: [] } });
        continue;
      }
      const sl = fuzzyScore(q, label);
      const sk = fuzzyScore(q, kind);
      const ss = fuzzyScore(q, subtitle);
      const sn = fuzzyScore(q, n.id);
      const best = Math.max(sl.score, sk.score, ss.score, sn.score);
      if (best < 0) continue;
      out.push({
        node: n,
        score: best + (sl.score >= 0 ? 5 : 0), // label hits trump
        hl: { label: sl.positions, kind: sk.positions, subtitle: ss.positions },
      });
    }
    out.sort((a, b) => b.score - a.score);
    return out.slice(0, 30);
  }, [query, nodes, open, chip]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(hits.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = hits[active];
      if (hit) {
        onSelect(hit.node.id);
        onClose();
      }
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-32 px-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-[min(640px,100%)] rounded-xl bg-surface-1 border border-border shadow-2xl overflow-hidden">
        <div className="flex items-center gap-3 px-4 h-12 border-b border-border-subtle">
          <Search className="w-4 h-4 text-ink-muted" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
            placeholder="Jump to any node…"
            className="flex-1 bg-transparent outline-none text-sm text-ink placeholder:text-ink-dim"
          />
          <kbd className="text-[10px] font-mono text-ink-dim">ESC</kbd>
          <button onClick={onClose} className="text-ink-muted hover:text-ink">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
        {/* Filter chips */}
        <div className="flex items-center gap-1 px-3 py-1.5 border-b border-border-subtle/60 overflow-x-auto">
          {CHIPS.map((c) => (
            <button
              key={c.id}
              onClick={() => { setChip(c.id); setActive(0); }}
              className={clsx(
                "h-6 px-2 inline-flex items-center rounded-md text-[10px] font-medium whitespace-nowrap transition-colors",
                chip === c.id
                  ? "bg-accent/15 text-accent"
                  : "text-ink-muted hover:text-ink hover:bg-surface-2",
              )}
            >
              {c.label}
            </button>
          ))}
        </div>
        <div className="max-h-[400px] overflow-y-auto">
          {hits.length === 0 && (
            <div className="px-4 py-6 text-center text-xs text-ink-dim italic">
              No matches.
            </div>
          )}
          {hits.map((h, i) => {
            const d = h.node.data;
            const isActive = i === active;
            return (
              <button
                key={h.node.id}
                onClick={() => { onSelect(h.node.id); onClose(); }}
                onMouseEnter={() => setActive(i)}
                className={clsx(
                  "w-full text-left flex items-center gap-3 px-4 py-2 border-b border-border-subtle/40 last:border-b-0",
                  isActive ? "bg-accent/10" : "hover:bg-surface-2",
                )}
              >
                <span className="text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded bg-surface-3 text-ink-muted flex-shrink-0">
                  {d.kind as string}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-ink truncate font-medium">
                    {d.label}
                  </div>
                  {d.subtitle && (
                    <div className="text-[10px] text-ink-muted truncate">
                      {d.subtitle}
                    </div>
                  )}
                </div>
                {isActive && <ArrowRight className="w-3 h-3 text-accent" />}
              </button>
            );
          })}
        </div>
        <div className="px-4 py-2 border-t border-border-subtle text-[10px] text-ink-dim font-mono flex items-center gap-3">
          <span><kbd>↑↓</kbd> navigate</span>
          <span><kbd>↵</kbd> jump + select</span>
          <span><kbd>ESC</kbd> close</span>
          <div className="flex-1" />
          <span>{hits.length} / {nodes.length}</span>
        </div>
      </div>
    </div>
  );
}
