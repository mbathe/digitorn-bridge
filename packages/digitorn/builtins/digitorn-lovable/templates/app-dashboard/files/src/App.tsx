import React, { useState } from "react";

const C = {
  bg: "#0B0E13",
  surface: "#11151B",
  surfaceAlt: "#161B23",
  surfaceHover: "#1B2129",
  border: "rgba(255,255,255,0.06)",
  borderSoft: "rgba(255,255,255,0.04)",
  text: "#F1F3F6",
  textMuted: "#9099A8",
  textDim: "#5D6573",
  accent: "#22D3EE",
  accentSoft: "rgba(34,211,238,0.15)",
  green: "#34D399",
  red: "#F87171",
  amber: "#FBBF24",
  violet: "#A78BFA",
} as const;

const PAGE = {
  display: "grid",
  gridTemplateColumns: "240px 1fr",
  minHeight: "100vh",
  background: C.bg,
  color: C.text,
  fontFamily:
    "'IBM Plex Sans', system-ui, -apple-system, sans-serif",
  WebkitFontSmoothing: "antialiased",
  letterSpacing: "-0.005em",
} as const;

// ── Sidebar ───────────────────────────────────────────────────────────

const SIDEBAR = {
  borderRight: `1px solid ${C.border}`,
  padding: "20px 16px",
  display: "flex",
  flexDirection: "column" as const,
  gap: 4,
  background: C.surface,
};

const SIDE_LOGO = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "0 8px 20px",
  marginBottom: 8,
  borderBottom: `1px solid ${C.borderSoft}`,
} as const;

const SIDE_LOGO_DOT = {
  width: 26,
  height: 26,
  borderRadius: 8,
  background: `linear-gradient(135deg, ${C.accent}, ${C.violet})`,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 13,
  fontWeight: 700,
  color: C.bg,
  letterSpacing: "-0.02em",
} as const;

const SIDE_LOGO_TEXT = { fontSize: 15, fontWeight: 600, letterSpacing: "-0.02em" } as const;
const SIDE_LOGO_SUB = { fontSize: 11, color: C.textDim, marginTop: 1 } as const;

const SIDE_GROUP = { marginTop: 16 } as const;
const SIDE_GROUP_LABEL = {
  fontSize: 10.5,
  letterSpacing: "0.10em",
  textTransform: "uppercase" as const,
  color: C.textDim,
  fontWeight: 500,
  padding: "6px 8px",
} as const;

const SIDE_ITEM = (active: boolean): React.CSSProperties => ({
  display: "flex",
  alignItems: "center",
  gap: 11,
  padding: "8px 10px",
  borderRadius: 8,
  fontSize: 13.5,
  color: active ? C.text : C.textMuted,
  background: active ? C.surfaceAlt : "transparent",
  fontWeight: active ? 500 : 400,
  cursor: "pointer",
  position: "relative" as const,
});

const SIDE_ICON = (active: boolean): React.CSSProperties => ({
  width: 16,
  height: 16,
  borderRadius: 4,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 13,
  color: active ? C.accent : C.textDim,
});

const SIDE_BADGE = {
  marginLeft: "auto",
  fontSize: 10.5,
  fontWeight: 500,
  padding: "2px 6px",
  borderRadius: 999,
  background: C.accentSoft,
  color: C.accent,
} as const;

const SIDE_PROFILE = {
  marginTop: "auto",
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "10px 8px",
  borderTop: `1px solid ${C.borderSoft}`,
} as const;
const SIDE_AVATAR = {
  width: 30,
  height: 30,
  borderRadius: 999,
  background: "linear-gradient(135deg, #22D3EE, #A78BFA)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  fontWeight: 600,
  color: C.bg,
} as const;
const SIDE_PROFILE_NAME = { fontSize: 13, fontWeight: 500 } as const;
const SIDE_PROFILE_PLAN = { fontSize: 11, color: C.textDim } as const;

// ── Topbar ────────────────────────────────────────────────────────────

const MAIN = { display: "flex", flexDirection: "column" as const, minHeight: "100vh" } as const;

const TOPBAR = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "16px 32px",
  borderBottom: `1px solid ${C.border}`,
  background: C.surface,
} as const;

