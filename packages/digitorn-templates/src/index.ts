/**
 * `@digitorn/templates` — curated, opinionated template library for
 * Digitorn apps. Drop into any consumer's `<TemplateEmptyState>`:
 *
 * ```tsx
 * import { DIGITORN_TEMPLATES } from "@digitorn/templates";
 * return <TemplateEmptyState templates={DIGITORN_TEMPLATES} />;
 * ```
 *
 * The seed sources live at `src/seeds/<id>/App.tsx` and ship as raw
 * `.tsx` files inside the published package. Consumers bundle them at
 * THEIR build time via the SDK's Vite plugin — pass
 * `seedsDirs: [TEMPLATES_SEEDS_DIR, ...]` from
 * `@digitorn/templates/vite` so Vite discovers the package's seeds
 * alongside the consumer's own. The plugin produces hashed JS chunks
 * + a static HTML page per seed under `dist/seeds/<id>/`, and rewrites
 * each template's `seed.bundleUrl` so `<TemplatePreview>` iframes the
 * bundled page directly (~30ms mount).
 */

import type { Template } from "@digitorn/preview-sdk";

import landingAiSaasSeed from "../seeds/landing-ai-saas?digitorn-seed";
import appDashboardSeed from "../seeds/app-dashboard?digitorn-seed";
import shopStorefrontSeed from "../seeds/shop-storefront?digitorn-seed";
import blogMagazineSeed from "../seeds/blog-magazine?digitorn-seed";
import { HTML_PLAYGROUND_SOURCE } from "./html-seeds.js";

export const LANDING_AI_SAAS: Template = {
  id: "landing-ai-saas",
  kind: "react",
  title: "AI SaaS landing",
  description:
    "Mesh-gradient hero, features, stats, testimonial, pricing, FAQ. Premium dark.",
  prompt:
    "Adapte ce landing AI premium pour [ta startup]. Garde le style premium dark, " +
    "le mesh gradient, les sections multiples (hero, features, stats, testimonial, " +
    "pricing 3-tier, FAQ, footer). Le toggle Monthly/Yearly et le FAQ accordion " +
    "sont déjà interactifs.",
  seed: landingAiSaasSeed,
  tags: ["web", "landing", "saas", "premium"],
};

export const APP_DASHBOARD: Template = {
  id: "app-dashboard",
  kind: "react",
  title: "Analytics dashboard",
  description:
    "Sidebar, search, KPIs with sparklines, area chart, activity feed, customers table.",
  prompt:
    "Adapte ce dashboard pour [ton produit]. Sidebar nav cliquable, range tabs " +
    "fonctionnelles, search input, 4 KPIs avec sparklines, graphique principal, " +
    "feed activité, table comptes. Renomme les KPIs et les comptes selon ton produit.",
  seed: appDashboardSeed,
  tags: ["web", "dashboard", "saas", "analytics"],
};

export const SHOP_STOREFRONT: Template = {
  id: "shop-storefront",
  kind: "react",
  title: "Editorial storefront",
  description:
    "Boutique furniture shop with hero, filters, product grid, editorial section, footer.",
  prompt:
    "Adapte ce storefront pour une marque de [furniture / ceramics / textiles]. " +
    "Filter tabs, favorites, cart counter sont interactifs. Garde l'esthétique " +
    "éditoriale: serif italique pour le hero, palette beige/terracotta.",
  seed: shopStorefrontSeed,
  tags: ["web", "ecommerce", "editorial", "shop"],
};

export const BLOG_MAGAZINE: Template = {
  id: "blog-magazine",
  kind: "react",
  title: "Slow magazine",
  description:
    "Editorial blog with serif masthead, hero feature, 3-col grid, photo essays, newsletter.",
  prompt:
    "Adapte ce magazine pour [thème: voyage lent / design slow / cuisine traditionnelle]. " +
    "Garde la masthead Fraunces serif, le hero featured cover story, la grille 3-col " +
    "d'articles, la section newsletter dark. Nav top et newsletter form sont interactifs.",
  seed: blogMagazineSeed,
  tags: ["web", "blog", "magazine", "editorial"],
};

/**
 * Static HTML template — proves the ``kind`` dispatcher: no React,
 * no esbuild-wasm, no bundling. The HtmlPreview renderer iframes
 * ``seed.files["index.html"]`` via ``srcdoc``, ~5 ms mount.
 *
 * Apps building static-site editors, slide decks (Reveal.js HTML),
 * or marketing-page generators can lean on this kind without ever
 * touching the React pipeline.
 */
export const HTML_PLAYGROUND: Template = {
  id: "html-playground",
  kind: "html",
  title: "Editorial one-pager (HTML)",
  description:
    "Static HTML landing — no React, no bundler. Tailwind via CDN, ~5ms mount.",
  prompt:
    "Adapte cette one-pager HTML pour [ta marque]. Tout est inline " +
    "(Tailwind CDN, fonts Google), tu peux donc l'éditer comme un " +
    "simple fichier HTML sans setup.",
  seed: {
    files: { "index.html": HTML_PLAYGROUND_SOURCE },
    entry: "index.html",
  },
  tags: ["html", "static", "landing"],
};

/**
 * Default ordering: premium / showcase first, more specialised second.
 * Apps cherry-pick or reorder via:
 *
 * ```tsx
 * import { LANDING_AI_SAAS, APP_DASHBOARD } from "@digitorn/templates";
 * <TemplateEmptyState templates={[LANDING_AI_SAAS, APP_DASHBOARD]} />
 * ```
 */
export const DIGITORN_TEMPLATES: readonly Template[] = [
  LANDING_AI_SAAS,
  APP_DASHBOARD,
  SHOP_STOREFRONT,
  BLOG_MAGAZINE,
  HTML_PLAYGROUND,
];
