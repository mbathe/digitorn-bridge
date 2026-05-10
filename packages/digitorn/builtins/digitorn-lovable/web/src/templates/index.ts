/**
 * Lovable's exposed template list = the curated SDK library +
 * a few legacy lovable-only seeds (kept under
 * ``./seeds/<id>/App.tsx``).
 *
 * Adding a NEW template:
 *   - If it's generally useful → add it to ``@digitorn/templates``
 *     so every app benefits.
 *   - If it's lovable-specific → drop ``./seeds/<id>/App.tsx`` here
 *     + add an entry below importing ``./seeds/<id>?digitorn-seed``.
 */

import type { Template } from "@digitorn/preview-sdk";
import { DIGITORN_TEMPLATES } from "@digitorn/templates";

import portfolio from "./seeds/portfolio-minimal?digitorn-seed";
import landing from "./seeds/landing-startup?digitorn-seed";
import dashboard from "./seeds/dashboard-minimal?digitorn-seed";
import pricing from "./seeds/pricing-three-tier?digitorn-seed";

const LOCAL_TEMPLATES: Template[] = [
  {
    id: "portfolio-minimal",
    kind: "react",
    title: "Minimal portfolio",
    description: "Bold typography, one page, no compromise",
    prompt:
      "Adapte ce portfolio pour un développeur backend nommé Paul. " +
      "Garde l'esthétique minimaliste: typo grasse, beaucoup de blanc, " +
      "une seule couleur d'accent. Ajoute trois projets factices avec " +
      "des descriptions courtes.",
    seed: portfolio,
    tags: ["web", "portfolio", "minimal"],
  },
  {
    id: "landing-startup",
    kind: "react",
    title: "Startup landing",
    description: "Hero, CTA, three reasons. Light theme.",
    prompt:
      "Adapte ce landing pour une startup qui s'appelle Acme Robotics " +
      "et qui vend une plateforme d'automatisation industrielle.",
    seed: landing,
    tags: ["web", "landing", "marketing"],
  },
  {
    id: "dashboard-minimal",
    kind: "react",
    title: "Minimal dashboard",
    description: "Sidebar, KPI cards, dense numbers. Dark theme.",
    prompt:
      "Adapte ce dashboard pour une plateforme SaaS B2B qui suit " +
      "l'engagement utilisateur. Renomme les KPI, ajuste les chiffres.",
    seed: dashboard,
    tags: ["web", "dashboard", "saas"],
  },
  {
    id: "pricing-three-tier",
    kind: "react",
    title: "Pricing, three tiers",
    description: "Starter, Pro, Enterprise. Recommended tier highlighted.",
    prompt:
      "Adapte ce pricing pour une application de gestion de projet " +
      "(Starter / Team / Business).",
    seed: pricing,
    tags: ["web", "pricing", "marketing"],
  },
];

export const TEMPLATES: Template[] = [
  ...DIGITORN_TEMPLATES,
  ...LOCAL_TEMPLATES,
];
