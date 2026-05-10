import React, { useState } from "react";

const C = {
  bg: "#0A0A0F",
  bgSoft: "#11111A",
  surface: "#16161F",
  surfaceAlt: "#1C1C28",
  border: "rgba(255,255,255,0.08)",
  borderSoft: "rgba(255,255,255,0.04)",
  text: "#F4F4F7",
  textMuted: "#9CA3B0",
  textDim: "#6B7280",
  accent: "#A78BFA",
  accentSoft: "rgba(167,139,250,0.18)",
  accentBright: "#C4B5FD",
  green: "#34D399",
} as const;

const PAGE = {
  minHeight: "100vh",
  background: C.bg,
  color: C.text,
  fontFamily:
    "'IBM Plex Sans', system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
  WebkitFontSmoothing: "antialiased",
  letterSpacing: "-0.005em",
} as const;

const NAV = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "20px 56px",
  borderBottom: `1px solid ${C.borderSoft}`,
  position: "sticky" as const,
  top: 0,
  background: "rgba(10,10,15,0.72)",
  backdropFilter: "blur(12px)",
  zIndex: 10,
};

const LOGO = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  fontWeight: 600,
  fontSize: 15,
  letterSpacing: "-0.02em",
};

const LOGO_DOT = {
  width: 14,
  height: 14,
  borderRadius: 4,
  background: `linear-gradient(135deg, ${C.accent}, ${C.accentBright})`,
  boxShadow: `0 0 12px ${C.accentSoft}`,
};

const NAV_LINKS = {
  display: "flex",
  gap: 28,
  fontSize: 13.5,
  color: C.textMuted,
  fontWeight: 450,
};

const NAV_BTNS = { display: "flex", alignItems: "center", gap: 12 } as const;

const BTN_GHOST = {
  background: "transparent",
  color: C.text,
  border: "none",
  fontSize: 13,
  fontWeight: 450,
  padding: "8px 14px",
  cursor: "pointer",
  borderRadius: 8,
} as const;

const BTN_PRIMARY = {
  background: C.text,
  color: C.bg,
  border: "none",
  fontSize: 13,
  fontWeight: 500,
  padding: "8px 16px",
  borderRadius: 999,
  cursor: "pointer",
  letterSpacing: "-0.005em",
} as const;

const HERO = {
  position: "relative" as const,
  padding: "112px 56px 96px",
  textAlign: "center" as const,
  overflow: "hidden",
};

const MESH = {
  position: "absolute" as const,
  inset: 0,
  background:
    "radial-gradient(800px circle at 50% 0%, rgba(167,139,250,0.20) 0%, transparent 60%), " +
    "radial-gradient(600px circle at 20% 30%, rgba(34,211,238,0.10) 0%, transparent 50%), " +
    "radial-gradient(700px circle at 80% 30%, rgba(244,114,182,0.10) 0%, transparent 50%)",
  pointerEvents: "none" as const,
} as const;

const PILL = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "5px 12px 5px 6px",
  background: C.accentSoft,
  border: `1px solid ${C.accent}`,
  borderRadius: 999,
  fontSize: 12.5,
  color: C.accentBright,
  fontWeight: 500,
  marginBottom: 32,
  position: "relative" as const,
};

const PILL_DOT = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 18,
  height: 18,
  borderRadius: 999,
  background: C.accent,
  color: C.bg,
  fontSize: 10,
  fontWeight: 700,
} as const;

const HEADLINE = {
  fontSize: 72,
  fontWeight: 600,
  letterSpacing: "-0.045em",
  lineHeight: 1.0,
  maxWidth: 880,
  margin: "0 auto 24px",
  position: "relative" as const,
  background: `linear-gradient(180deg, ${C.text} 0%, ${C.textMuted} 100%)`,
  WebkitBackgroundClip: "text",
  WebkitTextFillColor: "transparent",
  backgroundClip: "text",
} as const;

