import React, { useMemo, useState } from "react";

const C = {
  bg: "#F7F5EE",
  surface: "#FFFFFF",
  border: "rgba(15,15,18,0.10)",
  borderSoft: "rgba(15,15,18,0.06)",
  text: "#0F0F12",
  textMuted: "#5A5A60",
  textDim: "#9A9AA0",
  accent: "#9C2A1A",
} as const;

const SERIF = {
  fontFamily:
    "'Fraunces', 'Cormorant Garamond', 'Playfair Display', Georgia, serif",
};
const SANS = {
  fontFamily: "'IBM Plex Sans', system-ui, -apple-system, sans-serif",
};

const PAGE = {
  minHeight: "100vh",
  background: C.bg,
  color: C.text,
  ...SANS,
  WebkitFontSmoothing: "antialiased",
  letterSpacing: "-0.005em",
} as const;

// ── Masthead ──────────────────────────────────────────────────────────

const MAST_TOP = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "16px 56px",
  borderBottom: `1px solid ${C.borderSoft}`,
  fontSize: 12,
  color: C.textMuted,
} as const;

const MAST_TOP_LINKS = { display: "flex", gap: 22 } as const;

const MASTHEAD = {
  textAlign: "center" as const,
  padding: "44px 24px 28px",
  borderBottom: `1px solid ${C.text}`,
} as const;

const MASTHEAD_KICKER = {
  fontSize: 11,
  letterSpacing: "0.30em",
  textTransform: "uppercase" as const,
  color: C.textMuted,
  fontWeight: 500,
  marginBottom: 12,
} as const;

const MASTHEAD_TITLE = {
  ...SERIF,
  fontSize: 88,
  fontWeight: 600,
  letterSpacing: "-0.04em",
  lineHeight: 1,
  marginBottom: 12,
} as const;

const MASTHEAD_SUB = {
  ...SERIF,
  fontStyle: "italic" as const,
  fontSize: 18,
  color: C.textMuted,
  fontWeight: 400,
} as const;

const NAV = {
  display: "flex",
  justifyContent: "center",
  gap: 36,
  padding: "16px 24px",
  borderBottom: `1px solid ${C.text}`,
  fontSize: 13,
  fontWeight: 500,
  letterSpacing: "0.04em",
  textTransform: "uppercase" as const,
} as const;

const NAV_ITEM = (active: boolean): React.CSSProperties => ({
  paddingBottom: 4,
  borderBottom: active ? `2px solid ${C.text}` : "2px solid transparent",
  cursor: "pointer",
});

// ── Hero featured ─────────────────────────────────────────────────────

const HERO_WRAP = { padding: "56px 56px 80px", display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 48, borderBottom: `1px solid ${C.borderSoft}` } as const;

const HERO_VISUAL = {
  position: "relative" as const,
  aspectRatio: "5 / 4",
  background:
    "linear-gradient(135deg, #C9B89A 0%, #6F5538 100%), radial-gradient(circle at 30% 30%, rgba(255,255,255,0.18), transparent 50%)",
  borderRadius: 4,
  overflow: "hidden",
};

const HERO_CAPTION = {
  position: "absolute" as const,
  bottom: 16,
  left: 16,
  fontSize: 11,
  color: "rgba(255,255,255,0.85)",
  ...SERIF,
  fontStyle: "italic" as const,
  letterSpacing: "0.04em",
} as const;

const HERO_VISUAL_LABEL = {
  ...SERIF,
  fontSize: 120,
  fontStyle: "italic" as const,
  fontWeight: 400,
  color: "rgba(15,15,18,0.18)",
  letterSpacing: "-0.04em",
  position: "absolute" as const,
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
} as const;

const HERO_TEXT = { display: "flex", flexDirection: "column" as const, justifyContent: "center" } as const;

