"""Scoring engine - tokenizer, synonym expansion, and hybrid search."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from digitorn.modules.context_builder.types import ScoredTool, ToolIndex

_SYNONYM_GROUPS: list[tuple[str, ...]] = [
    ("read", "lire", "cat", "view", "show", "display", "afficher", "voir", "montrer", "consulter"),
    ("write", "ecrire", "save", "create", "creer", "rediger", "sauvegarder"),
    ("delete", "remove", "supprimer", "effacer", "rm", "nettoyer", "clean", "purge"),
    ("list", "ls", "dir", "lister", "enumerate", "explorer", "browse", "parcourir"),
    ("find", "search", "chercher", "trouver", "locate", "rechercher", "scanner"),
    ("copy", "cp", "copier", "duplicate", "dupliquer", "cloner"),
    ("move", "mv", "rename", "deplacer", "renommer"),
    ("edit", "modify", "update", "modifier", "changer", "corriger"),
    ("grep", "match", "filter", "pattern", "filtrer", "contenu", "content"),
    ("query", "select", "fetch", "requete", "database", "base", "donnees", "bdd", "sql", "table"),
    ("insert", "add", "ajouter"),
    ("execute", "run", "exec", "executer", "lancer"),
    ("report", "rapport", "resume", "summary", "export", "exporter"),
    ("help", "aide", "info", "information"),
    ("open", "ouvrir", "load", "charger"),
    ("close", "fermer", "stop", "arreter"),
    ("send", "envoyer", "emit", "push", "transmettre", "expedier"),
    ("get", "obtain", "retrieve", "fetch", "obtenir", "recuperer"),
    ("set", "configure", "config", "definir", "configurer"),
    ("inspect", "examine", "regarder", "verifier", "check", "analyser", "analyze"),
    ("directory", "folder", "dossier", "repertoire", "projet", "project"),
    ("file", "fichier", "document"),
    ("path", "chemin", "location"),
    ("hello", "hi", "greet", "salut", "bonjour"),
    ("data", "donnees", "info", "information", "donnee"),
    ("history", "historical", "historique", "archive", "journal"),
    ("price", "prix", "cout", "valeur", "taux", "tarif", "cours"),
    ("crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth", "monnaie"),
    ("market", "marche", "bourse", "exchange", "trading"),
    ("indicator", "indicateur", "signal", "metric", "mesure"),
    ("sentiment", "opinion", "tendance", "trend"),
    ("symbol", "symbole", "ticker", "asset", "actif"),
    ("feature", "caracteristique", "donnee", "indicateur"),
    ("latest", "dernier", "recent", "actuel", "courant", "current"),
    ("available", "disponible", "accessible", "supporte"),
    ("summary", "resume", "synthese", "overview", "apercu"),
    ("export", "exporter", "telecharger", "download", "extraire"),
    ("range", "intervalle", "periode", "period", "plage"),
    ("category", "categorie", "groupe", "group", "type"),
    ("stats", "statistics", "statistiques", "moyenne", "average"),
]

_SYNONYMS: dict[str, set[str]] = {}

def _build_synonym_map() -> None:
    for group in _SYNONYM_GROUPS:
        normalized = [_normalize(w) for w in group]
        for word in normalized:
            if word not in _SYNONYMS:
                _SYNONYMS[word] = set()
            _SYNONYMS[word].update(normalized)
            _SYNONYMS[word].discard(word)

_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "has", "have", "had", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "and", "or", "not",
    "it", "its", "this", "that", "i", "we", "you", "he", "she",
    "they", "me", "my", "your", "our", "all", "each", "which",
    "but", "if", "as", "so", "no", "can", "will",
    "le", "la", "les", "un", "une", "des", "du", "de", "et",
    "en", "est", "je", "tu", "il", "nous", "vous", "ils",
    "ce", "se", "ne", "pas", "que", "qui", "dans", "pour",
    "sur", "avec", "par", "au", "aux", "son", "sa", "ses",
})

def _normalize(text: str) -> str:
    text = text.lower()
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))

_build_synonym_map()

def tokenize(text: str) -> list[str]:
    """Split text into searchable tokens."""
    normalized = _normalize(text)
    raw_tokens = _SPLIT_RE.split(normalized)
    seen: set[str] = set()
    tokens: list[str] = []
    for t in raw_tokens:
        if len(t) >= 2 and t not in _STOP_WORDS and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens

def tokenize_for_index(text: str) -> list[str]:
    """Like tokenize() but keeps shorter tokens and doesn't remove stop words."""
    normalized = _normalize(text)
    raw_tokens = _SPLIT_RE.split(normalized)
    seen: set[str] = set()
    tokens: list[str] = []
    for t in raw_tokens:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens

