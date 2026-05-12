import React from "react";

const PAGE = {
  minHeight: "100vh",
  display: "grid",
  gridTemplateColumns: "200px 1fr",
  background: "#0b0f14",
  color: "#e2e8f0",
  fontFamily: "system-ui, -apple-system, sans-serif",
} as const;

const SIDEBAR = {
  borderRight: "1px solid #1e293b",
  padding: "20px 14px",
  display: "flex",
  flexDirection: "column" as const,
  gap: 4,
} as const;

const SIDE_LOGO = {
  fontSize: 14,
  fontWeight: 700,
  letterSpacing: "-0.02em",
  marginBottom: 22,
  padding: "0 8px",
} as const;

const SIDE_LABEL = {
  fontSize: 10,
  fontWeight: 600,
  letterSpacing: "0.12em",
  textTransform: "uppercase" as const,
  color: "#64748b",
  padding: "8px 8px 6px",
};

const SIDE_ITEM = {
  fontSize: 13,
  padding: "7px 10px",
  borderRadius: 6,
  color: "#cbd5e1",
  cursor: "pointer",
} as const;

const SIDE_ITEM_ACTIVE = {
  ...SIDE_ITEM,
  background: "#1e293b",
  color: "#f1f5f9",
  fontWeight: 600,
} as const;

const MAIN = { padding: "28px 36px", overflow: "auto" as const } as const;
const HEADER = {
  display: "flex",
  alignItems: "baseline",
  justifyContent: "space-between",
  marginBottom: 24,
} as const;
const TITLE = { fontSize: 22, fontWeight: 700, letterSpacing: "-0.02em" } as const;
const PERIOD = { fontSize: 12, color: "#94a3b8" } as const;

const KPIS = {
  display: "grid",
  gridTemplateColumns: "repeat(4, 1fr)",
  gap: 14,
  marginBottom: 24,
} as const;

const KPI = {
  background: "#0f172a",
  border: "1px solid #1e293b",
  borderRadius: 10,
  padding: "14px 16px",
} as const;

const KPI_LABEL = {
  fontSize: 10.5,
  color: "#64748b",
  letterSpacing: "0.06em",
  textTransform: "uppercase" as const,
  marginBottom: 6,
};

const KPI_VALUE = {
  fontSize: 26,
  fontWeight: 700,
  fontFamily: "ui-monospace, monospace",
  letterSpacing: "-0.02em",
} as const;

const KPI_DELTA_UP = {
  fontSize: 11,
  color: "#10b981",
  marginTop: 4,
  fontFamily: "ui-monospace, monospace",
} as const;

const KPI_DELTA_DOWN = { ...KPI_DELTA_UP, color: "#ef4444" } as const;

const SECTION = {
  background: "#0f172a",
  border: "1px solid #1e293b",
  borderRadius: 10,
  padding: 18,
} as const;

const SECTION_TITLE = {
  fontSize: 13,
  fontWeight: 600,
  marginBottom: 14,
  color: "#cbd5e1",
} as const;

const TABLE_ROW = {
  display: "grid",
  gridTemplateColumns: "1fr 90px 90px",
  padding: "8px 4px",
  borderBottom: "1px solid #1e293b",
  fontSize: 13,
  color: "#cbd5e1",
} as const;

const TABLE_HEAD = {
  ...TABLE_ROW,
  fontSize: 10.5,
  textTransform: "uppercase" as const,
  letterSpacing: "0.08em",
  color: "#64748b",
} as const;

const RIGHT = { textAlign: "right" as const } as const;
const NUM_RIGHT = {
  fontFamily: "ui-monospace, monospace",
  textAlign: "right" as const,
} as const;

export function App() {
  return (
    <div style={PAGE}>
      <aside style={SIDEBAR}>
        <div style={SIDE_LOGO}>◆ Acme</div>
        <div style={SIDE_LABEL}>Workspace</div>
        <div style={SIDE_ITEM_ACTIVE}>Overview</div>
        <div style={SIDE_ITEM}>Customers</div>
        <div style={SIDE_ITEM}>Revenue</div>
        <div style={SIDE_LABEL}>Account</div>
        <div style={SIDE_ITEM}>Settings</div>
        <div style={SIDE_ITEM}>Billing</div>
      </aside>
      <main style={MAIN}>
        <div style={HEADER}>
          <h1 style={TITLE}>Overview</h1>
          <span style={PERIOD}>Last 30 days</span>
        </div>

        <div style={KPIS}>
          <div style={KPI}>
            <div style={KPI_LABEL}>MRR</div>
            <div style={KPI_VALUE}>$48.2k</div>
            <div style={KPI_DELTA_UP}>+12.4%</div>
          </div>
          <div style={KPI}>
            <div style={KPI_LABEL}>Active users</div>
            <div style={KPI_VALUE}>3,184</div>
            <div style={KPI_DELTA_UP}>+318</div>
          </div>
          <div style={KPI}>
            <div style={KPI_LABEL}>Churn</div>
            <div style={KPI_VALUE}>2.1%</div>
            <div style={KPI_DELTA_DOWN}>+0.4 pp</div>
          </div>
          <div style={KPI}>
            <div style={KPI_LABEL}>Net new</div>
            <div style={KPI_VALUE}>+86</div>
            <div style={KPI_DELTA_UP}>+22%</div>
          </div>
        </div>

        <div style={SECTION}>
          <div style={SECTION_TITLE}>Top customers</div>
          <div style={TABLE_HEAD}>
            <span>Account</span>
            <span style={RIGHT}>Plan</span>
            <span style={RIGHT}>MRR</span>
          </div>
          <div style={TABLE_ROW}>
            <span>Globex Robotics</span>
            <span style={RIGHT}>Enterprise</span>
            <span style={NUM_RIGHT}>$8,400</span>
          </div>
          <div style={TABLE_ROW}>
            <span>Initech</span>
            <span style={RIGHT}>Pro</span>
            <span style={NUM_RIGHT}>$2,900</span>
          </div>
          <div style={TABLE_ROW}>
            <span>Hooli</span>
            <span style={RIGHT}>Pro</span>
            <span style={NUM_RIGHT}>$1,800</span>
          </div>
        </div>
      </main>
    </div>
  );
}