const SEARCH = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "8px 14px",
  background: C.surfaceAlt,
  border: `1px solid ${C.border}`,
  borderRadius: 8,
  width: 360,
  fontSize: 13,
  color: C.textMuted,
} as const;

const KEY_BADGE = {
  marginLeft: "auto",
  fontSize: 10.5,
  fontFamily: "ui-monospace, monospace",
  padding: "2px 6px",
  borderRadius: 4,
  background: C.bg,
  border: `1px solid ${C.border}`,
  color: C.textDim,
} as const;

const TOP_RIGHT = { display: "flex", alignItems: "center", gap: 14 } as const;

const NOTIF_BTN = {
  position: "relative" as const,
  width: 34,
  height: 34,
  borderRadius: 8,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  background: C.surfaceAlt,
  border: `1px solid ${C.border}`,
  fontSize: 14,
  color: C.textMuted,
} as const;
const NOTIF_DOT = {
  position: "absolute" as const,
  top: 6,
  right: 6,
  width: 7,
  height: 7,
  borderRadius: 999,
  background: C.red,
  border: `2px solid ${C.surface}`,
} as const;

const NEW_BTN = {
  background: C.text,
  color: C.bg,
  fontSize: 13,
  fontWeight: 500,
  padding: "8px 14px",
  borderRadius: 8,
  border: "none",
  display: "inline-flex",
  alignItems: "center",
  gap: 6,
  cursor: "pointer",
} as const;

// ── Main content ──────────────────────────────────────────────────────

const MAIN_PAD = { padding: "32px 32px 56px", flex: 1 } as const;

const HEADER_ROW = { display: "flex", alignItems: "flex-end", justifyContent: "space-between", marginBottom: 28 } as const;

const PAGE_TITLE = { fontSize: 26, fontWeight: 600, letterSpacing: "-0.025em", marginBottom: 4 } as const;
const PAGE_SUB = { fontSize: 13.5, color: C.textMuted } as const;

const RANGE_TABS = { display: "flex", gap: 4, padding: 4, background: C.surfaceAlt, border: `1px solid ${C.border}`, borderRadius: 10 } as const;
const RANGE_TAB = (active: boolean): React.CSSProperties => ({
  fontSize: 12.5,
  fontWeight: 500,
  padding: "6px 12px",
  borderRadius: 7,
  cursor: "pointer",
  background: active ? C.bg : "transparent",
  color: active ? C.text : C.textMuted,
  border: active ? `1px solid ${C.border}` : "1px solid transparent",
});

// ── KPI cards ─────────────────────────────────────────────────────────

const KPI_GRID = { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 14, marginBottom: 24 } as const;
const KPI = {
  padding: 18,
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
  display: "flex",
  flexDirection: "column" as const,
  gap: 10,
} as const;
const KPI_HEAD = { display: "flex", alignItems: "center", justifyContent: "space-between" } as const;
const KPI_LABEL = { fontSize: 12, color: C.textMuted, fontWeight: 500 } as const;
const KPI_PCT = (positive: boolean): React.CSSProperties => ({
  fontSize: 11.5,
  fontWeight: 500,
  padding: "2px 7px",
  borderRadius: 999,
  background: positive ? "rgba(52,211,153,0.14)" : "rgba(248,113,113,0.14)",
  color: positive ? C.green : C.red,
  display: "inline-flex",
  alignItems: "center",
  gap: 3,
});
const KPI_VAL = { fontSize: 28, fontWeight: 600, letterSpacing: "-0.025em", lineHeight: 1 } as const;
const KPI_SUB = { fontSize: 11.5, color: C.textDim } as const;
const KPI_SPARK = { height: 36, marginTop: 2 } as const;

function Sparkline({ color, points }: { color: string; points: number[] }) {
  const w = 200;
  const h = 36;
  const max = Math.max(...points);
  const min = Math.min(...points);
  const span = max - min || 1;
  const pts = points
    .map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p - min) / span) * h * 0.92 - 2}`)
    .join(" ");
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={KPI_SPARK}>
      <defs>
        <linearGradient id={`sg-${color}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.25" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <polyline points={`0,${h} ${pts} ${w},${h}`} fill={`url(#sg-${color})`} stroke="none" />
      <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── Main grid (chart + side cards) ────────────────────────────────────

