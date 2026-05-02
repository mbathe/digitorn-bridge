/**
 * Visual editor for the `widgets:` block — recursive tree view + per-node form.
 *
 * Lives inside Inspector's Configuration tab whenever the selected node
 * is the synthetic `widgets` canvas node. Lets the user assemble Flutter-
 * spec v1 widget trees without touching YAML by hand.
 *
 * What it does:
 *   1. Splits the widgets root into 4 sections (chat_side, workspace_tabs[],
 *      modals{}, inline{}). Each section has its own "Add" affordance —
 *      Add chat_side / Add workspace tab / Add modal "name" / Add inline "name".
 *   2. Renders the WidgetNode tree under each section as a recursive list of
 *      rows. Each row carries: type chip (color-coded by category), id badge,
 *      "Add child" picker (only when the node has at least one container slot),
 *      "Delete" button.
 *   3. When a row is selected, the panel below renders an inline EditableConfig
 *      form against THAT node's path — reusing the same primitive form
 *      machinery agents/hooks/etc. already use.
 *
 * Path conventions (so onEdit/onDelete callbacks stay schema-faithful):
 *   - chat_side.tree                  -> "widgets.chat_side.tree"
 *   - chat_side.tree.children[2]      -> "widgets.chat_side.tree.children.2"
 *   - workspace_tabs[0].tree          -> "widgets.workspace_tabs.0.tree"
 *   - modals.confirm_delete.tree      -> "widgets.modals.confirm_delete.tree"
 *   - inline.banner.tree              -> "widgets.inline.banner.tree"
 *
 * The editor never deletes the surrounding wrapper (`tree:` key) when a
 * user removes the root WidgetNode — instead it nulls the wrapper itself
 * (`onDelete("widgets.modals.confirm_delete")`) which keeps the YAML clean.
 */
import { useState } from "react";
import {
  Plus, Trash2, ChevronRight, ChevronDown, MessageSquare, Layers,
  Square, Anchor,
} from "lucide-react";
import clsx from "clsx";
import EditableConfig from "./EditableConfig";
import {
  WIDGET_PRIMITIVES, CATEGORY_LABEL, CATEGORY_COLOR, getPrimitive,
  makeWidgetDefault, UNKNOWN_PRIMITIVE_ICON,
  type ContainerKey, type WidgetCategory,
} from "../lib/widget-primitives";

type WidgetNode = Record<string, unknown> & { type?: string };

interface Props {
  /** The full `widgets:` block from the parsed YAML. */
  raw: Record<string, unknown>;
  /** Absolute YAML path to the widgets block (typically just "widgets"). */
  basePath: string;
  doc?: unknown;
  onEdit: (absolutePath: string, value: unknown) => void;
  onDelete: (absolutePath: string) => void;
}

type Section = "chat_side" | "workspace_tabs" | "modals" | "inline";

const SECTION_META: Record<
  Section,
  { label: string; icon: typeof MessageSquare; hint: string }
> = {
  chat_side: {
    label: "Chat sidebar",
    icon: MessageSquare,
    hint: "Companion panel docked next to the chat.",
  },
  workspace_tabs: {
    label: "Workspace tabs",
    icon: Layers,
    hint: "Tabs in the workspace 'Widgets' container.",
  },
  modals: {
    label: "Modals",
    icon: Square,
    hint: "Modals pushed by `action: open_modal`.",
  },
  inline: {
    label: "Inline",
    icon: Anchor,
    hint: "Named widgets the agent renders via `widget.render` + ref:.",
  },
};

