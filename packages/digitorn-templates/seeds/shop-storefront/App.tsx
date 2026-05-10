import React, { useMemo, useState } from "react";

const C = {
  bg: "#FAFAF7",
  bgSoft: "#F4F2EC",
  surface: "#FFFFFF",
  border: "rgba(0,0,0,0.07)",
  borderSoft: "rgba(0,0,0,0.04)",
  text: "#0F0F12",
  textMuted: "#5F5F66",
  textDim: "#9A9AA0",
  accent: "#0F0F12",
  amber: "#C2630B",
} as const;

const PAGE = {
  minHeight: "100vh",
  background: C.bg,
  color: C.text,
  fontFamily:
    "'IBM Plex Sans', system-ui, -apple-system, sans-serif",
  WebkitFontSmoothing: "antialiased",
  letterSpacing: "-0.005em",
} as const;

// ── Header ────────────────────────────────────────────────────────────

const ANNOUNCE = {
  background: C.text,
  color: C.bg,
  textAlign: "center" as const,
  padding: "8px 0",
  fontSize: 12.5,
  letterSpacing: "0.01em",
};

const HEADER = {
  display: "grid",
  gridTemplateColumns: "1fr auto 1fr",
  alignItems: "center",
  padding: "20px 48px",
  background: C.bg,
  borderBottom: `1px solid ${C.borderSoft}`,
  position: "sticky" as const,
  top: 0,
  zIndex: 10,
};

const NAV_LINKS = { display: "flex", gap: 28, fontSize: 13.5, color: C.text } as const;

const LOGO = {
  fontFamily: "'Fraunces', 'Cormorant Garamond', Georgia, serif",
  fontSize: 26,
  fontWeight: 500,
  letterSpacing: "-0.025em",
  textAlign: "center" as const,
} as const;

const HEADER_RIGHT = { display: "flex", justifyContent: "flex-end", alignItems: "center", gap: 18 } as const;

const SEARCH_BTN = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 14px",
  border: `1px solid ${C.border}`,
  borderRadius: 999,
  fontSize: 13,
  color: C.textMuted,
  background: C.surface,
  cursor: "pointer",
} as const;

const ICON_BTN = {
  width: 36,
  height: 36,
  borderRadius: 999,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  background: "transparent",
  border: "none",
  fontSize: 16,
  cursor: "pointer",
  color: C.text,
  position: "relative" as const,
} as const;

const CART_COUNT = {
  position: "absolute" as const,
  top: 4,
  right: 4,
  width: 16,
  height: 16,
  borderRadius: 999,
  background: C.text,
  color: C.bg,
  fontSize: 9.5,
  fontWeight: 600,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
} as const;

// ── Hero ──────────────────────────────────────────────────────────────

const HERO = {
  position: "relative" as const,
  padding: "72px 48px 80px",
  display: "grid",
  gridTemplateColumns: "1fr 1.1fr",
  gap: 48,
  alignItems: "center",
  background: C.bgSoft,
} as const;

const HERO_LEFT = { display: "flex", flexDirection: "column" as const, gap: 22 } as const;

const HERO_KICKER = {
  fontSize: 12,
  letterSpacing: "0.18em",
  textTransform: "uppercase" as const,
  color: C.textMuted,
  fontWeight: 500,
} as const;

const HERO_HEADLINE = {
  fontFamily: "'Fraunces', 'Cormorant Garamond', Georgia, serif",
  fontSize: 72,
  fontWeight: 400,
  letterSpacing: "-0.04em",
  lineHeight: 0.98,
  margin: 0,
  fontStyle: "italic" as const,
} as const;

const HERO_LEAD = {
  fontSize: 16,
  color: C.textMuted,
  lineHeight: 1.55,
  maxWidth: 440,
} as const;

const HERO_CTAS = { display: "flex", gap: 12, marginTop: 6 } as const;

const HERO_PRIMARY = {
  background: C.text,
  color: C.bg,
  padding: "13px 22px",
  borderRadius: 999,
  fontSize: 13.5,
  fontWeight: 500,
  border: "none",
  cursor: "pointer",
} as const;

const HERO_LINK = {
  background: "transparent",
  color: C.text,
  padding: "13px 8px",
  fontSize: 13.5,
  fontWeight: 500,
  border: "none",
  borderBottom: `1px solid ${C.text}`,
  cursor: "pointer",
} as const;

const HERO_RIGHT = { position: "relative" as const, height: 480 } as const;

