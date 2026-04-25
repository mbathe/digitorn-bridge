import { memo } from "react";
import { Handle, Position } from "reactflow";
import type { NodeData, NodeKind } from "../lib/yaml-to-graph";

/* ------------------------------------------------------------------ */
/*  Icon SVGs (inline, no deps)                                        */
/* ------------------------------------------------------------------ */

const ICONS: Record<string, string> = {
  user: "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 3a4 4 0 110 8 4 4 0 010-8z",
  cube: "M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z",
  brain: "M9.5 2A5.5 5.5 0 004 7.5c0 1.58.67 3 1.74 4.01A3.5 3.5 0 004 14.5 3.5 3.5 0 007.5 18H8v2h8v-2h.5a3.5 3.5 0 003.5-3.5c0-1.25-.66-2.35-1.65-2.97A5.5 5.5 0 0014.5 2h-5z",
  folder: "M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z",
  terminal: "M4 17l6-6-6-6M12 19h8",
  database: "M12 2C6.48 2 2 4.02 2 6.5v11C2 19.98 6.48 22 12 22s10-2.02 10-4.5v-11C22 4.02 17.52 2 12 2z",
  globe: "M12 2a10 10 0 100 20 10 10 0 000-20zM2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10A15.3 15.3 0 0112 2z",
  code: "M16 18l6-6-6-6M8 6l-6 6 6 6",
  layout: "M19 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2zM3 9h18M9 21V9",
  eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8zM12 9a3 3 0 110 6 3 3 0 010-6z",
  users: "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2M23 21v-2a4 4 0 00-3-3.87M9 7a4 4 0 110 8 4 4 0 010-8zM16 3.13a4 4 0 010 7.75",
  cpu: "M18 4H6a2 2 0 00-2 2v12a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3",
  cloud: "M18 10h-1.26A8 8 0 109 20h9a5 5 0 000-10z",
  search: "M11 3a8 8 0 100 16 8 8 0 000-16zM21 21l-4.35-4.35",
  archive: "M21 8v13H3V8M1 3h22v5H1zM10 12h4",
  box: "M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z",
  zap: "M13 2L3 14h9l-1 10 10-12h-9l1-10z",
  clock: "M12 2a10 10 0 100 20 10 10 0 000-20zM12 6v6l4 2",
  send: "M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  alert: "M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z",
  "arrow-down": "M12 5v14M19 12l-7 7-7-7",
  "arrow-up": "M12 19V5M5 12l7-7 7 7",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z",
};

function Icon({ name, size = 16, color }: { name: string; size?: number; color: string }) {
  const path = ICONS[name] ?? ICONS.box;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={path} />
    </svg>
  );
}

/* ------------------------------------------------------------------ */
/*  Kind-specific styling                                              */
/* ------------------------------------------------------------------ */

const KIND_STYLES: Record<NodeKind, { width: number; minHeight: number; borderRadius: number }> = {
  user: { width: 180, minHeight: 64, borderRadius: 24 },
  input: { width: 180, minHeight: 64, borderRadius: 24 },
  output: { width: 180, minHeight: 64, borderRadius: 24 },
  app: { width: 220, minHeight: 80, borderRadius: 16 },
  agent: { width: 220, minHeight: 72, borderRadius: 12 },
  module: { width: 200, minHeight: 64, borderRadius: 10 },
  trigger: { width: 200, minHeight: 64, borderRadius: 10 },
  channel: { width: 200, minHeight: 64, borderRadius: 10 },
  hook: { width: 200, minHeight: 64, borderRadius: 10 },
  variable: { width: 180, minHeight: 48, borderRadius: 8 },
  error: { width: 280, minHeight: 72, borderRadius: 12 },
};

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

interface Props {
  data: NodeData;
  selected?: boolean;
}

function CustomNodeInner({ data, selected }: Props) {
  const { kind, label, subtitle, icon, color } = data;
  const ks = KIND_STYLES[kind];

  return (
    <div
      style={{
        width: ks.width,
        minHeight: ks.minHeight,
        borderRadius: ks.borderRadius,
        background: "linear-gradient(180deg, #151d2e 0%, #0b1120 100%)",
        border: `1.5px solid ${selected ? color : `${color}55`}`,
        boxShadow: selected
          ? `0 0 20px ${color}33, 0 4px 12px rgba(0,0,0,0.4)`
          : `0 2px 8px rgba(0,0,0,0.3)`,
        padding: "12px 14px",
        display: "flex",
        alignItems: "center",
        gap: 10,
        cursor: "pointer",
        transition: "border-color 150ms, box-shadow 150ms",
        position: "relative",
      }}
    >
      <Handle
        type="target"
        position={Position.Top}
        style={{ background: color, border: "none", width: 8, height: 8 }}
      />
      <Handle
        type="source"
        position={Position.Bottom}
        style={{ background: color, border: "none", width: 8, height: 8 }}
      />

      {/* icon circle */}
      <div
        style={{
          width: kind === "variable" ? 28 : 36,
          height: kind === "variable" ? 28 : 36,
          borderRadius: "50%",
          background: `${color}22`,
          border: `1px solid ${color}44`,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        {icon ? (
          <Icon name={icon} size={kind === "variable" ? 14 : 18} color={color} />
        ) : (
          <span style={{ fontSize: kind === "variable" ? 12 : 16 }}>{kindEmoji(kind)}</span>
        )}
      </div>

      {/* text */}
      <div style={{ minWidth: 0, flex: 1 }}>
        <div
          style={{
            fontSize: kind === "app" ? 14 : kind === "variable" ? 11 : 12,
            fontWeight: 600,
            color: "#e2e8f0",
            letterSpacing: 0.2,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {label}
        </div>
        {subtitle && (
          <div
            style={{
              fontSize: kind === "variable" ? 9 : 10,
              color: "#94a3b8",
              marginTop: 2,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              fontFamily: kind === "agent" || kind === "variable" ? "monospace" : "inherit",
            }}
          >
            {subtitle}
          </div>
        )}
      </div>

      {/* actions count badge for modules */}
      {kind === "module" && data.actionsCount !== undefined && data.actionsCount > 0 && (
        <div
          style={{
            background: `${color}33`,
            color,
            fontSize: 10,
            fontWeight: 700,
            borderRadius: 6,
            padding: "2px 6px",
            flexShrink: 0,
          }}
        >
          {data.actionsCount}
        </div>
      )}
    </div>
  );
}

function kindEmoji(kind: NodeKind): string {
  switch (kind) {
    case "user": return "U";
    case "input": return "I";
    case "output": return "O";
    case "app": return "A";
    case "agent": return "B";
    case "module": return "M";
    case "trigger": return "T";
    case "channel": return "C";
    case "hook": return "H";
    case "variable": return "V";
    case "error": return "!";
  }
}

export default memo(CustomNodeInner);