export default function WidgetTreeEditor({
  raw, basePath, doc, onEdit, onDelete,
}: Props) {
  const [section, setSection] = useState<Section>("chat_side");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);

  return (
    <div className="space-y-3">
      <SectionTabs active={section} onChange={(s) => { setSection(s); setSelectedPath(null); }} />

      {section === "chat_side" && (
        <ChatSidePane
          raw={raw}
          basePath={basePath}
          selectedPath={selectedPath}
          onSelect={setSelectedPath}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      )}
      {section === "workspace_tabs" && (
        <WorkspaceTabsPane
          raw={raw}
          basePath={basePath}
          selectedPath={selectedPath}
          onSelect={setSelectedPath}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      )}
      {section === "modals" && (
        <NamedDictPane
          dictKey="modals"
          raw={raw}
          basePath={basePath}
          selectedPath={selectedPath}
          onSelect={setSelectedPath}
          onEdit={onEdit}
          onDelete={onDelete}
          newEntryDefaults={() => ({
            title: "New modal",
            dismissible: true,
            tree: { type: "text", text: "Modal body" },
          })}
        />
      )}
      {section === "inline" && (
        <NamedDictPane
          dictKey="inline"
          raw={raw}
          basePath={basePath}
          selectedPath={selectedPath}
          onSelect={setSelectedPath}
          onEdit={onEdit}
          onDelete={onDelete}
          newEntryDefaults={() => ({
            tree: { type: "text", text: "Inline widget" },
          })}
        />
      )}

      {selectedPath && (
        <NodeForm
          path={selectedPath}
          value={readPath(raw, stripBase(selectedPath, basePath))}
          doc={doc}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      )}
    </div>
  );
}

/* ─── Section tabs ──────────────────────────────────────── */

function SectionTabs({
  active, onChange,
}: {
  active: Section;
  onChange: (s: Section) => void;
}) {
  return (
    <div className="flex gap-1 p-1 bg-surface-2 rounded-lg border border-border-subtle">
      {(Object.keys(SECTION_META) as Section[]).map((s) => {
        const meta = SECTION_META[s];
        const Icon = meta.icon;
        const selected = s === active;
        return (
          <button
            key={s}
            onClick={() => onChange(s)}
            title={meta.hint}
            className={clsx(
              "flex-1 inline-flex items-center justify-center gap-1.5 h-7 px-2 rounded-md text-[11px] transition-colors",
              selected
                ? "bg-accent/15 text-accent font-medium"
                : "text-ink-muted hover:text-ink hover:bg-surface-3",
            )}
          >
            <Icon className="w-3 h-3" />
            <span className="truncate">{meta.label}</span>
          </button>
        );
      })}
    </div>
  );
}

/* ─── chat_side: optional single object ─────────────────── */

function ChatSidePane({
  raw, basePath, selectedPath, onSelect, onEdit, onDelete,
}: {
  raw: Record<string, unknown>;
  basePath: string;
  selectedPath: string | null;
  onSelect: (p: string | null) => void;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
}) {
  const cs = raw.chat_side as Record<string, unknown> | undefined;
  const csPath = `${basePath}.chat_side`;

  if (!cs) {
    return (
      <EmptyAdd
        label="No chat sidebar"
        sub="Add a docked panel next to the chat."
        onAdd={() =>
          onEdit(csPath, {
            title: "Sidebar",
            collapsible: true,
            default_open: true,
            tree: { type: "column", children: [] },
          })
        }
      />
    );
  }

  const treePath = `${csPath}.tree`;
  return (
    <div className="space-y-2">
      <RootMeta
        title={(cs.title as string) ?? "Sidebar"}
        onSelect={() => onSelect(csPath)}
        selected={selectedPath === csPath}
        onDelete={() => { onDelete(csPath); onSelect(null); }}
        deleteLabel="Remove sidebar"
      />
      <WidgetTree
        node={cs.tree as WidgetNode | undefined}
        path={treePath}
        depth={0}
        selectedPath={selectedPath}
        onSelect={onSelect}
        onEdit={onEdit}
        onDelete={onDelete}
        onReplaceRoot={(v) => onEdit(treePath, v)}
      />
    </div>
  );
}

/* ─── workspace_tabs: array ─────────────────────────────── */

