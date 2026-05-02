# Phase 1 Audit — état du compiler et plan d'action

**Date** : 2026-05-01
**Objectif** : amener le compiler au niveau **"si ça compile, ça tourne"**.
**Statut** : audit pour validation, AUCUNE modification de code à ce stade.

---

## 1. Résumé exécutif

Le compiler actuel est **déjà très avancé**. 149 points d'erreur dans `compiler.py` (3230 lignes). Pydantic + extra-forbid sur 44 modèles. Pre-flight pour les hallucinations LLM. Validation des références croisées. Suggestions fuzzy-match sur les typos.

**Ce qui est déjà solide :**
- Schema-level (Pydantic + extra: forbid partout)
- Module existence + per-module CONFIG_MODEL
- Cross-references (agent_id, capabilities.module, default_channel)
- Variable resolution + placeholder validation (incluant `{{credential.PROVIDER.FIELD}}`)
- Mode-specific gates (background sans triggers ⇒ erreur)
- Trigger types et required fields par type
- Setup steps params validés contre l'action's params_model

**Ce qui manque pour la garantie "if it compiles, it runs"** : 16 gaps identifiés ci-dessous, regroupés en 6 catégories.

Phase 1 ferme **9 de ces 16 gaps** sans aucun breaking change.

---

## 2. Inventaire des 24 validations actuelles

| # | Validation | Lieu | Force |
|---|---|---|---|
| 1 | Pydantic validation de `AppDefinition` (extra: forbid) | schema.py | Forte |
| 2 | Pre-flight LLM hallucinations (top-level `name:`, `modules:` en liste, `model:` au lieu de `brain:`, etc.) | compiler.py:1006-1058 | Forte |
| 3 | Variable resolution + placeholder syntax | variables.py + compiler.py:104-220 | Forte |
| 4 | Filtres de placeholder (`{{x \| filter}}`) contre 38 connus | compiler.py:75-89 | Forte |
| 5 | Module existence dans le registry | compiler.py:1274-1283 | Forte |
| 6 | Per-module CONFIG_MODEL validation | compiler.py:1295-1314 | Forte |
| 7 | Capabilities.grant/deny/approve module + actions | compiler.py:2052-2117 | Forte |
| 8 | Agent ID uniqueness | compiler.py:2150-2153 | Forte |
| 9 | Coordinator delegate_to references existing agents | compiler.py:245-255 | Forte |
| 10 | Pool config keys + types | compiler.py:2161-2179 | Forte |
| 11 | Default_channel reference (built-in ou déclaré) | compiler.py:274-299 | Forte |
| 12 | Hook module_action target | compiler.py:301-320 | Forte |
| 13 | Middleware names (built-in ou custom:) | compiler.py:322-356 | Forte |
| 14 | Execution mode validation | compiler.py:2434-2439 | Forte |
| 15 | Entry agent reference | compiler.py:2441-2450 | Forte mais retombe sur "premier agent" si vide |
| 16 | Background ⇒ triggers (ou channels module) | compiler.py:2456-2461 | Forte |
| 17 | Trigger type + required fields per type | compiler.py:2462-2489 | Forte |
| 18 | Trigger routing + routing_key | compiler.py:2481-2489 | Forte |
| 19 | Input/output types | compiler.py:2507-2519 | Forte |
| 20 | Context strategy + compression_trigger range | compiler.py:2544-2554 | Forte |
| 21 | Credentials schema validation | compiler.py:2604-2700 | Forte |
| 22 | Payload schema validation | compiler.py:2702-2760 | Forte |
| 23 | `{{credential.PROVIDER.FIELD}}` contre schéma déclaré | compiler.py:163-202 | Forte |
| 24 | Skill command/path required | compiler.py:1466-1469 | Forte |
| 25 | Setup steps params contre action's params_model | compiler.py:1338-1346 | Forte |

---

## 3. Gaps identifiés vs "if it compiles, it runs"

### Catégorie A — Validation Brain/Provider (3 gaps)

**A1. Modèle non validé contre la liste réelle du provider**
- Aujourd'hui : `brain.model: "claude-haiku-4-5-INVALID"` est accepté, runtime explose en 404 du provider.
- Pour fixer : hooks dans le provider module qui exposent `list_models()`. Compiler peut alors faire un best-effort fuzzy match pendant le compile.
- Note : c'est un best-effort, pas un dur. Les modèles changent vite et un cache offline est rapidement obsolète.

**A2. Provider config required fields — auth conditionnelle au type de provider**