const HERO_BIG_TILE = {
  position: "absolute" as const,
  top: 0,
  right: 0,
  width: "70%",
  height: "100%",
  background: "linear-gradient(135deg, #C8B69A 0%, #E8DEC8 100%)",
  borderRadius: 18,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  overflow: "hidden",
} as const;

const HERO_TILE_LABEL = {
  fontFamily: "'Fraunces', serif",
  fontSize: 96,
  fontWeight: 400,
  fontStyle: "italic" as const,
  color: "rgba(15,15,18,0.18)",
  letterSpacing: "-0.04em",
} as const;

const HERO_SMALL_TILE = {
  position: "absolute" as const,
  bottom: 0,
  left: 0,
  width: "55%",
  height: "60%",
  background: "linear-gradient(135deg, #4F4540 0%, #2A2520 100%)",
  borderRadius: 18,
  display: "flex",
  alignItems: "flex-end",
  padding: 24,
  color: "#F0E8D8",
  fontFamily: "'Fraunces', serif",
  fontSize: 36,
  fontStyle: "italic" as const,
  letterSpacing: "-0.02em",
  lineHeight: 1.05,
} as const;

const HERO_TAG = {
  position: "absolute" as const,
  top: 24,
  right: 24,
  padding: "5px 10px",
  background: "rgba(255,255,255,0.85)",
  borderRadius: 999,
  fontSize: 11,
  fontWeight: 500,
  color: C.text,
  letterSpacing: "0.04em",
  textTransform: "uppercase" as const,
} as const;

// ── Filters bar ───────────────────────────────────────────────────────

const FILTERS = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "20px 48px",
  borderBottom: `1px solid ${C.borderSoft}`,
  background: C.bg,
};

const FILTER_TABS = { display: "flex", gap: 8 } as const;
const FILTER_TAB = (active: boolean): React.CSSProperties => ({
  padding: "7px 14px",
  borderRadius: 999,
  fontSize: 13,
  cursor: "pointer",
  background: active ? C.text : "transparent",
  color: active ? C.bg : C.text,
  fontWeight: active ? 500 : 400,
  border: active ? "none" : `1px solid ${C.border}`,
});

const SORT = {
  display: "inline-flex",
  alignItems: "center",
  gap: 8,
  fontSize: 13,
  color: C.textMuted,
  cursor: "pointer",
} as const;

// ── Products ──────────────────────────────────────────────────────────

const PRODUCTS_PAD = { padding: "40px 48px 80px" } as const;
const COUNT_LABEL = { fontSize: 13, color: C.textMuted, marginBottom: 24 } as const;

const GRID = { display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 28 } as const;

const PROD_CARD = { display: "flex", flexDirection: "column" as const, cursor: "pointer", gap: 14 } as const;

const PROD_IMG = (gradient: string): React.CSSProperties => ({
  position: "relative",
  aspectRatio: "4 / 5",
  borderRadius: 14,
  background: gradient,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  overflow: "hidden",
});

const PROD_TAG = {
  position: "absolute" as const,
  top: 12,
  left: 12,
  padding: "3px 8px",
  background: C.surface,
  fontSize: 10.5,
  fontWeight: 500,
  letterSpacing: "0.04em",
  textTransform: "uppercase" as const,
  borderRadius: 999,
} as const;

const PROD_FAV = {
  position: "absolute" as const,
  top: 12,
  right: 12,
  width: 30,
  height: 30,
  borderRadius: 999,
  background: C.surface,
  border: `1px solid ${C.borderSoft}`,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  fontSize: 12,
  cursor: "pointer",
} as const;

const PROD_LABEL = {
  fontFamily: "'Fraunces', serif",
  fontSize: 36,
  fontWeight: 400,
  fontStyle: "italic" as const,
  color: "rgba(15,15,18,0.20)",
  letterSpacing: "-0.025em",
} as const;

const PROD_INFO = { display: "flex", flexDirection: "column" as const, gap: 4 } as const;
const PROD_BRAND = { fontSize: 11.5, color: C.textDim, letterSpacing: "0.04em", textTransform: "uppercase" as const, fontWeight: 500 } as const;
const PROD_NAME = { fontSize: 14.5, fontWeight: 500, letterSpacing: "-0.01em" } as const;
const PROD_ROW = { display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: 4 } as const;
const PROD_PRICE = { fontSize: 14.5, fontWeight: 500 } as const;
const PROD_PRICE_OLD = { fontSize: 12.5, color: C.textDim, textDecoration: "line-through", marginLeft: 8 } as const;
const PROD_SWATCH = { display: "flex", gap: 5 } as const;
const SWATCH = (color: string): React.CSSProperties => ({
  width: 13,
  height: 13,
  borderRadius: 999,
  background: color,
  border: `1px solid ${C.borderSoft}`,
});

