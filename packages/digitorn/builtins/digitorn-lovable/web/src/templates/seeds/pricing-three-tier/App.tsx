import React from "react";

const PAGE = {
  minHeight: "100vh",
  background: "#FAFAF8",
  color: "#0a0a0a",
  fontFamily: "system-ui, -apple-system, sans-serif",
  padding: "64px 32px",
} as const;

const HEADER = { textAlign: "center" as const, marginBottom: 56 } as const;

const EYEBROW = {
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: "0.18em",
  textTransform: "uppercase" as const,
  color: "#737373",
  marginBottom: 12,
};

const TITLE = {
  fontSize: 48,
  fontWeight: 700,
  letterSpacing: "-0.04em",
  lineHeight: 1.05,
  color: "#0a0a0a",
  marginBottom: 12,
} as const;

const SUBTITLE = {
  fontSize: 16,
  color: "#525252",
  maxWidth: 540,
  margin: "0 auto",
  lineHeight: 1.5,
} as const;

const GRID = {
  display: "grid",
  gridTemplateColumns: "repeat(3, 1fr)",
  gap: 16,
  maxWidth: 980,
  margin: "0 auto",
  alignItems: "stretch" as const,
} as const;

const CARD = {
  background: "#ffffff",
  border: "1px solid #e5e5e5",
  borderRadius: 14,
  padding: "28px 26px",
  display: "flex",
  flexDirection: "column" as const,
} as const;

const CARD_FEATURED = {
  background: "#0a0a0a",
  color: "#fafafa",
  border: "1px solid #0a0a0a",
  borderRadius: 14,
  padding: "32px 28px",
  marginTop: -8,
  display: "flex",
  flexDirection: "column" as const,
  boxShadow: "0 12px 32px -16px rgba(0,0,0,0.35)",
} as const;

const TIER = { fontSize: 13, fontWeight: 600, letterSpacing: "0.02em", marginBottom: 14 } as const;

const PRICE = { display: "flex", alignItems: "baseline", gap: 6, marginBottom: 6 } as const;

const PRICE_AMOUNT = { fontSize: 40, fontWeight: 700, letterSpacing: "-0.04em" } as const;

const PRICE_PERIOD = { fontSize: 13, color: "#737373" } as const;
const PRICE_PERIOD_DARK = { fontSize: 13, color: "#a3a3a3" } as const;

const TAGLINE = {
  fontSize: 13,
  color: "#525252",
  marginBottom: 24,
  lineHeight: 1.45,
} as const;

const TAGLINE_DARK = { ...TAGLINE, color: "#a3a3a3" } as const;

const FEATURES = {
  listStyle: "none" as const,
  padding: 0,
  margin: "0 0 28px",
  display: "flex",
  flexDirection: "column" as const,
  gap: 10,
  flex: 1,
} as const;

const FEAT_ROW = {
  display: "flex",
  alignItems: "flex-start",
  gap: 10,
  fontSize: 13.5,
  lineHeight: 1.45,
} as const;

const TICK = { color: "#0a0a0a", flex: "0 0 auto" } as const;
const TICK_DARK = { color: "#fafafa", flex: "0 0 auto" } as const;

const CTA_LIGHT = {
  background: "transparent",
  color: "#0a0a0a",
  border: "1px solid #0a0a0a",
  borderRadius: 999,
  padding: "10px 18px",
  fontSize: 13.5,
  fontWeight: 500,
  cursor: "pointer",
} as const;

const CTA_DARK = {
  background: "#fafafa",
  color: "#0a0a0a",
  border: "1px solid #fafafa",
  borderRadius: 999,
  padding: "10px 18px",
  fontSize: 13.5,
  fontWeight: 500,
  cursor: "pointer",
} as const;

const BADGE = {
  position: "absolute" as const,
  top: 16,
  right: 16,
  background: "#fafafa",
  color: "#0a0a0a",
  fontSize: 10.5,
  fontWeight: 600,
  letterSpacing: "0.06em",
  textTransform: "uppercase" as const,
  padding: "4px 10px",
  borderRadius: 999,
};

const FEATURED_WRAP = { position: "relative" as const } as const;

export function App() {
  return (
    <main style={PAGE}>
      <div style={HEADER}>
        <div style={EYEBROW}>Pricing</div>
        <h1 style={TITLE}>Simple. Pay for what you ship.</h1>
        <p style={SUBTITLE}>
          Three tiers, no hidden fees, cancel anytime. Annual
          billing saves 20%.
        </p>
      </div>

      <div style={GRID}>
        <div style={CARD}>
          <div style={TIER}>Starter</div>
          <div style={PRICE}>
            <span style={PRICE_AMOUNT}>$0</span>
            <span style={PRICE_PERIOD}>/ month</span>
          </div>
          <div style={TAGLINE}>For solo developers shipping side projects.</div>
          <ul style={FEATURES}>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>1 project</span></li>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>Community support</span></li>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>Public previews</span></li>
          </ul>
          <button style={CTA_LIGHT}>Start free</button>
        </div>

        <div style={FEATURED_WRAP}>
          <div style={CARD_FEATURED}>
            <span style={BADGE}>Recommended</span>
            <div style={TIER}>Pro</div>
            <div style={PRICE}>
              <span style={PRICE_AMOUNT}>$24</span>
              <span style={PRICE_PERIOD_DARK}>/ month</span>
            </div>
            <div style={TAGLINE_DARK}>For small teams that ship weekly.</div>
            <ul style={FEATURES}>
              <li style={FEAT_ROW}><span style={TICK_DARK}>✓</span><span>Unlimited projects</span></li>
              <li style={FEAT_ROW}><span style={TICK_DARK}>✓</span><span>Private previews + custom domain</span></li>
              <li style={FEAT_ROW}><span style={TICK_DARK}>✓</span><span>Priority email support</span></li>
              <li style={FEAT_ROW}><span style={TICK_DARK}>✓</span><span>5 seats included</span></li>
            </ul>
            <button style={CTA_DARK}>Choose Pro</button>
          </div>
        </div>

        <div style={CARD}>
          <div style={TIER}>Enterprise</div>
          <div style={PRICE}>
            <span style={PRICE_AMOUNT}>Custom</span>
          </div>
          <div style={TAGLINE}>For organizations with audit, SSO and SLA needs.</div>
          <ul style={FEATURES}>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>Everything in Pro</span></li>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>SSO, SAML, audit log</span></li>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>99.95% uptime SLA</span></li>
            <li style={FEAT_ROW}><span style={TICK}>✓</span><span>Dedicated account team</span></li>
          </ul>
          <button style={CTA_LIGHT}>Talk to sales</button>
        </div>
      </div>
    </main>
  );
}