Tous les providers ne sont **PAS** égaux face à l'authentification. La distinction existe déjà dans le code :
- `openai_compat.py:208-210` expose `_is_local_provider(hint)` qui retourne True pour `{ollama, lm_studio, vllm}`.

Règle correcte :

| Type de provider | Exemples | Credential requis ? |
|---|---|---|
| Local (auth-free) | `ollama`, `lm_studio`, `vllm` | **Non**. Le brain peut être `{provider: ollama, model: llama3.2}` tout court. |
| Cloud (api_key requis) | `anthropic`, `openai`, `deepseek`, `groq`, `mistral`, `together`, `gemini`, `cerebras`, `perplexity`, `fireworks`, `xai` | **Oui**, sauf override (voir ci-dessous) |
| Sentinel spécial | `anthropic` + `config.api_key: "claude-code"` | **Non**, le provider lit le token OAuth de `~/.claude/.credentials.json` |
| Override base_url | `provider: openai` + `config.base_url: http://localhost:*` | **Non**, on parle à un endpoint local via le wire format OpenAI |

Aujourd'hui : `brain: { provider: anthropic, model: claude-haiku-4-5 }` (cloud sans credential, sans api_key, sans provider_id) compile silencieusement et explose au runtime en 401.

Pour fixer, le validator vérifie :
1. Si provider ∈ `_LOCAL_PROVIDERS` (ollama/lm_studio/vllm) → aucune validation de credential
2. Si `config.api_key == "claude-code"` ET provider == anthropic → OK (sentinel)
3. Si `config.base_url` est explicitement localhost ou 127.0.0.1 → OK (assumé local)
4. Sinon → exiger au moins UN de `{credential, provider_id, config.api_key (non vide)}`

Sinon erreur compile avec message :
```
agents[0].brain: provider 'openai' requires authentication. Add one of:
  credential: openai_main           # recommended, references the vault
  provider_id: openai_main          # references modules.llm_provider.providers
  config: { api_key: "{{env.OPENAI_API_KEY}}" }    # inline, dev-only
For local providers (ollama, lm_studio, vllm), no credential is needed.
```

**A3. Vision capability vs model**
- Aujourd'hui : `brain.vision: true` sur un modèle non-vision (deepseek-chat) compile.
- Pour fixer : Pydantic field_validator dans AgentBrain + table provider→known_vision_models.

### Catégorie B — Validation Tool References (3 gaps)

**B1. Hook condition `tool_name.match` non validé**
- Aujourd'hui : `condition: { type: tool_name, match: "web.serch" }` (typo) compile, hook ne fire jamais en prod.
- Pour fixer : compile-time, collecter la liste des tools disponibles (modules × actions × short_names). Valider que le regex `match` matche au moins 1 tool. Sinon erreur avec suggestions.

**B2. Behavior rule `trigger:` non validé**
- Aujourd'hui : custom rule avec `trigger: "fileSystem.write"` (typo) compile.
- Pour fixer : même validation que B1 contre la liste agrégée des tools.

**B3. Hook expressions `expr:` non parsées**
- Aujourd'hui : `expr: "session.consecutive_failures.web_fetch >= 3"` est juste une string. Faute de frappe = silencieux.
- Pour fixer : parser d'expression au compile (lib comme `simpleeval` ou un mini-AST). Vérifier que les références (`session.X`, `tool.X`) résolvent contre un schéma de contexte runtime documenté.

### Catégorie C — Cross-references (4 gaps)

**C1. `agents[].modules: list[Any]` non validé**
- Aujourd'hui : `modules: [{ filsystem: [read] }]` (typo) compile parce que `Any` accepte tout.
- Pour fixer : typer en `list[str | dict[str, list[str]]]`, valider chaque entrée contre les modules déclarés.

**C2. `agents[].capabilities: [skill_name]` non validé contre fichiers**
- Aujourd'hui : `capabilities: [refund_runbook]` compile. Si `skills/refund_runbook.md` n'existe pas, runtime warning silencieux.
- Pour fixer : compiler liste les `.md` dans `skills/` et valide chaque nom.

**C3. `agents[].skills: ./path.md` (chemin) non vérifié au compile**
- Aujourd'hui : si le fichier n'existe pas, le compiler lit, échoue à runtime.
- Pour fixer : `Path.exists()` au compile.

