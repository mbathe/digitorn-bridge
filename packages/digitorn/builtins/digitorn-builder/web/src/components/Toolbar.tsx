import { useEffect, useRef, useState } from "react";
import {
  Search, Sun, Moon, Maximize2, RotateCcw, Download, Box,
  ArrowRight, ArrowDown, Layers, Rows3, Workflow, Eye, Play, BookOpen,
  LayoutGrid, List, Square, Sliders,
} from "lucide-react";
import clsx from "clsx";
import type { LayoutDir } from "../lib/auto-layout";
import type { Theme } from "../lib/useTheme";
import { VIEW_MODES, type ViewMode } from "../lib/view-modes";

export type LayoutMode = "lanes" | "auto";
export type DensityMode = "comfortable" | "compact" | "list";

interface Props {
  appName: string;
  theme: Theme;
  onToggleTheme: () => void;
  layoutDir: LayoutDir;
  onLayoutDir: (d: LayoutDir) => void;
  layoutMode: LayoutMode;
  onLayoutMode: (m: LayoutMode) => void;
  viewMode: ViewMode;
  onViewMode: (v: ViewMode) => void;
  beginnerMode: boolean;
  onBeginnerMode: (b: boolean) => void;
  density: DensityMode;
  onDensity: (d: DensityMode) => void;
  /** View-options popover toggles. Live under the gear icon so the
   *  toolbar stays clean while still exposing low-frequency knobs. */
  showFallbackBrains: boolean;
  onShowFallbackBrains: (b: boolean) => void;
  onPlayStory: () => void;
  onFit: () => void;
  onResetLayout: () => void;
  onExport: () => void;
  searchQuery: string;
  onSearch: (q: string) => void;
  searchHits: number;
  rightSlot?: React.ReactNode;
}

export default function Toolbar({
  appName,
  theme,
  onToggleTheme,
  layoutDir,
  onLayoutDir,
  layoutMode,
  onLayoutMode,
  viewMode,
  onViewMode,
  beginnerMode,
  onBeginnerMode,
  density,
  onDensity,
  showFallbackBrains,
  onShowFallbackBrains,
  onPlayStory,
  onFit,
  onResetLayout,
  onExport,
  searchQuery,
  onSearch,
  searchHits,
  rightSlot,
}: Props) {
  const [searchOpen, setSearchOpen] = useState(false);

  // Cmd+F / Ctrl+F to focus search
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea") return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        setSearchOpen(true);
        setTimeout(() => {
          const el = document.getElementById("toolbar-search");
          el?.focus();
        }, 0);
      }
      if (e.key === "Escape" && searchOpen) {
        setSearchOpen(false);
        onSearch("");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searchOpen, onSearch]);

  return (
    <header className="flex items-center gap-3 px-4 h-12 border-b border-border-subtle bg-surface-1/80 backdrop-blur-md flex-shrink-0">
      {/* App identity */}
      <div className="flex items-center gap-2 min-w-0">
        <div className="w-7 h-7 rounded-lg bg-accent/15 flex items-center justify-center text-accent">
          <Box className="w-4 h-4" />
        </div>
        <span className="text-sm font-semibold text-ink truncate max-w-[260px]">
          {appName}
        </span>
      </div>

      <div className="flex-1" />

      {/* Search */}
      {searchOpen ? (
        <div className="flex items-center gap-2 px-2.5 h-8 rounded-lg bg-surface-2 border border-border min-w-[260px]">
          <Search className="w-3.5 h-3.5 text-ink-muted" />
          <input
            id="toolbar-search"
            type="text"
            value={searchQuery}
            onChange={(e) => onSearch(e.target.value)}
            placeholder="Search nodes..."
            className="flex-1 bg-transparent outline-none text-xs text-ink placeholder:text-ink-dim"
            autoFocus
          />
          {searchQuery && (
            <span className="text-[10px] text-ink-dim font-mono">
              {searchHits} match{searchHits !== 1 ? "es" : ""}
            </span>
          )}
          <button
            onClick={() => {
              setSearchOpen(false);
              onSearch("");
            }}
            className="text-ink-dim hover:text-ink text-xs"
            title="Close (Esc)"
          >
            ×
          </button>
        </div>
      ) : (
        <ToolBtn icon={Search} label="Search" hint="⌘F" onClick={() => setSearchOpen(true)} />
      )}

      {/* Layout mode (lifecycle lanes vs free dagre) */}
      <div className="flex items-center gap-0.5 p-0.5 bg-surface-2 rounded-lg border border-border-subtle">
        <ToolBtn
          compact
          active={layoutMode === "lanes"}
          icon={Rows3}
          onClick={() => onLayoutMode("lanes")}
          label="Lifecycle lanes"
        />
        <ToolBtn
          compact
          active={layoutMode === "auto"}
          icon={Workflow}
          onClick={() => onLayoutMode("auto")}
          label="Free graph (dagre)"
        />
      </div>

      {/* View mode dropdown */}
      <div className="relative">
        <select
          value={viewMode}
          onChange={(e) => onViewMode(e.target.value as ViewMode)}
          title={VIEW_MODES.find((v) => v.id === viewMode)?.hint}
          className="h-8 pl-7 pr-2 rounded-lg bg-surface-2 border border-border-subtle text-xs text-ink-muted hover:text-ink hover:bg-surface-3 appearance-none cursor-pointer"
        >
          {VIEW_MODES.map((v) => (
            <option key={v.id} value={v.id}>{v.label}</option>
          ))}
        </select>
        <Eye className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-muted pointer-events-none" />
      </div>

      {/* Density toggle — comfortable / compact / list */}
      <div className="flex items-center gap-0.5 p-0.5 bg-surface-2 rounded-lg border border-border-subtle" title="Card density">
        <ToolBtn
          compact
          active={density === "comfortable"}
          icon={Square}
          onClick={() => onDensity("comfortable")}
          label="Comfortable cards"
        />
        <ToolBtn
          compact
          active={density === "compact"}
          icon={LayoutGrid}
          onClick={() => onDensity("compact")}
          label="Compact (smaller cards)"
        />
        <ToolBtn
          compact
          active={density === "list"}
          icon={List}
          onClick={() => onDensity("list")}
          label="List (single-row entries)"
        />
      </div>

      {/* Beginner mode toggle */}
      <ToolBtn
        active={beginnerMode}
        icon={BookOpen}
        label={beginnerMode ? "Plain language" : "Plain language"}
        onClick={() => onBeginnerMode(!beginnerMode)}
      />

      {/* Play story (only in runtime mode) */}
      {viewMode === "runtime" && (
        <ToolBtn icon={Play} label="Play story" onClick={onPlayStory} />
      )}

      {/* Layout direction (only relevant in dagre mode) */}
      {layoutMode === "auto" && (
        <div className="flex items-center gap-0.5 p-0.5 bg-surface-2 rounded-lg border border-border-subtle">
          <ToolBtn
            compact
            active={layoutDir === "LR"}
            icon={ArrowRight}
            onClick={() => onLayoutDir("LR")}
            label="Horizontal layout"
          />
          <ToolBtn
            compact
            active={layoutDir === "TB"}
            icon={ArrowDown}
            onClick={() => onLayoutDir("TB")}
            label="Vertical layout"
          />
        </div>
      )}

      <ToolBtn icon={Layers} label="Re-layout" onClick={onResetLayout} hint="⌘L" />
      <ToolBtn icon={Maximize2} label="Fit view" onClick={onFit} hint="⌘0" />
      <ToolBtn icon={Download} label="Export PNG" onClick={onExport} />

      <div className="w-px h-6 bg-border-subtle mx-1" />

      {/* View settings popover (gear icon) — low-frequency canvas toggles
          live here so the toolbar stays scannable. */}
      <SettingsPopover
        showFallbackBrains={showFallbackBrains}
        onShowFallbackBrains={onShowFallbackBrains}
      />

      {/* Theme toggle */}
      <ToolBtn
        icon={theme === "dark" ? Sun : Moon}
        label={theme === "dark" ? "Light mode" : "Dark mode"}
        onClick={onToggleTheme}
      />

      {rightSlot}
    </header>
  );
}