function WorkspaceTabsPane({
  raw, basePath, selectedPath, onSelect, onEdit, onDelete,
}: {
  raw: Record<string, unknown>;
  basePath: string;
  selectedPath: string | null;
  onSelect: (p: string | null) => void;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
}) {
  const tabs = (raw.workspace_tabs as Array<Record<string, unknown>> | undefined) ?? [];
  const tabsPath = `${basePath}.workspace_tabs`;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-[11px] text-ink-dim">{tabs.length} tab{tabs.length === 1 ? "" : "s"}</div>
        <button
          onClick={() => {
            const nextId = `tab_${tabs.length + 1}`;
            onEdit(`${tabsPath}.${tabs.length}`, {
              id: nextId,
              title: "New tab",
              tree: { type: "column", children: [] },
            });
          }}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] bg-accent/15 text-accent hover:bg-accent/25"
        >
          <Plus className="w-3 h-3" /> Add tab
        </button>
      </div>

      {tabs.length === 0 ? (
        <EmptyAdd
          label="No workspace tabs"
          sub="Each tab carries its own widget tree."
          onAdd={() =>
            onEdit(`${tabsPath}.0`, {
              id: "main",
              title: "Main",
              tree: { type: "column", children: [] },
            })
          }
        />
      ) : (
        tabs.map((tab, i) => {
          const tabPath = `${tabsPath}.${i}`;
          const treePath = `${tabPath}.tree`;
          return (
            <div key={i} className="space-y-1">
              <RootMeta
                title={(tab.title as string) ?? `Tab ${i + 1}`}
                badge={(tab.id as string) ?? `#${i}`}
                onSelect={() => onSelect(tabPath)}
                selected={selectedPath === tabPath}
                onDelete={() => { onDelete(tabPath); onSelect(null); }}
                deleteLabel="Remove tab"
              />
              <WidgetTree
                node={tab.tree as WidgetNode | undefined}
                path={treePath}
                depth={0}
                selectedPath={selectedPath}
                onSelect={onSelect}
                onEdit={onEdit}
                onDelete={onDelete}
                onReplaceRoot={(v) => onEdit(treePath, v)}
              />
            </div>
          );
        })
      )}
    </div>
  );
}

/* ─── modals / inline: dict<name, {tree:...}> ───────────── */

function NamedDictPane({
  dictKey, raw, basePath, selectedPath, onSelect, onEdit, onDelete, newEntryDefaults,
}: {
  dictKey: "modals" | "inline";
  raw: Record<string, unknown>;
  basePath: string;
  selectedPath: string | null;
  onSelect: (p: string | null) => void;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
  newEntryDefaults: () => Record<string, unknown>;
}) {
  const dict = (raw[dictKey] as Record<string, Record<string, unknown>> | undefined) ?? {};
  const entries = Object.entries(dict);
  const dictPath = `${basePath}.${dictKey}`;
  const [pendingName, setPendingName] = useState("");

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={pendingName}
          onChange={(e) => setPendingName(e.target.value)}
          placeholder={dictKey === "modals" ? "modal name (e.g. confirm_delete)" : "inline name (e.g. status_banner)"}
          className="flex-1 h-7 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim font-mono focus:outline-none focus:border-accent"
        />
        <button
          disabled={!pendingName.trim() || pendingName in dict}
          onClick={() => {
            const name = pendingName.trim();
            if (!name) return;
            onEdit(`${dictPath}.${name}`, newEntryDefaults());
            setPendingName("");
          }}
          className={clsx(
            "inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px]",
            pendingName.trim() && !(pendingName in dict)
              ? "bg-accent/15 text-accent hover:bg-accent/25"
              : "bg-surface-2 text-ink-dim cursor-not-allowed",
          )}
          title={pendingName in dict ? "Already exists" : `Add ${dictKey === "modals" ? "modal" : "inline widget"}`}
        >
          <Plus className="w-3 h-3" /> Add
        </button>
      </div>

      {entries.length === 0 ? (
        <EmptyAdd
          label={dictKey === "modals" ? "No modals" : "No inline widgets"}
          sub={
            dictKey === "modals"
              ? "Modals are pushed by `action: open_modal`."
              : "Inline widgets are referenced by `ref:` from chat or modals."
          }
        />
      ) : (
        entries.map(([name, entry]) => {
          const entryPath = `${dictPath}.${name}`;
          const treePath = `${entryPath}.tree`;
          return (
            <div key={name} className="space-y-1">
              <RootMeta
                title={(entry.title as string) ?? name}
                badge={name}
                onSelect={() => onSelect(entryPath)}
                selected={selectedPath === entryPath}
                onDelete={() => { onDelete(entryPath); onSelect(null); }}
                deleteLabel={`Remove ${name}`}
              />
              <WidgetTree
                node={entry.tree as WidgetNode | undefined}
                path={treePath}
                depth={0}
                selectedPath={selectedPath}
                onSelect={onSelect}
                onEdit={onEdit}
                onDelete={onDelete}
                onReplaceRoot={(v) => onEdit(treePath, v)}
              />
            </div>
          );
        })
      )}
    </div>
  );
}

/* ─── Recursive WidgetNode tree ─────────────────────────── */