const SUBHEAD = {
  fontSize: 18,
  fontWeight: 400,
  color: C.textMuted,
  lineHeight: 1.55,
  maxWidth: 580,
  margin: "0 auto 40px",
  position: "relative" as const,
} as const;

const CTA_ROW = {
  display: "flex",
  gap: 12,
  justifyContent: "center",
  marginBottom: 64,
  position: "relative" as const,
} as const;

const CTA_PRIMARY = {
  background: C.text,
  color: C.bg,
  border: "none",
  fontSize: 14.5,
  fontWeight: 500,
  padding: "14px 22px",
  borderRadius: 999,
  cursor: "pointer",
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
} as const;

const CTA_SECONDARY = {
  background: "transparent",
  color: C.text,
  border: `1px solid ${C.border}`,
  fontSize: 14.5,
  fontWeight: 500,
  padding: "14px 22px",
  borderRadius: 999,
  cursor: "pointer",
} as const;

const SOCIAL_PROOF = {
  fontSize: 12.5,
  color: C.textDim,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 14,
  position: "relative" as const,
} as const;

const AVATAR_STACK = { display: "flex" } as const;
const AVATAR = (i: number): React.CSSProperties => ({
  width: 22,
  height: 22,
  borderRadius: 999,
  background: ["#A78BFA", "#34D399", "#22D3EE", "#F472B6", "#FB923C"][i % 5],
  border: `2px solid ${C.bg}`,
  marginLeft: i === 0 ? 0 : -8,
});

const PRODUCT_FRAME = {
  marginTop: 64,
  maxWidth: 1080,
  margin: "64px auto 0",
  position: "relative" as const,
  borderRadius: 16,
  overflow: "hidden",
  border: `1px solid ${C.border}`,
  background: C.surface,
  boxShadow:
    "0 60px 120px -40px rgba(167,139,250,0.25), 0 24px 48px -16px rgba(0,0,0,0.6)",
} as const;

const PRODUCT_TOPBAR = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "12px 16px",
  borderBottom: `1px solid ${C.borderSoft}`,
  background: C.surfaceAlt,
} as const;

const PRODUCT_DOTS = { display: "flex", gap: 6 } as const;
const PRODUCT_DOT = (color: string): React.CSSProperties => ({
  width: 9,
  height: 9,
  borderRadius: 999,
  background: color,
});

const PRODUCT_BODY = { padding: 28, minHeight: 380, display: "grid", gridTemplateColumns: "200px 1fr", gap: 24 } as const;

const PRODUCT_SIDEBAR = { display: "flex", flexDirection: "column" as const, gap: 4 } as const;
const SIDE_ITEM = (active: boolean): React.CSSProperties => ({
  padding: "8px 12px",
  borderRadius: 8,
  fontSize: 13,
  color: active ? C.text : C.textMuted,
  background: active ? C.surfaceAlt : "transparent",
  fontWeight: active ? 500 : 400,
  display: "flex",
  alignItems: "center",
  gap: 10,
});

const SIDE_DOT = {
  width: 6,
  height: 6,
  borderRadius: 999,
  background: C.accent,
} as const;

const PRODUCT_MAIN = { display: "flex", flexDirection: "column" as const, gap: 16 } as const;
const MAIN_TITLE = {
  fontSize: 22,
  fontWeight: 600,
  letterSpacing: "-0.025em",
  color: C.text,
  marginBottom: 4,
} as const;
const MAIN_SUB = { fontSize: 13, color: C.textMuted, marginBottom: 12 } as const;

const KPI_ROW = { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 } as const;
const KPI_CARD = {
  padding: 14,
  background: C.surfaceAlt,
  border: `1px solid ${C.borderSoft}`,
  borderRadius: 10,
} as const;
const KPI_LABEL = { fontSize: 11, color: C.textDim, textTransform: "uppercase" as const, letterSpacing: "0.04em", marginBottom: 6 } as const;
const KPI_VAL = { fontSize: 22, fontWeight: 600, letterSpacing: "-0.02em" } as const;
const KPI_DELTA = { fontSize: 11, color: C.green, marginTop: 2 } as const;