**C4. Credential references non checkées contre schéma déclaré**
- Aujourd'hui : `brain.credential: anthropic_main` compile même si `credentials_schema` ne déclare pas `anthropic_main`.
- Pour fixer : si `dependencies.credentials.schema` est déclaré, toute `credential:` ref doit y exister. Sinon warning.

### Catégorie D — Mode Coherence (2 gaps)

**D1. Triggers en mode non-background = warning silencieux**
- Aujourd'hui : `compiler.py:2503-2505` dit "warning emitted in compile()" mais aucun emit n'est fait.
- Pour fixer : émettre un vrai warning visible dans la sortie compile, ou mieux : refuser compilation en strict mode.

**D2. compact_context hook + mode one_shot**
- Aujourd'hui : compile, runtime ne fait rien (pas de turns à compacter).
- Pour fixer : compile-time warning.

### Catégorie E — Agent capability coherence (2 gaps)

**E1. Agent qui call `Agent` tool sans `agent_spawn` chargé**
- Aujourd'hui : si le coordinator a `modules: [{agent_spawn: [Agent]}]` mais `agent_spawn` n'est PAS dans le top-level `modules:`, runtime erreur.
- Pour fixer : compile-time, valider que tout module utilisé dans `agents[].modules:` existe au top-level (sauf "system modules" auto-chargés).

**E2. Hook référence un module non chargé via templating**
- Aujourd'hui : `action.module: rag` compile si `rag` est listé. Mais `condition.expr: "rag.kbs.X"` ne valide pas que rag est chargé.
- Pour fixer : parser d'expression (cf B3) résout les références.

### Catégorie F — Resource bounds (2 gaps)

**F1. `max_turns: 0` ou négatif accepté**
- Aujourd'hui : `max_turns: 0` compile, runtime se termine immédiatement.
- Pour fixer : Pydantic `Field(ge=1)` sur `max_turns` et `timeout`.

**F2. `session_mode: multi` + `max_sessions_per_user: 0`**
- Aujourd'hui : `0 = unlimited`. Ambigu.
- Pour fixer : documenter explicitement et/ou imposer `≥ 1` si une limite est désirée.

---

## 4. Plan Phase 1 — 9 gaps fermés, zéro breaking

### Gaps fermés en Phase 1

| Gap | Ce que je change | Risque |
|---|---|---|
| A2 | `AgentBrain` ajoute un model_validator qui exige au moins 1 source de credential | Aucun (compatible) |
| A3 | `AgentBrain.vision` field_validator vérifie modèle | Aucun |
| B1 | `_validate_hook_tool_refs` : nouvelle pass sur les hooks, collecte tools de tous les modules, valide chaque `tool_name.match` | Aucun (validation purement additive) |
| B2 | Étendu à `behavior.rules[].trigger` | Aucun |
| C1 | Typer `agents[].modules: list[ModuleScope]` avec `ModuleScope = str \| dict[str, list[str]]`. Pydantic accepte les deux formes | **Aucun si testé** : ce que les YAML actuels écrivent est strictement compatible |
| C3 | Vérifier `Path.exists()` pour chaque `agents[].skills:` quand non vide | Aucun |
| C4 | Si `credentials_schema` déclaré, valider chaque `credential: foo` ref | Warning au début, erreur après deprecation |
| D1 | Vrai warning visible quand `triggers:` sans `mode: background` | Aucun |
| F1 | `Field(ge=1)` sur `max_turns`, `Field(gt=0)` sur `timeout` | Aucun (les YAML actuels sont conformes) |

### Gaps reportés

- **A1** (model name validation) → Phase 2 ou 3, demande un système de cache provider.
- **B3** (expressions parser) → Phase 2, partagé avec le `flow:` block (qui parsera les `when:` de la même façon).
- **C2** (skill name validation) → Phase 1 si on a le temps, sinon Phase 2.
- **D2** (compact_context + one_shot) → Phase 1 dans le validator externe (déjà fait), reporté côté compiler en Phase 2.
- **E1, E2** (module dependency from agents/hooks) → Phase 2, demande introspection plus poussée.
- **F2** (session bounds clarity) → doc-only.

### Travail concret

**Fichiers touchés en Phase 1 :**

1. `packages/digitorn/core/app/schema.py`
   - Type `agents[].modules: list[Any]` → `list[ModuleScope]` avec union typée
   - Type `AgentDefinition.pool: dict[str, Any]` → `AgentPoolConfig` modèle
   - `Field(ge=1)` sur `max_turns`, `Field(gt=0)` sur `timeout`
   - `AgentBrain.vision` validator
   - `AgentBrain` model_validator qui exige au moins 1 source de credential
   - Typer 8 autres `dict[str, Any]` listés section 5 du schema-redesign proposal

