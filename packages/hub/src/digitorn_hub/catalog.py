"""Single source of truth for the Hub's package catalogue metadata.

The catalogue ships:
  * a closed list of valid `category` values (validated at publish, used
    for filtering in `/api/v1/search`),
  * the visual identity per category (gradient colour pair, icon name)
    so every client renders the same hero / chips / cards without
    drifting into hardcoded copies.

Adding a new category = a single entry below + Hub redeploy. Clients
fetch `/api/v1/categories` and re-hydrate their chip lists, gradient
lookups, and (eventually) the publisher's "category" picker.
"""
from __future__ import annotations

from typing import Final

from pydantic import BaseModel


class CategoryOut(BaseModel):
    """Public representation of a category, served from
    `GET /api/v1/categories` and proxied to clients via the daemon."""

    id: str
    label_en: str
    label_fr: str
    description_en: str
    description_fr: str
    color_from: str
    color_to: str
    icon: str


# Order matters - clients render chips in this order. Keep
# beginner-friendly categories first; specialised buckets after.
_CATEGORIES: Final[list[CategoryOut]] = [
    CategoryOut(
        id="assistant",
        label_en="Assistant",
        label_fr="Assistant",
        description_en="General-purpose chat agents and copilots.",
        description_fr="Agents conversationnels et copilotes généralistes.",
        color_from="#059669",
        color_to="#0d9488",
        icon="MessageCircle",
    ),
    CategoryOut(
        id="productivity",
        label_en="Productivity",
        label_fr="Productivité",
        description_en="Time, tasks, calendars, focus.",
        description_fr="Temps, tâches, calendriers, focus.",
        color_from="#d97706",
        color_to="#dc2626",
        icon="Zap",
    ),
    CategoryOut(
        id="developer-tools",
        label_en="Developer Tools",
        label_fr="Outils développeur",
        description_en="Coding, debugging, deployment, scripting.",
        description_fr="Code, debug, déploiement, scripting.",
        color_from="#0891b2",
        color_to="#1d4ed8",
        icon="Code",
    ),
    CategoryOut(
        id="creative",
        label_en="Creative",
        label_fr="Créatif",
        description_en="Writing, design, music, art generation.",
        description_fr="Écriture, design, musique, art génératif.",
        color_from="#7c3aed",
        color_to="#db2777",
        icon="Sparkles",
    ),
    CategoryOut(
        id="research",
        label_en="Research",
        label_fr="Recherche",
        description_en="Deep research, summarisation, citations.",
        description_fr="Recherche approfondie, synthèse, citations.",
        color_from="#4f46e5",
        color_to="#7c3aed",
        icon="Microscope",
    ),
    CategoryOut(
        id="education",
        label_en="Education",
        label_fr="Éducation",
        description_en="Teaching, tutoring, learning paths.",
        description_fr="Enseignement, tutorat, parcours d'apprentissage.",
        color_from="#0284c7",
        color_to="#4338ca",
        icon="GraduationCap",
    ),
    CategoryOut(
        id="communication",
        label_en="Communication",
        label_fr="Communication",
        description_en="Email, chat, social, customer support.",
        description_fr="Email, chat, social, support client.",
        color_from="#e11d48",
        color_to="#c026d3",
        icon="MessagesSquare",
    ),
    CategoryOut(
        id="data",
        label_en="Data",
        label_fr="Données",
        description_en="Analytics, ETL, dashboards, scraping.",
        description_fr="Analytics, ETL, tableaux de bord, scraping.",
        color_from="#0e7490",
        color_to="#1e40af",
        icon="Database",
    ),
]


CATEGORY_IDS: Final[frozenset[str]] = frozenset(c.id for c in _CATEGORIES)


def all_categories() -> list[CategoryOut]:
    """Return the full ordered list of categories."""
    return list(_CATEGORIES)


def is_valid_category(value: str) -> bool:
    return value in CATEGORY_IDS