def expand_with_synonyms(tokens: list[str]) -> list[str]:
    """Expand query tokens with synonyms. O(t) where t = len(tokens)."""
    expanded: list[str] = list(tokens)
    seen = set(tokens)
    for token in tokens:
        syns = _SYNONYMS.get(token)
        if syns:
            for syn in syns:
                if syn not in seen:
                    seen.add(syn)
                    expanded.append(syn)
    return expanded

def _expand_with_dynamic_synonyms(tokens: list[str], index: ToolIndex) -> list[str]:
    # Start with static synonym expansion
    expanded = expand_with_synonyms(tokens)
    seen = set(expanded)

    # Build a reverse map: alias_token → set of action_name tokens
    # This lets us surface tools when the user queries with an alias
    for tool in index.tools.values():
        if not tool.aliases:
            continue
        alias_tokens_list = [_normalize(a) for a in tool.aliases]
        action_tokens = tokenize_for_index(tool.action_name)
        for at in alias_tokens_list:
            if at in seen:
                # User query matches an alias - add the action name tokens
                for act_tok in action_tokens:
                    if act_tok not in seen:
                        seen.add(act_tok)
                        expanded.append(act_tok)

    return expanded

def search(
    index: ToolIndex,
    query: str,
    max_results: int = 5,
    usage_counts: dict[str, int] | None = None,
    min_relevance: float = 5.0,
) -> list[ScoredTool]:
    """Hybrid search: semantic + keyword scored independently, then merged."""
    from digitorn.modules.context_builder.types import ScoredTool

    if not query or not query.strip():
        return []

    candidates: dict[str, float] = {}

    if index.semantic_index is not None:
        semantic_hits = index.semantic_index.search(
            query, top_k=max_results * 3, min_score=0.20,
        )
        for fqn, sim in semantic_hits:
            candidates[fqn] = sim * 10.0

    query_tokens = tokenize(query)
    if query_tokens:
        # Merge module-declared aliases into synonym expansion
        expanded = _expand_with_dynamic_synonyms(query_tokens, index)
        original_set = set(query_tokens)

        for token in expanded:
            is_original = token in original_set
            weight = 1.0 if is_original else 0.75

            matched_fqns = index.keyword_index.get(token)
            if matched_fqns:
                for fqn in matched_fqns:
                    candidates[fqn] = candidates.get(fqn, 0) + 2.0 * weight

            if len(token) >= 3:
                prefix = token[:3]
                prefix_keywords = index.keyword_prefixes.get(prefix)
                if prefix_keywords:
                    for keyword in prefix_keywords:
                        if keyword.startswith(token) or token.startswith(keyword):
                            matched = index.keyword_index.get(keyword)
                            if matched:
                                for fqn in matched:
                                    candidates[fqn] = candidates.get(fqn, 0) + 1.0 * weight

            cat = index.categories.get(token)
            if cat:
                for fqn in cat.tool_names:
                    candidates[fqn] = candidates.get(fqn, 0) + 1.5 * weight

            tagged = index.tag_index.get(token)
            if tagged:
                for fqn in tagged:
                    candidates[fqn] = candidates.get(fqn, 0) + 1.5 * weight

            fqn_matched = index.fqn_index.get(token)
            if fqn_matched:
                for fqn in fqn_matched:
                    candidates[fqn] = candidates.get(fqn, 0) + 3.0 * weight

    if not candidates:
        return []

    # Usage boost: recently used tools get a score bump
    if usage_counts:
        for fqn in candidates:
            count = usage_counts.get(fqn, 0)
            if count > 0:
                candidates[fqn] += min(count * 0.5, 3.0)

    ranked = sorted(candidates.items(), key=lambda x: -x[1])
    ranked = [(fqn, score) for fqn, score in ranked if score >= min_relevance]
    ranked = ranked[:max_results]

    results: list[ScoredTool] = []
    for fqn, score in ranked:
        tool = index.tools[fqn]
        results.append(ScoredTool(
            fqn=fqn,
            description=tool.description,
            risk_level=tool.risk_level,
            params_summary=_build_params_summary(tool.params_schema),
            relevance=round(score, 2),
            tags=tool.tags,
        ))

    return results

def _build_params_summary(schema: dict) -> str:
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    if not props:
        return "(none)"

    parts: list[str] = []
    for name, prop in props.items():
        json_type = prop.get("type", "any")
        type_map = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
            "array": "list",
            "object": "dict",
        }
        display_type = type_map.get(json_type, json_type)
        opt = "" if name in required else "?"
        parts.append(f"{name}{opt}: {display_type}")

    return ", ".join(parts)
