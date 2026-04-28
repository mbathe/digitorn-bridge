import { useEffect, useState } from "react";
import {
  Search, Sun, Moon, Maximize2, RotateCcw, Download, Box,
  ArrowRight, ArrowDown, Layers, Rows3, Workflow,
} from "lucide-react";
import clsx from "clsx";
import type { LayoutDir } from "../lib/auto-layout";
import type { Theme } from "../lib/useTheme";

export type LayoutMode = "lanes" | "auto";

interface Props {
  appName: string;
  theme: Theme;
  onToggleTheme: () => void;
  layoutDir: LayoutDir;
  onLayoutDir: (d: LayoutDir) => void;
  layoutMode: LayoutMode;
  onLayoutMode: (m: LayoutMode) => void;
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
