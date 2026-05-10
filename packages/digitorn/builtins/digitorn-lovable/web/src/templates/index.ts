/**
 * Lovable's empty-state templates — declarative, owned by THIS bundle.
 *
 * Each ``?digitorn-seed`` import is resolved at build time by the SDK's
 * Vite plugin: it auto-bundles ``./seeds/<id>/App.tsx`` into a static
 * page under ``dist/seeds/<id>/`` and gives back ``{bundleUrl, files,
 * entry}``. The SDK ``<TemplatePreview>`` iframes ``bundleUrl`` direct
 * (~30 ms mount) and ``files`` is still available for the agent
 * confirm flow.
 *
 * Adding a new template:
 *   1. Drop ``./seeds/<id>/App.tsx``.
 *   2. Add a Template entry below importing ``./seeds/<id>?digitorn-seed``.
 *   3. ``npm run build``. Done.
 */

import type { Template } from "@digitorn/preview-sdk";

import portfolio from "./seeds/portfolio-minimal?digitorn-seed";
import landing from "./seeds/landing-startup?digitorn-seed";
import dashboard from "./seeds/dashboard-minimal?digitorn-seed";
import pricing from "./seeds/pricing-three-tier?digitorn-seed";

export const TEMPLATES: Template[] = [
  {
    id: "portfolio-minimal",
    title: "Minimal portfolio",
    description: "Bold typography, one page, no compromise",
    prompt: (
      "Adapte ce portfolio pour un développeur backend nommé Paul. " +
      "Garde l'esthétique minimaliste: typo grasse, beaucoup de blanc, " +
      "une seule couleur d'accent. Ajoute trois projets factices avec " +
      "des descriptions courtes."
    ),
    seed: portfolio,
    tags: ["web", "portfolio", "minimal"],
  },
  {
    id: "landing-startup",
    title: "Startup landing",
    description: "Hero, CTA, three reasons. Light theme.",
    prompt: (
      "Adapte ce landing pour une startup qui s'appelle Acme Robotics " +
      "et qui vend une plateforme d'automatisation industrielle."
    ),
    seed: landing,
    tags: ["web", "landing", "marketing"],
  },
  {
    id: "dashboard-minimal",
    title: "Minimal dashboard",
    description: "Sidebar, KPI cards, dense numbers. Dark theme.",
    prompt: (
      "Adapte ce dashboard pour une plateforme SaaS B2B qui suit " +
      "l'engagement utilisateur. Renomme les KPI, ajuste les chiffres."
    ),
    seed: dashboard,
    tags: ["web", "dashboard", "saas"],
  },
  {
    id: "pricing-three-tier",
    title: "Pricing, three tiers",
    description: "Starter, Pro, Enterprise. Recommended tier highlighted.",
    prompt: (
      "Adapte ce pricing pour une application de gestion de projet " +
      "(Starter / Team / Business)."
    ),
    seed: pricing,
    tags: ["web", "pricing", "marketing"],
  },
];
