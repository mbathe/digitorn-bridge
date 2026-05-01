/**
 * Action-level capability grant editor.
 *
 * Replaces the generic EditableConfig form for the `capabilities`
 * node with a 2D matrix:
 *   - rows = declared modules
 *   - columns = grant / approve / deny columns with checkable per-action chips
 *
 * Editing a cell mutates the corresponding capabilities.{grant,approve,deny}
 * sub-tree in the YAML via onEdit.
 */
import { useMemo, useState } from "react";
import { Shield, ShieldCheck, ShieldX, Search } from "lucide-react";
import clsx from "clsx";

interface CapEntry { module: string; actions: string[] }
interface CapBlock {
  default_policy?: string;
  max_risk_level?: string;
  grant?: CapEntry[];
  approve?: CapEntry[];
  deny?: CapEntry[];
}

interface Props {
  capabilities: CapBlock;
  declaredModules: string[];
  /** When known, the action manifest per module — drives the columns
   *  available per row. When unknown, falls back to "show what's
   *  granted only" (no checkbox grid). */
  moduleActions?: Record<string, string[]>;
  /** Path inside the YAML doc (e.g. "capabilities"). Edits dispatch
   *  on `${basePath}.<grant|approve|deny>`. */
  basePath: string;
  onEdit: (path: string, value: unknown) => void;
}

type Column = "grant" | "approve" | "deny";
const COLUMNS: Array<{ id: Column; label: string; tone: string; icon: typeof Shield; hint: string }> = [
  { id: "grant",   label: "Grant",   tone: "ok",    icon: Shield,      hint: "Allowed at runtime" },
  { id: "approve", label: "Approve", tone: "warn",  icon: ShieldCheck, hint: "Allowed but pauses for human approval" },
  { id: "deny",    label: "Deny",    tone: "error", icon: ShieldX,     hint: "Hard-blocked even if granted" },
];

const TONE_CLASS: Record<string, { on: string; off: string }> = {
  ok:    { on: "bg-status-ok/20 text-status-ok border-status-ok/40",       off: "bg-surface-2 text-ink-muted border-border-subtle" },
  warn:  { on: "bg-status-warn/20 text-status-warn border-status-warn/40", off: "bg-surface-2 text-ink-muted border-border-subtle" },
  error: { on: "bg-status-error/20 text-status-error border-status-error/40", off: "bg-surface-2 text-ink-muted border-border-subtle" },
};