const GRID_2 = { display: "grid", gridTemplateColumns: "1.7fr 1fr", gap: 14, marginBottom: 24 } as const;

const PANEL = {
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
  overflow: "hidden",
} as const;

const PANEL_HEAD = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "16px 18px",
  borderBottom: `1px solid ${C.borderSoft}`,
} as const;
const PANEL_TITLE = { fontSize: 14, fontWeight: 600, letterSpacing: "-0.015em" } as const;
const PANEL_SUB = { fontSize: 11.5, color: C.textDim, marginTop: 1 } as const;
const PANEL_BODY = { padding: 18 } as const;

const LEGEND = { display: "flex", gap: 14, fontSize: 11.5, color: C.textMuted } as const;
const LEGEND_DOT = (color: string): React.CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: 2,
  background: color,
  display: "inline-block",
  marginRight: 6,
});

// Activity feed
const ACTIVITY = { display: "flex", flexDirection: "column" as const } as const;
const ACT_ITEM = {
  display: "flex",
  alignItems: "flex-start",
  gap: 12,
  padding: "12px 18px",
  borderBottom: `1px solid ${C.borderSoft}`,
} as const;
const ACT_AVATAR = (color: string): React.CSSProperties => ({
  width: 30,
  height: 30,
  borderRadius: 999,
  background: color,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  fontWeight: 600,
  color: C.bg,
  flex: "0 0 auto",
});
const ACT_TEXT = { fontSize: 13, lineHeight: 1.45, flex: 1 } as const;
const ACT_TIME = { fontSize: 11, color: C.textDim, marginTop: 3 } as const;
const ACT_BOLD = { color: C.text, fontWeight: 500 } as const;

// Customers table
const TABLE = { width: "100%", borderCollapse: "collapse" as const, fontSize: 13 } as const;
const TH = {
  textAlign: "left" as const,
  padding: "12px 18px",
  borderBottom: `1px solid ${C.borderSoft}`,
  fontSize: 11,
  fontWeight: 500,
  letterSpacing: "0.06em",
  textTransform: "uppercase" as const,
  color: C.textDim,
};
const TD = { padding: "12px 18px", borderBottom: `1px solid ${C.borderSoft}` } as const;
const TD_LAST = { padding: "12px 18px" } as const;

const ACCT_CELL = { display: "flex", alignItems: "center", gap: 10 } as const;
const ACCT_LOGO = (color: string): React.CSSProperties => ({
  width: 28,
  height: 28,
  borderRadius: 6,
  background: color,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  fontWeight: 700,
  color: C.bg,
});

const PLAN_PILL = (variant: "ent" | "pro" | "starter"): React.CSSProperties => ({
  fontSize: 11.5,
  fontWeight: 500,
  padding: "3px 9px",
  borderRadius: 999,
  background:
    variant === "ent"
      ? "rgba(167,139,250,0.16)"
      : variant === "pro"
      ? "rgba(34,211,238,0.14)"
      : "rgba(255,255,255,0.06)",
  color:
    variant === "ent"
      ? C.violet
      : variant === "pro"
      ? C.accent
      : C.textMuted,
});

const STATUS_PILL = (ok: boolean): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  gap: 5,
  fontSize: 11.5,
  color: ok ? C.green : C.amber,
});
const STATUS_DOT = (ok: boolean): React.CSSProperties => ({
  width: 6,
  height: 6,
  borderRadius: 999,
  background: ok ? C.green : C.amber,
});

const RANGE_OPTIONS = ["24h", "7d", "30d", "90d", "YTD"] as const;
const NAV_OPTIONS = [
  { key: "overview", label: "Overview", icon: "◆" },
  { key: "customers", label: "Customers", icon: "◇" },
  { key: "revenue", label: "Revenue", icon: "↗" },
  { key: "pipeline", label: "Pipeline", icon: "⚡", badge: 12 },
  { key: "reports", label: "Reports", icon: "⊕" },
] as const;