function WidgetTree({
  node, path, depth, selectedPath, onSelect, onEdit, onDelete, onReplaceRoot,
}: {
  node: WidgetNode | undefined;
  path: string;
  depth: number;
  selectedPath: string | null;
  onSelect: (p: string | null) => void;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
  /** When the root of this tree is empty, called with the new node. */
  onReplaceRoot?: (node: WidgetNode) => void;
}) {
  if (!node || typeof node !== "object" || !node.type) {
    return (
      <div className="ml-4">
        <PrimitivePicker
          label="Pick a root primitive"
          onPick={(t) => {
            if (onReplaceRoot) onReplaceRoot(makeWidgetDefault(t) as WidgetNode);
            else onEdit(path, makeWidgetDefault(t));
          }}
        />
      </div>
    );
  }
  return (
    <NodeRow
      node={node}
      path={path}
      depth={depth}
      selectedPath={selectedPath}
      onSelect={onSelect}
      onEdit={onEdit}
      onDelete={onDelete}
    />
  );
}

function NodeRow({
  node, path, depth, selectedPath, onSelect, onEdit, onDelete,
}: {
  node: WidgetNode;
  path: string;
  depth: number;
  selectedPath: string | null;
  onSelect: (p: string | null) => void;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const spec = getPrimitive(String(node.type));
  const Icon = spec?.icon ?? UNKNOWN_PRIMITIVE_ICON;
  const color = spec ? CATEGORY_COLOR[spec.category] : "#94a3b8";
  const containerKeys: ContainerKey[] = (spec?.containerFields ?? []) as ContainerKey[];
  const selected = selectedPath === path;
  const id = node.id as string | undefined;

  const hasAnyChildren = containerKeys.some((k) => {
    const v = node[k];
    return Array.isArray(v) ? v.length > 0 : !!v;
  });

  return (
    <div className="space-y-0.5">
      <div
        className={clsx(
          "group flex items-center gap-1.5 h-7 px-1.5 rounded-md text-[11px] cursor-pointer transition-colors",
          selected ? "bg-accent/15 ring-1 ring-accent/40" : "hover:bg-surface-2",
        )}
        style={{ paddingLeft: `${4 + depth * 12}px` }}
        onClick={() => onSelect(selected ? null : path)}
      >
        {hasAnyChildren ? (
          <button
            onClick={(e) => { e.stopPropagation(); setCollapsed(!collapsed); }}
            className="p-0.5 -ml-0.5 text-ink-dim hover:text-ink"
            title={collapsed ? "Expand" : "Collapse"}
          >
            {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          </button>
        ) : (
          <span className="w-3 h-3 inline-block" />
        )}
        <Icon className="w-3 h-3 flex-shrink-0" style={{ color }} />
        <span className="font-mono font-medium" style={{ color }}>{node.type}</span>
        {id && (
          <span className="text-ink-dim font-mono text-[10px]">#{id}</span>
        )}
        <span className="flex-1" />

        {containerKeys.length > 0 && (
          <ContainerAddMenu
            containerKeys={containerKeys}
            onPick={(slot, type) => {
              const newNode = makeWidgetDefault(type);
              const existing = node[slot];
              if (Array.isArray(existing)) {
                onEdit(`${path}.${slot}.${existing.length}`, newNode);
              } else if (existing && typeof existing === "object") {
                // single-child slot already taken — overwrite
                onEdit(`${path}.${slot}`, newNode);
              } else {
                // Empty slot: first child → seed array OR single value
                if (slot === "children") {
                  onEdit(`${path}.${slot}.0`, newNode);
                } else {
                  onEdit(`${path}.${slot}`, newNode);
                }
              }
            }}
          />
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete(path);
            if (selected) onSelect(null);
          }}
          className="p-0.5 text-status-error/60 hover:text-status-error opacity-0 group-hover:opacity-100 transition-opacity"
          title="Delete this widget"
        >
          <Trash2 className="w-3 h-3" />
        </button>
      </div>

      {!collapsed && containerKeys.map((slot) => {
        const v = node[slot];
        if (Array.isArray(v) && v.length > 0) {
          return (
            <div key={slot}>
              <SlotLabel slot={slot} depth={depth + 1} />
              {(v as WidgetNode[]).map((child, i) => (
                <NodeRow
                  key={i}
                  node={child}
                  path={`${path}.${slot}.${i}`}
                  depth={depth + 2}
                  selectedPath={selectedPath}
                  onSelect={onSelect}
                  onEdit={onEdit}
                  onDelete={onDelete}
                />
              ))}
            </div>
          );
        }
        if (v && typeof v === "object" && !Array.isArray(v)) {
          return (
            <div key={slot}>
              <SlotLabel slot={slot} depth={depth + 1} />
              <NodeRow
                node={v as WidgetNode}
                path={`${path}.${slot}`}
                depth={depth + 2}
                selectedPath={selectedPath}
                onSelect={onSelect}
                onEdit={onEdit}
                onDelete={onDelete}
              />
            </div>
          );
        }
        return null;
      })}
    </div>
  );
}

function SlotLabel({ slot, depth }: { slot: ContainerKey; depth: number }) {
  return (
    <div
      className="text-[9px] uppercase tracking-wider text-ink-dim font-mono"
      style={{ paddingLeft: `${4 + depth * 12}px` }}
    >
      {slot}
    </div>
  );
}

/* ─── Add primitive button + dropdown ───────────────────── */

function ContainerAddMenu({
  containerKeys, onPick,
}: {
  containerKeys: ContainerKey[];
  onPick: (slot: ContainerKey, type: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [slot, setSlot] = useState<ContainerKey>(containerKeys[0]);

  return (
    <div className="relative">
      <button
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}
        className="p-0.5 text-ink-dim hover:text-accent opacity-0 group-hover:opacity-100 transition-opacity"
        title="Add child"
      >
        <Plus className="w-3 h-3" />
      </button>
      {open && (
        <PrimitivePopover
          onClose={() => setOpen(false)}
          slotPicker={
            containerKeys.length > 1 ? (
              <div className="px-2 py-1.5 border-b border-border-subtle">
                <div className="text-[9px] uppercase tracking-wider text-ink-dim mb-1">Add to slot</div>
                <div className="flex gap-1 flex-wrap">
                  {containerKeys.map((k) => (
                    <button
                      key={k}
                      onClick={() => setSlot(k)}
                      className={clsx(
                        "px-1.5 py-0.5 rounded text-[10px] font-mono",
                        slot === k ? "bg-accent/15 text-accent" : "text-ink-muted hover:bg-surface-3",
                      )}
                    >
                      {k}
                    </button>
                  ))}
                </div>
              </div>
            ) : null
          }
          onPick={(type) => {
            onPick(slot, type);
            setOpen(false);
          }}
        />
      )}
    </div>
  );
}

function PrimitivePicker({
  label, onPick,
}: {
  label: string;
  onPick: (type: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] bg-surface-2 border border-border-subtle text-ink-muted hover:text-ink hover:bg-surface-3"
      >
        <Plus className="w-3 h-3" /> {label}
      </button>
      {open && (
        <PrimitivePopover
          onClose={() => setOpen(false)}
          onPick={(t) => { onPick(t); setOpen(false); }}
        />
      )}
    </div>
  );
}

function PrimitivePopover({
  onPick, onClose, slotPicker,
}: {
  onPick: (type: string) => void;
  onClose: () => void;
  slotPicker?: React.ReactNode;
}) {
  const [filter, setFilter] = useState("");
  const groups: Record<WidgetCategory, typeof WIDGET_PRIMITIVES[number][]> = {
    layout: [], content: [], data: [], input: [], action: [], feedback: [],
  };
  for (const p of WIDGET_PRIMITIVES) {
    if (filter && !p.type.toLowerCase().includes(filter.toLowerCase()) && !p.label.toLowerCase().includes(filter.toLowerCase())) {
      continue;
    }
    groups[p.category].push(p);
  }

  return (
    <>
      <div
        className="fixed inset-0 z-40"
        onClick={onClose}
      />
      <div className="absolute right-0 top-6 z-50 w-72 max-h-96 overflow-auto rounded-lg bg-surface-1 border border-border-subtle shadow-2xl">
        {slotPicker}
        <div className="p-2 border-b border-border-subtle">
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            autoFocus
            placeholder="Filter primitives…"
            className="w-full h-7 px-2 rounded-md bg-surface-2 border border-border-subtle text-[11px] text-ink placeholder:text-ink-dim focus:outline-none focus:border-accent"
          />
        </div>
        {(Object.keys(groups) as WidgetCategory[]).map((cat) => {
          if (groups[cat].length === 0) return null;
          return (
            <div key={cat} className="p-1.5">
              <div className="px-1.5 mb-1 text-[9px] uppercase tracking-wider text-ink-dim font-semibold flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: CATEGORY_COLOR[cat] }} />
                {CATEGORY_LABEL[cat]}
              </div>
              <div className="grid grid-cols-2 gap-0.5">
                {groups[cat].map((p) => {
                  const Icon = p.icon;
                  return (
                    <button
                      key={p.type}
                      onClick={() => onPick(p.type)}
                      title={p.hint}
                      className="inline-flex items-center gap-1.5 h-7 px-2 rounded-md text-[11px] text-ink-muted hover:text-ink hover:bg-surface-2 text-left"
                    >
                      <Icon className="w-3 h-3 flex-shrink-0" style={{ color: CATEGORY_COLOR[cat] }} />
                      <span className="truncate">{p.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

/* ─── Section root summary row ──────────────────────────── */

function RootMeta({
  title, badge, onSelect, selected, onDelete, deleteLabel,
}: {
  title: string;
  badge?: string;
  onSelect: () => void;
  selected: boolean;
  onDelete: () => void;
  deleteLabel: string;
}) {
  return (
    <div
      className={clsx(
        "group flex items-center gap-2 h-7 px-2 rounded-md text-[11px] cursor-pointer transition-colors",
        selected ? "bg-accent/15 ring-1 ring-accent/40" : "bg-surface-2 hover:bg-surface-3",
      )}
      onClick={onSelect}
    >
      <span className="font-medium text-ink truncate">{title}</span>
      {badge && <span className="font-mono text-[10px] text-ink-dim">#{badge}</span>}
      <span className="flex-1" />
      <button
        onClick={(e) => { e.stopPropagation(); onDelete(); }}
        className="p-0.5 text-status-error/60 hover:text-status-error opacity-0 group-hover:opacity-100 transition-opacity"
        title={deleteLabel}
      >
        <Trash2 className="w-3 h-3" />
      </button>
    </div>
  );
}

function EmptyAdd({
  label, sub, onAdd,
}: {
  label: string;
  sub: string;
  onAdd?: () => void;
}) {
  return (
    <div className="flex items-center gap-3 px-3 py-3 rounded-md bg-surface-2 border border-dashed border-border-subtle">
      <div className="flex-1">
        <div className="text-[11px] text-ink">{label}</div>
        <div className="text-[10px] text-ink-dim">{sub}</div>
      </div>
      {onAdd && (
        <button
          onClick={onAdd}
          className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[11px] bg-accent/15 text-accent hover:bg-accent/25"
        >
          <Plus className="w-3 h-3" /> Add
        </button>
      )}
    </div>
  );
}

/* ─── Inline form for the selected node ─────────────────── */

function NodeForm({
  path, value, doc, onEdit, onDelete,
}: {
  path: string;
  value: unknown;
  doc?: unknown;
  onEdit: (path: string, value: unknown) => void;
  onDelete: (path: string) => void;
}) {
  if (value === undefined) {
    return null;
  }
  return (
    <div className="rounded-lg border border-border-subtle bg-surface-2 p-3">
      <div className="text-[10px] uppercase tracking-wider text-ink-dim font-mono mb-2">
        {path}
      </div>
      <EditableConfig
        value={value}
        basePath={path}
        doc={doc}
        onEdit={onEdit}
        onDelete={onDelete}
      />
    </div>
  );
}

/* ─── Path utilities ────────────────────────────────────── */

/** Strip the `widgets.` prefix so we can index into the raw block. */
function stripBase(path: string, basePath: string): string {
  if (path === basePath) return "";
  const prefix = `${basePath}.`;
  return path.startsWith(prefix) ? path.slice(prefix.length) : path;
}

/** Read a value from `obj` using a dotted path (numbers index arrays). */
function readPath(obj: unknown, path: string): unknown {
  if (!path) return obj;
  let cur: unknown = obj;
  for (const seg of path.split(".")) {
    if (cur == null || typeof cur !== "object") return undefined;
    if (Array.isArray(cur)) {
      const n = Number(seg);
      if (!Number.isInteger(n)) return undefined;
      cur = cur[n];
    } else {
      cur = (cur as Record<string, unknown>)[seg];
    }
  }
  return cur;
}
