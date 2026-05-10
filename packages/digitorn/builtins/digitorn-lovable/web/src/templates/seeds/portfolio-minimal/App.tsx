import React from "react";

const PAGE = {
  minHeight: "100vh",
  background: "#0a0a0a",
  color: "#f5f5f5",
  fontFamily: "system-ui, -apple-system, sans-serif",
  padding: "80px 24px",
} as const;

const WRAP = { maxWidth: 720, margin: "0 auto" } as const;

const NAME = {
  fontSize: 56,
  fontWeight: 700,
  letterSpacing: "-0.04em",
  lineHeight: 1.0,
  marginBottom: 8,
} as const;

const ROLE = {
  fontSize: 18,
  fontWeight: 500,
  color: "#a3a3a3",
  marginBottom: 64,
} as const;

const SECTION_LABEL = {
  fontSize: 11,
  fontWeight: 600,
  letterSpacing: "0.18em",
  textTransform: "uppercase" as const,
  color: "#737373",
  marginBottom: 12,
};

const SECTION = { marginBottom: 48 } as const;

const PROJECT = {
  padding: "16px 0",
  borderTop: "1px solid #262626",
} as const;

const PROJECT_TITLE = {
  fontSize: 18,
  fontWeight: 600,
  marginBottom: 4,
} as const;

const PROJECT_DESC = {
  fontSize: 14,
  color: "#a3a3a3",
  lineHeight: 1.5,
} as const;

const ABOUT_TEXT = {
  fontSize: 16,
  lineHeight: 1.7,
  color: "#d4d4d4",
} as const;

const CONTACT_LINK = { color: "#f5f5f5" } as const;

export function App() {
  return (
    <main style={PAGE}>
      <div style={WRAP}>
        <h1 style={NAME}>Your Name</h1>
        <p style={ROLE}>Engineer · Builder · Curious mind</p>

        <section style={SECTION}>
          <div style={SECTION_LABEL}>About</div>
          <p style={ABOUT_TEXT}>
            One paragraph that says who you are and what you care
            about. Keep it tight; the projects below do the talking.
          </p>
        </section>

        <section style={SECTION}>
          <div style={SECTION_LABEL}>Selected work</div>
          <div style={PROJECT}>
            <div style={PROJECT_TITLE}>Project one</div>
            <div style={PROJECT_DESC}>Short description.</div>
          </div>
          <div style={PROJECT}>
            <div style={PROJECT_TITLE}>Project two</div>
            <div style={PROJECT_DESC}>Short description.</div>
          </div>
          <div style={PROJECT}>
            <div style={PROJECT_TITLE}>Project three</div>
            <div style={PROJECT_DESC}>Short description.</div>
          </div>
        </section>

        <section>
          <div style={SECTION_LABEL}>Contact</div>
          <a href="mailto:hi@example.com" style={CONTACT_LINK}>
            hi@example.com
          </a>
        </section>
      </div>
    </main>
  );
}