const CATEGORY = {
  fontSize: 11,
  letterSpacing: "0.18em",
  textTransform: "uppercase" as const,
  color: C.accent,
  fontWeight: 600,
  marginBottom: 14,
} as const;

const HERO_HEADLINE = {
  ...SERIF,
  fontSize: 56,
  fontWeight: 500,
  letterSpacing: "-0.035em",
  lineHeight: 1.0,
  marginBottom: 18,
} as const;

const HERO_DEK = {
  ...SERIF,
  fontSize: 21,
  fontStyle: "italic" as const,
  fontWeight: 400,
  color: C.textMuted,
  lineHeight: 1.4,
  marginBottom: 24,
} as const;

const BYLINE = { fontSize: 13, color: C.text, marginBottom: 4 } as const;
const BYLINE_AUTHOR = { fontWeight: 500 } as const;
const META = { fontSize: 12, color: C.textMuted, letterSpacing: "0.04em" } as const;

const READ_LINK = {
  marginTop: 24,
  fontSize: 13,
  fontWeight: 500,
  letterSpacing: "0.06em",
  textTransform: "uppercase" as const,
  borderBottom: `1px solid ${C.text}`,
  alignSelf: "flex-start" as const,
  paddingBottom: 4,
  cursor: "pointer",
} as const;

// ── Section: Latest ───────────────────────────────────────────────────

const SECTION_PAD = { padding: "72px 56px" } as const;

const SECTION_HEAD = {
  display: "flex",
  alignItems: "baseline",
  justifyContent: "space-between",
  marginBottom: 36,
  paddingBottom: 14,
  borderBottom: `1px solid ${C.text}`,
} as const;

const SECTION_TITLE = {
  ...SERIF,
  fontSize: 36,
  fontWeight: 500,
  letterSpacing: "-0.025em",
  fontStyle: "italic" as const,
} as const;

const SECTION_LINK = {
  fontSize: 12,
  fontWeight: 500,
  letterSpacing: "0.10em",
  textTransform: "uppercase" as const,
  color: C.textMuted,
  cursor: "pointer",
} as const;

const ARTICLES_GRID = { display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 36 } as const;

const ARTICLE = { display: "flex", flexDirection: "column" as const, gap: 14, cursor: "pointer" } as const;

const ART_VISUAL = (gradient: string): React.CSSProperties => ({
  position: "relative",
  aspectRatio: "5 / 4",
  background: gradient,
  borderRadius: 3,
  overflow: "hidden",
});

const ART_LABEL = {
  ...SERIF,
  fontSize: 64,
  fontStyle: "italic" as const,
  fontWeight: 400,
  color: "rgba(15,15,18,0.18)",
  letterSpacing: "-0.025em",
  position: "absolute" as const,
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
} as const;

const ART_HEADLINE = {
  ...SERIF,
  fontSize: 24,
  fontWeight: 500,
  letterSpacing: "-0.02em",
  lineHeight: 1.15,
} as const;

const ART_DEK = {
  ...SERIF,
  fontSize: 15,
  fontStyle: "italic" as const,
  color: C.textMuted,
  lineHeight: 1.5,
} as const;

const ART_BY = { fontSize: 12, color: C.textMuted, marginTop: 4, letterSpacing: "0.04em" } as const;

// ── Two-column featured ───────────────────────────────────────────────

const TWOCOL_WRAP = {
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: 48,
  padding: "72px 56px",
  borderTop: `1px solid ${C.borderSoft}`,
  borderBottom: `1px solid ${C.borderSoft}`,
  background: C.surface,
} as const;

const COL = { display: "flex", flexDirection: "column" as const, gap: 16 } as const;

// ── Newsletter ────────────────────────────────────────────────────────

const NEWSLETTER = {
  textAlign: "center" as const,
  padding: "120px 56px",
  background: C.text,
  color: C.bg,
};