export default function GrantMatrix({ capabilities, declaredModules, moduleActions, basePath, onEdit }: Props) {
  const [query, setQuery] = useState("");
  const [pageSize] = useState(20);
  const [page, setPage] = useState(0);
  const allRows = useMemo(() => {
    // Aggregate every module mentioned across declaredModules + the
    // existing capability lists, so renaming or stale grants are still
    // visible.
    const set = new Set<string>(declaredModules);
    for (const list of [capabilities.grant, capabilities.approve, capabilities.deny]) {
      for (const e of list ?? []) if (e.module) set.add(e.module);
    }
    return Array.from(set).sort();
  }, [capabilities, declaredModules]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return allRows;
    return allRows.filter((m) => m.toLowerCase().includes(q));
  }, [allRows, query]);
  const rows = useMemo(
    () => filtered.slice(page * pageSize, (page + 1) * pageSize),
    [filtered, page, pageSize],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));

  const lookup = (col: Column, mod: string): string[] => {
    const list = capabilities[col] ?? [];
    const entry = list.find((e) => e.module === mod);
    return entry?.actions ?? [];
  };

  const allActionsFor = (mod: string): string[] => {
    if (moduleActions?.[mod]) return moduleActions[mod];
    // Fall back: union of every action mentioned in any of the 3 lists.
    const set = new Set<string>();
    for (const col of COLUMNS) {
      const list = capabilities[col.id] ?? [];
      const e = list.find((x) => x.module === mod);
      for (const a of e?.actions ?? []) set.add(a);
    }
    return Array.from(set).sort();
  };

  const toggleAction = (col: Column, mod: string, action: string) => {
    const list = (capabilities[col] ?? []) as CapEntry[];
    const idx = list.findIndex((e) => e.module === mod);
    let nextList: CapEntry[];
    if (idx < 0) {
      nextList = [...list, { module: mod, actions: [action] }];
    } else {
      const cur = list[idx];
      const has = cur.actions.includes(action);
      const acts = has ? cur.actions.filter((a) => a !== action) : [...cur.actions, action];
      const updated = { ...cur, actions: acts };
      if (acts.length === 0) {
        nextList = list.filter((_, i) => i !== idx);
      } else {
        nextList = list.map((e, i) => (i === idx ? updated : e));
      }
    }
    onEdit(`${basePath}.${col}`, nextList);
  };

  return (
    <div className="p-4 space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold mb-2">
        Per-action capability matrix · {filtered.length} module{filtered.length !== 1 ? "s" : ""}
      </div>
      {/* Search input + pagination — shown only when needed */}
      {allRows.length > 8 && (
        <div className="flex items-center gap-2">
          <div className="flex-1 flex items-center gap-1.5 h-7 px-2 rounded-md bg-surface-2 border border-border-subtle">
            <Search className="w-3 h-3 text-ink-dim" />
            <input
              value={query}
              onChange={(e) => { setQuery(e.target.value); setPage(0); }}
              placeholder={`Filter ${allRows.length} modules…`}
              className="flex-1 bg-transparent outline-none text-[11px] text-ink placeholder:text-ink-dim"
            />
          </div>
          {totalPages > 1 && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="h-6 px-1.5 rounded-md text-[10px] text-ink-muted hover:text-ink hover:bg-surface-2 disabled:opacity-40"
              >
                ‹
              </button>
              <span className="text-[10px] font-mono text-ink-dim">
                {page + 1}/{totalPages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                disabled={page >= totalPages - 1}
                className="h-6 px-1.5 rounded-md text-[10px] text-ink-muted hover:text-ink hover:bg-surface-2 disabled:opacity-40"
              >
                ›
              </button>
            </div>
          )}
        </div>
      )}
      {rows.length === 0 && (
        <div className="text-xs text-ink-dim italic">
          {query ? "No modules match the filter." : "No modules declared yet. Drop a Module from the palette."}
        </div>
      )}
      <div className="space-y-2">
        {rows.map((mod) => {
          const acts = allActionsFor(mod);
          return (
            <div key={mod} className="rounded-lg border border-border-subtle bg-surface-2/40 p-2.5">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-[11px] font-mono font-semibold text-ink">{mod}</span>
                <span className="text-[10px] text-ink-dim">{acts.length} action{acts.length !== 1 ? "s" : ""}</span>
              </div>
              {acts.length === 0 ? (
                <div className="text-[10px] text-ink-dim italic">
                  No actions known. Type names below to grant.
                </div>
              ) : (
                <div className="grid grid-cols-3 gap-1.5">
                  {COLUMNS.map((col) => {
                    const Icon = col.icon;
                    const tone = TONE_CLASS[col.tone];
                    const onActs = lookup(col.id, mod);
                    return (
                      <div key={col.id} className="space-y-1">
                        <div className="flex items-center gap-1 text-[9px] uppercase tracking-wider font-semibold text-ink-dim mb-0.5" title={col.hint}>
                          <Icon className="w-2.5 h-2.5" />
                          {col.label}
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {acts.map((a) => {
                            const on = onActs.includes(a);
                            return (
                              <button
                                key={a}
                                onClick={() => toggleAction(col.id, mod, a)}
                                className={clsx(
                                  "px-1.5 h-5 inline-flex items-center text-[10px] font-mono rounded border",
                                  on ? tone.on : tone.off,
                                  "transition-colors hover:opacity-80",
                                )}
                                title={`${col.label} ${mod}.${a}`}
                              >
                                {a}
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="text-[10px] text-ink-dim leading-snug pt-2 border-t border-border-subtle">
        <strong>Grant</strong> allows a tool at runtime. <strong>Approve</strong> lets it run but
        pauses for the human first. <strong>Deny</strong> hard-blocks it even if granted elsewhere.
      </div>
    </div>
  );
}