// ── Editorial section ────────────────────────────────────────────────

const EDITORIAL = {
  margin: "0 48px",
  padding: "72px 56px",
  background: C.text,
  color: C.bg,
  borderRadius: 22,
  display: "grid",
  gridTemplateColumns: "1fr 1fr",
  gap: 48,
  alignItems: "center",
} as const;

const EDIT_TITLE = {
  fontFamily: "'Fraunces', serif",
  fontStyle: "italic" as const,
  fontSize: 56,
  fontWeight: 400,
  letterSpacing: "-0.035em",
  lineHeight: 1.0,
  marginBottom: 18,
} as const;

const EDIT_LEAD = { fontSize: 15, color: "rgba(255,255,255,0.7)", lineHeight: 1.6, maxWidth: 440 } as const;

const EDIT_CTA = {
  marginTop: 28,
  background: C.bg,
  color: C.text,
  padding: "13px 22px",
  borderRadius: 999,
  fontSize: 13.5,
  fontWeight: 500,
  border: "none",
  cursor: "pointer",
} as const;

const EDIT_VISUAL = {
  height: 360,
  borderRadius: 18,
  background: "linear-gradient(135deg, #8C7960 0%, #DCCBA8 100%)",
  display: "flex",
  alignItems: "flex-end",
  padding: 32,
  fontFamily: "'Fraunces', serif",
  fontStyle: "italic" as const,
  fontSize: 64,
  fontWeight: 400,
  color: "rgba(15,15,18,0.18)",
  letterSpacing: "-0.025em",
} as const;

// ── Footer ────────────────────────────────────────────────────────────

const FOOTER = {
  borderTop: `1px solid ${C.borderSoft}`,
  marginTop: 80,
  padding: "56px 48px 40px",
  background: C.bg,
};

const FOOTER_GRID = {
  display: "grid",
  gridTemplateColumns: "2fr 1fr 1fr 1fr 1.4fr",
  gap: 48,
  marginBottom: 48,
} as const;

const FOOTER_LOGO = {
  fontFamily: "'Fraunces', serif",
  fontSize: 32,
  fontStyle: "italic" as const,
  fontWeight: 400,
  letterSpacing: "-0.025em",
  marginBottom: 12,
} as const;

const FOOTER_BLURB = { fontSize: 13.5, color: C.textMuted, maxWidth: 280, lineHeight: 1.6 } as const;
const FOOTER_HEAD = { fontSize: 12, fontWeight: 500, letterSpacing: "0.06em", textTransform: "uppercase" as const, color: C.textMuted, marginBottom: 14 } as const;
const FOOTER_LINK = { fontSize: 13.5, color: C.text, marginBottom: 9, cursor: "pointer", display: "block" } as const;

const NEWSLETTER_INPUT = {
  display: "flex",
  alignItems: "center",
  border: `1px solid ${C.border}`,
  borderRadius: 999,
  padding: "4px 4px 4px 16px",
  background: C.surface,
  marginTop: 4,
} as const;

const NEWSLETTER_PLACEHOLDER = { flex: 1, fontSize: 13.5, color: C.textDim } as const;

const NEWSLETTER_BTN = {
  background: C.text,
  color: C.bg,
  padding: "9px 18px",
  borderRadius: 999,
  fontSize: 13,
  fontWeight: 500,
  border: "none",
  cursor: "pointer",
} as const;

const FOOTER_BAR = {
  borderTop: `1px solid ${C.borderSoft}`,
  paddingTop: 24,
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  fontSize: 12.5,
  color: C.textMuted,
} as const;

// ── Product seed data ────────────────────────────────────────────────