const CHART_BOX = { marginTop: 8, padding: 16, background: C.surfaceAlt, border: `1px solid ${C.borderSoft}`, borderRadius: 10, height: 160, position: "relative" as const } as const;

// ── Section: Logos ────────────────────────────────────────────────────

const LOGOS = {
  padding: "40px 56px 80px",
  textAlign: "center" as const,
  borderTop: `1px solid ${C.borderSoft}`,
  borderBottom: `1px solid ${C.borderSoft}`,
};
const LOGOS_LABEL = {
  fontSize: 11.5,
  letterSpacing: "0.18em",
  textTransform: "uppercase" as const,
  color: C.textDim,
  marginBottom: 20,
  fontWeight: 500,
} as const;
const LOGOS_ROW = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 56,
  flexWrap: "wrap" as const,
  opacity: 0.55,
};

// ── Section: Features ─────────────────────────────────────────────────

const SECTION = { padding: "120px 56px", maxWidth: 1280, margin: "0 auto" } as const;
const SECTION_HEAD = { textAlign: "center" as const, marginBottom: 72 } as const;
const SECTION_KICKER = { fontSize: 12.5, color: C.accent, fontWeight: 500, letterSpacing: "0.04em", textTransform: "uppercase" as const, marginBottom: 14 } as const;
const SECTION_TITLE = { fontSize: 44, fontWeight: 600, letterSpacing: "-0.035em", lineHeight: 1.1, marginBottom: 16, maxWidth: 720, marginLeft: "auto", marginRight: "auto" } as const;
const SECTION_LEAD = { fontSize: 17, color: C.textMuted, lineHeight: 1.55, maxWidth: 560, margin: "0 auto" } as const;

const FEATURES_GRID = { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16 } as const;
const F_CARD = {
  padding: 28,
  background: C.surface,
  border: `1px solid ${C.border}`,
  borderRadius: 16,
  display: "flex",
  flexDirection: "column" as const,
  gap: 14,
  position: "relative" as const,
  overflow: "hidden",
} as const;
const F_ICON = {
  width: 36,
  height: 36,
  borderRadius: 9,
  background: C.accentSoft,
  border: `1px solid ${C.accent}`,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 16,
  color: C.accentBright,
} as const;
const F_TITLE = { fontSize: 16, fontWeight: 600, letterSpacing: "-0.015em" } as const;
const F_DESC = { fontSize: 13.5, color: C.textMuted, lineHeight: 1.55 } as const;

// ── Section: Stats ────────────────────────────────────────────────────