const NEWSLETTER_TITLE = {
  ...SERIF,
  fontSize: 64,
  fontWeight: 500,
  letterSpacing: "-0.035em",
  lineHeight: 1.05,
  marginBottom: 22,
  fontStyle: "italic" as const,
} as const;

const NEWSLETTER_LEAD = { fontSize: 17, color: "rgba(255,255,255,0.7)", maxWidth: 520, margin: "0 auto 36px", lineHeight: 1.55 } as const;

const NEWSLETTER_FORM = {
  display: "flex",
  alignItems: "center",
  maxWidth: 480,
  margin: "0 auto",
  background: "transparent",
  border: `1px solid rgba(255,255,255,0.25)`,
  borderRadius: 999,
  padding: "4px 4px 4px 22px",
} as const;

const NEWSLETTER_INPUT_TEXT = { flex: 1, fontSize: 14, color: "rgba(255,255,255,0.5)", textAlign: "left" as const } as const;

const NEWSLETTER_BTN = {
  background: C.bg,
  color: C.text,
  border: "none",
  padding: "12px 24px",
  borderRadius: 999,
  fontSize: 13.5,
  fontWeight: 500,
  cursor: "pointer",
} as const;

// ── Footer ────────────────────────────────────────────────────────────

const FOOTER = {
  padding: "56px 56px 32px",
  borderTop: `1px solid ${C.borderSoft}`,
};

const FOOTER_GRID = { display: "grid", gridTemplateColumns: "2fr 1fr 1fr 1fr", gap: 56, marginBottom: 56 } as const;
const FOOTER_TITLE = { ...SERIF, fontSize: 28, fontStyle: "italic" as const, fontWeight: 500, letterSpacing: "-0.025em", marginBottom: 12 } as const;
const FOOTER_BLURB = { fontSize: 13.5, color: C.textMuted, lineHeight: 1.6, maxWidth: 320 } as const;
const FOOTER_HEAD = { fontSize: 11, fontWeight: 600, letterSpacing: "0.10em", textTransform: "uppercase" as const, marginBottom: 14 } as const;
const FOOTER_LINK = { fontSize: 13.5, color: C.text, marginBottom: 9, display: "block", cursor: "pointer" } as const;
const FOOTER_BAR = { borderTop: `1px solid ${C.borderSoft}`, paddingTop: 22, fontSize: 12, color: C.textMuted, display: "flex", justifyContent: "space-between" } as const;

const ARTICLES = [
  {
    cat: "Travel",
    head: "A slow week in the Aeolian Islands",
    dek: "Volcanoes, capers and unhurried ferries — a love letter to Sicily's quiet north.",
    author: "Lila Marchetti",
    date: "March 22",
    read: "9 min",
    img: "linear-gradient(135deg, #6FB1C2 0%, #2C5C6E 100%)",
    label: "Sicily",
  },
  {
    cat: "Design",
    head: "The architects who refuse to make things bigger",
    dek: "A new generation is building 40 m² apartments and calling it freedom.",
    author: "Naomi Kessler",
    date: "March 18",
    read: "12 min",
    img: "linear-gradient(135deg, #D8C8B0 0%, #8B7958 100%)",
    label: "Studio",
  },
  {
    cat: "Food",
    head: "Twenty-three ways to use stale bread",
    dek: "From panzanella to ribollita, the Italian dishes built on yesterday's loaf.",
    author: "Mara Voss",
    date: "March 14",
    read: "7 min",
    img: "linear-gradient(135deg, #E8D9B0 0%, #B59070 100%)",
    label: "Pane",
  },
];

const COL_ARTICLES = [
  {
    cat: "Essay",
    head: "On reading slowly",
    dek: "We treat speed as virtue. The classics were written by people who had nowhere to be.",
    author: "Charlotte Aldrich",
    date: "March 10",
    img: "linear-gradient(135deg, #4F4035 0%, #1F1A14 100%)",
    label: "Read",
  },
  {
    cat: "Profile",
    head: "The last potter in Marsala",
    dek: "Antonia Russo turned 78 last spring. Her wheel is still spinning, just barely.",
    author: "Lila Marchetti",
    date: "March 6",
    img: "linear-gradient(135deg, #C8997B 0%, #6F4830 100%)",
    label: "Antonia",
  },
];