const PRODUCTS = [
  { id: "1", category: "Seating", brand: "Maison Atelier", name: "Linen lounge chair", price: "€680", old: "€790", tag: "New", img: "linear-gradient(135deg, #DCCBA8 0%, #B89E7B 100%)", letter: "Lc", swatches: ["#D4C2A0", "#3B342B", "#7B6B58"] },
  { id: "2", category: "Tables", brand: "Studio Rive", name: "Walnut side table", price: "€340", img: "linear-gradient(135deg, #5C4838 0%, #2D241B 100%)", letter: "St", swatches: ["#3B2E22", "#7B5B40"] },
  { id: "3", category: "Textiles", brand: "Norra", name: "Wool throw — sand", price: "€140", img: "linear-gradient(135deg, #E5DCC2 0%, #B8AB88 100%)", letter: "Th", swatches: ["#E5DCC2", "#3B342B", "#A07060"] },
  { id: "4", category: "Objects", brand: "Casa Lume", name: "Ceramic vase, tall", price: "€95", old: "€120", tag: "-21%", img: "linear-gradient(135deg, #E8DDC4 0%, #B59E78 100%)", letter: "Va", swatches: ["#E8DDC4", "#3B342B"] },
  { id: "5", category: "Seating", brand: "Maison Atelier", name: "Boucle ottoman", price: "€420", img: "linear-gradient(135deg, #F2E8D0 0%, #C2B190 100%)", letter: "Bo", swatches: ["#F2E8D0", "#3B342B", "#8C7253"] },
  { id: "6", category: "Lighting", brand: "Norra", name: "Floor lamp, brass", price: "€520", tag: "Limited", img: "linear-gradient(135deg, #C9A76A 0%, #6F5530 100%)", letter: "Fl", swatches: ["#C9A76A", "#3B342B"] },
  { id: "7", category: "Seating", brand: "Studio Rive", name: "Velvet armchair", price: "€890", img: "linear-gradient(135deg, #6B4530 0%, #3A271B 100%)", letter: "Vc", swatches: ["#6B4530", "#3B342B", "#A07060"] },
  { id: "8", category: "Textiles", brand: "Casa Lume", name: "Linen pillow set", price: "€110", img: "linear-gradient(135deg, #DCD0B0 0%, #A89878 100%)", letter: "Pi", swatches: ["#DCD0B0", "#3B342B", "#C2B190"] },
];

const FILTERS_LIST = ["All", "Seating", "Tables", "Lighting", "Textiles", "Objects"] as const;

