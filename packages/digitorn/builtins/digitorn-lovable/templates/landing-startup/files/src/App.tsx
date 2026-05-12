import React from "react";

const PAGE = {
  minHeight: "100vh",
  background: "#FAFAF8",
  color: "#0a0a0a",
  fontFamily: "system-ui, -apple-system, sans-serif",
  WebkitFontSmoothing: "antialiased",
} as const;

const NAV = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "28px 64px",
  fontSize: 14,
  color: "#525252",
} as const;

const LOGO = {
  fontWeight: 600,
  color: "#0a0a0a",
  fontSize: 16,
  letterSpacing: "-0.02em",
} as const;

const LINKS = { display: "flex", gap: 32 } as const;

const SIGNIN = {
  background: "transparent",
  border: "1px solid #0a0a0a",
  color: "#0a0a0a",
  padding: "8px 16px",
  borderRadius: 999,
  fontSize: 13,
  fontWeight: 500,
  cursor: "pointer",
} as const;

const HERO = {
  padding: "96px 64px 80px",
  textAlign: "center" as const,
  maxWidth: 880,
  margin: "0 auto",
};

const TAGLINE = {
  fontSize: 72,
  fontWeight: 700,
  letterSpacing: "-0.04em",
  lineHeight: 1.0,
  color: "#0a0a0a",
  marginBottom: 24,
} as const;

const SUBLINE = {
  fontSize: 18,
  fontWeight: 400,
  color: "#525252",
  lineHeight: 1.5,
  maxWidth: 540,
  margin: "0 auto 40px",
} as const;

const CTAROW = {
  display: "flex",
  gap: 12,
  justifyContent: "center",
} as const;

const PRIMARY = {
  background: "#0a0a0a",
  color: "#ffffff",
  padding: "12px 24px",
  borderRadius: 999,
  fontSize: 14,
  fontWeight: 500,
  border: "none",
  cursor: "pointer",
} as const;

const SECONDARY = {
  background: "transparent",
  color: "#0a0a0a",
  padding: "12px 16px",
  borderRadius: 999,
  fontSize: 14,
  fontWeight: 500,
  border: "none",
  cursor: "pointer",
} as const;

const FEATURES = {
  display: "grid",
  gridTemplateColumns: "repeat(3, 1fr)",
  gap: 56,
  padding: "32px 96px 96px",
  maxWidth: 1120,
  margin: "0 auto",
} as const;

const FEAT = { textAlign: "left" as const } as const;
const ICON = { fontSize: 22, marginBottom: 14, color: "#0a0a0a" } as const;
const FTITLE = { fontSize: 16, fontWeight: 600, marginBottom: 8, color: "#0a0a0a" } as const;
const FDESC = { fontSize: 14, color: "#525252", lineHeight: 1.55 } as const;

export function App() {
  return (
    <main style={PAGE}>
      <nav style={NAV}>
        <span style={LOGO}>Acme</span>
        <div style={LINKS}>
          <span>Product</span>
          <span>Pricing</span>
          <span>Docs</span>
        </div>
        <button style={SIGNIN}>Sign in</button>
      </nav>

      <section style={HERO}>
        <h1 style={TAGLINE}>
          Ship faster<br />
          than your competitors.
        </h1>
        <p style={SUBLINE}>
          Build, deploy and iterate at the speed of thought. Stop
          wrestling tools, start shipping.
        </p>
        <div style={CTAROW}>
          <button style={PRIMARY}>Get started →</button>
          <button style={SECONDARY}>Live demo</button>
        </div>
      </section>

      <section style={FEATURES}>
        <div style={FEAT}>
          <div style={ICON}>▣</div>
          <div style={FTITLE}>Built for speed</div>
          <div style={FDESC}>
            Compile, deploy and preview in seconds. No more
            20-minute CI cycles.
          </div>
        </div>
        <div style={FEAT}>
          <div style={ICON}>◇</div>
          <div style={FTITLE}>Ship anywhere</div>
          <div style={FDESC}>
            Any cloud, any region, any runtime. Bring your own stack.
          </div>
        </div>
        <div style={FEAT}>
          <div style={ICON}>▲</div>
          <div style={FTITLE}>Predictable cost</div>
          <div style={FDESC}>
            Pay for what you use. No hidden fees, no surprise bills.
          </div>
        </div>
      </section>
    </main>
  );
}