2. `packages/digitorn/core/app/compiler.py`
   - Nouvelle fonction `_validate_hook_tool_refs(definition, registry, errors)` (B1+B2)
   - Nouvelle fonction `_validate_skill_files(definition, source_dir, errors)` (C3)
   - Nouvelle fonction `_validate_credential_refs(definition, errors)` (C4)
   - Émettre le warning manquant trigger-without-background (D1)

3. **Pas de changement** sur :
   - `agent_loop.py`, `app.py`, `runtime/*` (le runtime ne change pas)
   - Builtins app.yaml (pas de syntaxe nouvelle requise)
   - Le canvas web (`yaml-to-graph.ts`) (pas de top-level changé)

### Tests ajoutés (TDD : tests d'abord, code après)

Dans `tests/module/test_compiler_validation.py` (nouveau fichier) :

- `test_hook_tool_name_typo_caught` : un YAML avec `condition: { type: tool_name, match: "web.serch" }` doit faire échouer le compile avec suggestion.
- `test_behavior_rule_trigger_typo_caught` : idem pour behavior.
- `test_skill_file_missing_caught` : `agents[0].skills: ./missing.md` fait échouer le compile.
- `test_credential_ref_undeclared_caught` : `brain.credential: undeclared` fait warning si credentials_schema est déclaré.
- `test_max_turns_zero_rejected` : `execution.max_turns: 0` fait échouer le compile.
- `test_brain_vision_on_non_vision_model_caught` : `brain.vision: true` sur deepseek-chat fait erreur.
- `test_agent_modules_typo_caught` : `modules: [{ filsystem: [read] }]` (typo) fait erreur.
- `test_agent_brain_no_credential_source_caught` : `brain: { provider: anthropic, model: ... }` (rien d'autre) fait erreur.
- `test_existing_builtin_apps_compile` : compile chacun des 6 builtins, doit passer.
- `test_existing_web_yamls_compile` : les 28 YAML du site doivent passer.

### Gates de validation avant qu'on déclare Phase 1 finie

- **Gate A** : `tools/validate_web_yaml.py` reste 28/28 vert ✓
- **Gate B** : `pytest tests/module/ tests/test_security_advanced.py tests/test_app_agents.py tests/module/test_compiler_validation.py -x` passe à 100%
- **Gate C** : `digitorn package validate <dir>` sur les 6 builtins passe sans warnings non-attendus
- **Gate D** : Le canvas Builder (web/dist) charge un app et rend correctement (smoke test manuel sur digitorn-builder lui-même)

---

## 5. Ce qui n'est PAS dans Phase 1

| Non fait | Raison | Quand |
|---|---|---|
| Bloc `flow:` runtime | Feature à implémenter demain par le user | Phase 2 |
| Discriminated union sur `runtime.mode` | Demande de refactor Pydantic complexe ; peut casser des accès `definition.execution.input` qui marchent aujourd'hui même en mode non-one_shot | Phase 2 ou 3 |
| Renommer `execution:` → `runtime:` | Breaking, deprecation cycle nécessaire | Phase 3 |
| Renommer `app_id` → `id` | User a dit non | Jamais |
| Regrouper UI sous `ui:` | Breaking, deprecation cycle nécessaire | Phase 3 |
| Bloc `dependencies:` top-level | Pas urgent, gain UX | Phase 3 |
| Bloc `include:` (fragmentation) | Demande extra du user pour Phase 1, à inclure si je peux le faire sans casser quoi que ce soit | À évaluer |

---

## 6. Question pour validation finale

**Q1.** L'inventaire des 25 validations actuelles + 16 gaps + 9 fermés en Phase 1 est-il fidèle à ton mental model ? Si tu vois un gap que j'ai raté, dis-le maintenant.

**Q2.** Tu valides la liste des 9 gaps que je ferme en Phase 1, et la liste de ceux reportés ?

**Q3.** Tu valides les 4 gates (A/B/C/D) comme critères d'acceptance avant de déclarer Phase 1 finie ?

**Q4.** Tu veux que j'inclue le bloc `include:` (fragmentation) dans Phase 1, ou on le garde pour Phase 2 avec le `flow:` ?

Une fois ces 4 validations, je commence par écrire les **tests** (TDD), puis le code. Aucun caractère de schema modifié avant ça.