export function App() {
  const [activeFilter, setActiveFilter] = useState<string>("All");
  const [favorites, setFavorites] = useState<Set<string>>(new Set());
  const [cartCount, setCartCount] = useState<number>(2);
  const visibleProducts = useMemo(
    () =>
      activeFilter === "All"
        ? PRODUCTS
        : PRODUCTS.filter((p) => p.category === activeFilter),
    [activeFilter],
  );
  const toggleFav = (id: string) => {
    setFavorites((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  return (
    <main style={PAGE}>
      <div style={ANNOUNCE}>Free shipping on orders over €200 — autumn collection in stock</div>

      {/* ── Header ────────────────────────────────────────────────── */}
      <header style={HEADER}>
        <div style={NAV_LINKS}>
          <span>Shop</span>
          <span>Collections</span>
          <span>Journal</span>
          <span>About</span>
        </div>
        <div style={LOGO}>maison atelier</div>
        <div style={HEADER_RIGHT}>
          <button style={SEARCH_BTN}>
            <span aria-hidden>⌕</span>
            <span>Search</span>
          </button>
          <button style={ICON_BTN}>
            ♡
            {favorites.size > 0 && (
              <span style={{ ...CART_COUNT, background: C.accent }}>
                {favorites.size}
              </span>
            )}
          </button>
          <button style={ICON_BTN} onClick={() => setCartCount((c) => c + 1)}>
            <span aria-hidden>⊕</span>
            <span style={CART_COUNT}>{cartCount}</span>
          </button>
        </div>
      </header>

      {/* ── Hero ──────────────────────────────────────────────────── */}
      <section style={HERO}>
        <div style={HERO_LEFT}>
          <div style={HERO_KICKER}>Autumn 2025 · Collection no. 14</div>
          <h1 style={HERO_HEADLINE}>
            Furniture<br />
            for slow living.
          </h1>
          <p style={HERO_LEAD}>
            Hand-finished pieces from European workshops. Solid woods, wools and
            naturally tanned leathers. Made to last a lifetime, then to be passed on.
          </p>
          <div style={HERO_CTAS}>
            <button style={HERO_PRIMARY}>Shop the collection</button>
            <button style={HERO_LINK}>Read the story →</button>
          </div>
        </div>
        <div style={HERO_RIGHT}>
          <div style={HERO_BIG_TILE}>
            <span style={HERO_TILE_LABEL}>Limited</span>
            <span style={HERO_TAG}>New arrival</span>
          </div>
          <div style={HERO_SMALL_TILE}>
            Made<br />
            in Florence.
          </div>
        </div>
      </section>

      {/* ── Filter bar ───────────────────────────────────────────── */}
      <section style={FILTERS}>
        <div style={FILTER_TABS}>
          {FILTERS_LIST.map((f) => (
            <span
              key={f}
              style={FILTER_TAB(activeFilter === f)}
              onClick={() => setActiveFilter(f)}
            >
              {f}
            </span>
          ))}
        </div>
        <div style={SORT}>Sort: Featured ⌄</div>
      </section>

      {/* ── Products ──────────────────────────────────────────── */}
      <section style={PRODUCTS_PAD}>
        <div style={COUNT_LABEL}>
          Showing {visibleProducts.length} of {PRODUCTS.length} pieces
          {activeFilter !== "All" ? ` · filtered by ${activeFilter}` : ""}
        </div>
        <div style={GRID}>
          {visibleProducts.map((p) => (
            <div key={p.id} style={PROD_CARD}>
              <div style={PROD_IMG(p.img)}>
                {p.tag && <span style={PROD_TAG}>{p.tag}</span>}
                <span
                  style={{
                    ...PROD_FAV,
                    color: favorites.has(p.id) ? "#C03030" : C.text,
                    background: favorites.has(p.id) ? "rgba(255,255,255,0.95)" : C.surface,
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    toggleFav(p.id);
                  }}
                >
                  {favorites.has(p.id) ? "♥" : "♡"}
                </span>
                <span style={PROD_LABEL}>{p.letter}</span>
              </div>
              <div style={PROD_INFO}>
                <div style={PROD_BRAND}>{p.brand}</div>
                <div style={PROD_NAME}>{p.name}</div>
                <div style={PROD_ROW}>
                  <div>
                    <span style={PROD_PRICE}>{p.price}</span>
                    {p.old && <span style={PROD_PRICE_OLD}>{p.old}</span>}
                  </div>
                  <div style={PROD_SWATCH}>
                    {p.swatches.map((c) => (
                      <span key={c} style={SWATCH(c)} />
                    ))}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ── Editorial / story ─────────────────────────────────── */}
      <section style={EDITORIAL}>
        <div>
          <div style={{ ...HERO_KICKER, color: "rgba(255,255,255,0.6)", marginBottom: 18 }}>
            Behind the workshop
          </div>
          <h3 style={EDIT_TITLE}>
            Things you<br />
            keep forever.
          </h3>
          <p style={EDIT_LEAD}>
            We work with eight family-owned ateliers across Italy, France and
            Portugal. Each piece is signed, numbered and built to outlast the
            person who buys it.
          </p>
          <button style={EDIT_CTA}>Visit the journal</button>
        </div>
        <div style={EDIT_VISUAL}>Atelier No. 8</div>
      </section>

      {/* ── Footer ──────────────────────────────────────────── */}
      <footer style={FOOTER}>
        <div style={FOOTER_GRID}>
          <div>
            <div style={FOOTER_LOGO}>maison atelier</div>
            <p style={FOOTER_BLURB}>
              Furniture and objects for slow living, made by European
              workshops. Established 2014 in Lyon.
            </p>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Shop</div>
            <span style={FOOTER_LINK}>New arrivals</span>
            <span style={FOOTER_LINK}>Best sellers</span>
            <span style={FOOTER_LINK}>Collections</span>
            <span style={FOOTER_LINK}>Gift cards</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Help</div>
            <span style={FOOTER_LINK}>Shipping</span>
            <span style={FOOTER_LINK}>Returns</span>
            <span style={FOOTER_LINK}>Care guide</span>
            <span style={FOOTER_LINK}>Contact us</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>About</div>
            <span style={FOOTER_LINK}>The makers</span>
            <span style={FOOTER_LINK}>Sustainability</span>
            <span style={FOOTER_LINK}>Showrooms</span>
            <span style={FOOTER_LINK}>Press</span>
          </div>
          <div>
            <div style={FOOTER_HEAD}>Newsletter</div>
            <p style={{ ...FOOTER_BLURB, marginBottom: 4, maxWidth: "none" }}>
              New pieces, ateliers & invites. Once a month.
            </p>
            <div style={NEWSLETTER_INPUT}>
              <span style={NEWSLETTER_PLACEHOLDER}>your@email.com</span>
              <button style={NEWSLETTER_BTN}>Subscribe</button>
            </div>
          </div>
        </div>
        <div style={FOOTER_BAR}>
          <span>© 2025 Maison Atelier · Lyon, France</span>
          <span>Privacy · Terms · Imprint</span>
        </div>
      </footer>
    </main>
  );
}