const STATS = {
  display: "grid",
  gridTemplateColumns: "repeat(3, 1fr)",
  gap: 0,
  borderTop: `1px solid ${C.borderSoft}`,
  borderBottom: `1px solid ${C.borderSoft}`,
};
const STAT = {
  padding: "56px 32px",
  textAlign: "center" as const,
  borderRight: `1px solid ${C.borderSoft}`,
};
const STAT_NUM = { fontSize: 56, fontWeight: 600, letterSpacing: "-0.045em", lineHeight: 1, marginBottom: 10, background: `linear-gradient(180deg, ${C.text}, ${C.textMuted})`, WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" } as const;
const STAT_LABEL = { fontSize: 14, color: C.textMuted } as const;

// ── Section: Testimonial ──────────────────────────────────────────────

const QUOTE = {
  maxWidth: 760,
  margin: "0 auto",
  textAlign: "center" as const,
  padding: "0 32px",
};
const QUOTE_TEXT = { fontSize: 28, fontWeight: 500, letterSpacing: "-0.025em", lineHeight: 1.35, color: C.text, marginBottom: 32 } as const;
const QUOTE_AVATAR = {
  width: 48,
  height: 48,
  borderRadius: 999,
  background: `linear-gradient(135deg, #A78BFA, #F472B6)`,
  display: "inline-block",
  marginBottom: 16,
} as const;
const QUOTE_NAME = { fontSize: 15, fontWeight: 500, letterSpacing: "-0.01em" } as const;
const QUOTE_ROLE = { fontSize: 13.5, color: C.textMuted, marginTop: 2 } as const;

// ── Section: Pricing ──────────────────────────────────────────────────

const PRICING_GRID = { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 16, maxWidth: 1080, margin: "0 auto" } as const;
const P_CARD = (featured: boolean): React.CSSProperties => ({
  padding: 32,
  background: featured ? C.surfaceAlt : C.surface,
  border: `1px solid ${featured ? C.accent : C.border}`,
  borderRadius: 18,
  display: "flex",
  flexDirection: "column" as const,
  gap: 18,
  position: "relative" as const,
  boxShadow: featured ? "0 30px 60px -20px rgba(167,139,250,0.25)" : "none",
});
const P_BADGE = {
  position: "absolute" as const,
  top: -12,
  right: 24,
  padding: "4px 10px",
  background: C.accent,
  color: C.bg,
  borderRadius: 999,
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: "0.02em",
  textTransform: "uppercase" as const,
} as const;
const P_TIER = { fontSize: 13.5, color: C.textMuted, fontWeight: 500 } as const;
const P_PRICE_ROW = { display: "flex", alignItems: "baseline", gap: 6 } as const;
const P_PRICE = { fontSize: 44, fontWeight: 600, letterSpacing: "-0.03em", lineHeight: 1 } as const;
const P_PRICE_UNIT = { fontSize: 14, color: C.textMuted } as const;
const P_DESC = { fontSize: 13.5, color: C.textMuted, lineHeight: 1.5 } as const;
const P_CTA = (featured: boolean): React.CSSProperties => ({
  background: featured ? C.text : "transparent",
  color: featured ? C.bg : C.text,
  border: featured ? "none" : `1px solid ${C.border}`,
  fontSize: 14,
  fontWeight: 500,
  padding: "10px 18px",
  borderRadius: 10,
  cursor: "pointer",
  width: "100%",
});
const P_LIST = { display: "flex", flexDirection: "column" as const, gap: 10, marginTop: 4 } as const;
const P_FEAT = { display: "flex", alignItems: "flex-start", gap: 10, fontSize: 13.5, color: C.text, lineHeight: 1.5 } as const;
const P_CHECK = {
  flex: "0 0 auto",
  width: 16,
  height: 16,
  borderRadius: 999,
  background: C.accentSoft,
  color: C.accent,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 10,
  marginTop: 2,
} as const;

// ── Section: FAQ ──────────────────────────────────────────────────────

const FAQ_LIST = { maxWidth: 760, margin: "0 auto", display: "flex", flexDirection: "column" as const, gap: 0 } as const;
const FAQ_ITEM = { padding: "20px 0", borderBottom: `1px solid ${C.borderSoft}` } as const;
const FAQ_Q = { display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 16, fontWeight: 500, marginBottom: 6 } as const;
const FAQ_A = { fontSize: 14.5, color: C.textMuted, lineHeight: 1.55 } as const;
const FAQ_PLUS = { color: C.textDim, fontSize: 18, fontWeight: 300 } as const;

// ── Section: Footer ──────────────────────────────────────────────────

const FOOTER = {
  borderTop: `1px solid ${C.borderSoft}`,
  padding: "56px 56px 40px",
  background: C.bgSoft,
};
const FOOTER_GRID = { display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 48, maxWidth: 1280, margin: "0 auto", marginBottom: 48 } as const;
const FOOTER_BLURB = { fontSize: 13.5, color: C.textMuted, lineHeight: 1.55, maxWidth: 280, marginTop: 14 } as const;
const FOOTER_HEAD = { fontSize: 12.5, fontWeight: 500, letterSpacing: "0.04em", textTransform: "uppercase" as const, color: C.text, marginBottom: 16 } as const;
const FOOTER_LINK = { fontSize: 13.5, color: C.textMuted, marginBottom: 10, cursor: "pointer", display: "block" } as const;
const FOOTER_BAR = {
  borderTop: `1px solid ${C.borderSoft}`,
  paddingTop: 24,
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  fontSize: 12.5,
  color: C.textDim,
  maxWidth: 1280,
  margin: "0 auto",
} as const;

export function App() {
  const [openFaq, setOpenFaq] = useState<number | null>(0);
  const [yearly, setYearly] = useState<boolean>(false);
  return (
    <main style={PAGE}>
      {/* ── Nav ────────────────────────────────────────────────── */}
      <nav style={NAV}>
        <div style={LOGO}>
          <span style={LOGO_DOT} />
          <span>Mira</span>
        </div>
        <div style={NAV_LINKS}>
          <span>Product</span>
          <span>Solutions</span>
          <span>Pricing</span>
          <span>Customers</span>
          <span>Docs</span>
        </div>
        <div style={NAV_BTNS}>
          <button style={BTN_GHOST}>Sign in</button>
          <button style={BTN_PRIMARY}>Start free</button>
        </div>
      </nav>

      {/* ── Hero ──────────────────────────────────────────────── */}
      <section style={HERO}>
        <div style={MESH} />
        <div style={PILL}>
          <span style={PILL_DOT}>★</span>
          New: agents that ship code, not suggestions
        </div>
        <h1 style={HEADLINE}>
          The autonomous engineer<br />
          for your codebase.
        </h1>
        <p style={SUBHEAD}>
          Mira plans, writes, reviews and ships pull requests across your
          stack. Plug it into GitHub, Slack and your IDE — wake up to merged
          PRs.
        </p>
        <div style={CTA_ROW}>
          <button style={CTA_PRIMARY}>
            Start building free <span aria-hidden>→</span>
          </button>
          <button style={CTA_SECONDARY}>Book a demo</button>
        </div>
        <div style={SOCIAL_PROOF}>
          <div style={AVATAR_STACK}>
            {[0, 1, 2, 3, 4].map((i) => (
              <span key={i} style={AVATAR(i)} />
            ))}
          </div>
          <span>Trusted by 8,000+ engineering teams</span>
        </div>

        {/* Product visual */}
        <div style={PRODUCT_FRAME}>
          <div style={PRODUCT_TOPBAR}>
            <div style={PRODUCT_DOTS}>
              <span style={PRODUCT_DOT("#FF5F57")} />
              <span style={PRODUCT_DOT("#FEBC2E")} />
              <span style={PRODUCT_DOT("#28C840")} />
            </div>
            <span style={{ fontSize: 11.5, color: C.textDim, marginLeft: 12, fontFamily: "ui-monospace, monospace" }}>
              app.mira.ai/workspace
            </span>
          </div>
          <div style={PRODUCT_BODY}>
            <aside style={PRODUCT_SIDEBAR}>
              <div style={SIDE_ITEM(true)}>
                <span style={SIDE_DOT} /> Workspaces
              </div>
              <div style={SIDE_ITEM(false)}>Agents</div>
              <div style={SIDE_ITEM(false)}>Pull requests</div>
              <div style={SIDE_ITEM(false)}>Knowledge</div>
              <div style={SIDE_ITEM(false)}>Audit log</div>
              <div style={{ height: 16 }} />
              <div style={{ ...SIDE_ITEM(false), opacity: 0.6, fontSize: 11.5, textTransform: "uppercase", letterSpacing: "0.06em" }}>Account</div>
              <div style={SIDE_ITEM(false)}>Settings</div>
              <div style={SIDE_ITEM(false)}>Billing</div>
            </aside>
            <div style={PRODUCT_MAIN}>
              <div>
                <div style={MAIN_TITLE}>Workspaces</div>
                <div style={MAIN_SUB}>3 active agents · 17 PRs merged this week</div>
              </div>
              <div style={KPI_ROW}>
                <div style={KPI_CARD}>
                  <div style={KPI_LABEL}>Time saved</div>
                  <div style={KPI_VAL}>184h</div>
                  <div style={KPI_DELTA}>+22% vs last week</div>
                </div>
                <div style={KPI_CARD}>
                  <div style={KPI_LABEL}>PRs merged</div>
                  <div style={KPI_VAL}>17</div>
                  <div style={KPI_DELTA}>+4 this week</div>
                </div>
                <div style={KPI_CARD}>
                  <div style={KPI_LABEL}>Tests passing</div>
                  <div style={KPI_VAL}>99.4%</div>
                  <div style={KPI_DELTA}>+0.3 pp</div>
                </div>
              </div>
              <div style={CHART_BOX}>
                <svg width="100%" height="100%" viewBox="0 0 600 130" preserveAspectRatio="none">
                  <defs>
                    <linearGradient id="grad-line" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={C.accent} stopOpacity="0.4" />
                      <stop offset="100%" stopColor={C.accent} stopOpacity="0" />
                    </linearGradient>
                  </defs>
                  <path
                    d="M0,90 C60,80 100,40 160,55 S280,95 340,70 S470,30 540,50 S600,30 600,30 L600,130 L0,130 Z"
                    fill="url(#grad-line)"
                  />
                  <path
                    d="M0,90 C60,80 100,40 160,55 S280,95 340,70 S470,30 540,50 S600,30 600,30"
                    fill="none"
                    stroke={C.accent}
                    strokeWidth="2"
                  />
                </svg>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Logos ─────────────────────────────────────────────── */}
      <section style={LOGOS}>
        <div style={LOGOS_LABEL}>Building with Mira</div>
        <div style={LOGOS_ROW}>
          {["Linear", "Notion", "Vercel", "Supabase", "Anthropic", "Replit"].map(
            (name) => (
              <span key={name} style={{ fontSize: 17, fontWeight: 600, letterSpacing: "-0.02em" }}>
                {name}
              </span>
            ),
          )}
        </div>
      </section>

      {/* ── Features ──────────────────────────────────────────── */}
      <section style={SECTION}>
        <div style={SECTION_HEAD}>
          <div style={SECTION_KICKER}>Built for engineering teams</div>
          <h2 style={SECTION_TITLE}>
            One agent. Your whole pipeline.
          </h2>
          <p style={SECTION_LEAD}>
            Mira reads your code, runs your tests, and opens pull requests that
            match your team's conventions. No prompts to engineer.
          </p>
        </div>
        <div style={FEATURES_GRID}>
          {[
            { icon: "✶", title: "Plan and execute", desc: "Mira breaks down tickets into commits, runs them, and reviews its own diff before opening a PR." },
            { icon: "↗", title: "Native to your stack", desc: "Works with TypeScript, Python, Go, Rust. Reads your linters, follows your style guide." },
            { icon: "◇", title: "Self-correcting", desc: "Failed tests, lint errors, conflicts — Mira fixes its own work until CI is green." },
            { icon: "⚡", title: "10× faster", desc: "What used to take a senior engineer 2 days now takes 90 minutes. End-to-end, no context switching." },
          ].map((f) => (
            <div key={f.title} style={F_CARD}>
              <div style={F_ICON}>{f.icon}</div>
              <div style={F_TITLE}>{f.title}</div>
              <div style={F_DESC}>{f.desc}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ── Stats ─────────────────────────────────────────────── */}
      <section style={STATS}>
        <div style={STAT}>
          <div style={STAT_NUM}>2.4M+</div>
          <div style={STAT_LABEL}>PRs shipped autonomously</div>
        </div>
        <div style={STAT}>
          <div style={STAT_NUM}>87%</div>
          <div style={STAT_LABEL}>Merged on first review</div>
        </div>
        <div style={{ ...STAT, borderRight: "none" }}>
          <div style={STAT_NUM}>$8.6M</div>
          <div style={STAT_LABEL}>Engineering hours saved this year</div>
        </div>
      </section>

      {/* ── Testimonial ───────────────────────────────────────── */}
      <section style={SECTION}>
        <div style={QUOTE}>
          <div style={QUOTE_AVATAR} />
          <p style={QUOTE_TEXT}>
            "Mira ships features faster than the engineer who wrote the spec.
            We doubled our velocity in six weeks without hiring."
          </p>
          <div style={QUOTE_NAME}>Camille Reyes</div>
          <div style={QUOTE_ROLE}>VP Engineering, Lattice</div>
        </div>
      </section>

      {/* ── Pricing ──────────────────────────────────────────── */}
      <section style={SECTION}>
        <div style={SECTION_HEAD}>
          <div style={SECTION_KICKER}>Pricing</div>
          <h2 style={SECTION_TITLE}>Pay for what your agents ship.</h2>
          <p style={SECTION_LEAD}>No seats, no surprises. Cancel anytime.</p>
          <div
            style={{
              display: "inline-flex",
              marginTop: 24,
              padding: 4,
              background: C.surface,
              border: `1px solid ${C.border}`,
              borderRadius: 999,
              gap: 4,
            }}
          >
            <button
              type="button"
              onClick={() => setYearly(false)}
              style={{
                padding: "8px 18px",
                borderRadius: 999,
                border: "none",
                cursor: "pointer",
                fontSize: 13,
                fontWeight: 500,
                background: !yearly ? C.text : "transparent",
                color: !yearly ? C.bg : C.textMuted,
              }}
            >
              Monthly
            </button>
            <button
              type="button"
              onClick={() => setYearly(true)}
              style={{
                padding: "8px 18px",
                borderRadius: 999,
                border: "none",
                cursor: "pointer",
                fontSize: 13,
                fontWeight: 500,
                background: yearly ? C.text : "transparent",
                color: yearly ? C.bg : C.textMuted,
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              Yearly
              <span
                style={{
                  fontSize: 10,
                  padding: "2px 6px",
                  borderRadius: 999,
                  background: yearly ? C.accentSoft : C.accentSoft,
                  color: C.accent,
                }}
              >
                -20%
              </span>
            </button>
          </div>
        </div>
        <div style={PRICING_GRID}>
          <div style={P_CARD(false)}>
            <div style={P_TIER}>Hobby</div>
            <div style={P_PRICE_ROW}>
              <span style={P_PRICE}>$0</span>
              <span style={P_PRICE_UNIT}>/ {yearly ? "year" : "month"}</span>
            </div>
            <div style={P_DESC}>For solo developers exploring agentic workflows.</div>
            <button style={P_CTA(false)}>Start free</button>
            <div style={P_LIST}>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>50 PR runs / month</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>1 connected repo</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Community support</div>
            </div>
          </div>
          <div style={P_CARD(true)}>
            <div style={P_BADGE}>Most popular</div>
            <div style={P_TIER}>Team</div>
            <div style={P_PRICE_ROW}>
              <span style={P_PRICE}>${yearly ? 38 : 48}</span>
              <span style={P_PRICE_UNIT}>/ agent / {yearly ? "month, billed yearly" : "month"}</span>
            </div>
            <div style={P_DESC}>For shipping teams that want their backlog cleared by Friday.</div>
            <button style={P_CTA(true)}>Start 14-day trial</button>
            <div style={P_LIST}>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Unlimited PR runs</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Up to 25 repos</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>GitHub + Slack + Linear</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Priority email support</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Audit log + SSO</div>
            </div>
          </div>
          <div style={P_CARD(false)}>
            <div style={P_TIER}>Enterprise</div>
            <div style={P_PRICE_ROW}>
              <span style={P_PRICE}>Custom</span>
            </div>
            <div style={P_DESC}>Self-hosted runners, SOC 2, dedicated success engineer.</div>
            <button style={P_CTA(false)}>Talk to sales</button>
            <div style={P_LIST}>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>On-prem deployment</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>Custom model access</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>SOC 2, SAML, audit log</div>
              <div style={P_FEAT}><span style={P_CHECK}>✓</span>99.95% uptime SLA</div>
            </div>
          </div>
        </div>
      </section>

      {/* ── FAQ ──────────────────────────────────────────────── */}
      <section style={SECTION}>
        <div style={SECTION_HEAD}>
          <div style={SECTION_KICKER}>FAQ</div>
          <h2 style={SECTION_TITLE}>The short version.</h2>
        </div>
        <div style={FAQ_LIST}>
          {[
            { q: "Does Mira touch production?", a: "Mira opens pull requests. Your team reviews and merges. Production deploys are gated by your existing CI/CD pipeline — Mira never bypasses your safety rails." },
            { q: "Which models do you use?", a: "Frontier models from Anthropic and OpenAI for planning and code generation, plus a fine-tuned diff-review model. Bring your own keys on the Enterprise plan." },
            { q: "How does it compare to Copilot or Cursor?", a: "Copilot autocompletes lines. Mira ships entire features. Different jobs, different tools — most teams use both." },
            { q: "Can I host it on-prem?", a: "Yes. The Enterprise plan ships a self-hosted runner that connects to your private models and stays inside your VPC." },
            { q: "How much does it cost compared to a senior engineer?", a: "About 4% of one. The math gets a lot more interesting when you factor in 24/7 availability and zero context switches." },
          ].map((item, i) => {
            const isOpen = openFaq === i;
            return (
              <div
                key={item.q}
                style={{ ...FAQ_ITEM, cursor: "pointer" }}
                onClick={() => setOpenFaq(isOpen ? null : i)}
              >
                <div style={FAQ_Q}>
                  {item.q}
                  <span
                    style={{
                      ...FAQ_PLUS,
                      transition: "transform 200ms ease",
                      transform: isOpen ? "rotate(45deg)" : "rotate(0deg)",
                    }}
                  >
                    +
                  </span>
                </div>
                <div
                  style={{
                    ...FAQ_A,
                    overflow: "hidden",
                    maxHeight: isOpen ? 200 : 0,
                    opacity: isOpen ? 1 : 0,
                    marginTop: isOpen ? 6 : 0,
                    transition:
                      "max-height 250ms ease, opacity 200ms ease, margin-top 200ms ease",
                  }}
                >
                  {item.a}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* ── Footer ───────────────────────────────────────────── */}
      <footer style={FOOTER}>
        <div style={FOOTER_GRID}>
          <div>
            <div style={LOGO}>
              <span style={LOGO_DOT} />
              <span>Mira</span>
            </div>
            <p style={FOOTER_BLURB}>
              The autonomous engineer for your codebase. Plan, write, ship —
              while you sleep.
            </p>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Product</div>
            <span style={FOOTER_LINK}>Features</span>
            <span style={FOOTER_LINK}>Pricing</span>
            <span style={FOOTER_LINK}>Changelog</span>
            <span style={FOOTER_LINK}>Roadmap</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Company</div>
            <span style={FOOTER_LINK}>About</span>
            <span style={FOOTER_LINK}>Careers</span>
            <span style={FOOTER_LINK}>Press</span>
            <span style={FOOTER_LINK}>Contact</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Resources</div>
            <span style={FOOTER_LINK}>Documentation</span>
            <span style={FOOTER_LINK}>Guides</span>
            <span style={FOOTER_LINK}>Security</span>
            <span style={FOOTER_LINK}>Status</span>
          </div>
        </div>
        <div style={FOOTER_BAR}>
          <span>© {new Date().getFullYear()} Mira Labs Inc.</span>
          <span>Privacy · Terms · Cookies</span>
        </div>
      </footer>
    </main>
  );
}