const NAV_ITEMS = ["Latest", "Travel", "Design", "Food", "Essays", "Profiles", "Photo"] as const;

export function App() {
  const [activeNav, setActiveNav] = useState<string>("Latest");
  const [email, setEmail] = useState<string>("");
  const [subscribed, setSubscribed] = useState<boolean>(false);
  const visibleArticles = useMemo(() => {
    if (activeNav === "Latest") return ARTICLES;
    return ARTICLES.filter((a) => a.cat.toLowerCase() === activeNav.toLowerCase());
  }, [activeNav]);
  return (
    <main style={PAGE}>
      <div style={MAST_TOP}>
        <span>Saturday, March 22 · 2025</span>
        <div style={MAST_TOP_LINKS}>
          <span>Subscribe</span>
          <span>Sign in</span>
          <span>Search</span>
        </div>
      </div>

      <header style={MASTHEAD}>
        <div style={MASTHEAD_KICKER}>The Slow Quarterly · Issue 14</div>
        <h1 style={MASTHEAD_TITLE}>The Lantern</h1>
        <div style={MASTHEAD_SUB}>Slow stories about places, things and people worth keeping.</div>
      </header>

      <nav style={NAV}>
        {NAV_ITEMS.map((item) => (
          <span
            key={item}
            style={NAV_ITEM(activeNav === item)}
            onClick={() => setActiveNav(item)}
          >
            {item}
          </span>
        ))}
      </nav>

      {/* ── Hero featured ────────────────────────────────────────── */}
      <section style={HERO_WRAP}>
        <div style={HERO_VISUAL}>
          <div style={HERO_VISUAL_LABEL}>Toscana</div>
          <div style={HERO_CAPTION}>San Quirico d'Orcia, October light, 2024.</div>
        </div>
        <div style={HERO_TEXT}>
          <div style={CATEGORY}>Cover Story · Travel</div>
          <h2 style={HERO_HEADLINE}>
            The road that<br />
            doesn't end<br />
            in Tuscany.
          </h2>
          <p style={HERO_DEK}>
            Five days, four hand-painted ceramic shops, one olive harvest and
            an absolutely unreasonable amount of pici. A love letter to the
            province they forgot to ruin.
          </p>
          <div style={BYLINE}>
            <span style={BYLINE_AUTHOR}>By Lila Marchetti</span>
          </div>
          <div style={META}>March 22 · 14 min read · Photographs by Eli Cordero</div>
          <span style={READ_LINK}>Read the story →</span>
        </div>
      </section>

      {/* ── Latest ───────────────────────────────────────────────── */}
      <section style={SECTION_PAD}>
        <div style={SECTION_HEAD}>
          <h3 style={SECTION_TITLE}>{activeNav}</h3>
          <span style={SECTION_LINK}>
            {visibleArticles.length} article{visibleArticles.length === 1 ? "" : "s"} →
          </span>
        </div>
        <div style={ARTICLES_GRID}>
          {visibleArticles.length === 0 && (
            <div
              style={{
                gridColumn: "1 / -1",
                padding: "48px 24px",
                textAlign: "center",
                color: C.textMuted,
                fontFamily: SERIF.fontFamily,
                fontStyle: "italic",
                fontSize: 18,
              }}
            >
              No stories under "{activeNav}" yet. Check back next issue.
            </div>
          )}
          {visibleArticles.map((a) => (
            <article key={a.head} style={ARTICLE}>
              <div style={ART_VISUAL(a.img)}>
                <div style={ART_LABEL}>{a.label}</div>
              </div>
              <div style={CATEGORY}>{a.cat}</div>
              <h4 style={ART_HEADLINE}>{a.head}</h4>
              <p style={ART_DEK}>{a.dek}</p>
              <div style={ART_BY}>
                {a.author.toUpperCase()} · {a.date.toUpperCase()} · {a.read}
              </div>
            </article>
          ))}
        </div>
      </section>

      {/* ── Two-column ──────────────────────────────────────────── */}
      <section style={TWOCOL_WRAP}>
        {COL_ARTICLES.map((a) => (
          <div key={a.head} style={COL}>
            <div style={ART_VISUAL(a.img)}>
              <div style={ART_LABEL}>{a.label}</div>
            </div>
            <div style={CATEGORY}>{a.cat}</div>
            <h4 style={ART_HEADLINE}>{a.head}</h4>
            <p style={ART_DEK}>{a.dek}</p>
            <div style={ART_BY}>
              {a.author.toUpperCase()} · {a.date.toUpperCase()}
            </div>
          </div>
        ))}
      </section>

      {/* ── Newsletter ──────────────────────────────────────────── */}
      <section style={NEWSLETTER}>
        <div style={MASTHEAD_KICKER}>The Slow Quarterly</div>
        <h2 style={NEWSLETTER_TITLE}>
          Three stories,<br />
          once a month,<br />
          delivered slowly.
        </h2>
        <p style={NEWSLETTER_LEAD}>
          A long-read, a short essay and a photo report — printed on
          uncoated paper if you want, in your inbox if you don't.
        </p>
        {subscribed ? (
          <div
            style={{
              ...NEWSLETTER_LEAD,
              ...SERIF,
              fontStyle: "italic",
              color: "#F1F1F1",
              fontSize: 22,
            }}
          >
            Thank you. The next issue will reach {email || "you"} on the first
            of the month.
          </div>
        ) : (
          <form
            style={NEWSLETTER_FORM}
            onSubmit={(e) => {
              e.preventDefault();
              if (email.includes("@")) setSubscribed(true);
            }}
          >
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="your.email@somewhere.com"
              style={{
                ...NEWSLETTER_INPUT_TEXT,
                background: "transparent",
                border: "none",
                outline: "none",
                color: "rgba(255,255,255,0.95)",
                fontFamily: "inherit",
                padding: "10px 0",
              }}
            />
            <button type="submit" style={NEWSLETTER_BTN}>
              Subscribe
            </button>
          </form>
        )}
      </section>

      {/* ── Footer ──────────────────────────────────────────── */}
      <footer style={FOOTER}>
        <div style={FOOTER_GRID}>
          <div>
            <div style={FOOTER_TITLE}>The Lantern</div>
            <p style={FOOTER_BLURB}>
              An independent quarterly about places, things and people worth
              keeping. Founded in 2019, in Lyon. Printed in Florence on
              FSC-certified paper.
            </p>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Read</div>
            <span style={FOOTER_LINK}>Latest issue</span>
            <span style={FOOTER_LINK}>Archive</span>
            <span style={FOOTER_LINK}>Photo essays</span>
            <span style={FOOTER_LINK}>Long reads</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>About</div>
            <span style={FOOTER_LINK}>The masthead</span>
            <span style={FOOTER_LINK}>Submissions</span>
            <span style={FOOTER_LINK}>Print shop</span>
            <span style={FOOTER_LINK}>Contact</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Follow</div>
            <span style={FOOTER_LINK}>Instagram</span>
            <span style={FOOTER_LINK}>Newsletter</span>
            <span style={FOOTER_LINK}>RSS</span>
            <span style={FOOTER_LINK}>Bluesky</span>
          </div>
        </div>
        <div style={FOOTER_BAR}>
          <span>© 2025 The Lantern Editions · Lyon, France</span>
          <span>Privacy · Terms · Imprint</span>
        </div>
      </footer>
    </main>
  );
}
