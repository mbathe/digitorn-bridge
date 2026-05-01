/**
 * Right-click context menu on a canvas node.
 *
 * Actions:
 *  - Inspect       — open the inspector on this node
 *  - Center        — pan/zoom the viewport to focus on this node
 *  - Duplicate     — clone the node's YAML sub-tree (next sibling)
 *  - Copy YAML path — clipboard the dotted path (e.g. agents.0.brain)
 *  - Delete        — remove the node from YAML (and ripple references)
 *
 * Dispatched from App.tsx via ReactFlow's `onNodeContextMenu`.
 */
import { useEffect, useRef } from "react";
import { Eye, Crosshair, Copy, Trash2, Check } from "lucide-react";
import clsx from "clsx";

interface Props {
  /** Position in viewport pixels (from the right-click event). */
  x: number;
  y: number;
  nodeId: string;
  yamlPath: string | null;
  /** Whether deleting this node is supported (delete is risky for
   *  parser-emitted singletons like app-root, capabilities, behavior). */
  canDelete: boolean;
  /** Whether duplicating is supported (only array members and map
   *  entries — not singletons). */
  canDuplicate: boolean;
  onInspect: () => void;
  onCenter: () => void;
  onDuplicate: () => void;
  onCopyPath: () => void;
  onDelete: () => void;
  onClose: () => void;
}

export default function NodeContextMenu({
  x, y, nodeId, yamlPath, canDelete, canDuplicate,
  onInspect, onCenter, onDuplicate, onCopyPath, onDelete, onClose,
}: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("mousedown", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // Keep the menu fully on-screen
  const maxX = window.innerWidth - 220;
  const maxY = window.innerHeight - 240;
  const px = Math.min(x, maxX);
  const py = Math.min(y, maxY);

  return (
    <div
      ref={wrapRef}
      style={{ left: px, top: py }}
      className="fixed z-50 w-[210px] rounded-lg bg-surface-1 border border-border shadow-2xl py-1 text-xs"
    >
      <div className="px-3 py-1.5 border-b border-border-subtle text-[10px] uppercase tracking-wider text-ink-dim font-mono truncate">
        {nodeId}
      </div>
      <Item icon={Eye} label="Inspect" hint="Enter" onClick={() => { onInspect(); onClose(); }} />
      <Item icon={Crosshair} label="Center on canvas" hint="C" onClick={() => { onCenter(); onClose(); }} />
      <Item
        icon={Copy}
        label="Duplicate"
        hint="⌘D"
        disabled={!canDuplicate}
        onClick={() => { onDuplicate(); onClose(); }}
      />
      <Item
        icon={Check}
        label={yamlPath ? "Copy YAML path" : "(no YAML path)"}
        disabled={!yamlPath}
        onClick={() => { onCopyPath(); onClose(); }}
      />
      <div className="my-1 mx-2 border-t border-border-subtle" />
      <Item
        icon={Trash2}
        label="Delete"
        hint="⌫"
        danger
        disabled={!canDelete}
        onClick={() => { onDelete(); onClose(); }}
      />
    </div>
  );
}

function Item({
  icon: Icon, label, hint, danger, disabled, onClick,
}: {
  icon: typeof Eye; label: string; hint?: string; danger?: boolean; disabled?: boolean; onClick: () => void;
}) {
  return (
    <button
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      className={clsx(
        "w-full flex items-center gap-2 px-3 h-7 text-left",
        disabled
          ? "text-ink-dim/40 cursor-not-allowed"
          : danger
            ? "text-status-error hover:bg-status-error/10"
            : "text-ink-muted hover:text-ink hover:bg-surface-2",
      )}
    >
      <Icon className="w-3 h-3 flex-shrink-0" />
      <span className="flex-1">{label}</span>
      {hint && <span className="text-[9px] font-mono text-ink-dim">{hint}</span>}
    </button>
  );
}