function SettingsPopover({
  showFallbackBrains,
  onShowFallbackBrains,
}: {
  showFallbackBrains: boolean;
  onShowFallbackBrains: (b: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <ToolBtn
        compact
        icon={Sliders}
        label="View settings"
        active={open}
        onClick={() => setOpen((v) => !v)}
      />
      {open && (
        <div className="absolute right-0 top-9 z-50 w-72 rounded-lg border border-border-subtle bg-surface-1 shadow-xl p-3 space-y-3">
          <div className="text-[10px] uppercase tracking-wider text-ink-dim font-semibold">
            Canvas display
          </div>
          <SettingRow
            label="Show fallback brains"
            hint="Synthetic '↩ on 402' nodes next to each agent. Off by default — they clutter the canvas; the agent card already shows a fallback chip."
            value={showFallbackBrains}
            onChange={onShowFallbackBrains}
          />
        </div>
      )}
    </div>
  );
}

function SettingRow({
  label, hint, value, onChange,
}: {
  label: string;
  hint?: string;
  value: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-start gap-2.5 cursor-pointer group">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 w-3.5 h-3.5 rounded border-border-subtle bg-surface-2 text-accent focus:ring-1 focus:ring-accent/40"
      />
      <div className="flex-1 min-w-0">
        <div className="text-xs font-medium text-ink group-hover:text-accent transition-colors">{label}</div>
        {hint && <div className="text-[10px] text-ink-dim mt-0.5 leading-relaxed">{hint}</div>}
      </div>
    </label>
  );
}

interface ToolBtnProps {
  icon: typeof Search;
  label: string;
  hint?: string;
  active?: boolean;
  compact?: boolean;
  onClick?: () => void;
}

function ToolBtn({ icon: Icon, label, hint, active, compact, onClick }: ToolBtnProps) {
  return (
    <button
      onClick={onClick}
      title={hint ? `${label} (${hint})` : label}
      className={clsx(
        "h-8 inline-flex items-center justify-center gap-1.5 rounded-lg transition-colors",
        compact ? "w-8" : "px-2.5",
        active
          ? "bg-accent/15 text-accent"
          : "text-ink-muted hover:text-ink hover:bg-surface-2",
      )}
    >
      <Icon className="w-3.5 h-3.5" />
      {!compact && <span className="text-xs">{label}</span>}
    </button>
  );
}

export function RotateLayout({ onClick }: { onClick: () => void }) {
  return <ToolBtn icon={RotateCcw} label="Reset positions" onClick={onClick} />;
}