export function App() {
  const [range, setRange] = useState<string>("30d");
  const [activeNav, setActiveNav] = useState<string>("overview");
  const [search, setSearch] = useState<string>("");
  return (
    <main style={PAGE}>
      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside style={SIDEBAR}>
        <div style={SIDE_LOGO}>
          <span style={SIDE_LOGO_DOT}>L</span>
          <div>
            <div style={SIDE_LOGO_TEXT}>Lattice</div>
            <div style={SIDE_LOGO_SUB}>Acme Robotics</div>
          </div>
        </div>

        {NAV_OPTIONS.map((opt) => {
          const isActive = activeNav === opt.key;
          return (
            <div
              key={opt.key}
              style={SIDE_ITEM(isActive)}
              onClick={() => setActiveNav(opt.key)}
            >
              <span style={SIDE_ICON(isActive)}>{opt.icon}</span> {opt.label}
              {"badge" in opt && opt.badge ? <span style={SIDE_BADGE}>{opt.badge}</span> : null}
            </div>
          );
        })}

        <div style={SIDE_GROUP}>
          <div style={SIDE_GROUP_LABEL}>Workspace</div>
          <div style={SIDE_ITEM(false)}>
            <span style={SIDE_ICON(false)}>☷</span> Team
          </div>
          <div style={SIDE_ITEM(false)}>
            <span style={SIDE_ICON(false)}>⚙</span> Settings
          </div>
          <div style={SIDE_ITEM(false)}>
            <span style={SIDE_ICON(false)}>⊟</span> Billing
          </div>
        </div>

        <div style={SIDE_PROFILE}>
          <span style={SIDE_AVATAR}>CR</span>
          <div>
            <div style={SIDE_PROFILE_NAME}>Camille Reyes</div>
            <div style={SIDE_PROFILE_PLAN}>Pro plan</div>
          </div>
        </div>
      </aside>

      {/* ── Main ────────────────────────────────────────────────────── */}
      <div style={MAIN}>
        {/* Topbar */}
        <header style={TOPBAR}>
          <label style={SEARCH}>
            <span style={{ color: C.textDim, fontSize: 14 }}>⌕</span>
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search customers, invoices, reports…"
              style={{
                flex: 1,
                background: "transparent",
                border: "none",
                outline: "none",
                fontSize: 13,
                color: C.text,
                fontFamily: "inherit",
              }}
            />
            <span style={KEY_BADGE}>⌘K</span>
          </label>
          <div style={TOP_RIGHT}>
            <span style={{ fontSize: 12.5, color: C.textMuted }}>Live data · 2s ago</span>
            <button style={NOTIF_BTN}>
              ◔
              <span style={NOTIF_DOT} />
            </button>
            <button style={NEW_BTN}>
              <span aria-hidden>+</span> New invoice
            </button>
          </div>
        </header>

        <div style={MAIN_PAD}>
          {/* Page header */}
          <div style={HEADER_ROW}>
            <div>
              <div style={PAGE_TITLE}>
                {NAV_OPTIONS.find((o) => o.key === activeNav)?.label ?? "Overview"}
              </div>
              <div style={PAGE_SUB}>
                How Acme Robotics is performing — last {range}.
              </div>
            </div>
            <div style={RANGE_TABS}>
              {RANGE_OPTIONS.map((opt) => (
                <span
                  key={opt}
                  style={RANGE_TAB(range === opt)}
                  onClick={() => setRange(opt)}
                >
                  {opt}
                </span>
              ))}
            </div>
          </div>

          {/* KPIs */}
          <div style={KPI_GRID}>
            <div style={KPI}>
              <div style={KPI_HEAD}>
                <div style={KPI_LABEL}>Recurring revenue</div>
                <span style={KPI_PCT(true)}>↑ 12.4%</span>
              </div>
              <div style={KPI_VAL}>$48,210</div>
              <div style={KPI_SUB}>$5,328 vs. last 30 days</div>
              <Sparkline color={C.green} points={[12, 14, 13, 17, 16, 18, 22, 21, 24, 26, 25, 28]} />
            </div>
            <div style={KPI}>
              <div style={KPI_HEAD}>
                <div style={KPI_LABEL}>Active accounts</div>
                <span style={KPI_PCT(true)}>↑ 318</span>
              </div>
              <div style={KPI_VAL}>3,184</div>
              <div style={KPI_SUB}>Across 27 countries</div>
              <Sparkline color={C.accent} points={[20, 22, 21, 24, 26, 28, 27, 29, 31, 30, 33, 35]} />
            </div>
            <div style={KPI}>
              <div style={KPI_HEAD}>
                <div style={KPI_LABEL}>Churn rate</div>
                <span style={KPI_PCT(false)}>↑ 0.4 pp</span>
              </div>
              <div style={KPI_VAL}>2.1%</div>
              <div style={KPI_SUB}>Industry avg. 4.6%</div>
              <Sparkline color={C.amber} points={[18, 16, 17, 14, 15, 13, 12, 14, 11, 13, 14, 16]} />
            </div>
            <div style={KPI}>
              <div style={KPI_HEAD}>
                <div style={KPI_LABEL}>Net new MRR</div>
                <span style={KPI_PCT(true)}>↑ 22%</span>
              </div>
              <div style={KPI_VAL}>+$8,640</div>
              <div style={KPI_SUB}>86 net adds, 12 expansions</div>
              <Sparkline color={C.violet} points={[8, 10, 12, 11, 14, 16, 15, 18, 19, 22, 21, 24]} />
            </div>
          </div>

          {/* Chart + Activity */}
          <div style={GRID_2}>
            <div style={PANEL}>
              <div style={PANEL_HEAD}>
                <div>
                  <div style={PANEL_TITLE}>Revenue trend</div>
                  <div style={PANEL_SUB}>30 days · stacked by plan tier</div>
                </div>
                <div style={LEGEND}>
                  <span><span style={LEGEND_DOT(C.violet)} />Enterprise</span>
                  <span><span style={LEGEND_DOT(C.accent)} />Pro</span>
                  <span><span style={LEGEND_DOT(C.green)} />Starter</span>
                </div>
              </div>
              <div style={{ ...PANEL_BODY, height: 240 }}>
                <svg width="100%" height="100%" viewBox="0 0 600 220" preserveAspectRatio="none">
                  <defs>
                    <linearGradient id="rev-grad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={C.accent} stopOpacity="0.35" />
                      <stop offset="100%" stopColor={C.accent} stopOpacity="0" />
                    </linearGradient>
                  </defs>
                  {/* gridlines */}
                  {[40, 90, 140, 190].map((y) => (
                    <line key={y} x1="0" x2="600" y1={y} y2={y} stroke={C.borderSoft} strokeWidth="1" />
                  ))}
                  <path
                    d="M0,170 C40,160 80,140 120,135 S200,160 240,140 S320,80 380,90 S460,40 520,55 S600,30 600,30 L600,220 L0,220 Z"
                    fill="url(#rev-grad)"
                  />
                  <path
                    d="M0,170 C40,160 80,140 120,135 S200,160 240,140 S320,80 380,90 S460,40 520,55 S600,30 600,30"
                    fill="none"
                    stroke={C.accent}
                    strokeWidth="2"
                  />
                  <path
                    d="M0,180 C40,175 80,170 120,160 S200,175 240,165 S320,140 380,145 S460,120 520,125 S600,110 600,110"
                    fill="none"
                    stroke={C.violet}
                    strokeWidth="1.5"
                    strokeDasharray="4 4"
                    opacity="0.7"
                  />
                  {/* x-axis labels */}
                  {["Mar 1", "Mar 8", "Mar 15", "Mar 22", "Mar 29"].map((label, i) => (
                    <text
                      key={label}
                      x={20 + i * 140}
                      y="212"
                      fill={C.textDim}
                      fontSize="10"
                      fontFamily="ui-sans-serif"
                    >
                      {label}
                    </text>
                  ))}
                </svg>
              </div>
            </div>

            <div style={PANEL}>
              <div style={PANEL_HEAD}>
                <div>
                  <div style={PANEL_TITLE}>Recent activity</div>
                  <div style={PANEL_SUB}>Live · last 24h</div>
                </div>
                <span style={{ fontSize: 12, color: C.textMuted, cursor: "pointer" }}>View all →</span>
              </div>
              <div style={ACTIVITY}>
                <div style={ACT_ITEM}>
                  <span style={ACT_AVATAR("#A78BFA")}>GR</span>
                  <div style={ACT_TEXT}>
                    <span style={ACT_BOLD}>Globex Robotics</span> upgraded to <span style={ACT_BOLD}>Enterprise</span>
                    <div style={ACT_TIME}>14 min ago · $4,200 ARR</div>
                  </div>
                </div>
                <div style={ACT_ITEM}>
                  <span style={ACT_AVATAR("#22D3EE")}>IN</span>
                  <div style={ACT_TEXT}>
                    <span style={ACT_BOLD}>Initech</span> renewed for 12 months
                    <div style={ACT_TIME}>1h ago · $2,900 ARR</div>
                  </div>
                </div>
                <div style={ACT_ITEM}>
                  <span style={ACT_AVATAR("#34D399")}>HL</span>
                  <div style={ACT_TEXT}>
                    Invoice <span style={ACT_BOLD}>#10,482</span> sent to Hooli Labs
                    <div style={ACT_TIME}>2h ago · pending</div>
                  </div>
                </div>
                <div style={ACT_ITEM}>
                  <span style={ACT_AVATAR("#FBBF24")}>PE</span>
                  <div style={ACT_TEXT}>
                    <span style={ACT_BOLD}>Pied Piper</span> downgraded to Pro
                    <div style={ACT_TIME}>4h ago · -$640 MRR</div>
                  </div>
                </div>
                <div style={{ ...ACT_ITEM, borderBottom: "none" }}>
                  <span style={ACT_AVATAR("#F472B6")}>SO</span>
                  <div style={ACT_TEXT}>
                    <span style={ACT_BOLD}>Soylent Green</span> joined Starter
                    <div style={ACT_TIME}>5h ago · $0 trial</div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Customers table */}
          <div style={PANEL}>
            <div style={PANEL_HEAD}>
              <div>
                <div style={PANEL_TITLE}>Top customers</div>
                <div style={PANEL_SUB}>By recurring revenue · this month</div>
              </div>
              <span style={{ fontSize: 12, color: C.textMuted, cursor: "pointer" }}>Export CSV</span>
            </div>
            <table style={TABLE}>
              <thead>
                <tr>
                  <th style={TH}>Account</th>
                  <th style={TH}>Plan</th>
                  <th style={TH}>MRR</th>
                  <th style={TH}>Owner</th>
                  <th style={TH}>Status</th>
                  <th style={{ ...TH, textAlign: "right" }}>Last activity</th>
                </tr>
              </thead>
              <tbody>
                {[
                  { logo: "G", color: "#A78BFA", name: "Globex Robotics", plan: "ent", mrr: "$8,400", owner: "Eli", status: true, last: "12 min ago" },
                  { logo: "I", color: "#22D3EE", name: "Initech", plan: "pro", mrr: "$2,900", owner: "Camille", status: true, last: "1h ago" },
                  { logo: "H", color: "#34D399", name: "Hooli Labs", plan: "pro", mrr: "$1,800", owner: "Naomi", status: false, last: "3h ago" },
                  { logo: "P", color: "#FBBF24", name: "Pied Piper", plan: "pro", mrr: "$1,420", owner: "Eli", status: true, last: "8h ago" },
                  { logo: "M", color: "#F472B6", name: "Massive Dynamic", plan: "ent", mrr: "$5,200", owner: "Camille", status: true, last: "1d ago" },
                  { logo: "S", color: "#FB923C", name: "Stark Industries", plan: "starter", mrr: "$240", owner: "—", status: true, last: "2d ago" },
                ].map((row, i) => (
                  <tr key={row.name}>
                    <td style={TD}>
                      <div style={ACCT_CELL}>
                        <span style={ACCT_LOGO(row.color)}>{row.logo}</span>
                        <span style={{ fontWeight: 500 }}>{row.name}</span>
                      </div>
                    </td>
                    <td style={TD}>
                      <span style={PLAN_PILL(row.plan as "ent" | "pro" | "starter")}>
                        {row.plan === "ent" ? "Enterprise" : row.plan === "pro" ? "Pro" : "Starter"}
                      </span>
                    </td>
                    <td style={{ ...TD, fontWeight: 500 }}>{row.mrr}</td>
                    <td style={{ ...TD, color: C.textMuted }}>{row.owner}</td>
                    <td style={TD}>
                      <span style={STATUS_PILL(row.status)}>
                        <span style={STATUS_DOT(row.status)} />
                        {row.status ? "Healthy" : "At risk"}
                      </span>
                    </td>
                    <td style={{ ...TD, textAlign: "right", color: C.textMuted, fontSize: 12.5 }}>
                      {row.last}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </main>
  );
}
