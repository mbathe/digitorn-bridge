/**
 * Outline tree explorer — VSCode-style sidebar that lets the user
 * navigate 1000+ nodes without panning the canvas.
 *
 * The tree groups nodes by lane (Inputs / Palette / Behavior /
 * Agents / Capabilities / Tools / Hooks / Outputs / App). Each
 * group is collapsible. Each entry shows kind icon + label +
 * validation dot. Click → centers viewport + selects the node.
 *
 * Powered search bar at top filters by substring match across all
 * labels — far faster than panning to find a specific module on a
 * 1000-node canvas.
 */
import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Search, X, ListTree } from "lucide-react";
import clsx from "clsx";
import type { Node as RFNode } from "reactflow";
import type { EnrichedNodeData } from "../lib/enrich-graph";
import { LANES, assignLane, type LaneId } from "../lib/lanes";

interface Props {
  nodes: RFNode<EnrichedNodeData>[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  collapsed: boolean;
  onToggle: () => void;
}

export default function OutlineTree({ nodes, selectedId, onSelect, collapsed, onToggle }: Props) {
  const [query, setQuery] = useState("");
  const [openLanes, setOpenLanes] = useState<Set<LaneId>>(
    () => new Set(LANES.map((l) => l.id)),
  );

  // Bucket by lane. Skip child nodes (parentNode set) — they'll
  // appear under their parent in the tree.
  const buckets = useMemo(() => {
    const m = new Map<LaneId, RFNode<EnrichedNodeData>[]>();
    for (const n of nodes) {
      if ((n as RFNode & { parentNode?: string }).parentNode) continue;
      const lane = assignLane((n.data?.kind as string) ?? "", n.id);
      if (!lane) continue;
      const arr = m.get(lane) ?? [];
      arr.push(n);
      m.set(lane, arr);
    }
    return m;
  }, [nodes]);

  // Children index for parent expansion
  const childrenByParent = useMemo(() => {
    const m = new Map<string, RFNode<EnrichedNodeData>[]>();
    for (const n of nodes) {
      const pid = (n as RFNode & { parentNode?: string }).parentNode;
      if (!pid) continue;
      const arr = m.get(pid) ?? [];
      arr.push(n);
      m.set(pid, arr);
    }
    return m;
  }, [nodes]);

  const q = query.trim().toLowerCase();
  const matches = (n: RFNode<EnrichedNodeData>) => {
    if (!q) return true;
    const d = n.data;
    return (
      (d.label ?? "").toLowerCase().includes(q)
      || (d.kind as string).toLowerCase().includes(q)
      || (d.subtitle ?? "").toLowerCase().includes(q)
      || n.id.toLowerCase().includes(q)
    );
  };

  if (collapsed) {
    return (
      <div className="w-9 border-r border-border-subtle bg-surface-1 flex flex-col items-center py-2">
        <button
          onClick={onToggle}
          className="w-7 h-7 inline-flex items-center justify-center rounded-md text-ink-muted hover:text-ink hover:bg-surface-2"
          title="Open outline"
        >
          <ListTree className="w-4 h-4" />
        </button>
      </div>
    );
  }

  return (
    <aside className="w-[240px] flex-shrink-0 border-r border-border-subtle bg-surface-1 flex flex-col">
      <div className="flex items-center gap-2 px-3 h-11 border-b border-border-subtle">
        <ListTree className="w-3.5 h-3.5 text-accent" />
        <span className="text-[10px] uppercase tracking-wider font-bold text-accent">
          Outline · {nodes.length}
        </span>
        <div className="flex-1" />
        <button
          onClick={onToggle}
          className="w-6 h-6 inline-flex items-center justify-center rounded text-ink-muted hover:text-ink"
          title="Collapse outline"
        >
          ‹
        </button>
      </div>
      <div className="px-2 py-2 border-b border-border-subtle">
        <div className="flex items-center gap-1.5 h-7 px-2 rounded-md bg-surface-2 border border-border-subtle">
          <Search className="w-3 h-3 text-ink-dim" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter…"
            className="flex-1 bg-transparent outline-none text-[11px] text-ink placeholder:text-ink-dim"
          />
          {query && (
            <button onClick={() => setQuery("")} className="text-ink-dim hover:text-ink"><X className="w-3 h-3" /></button>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto py-1">
        {LANES.map((lane) => {
          const items = (buckets.get(lane.id) ?? []).filter(matches);
          if (items.length === 0 && q) return null;
          if ((buckets.get(lane.id) ?? []).length === 0 && !q) return null;
          const open = openLanes.has(lane.id);
          return (
            <div key={lane.id} className="mb-0.5">
              <button
                onClick={() => {
                  const next = new Set(openLanes);
                  if (open) next.delete(lane.id); else next.add(lane.id);
                  setOpenLanes(next);
                }}
                className="w-full flex items-center gap-1.5 px-2 h-6 text-[10px] uppercase tracking-wider font-semibold text-ink-dim hover:text-ink hover:bg-surface-2"
              >
                {open ? <ChevronDown className="w-2.5 h-2.5" /> : <ChevronRight className="w-2.5 h-2.5" />}
                <span>{lane.label}</span>
                <span className="text-ink-dim font-mono">{items.length}</span>
              </button>
              {open && items.map((n) => (
                <OutlineRow
                  key={n.id}
                  node={n}
                  selected={selectedId === n.id}
                  onSelect={onSelect}
                  childrenIdx={childrenByParent}
                  matches={matches}
                  depth={0}
                />
              ))}
            </div>
          );
        })}
      </div>
    </aside>
  );
}

function OutlineRow({
  node,
  selected,
  onSelect,
  childrenIdx,
  matches,
  depth,
}: {
  node: RFNode<EnrichedNodeData>;
  selected: boolean;
  onSelect: (id: string) => void;
  childrenIdx: Map<string, RFNode<EnrichedNodeData>[]>;
  matches: (n: RFNode<EnrichedNodeData>) => boolean;
  depth: number;
}) {
  const [open, setOpen] = useState(true);
  const children = childrenIdx.get(node.id) ?? [];
  const filteredChildren = children.filter(matches);
  const hasChildren = filteredChildren.length > 0;
  const d = node.data;
  return (
    <>
      <div className="group">
        <button
          onClick={() => onSelect(node.id)}
          style={{ paddingLeft: 8 + depth * 12 }}
          className={clsx(
            "w-full flex items-center gap-1.5 h-6 pr-2 text-[11px]",
            selected ? "bg-accent/15 text-accent" : "text-ink-muted hover:bg-surface-2 hover:text-ink",
          )}
        >
          {hasChildren ? (
            <span
              onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
              className="w-3 h-3 inline-flex items-center justify-center text-ink-dim hover:text-ink"
            >
              {open ? <ChevronDown className="w-2.5 h-2.5" /> : <ChevronRight className="w-2.5 h-2.5" />}
            </span>
          ) : (
            <span className="w-3 h-3" />
          )}
          {d.validation && (
            <span
              className={clsx(
                "w-1.5 h-1.5 rounded-full flex-shrink-0",
                d.validation === "error" && "bg-status-error",
                d.validation === "warn" && "bg-status-warn",
                d.validation === "info" && "bg-status-running",
              )}
            />
          )}
          <span className="text-[9px] font-mono uppercase text-ink-dim tracking-wider">
            {(d.kind as string).slice(0, 3)}
          </span>
          <span className="truncate flex-1 text-left">{d.label}</span>
          {hasChildren && (
            <span className="text-[9px] text-ink-dim font-mono">{filteredChildren.length}</span>
          )}
        </button>
      </div>
      {open && hasChildren && filteredChildren.map((c) => (
        <OutlineRow
          key={c.id}
          node={c}
          selected={false}
          onSelect={onSelect}
          childrenIdx={childrenIdx}
          matches={matches}
          depth={depth + 1}
        />
      ))}
    </>
  );
}
