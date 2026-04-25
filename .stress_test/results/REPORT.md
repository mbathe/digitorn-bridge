# Compiler Stress Test Report

Stress test run on isolated daemon (port 9876), real LLM (DeepSeek) credentials.

## Resultats

| Phase | Score | Détail |
|---|---|---|
| Compilation | **73/73 (100%)** | 0 faux positif, 0 faux négatif |
| Déploiement | **40/40 (100%)** | Tous les valides compilent ET déploient |
| Chat live | impossible à valider | 2 daemons partageant la même DB se marchent dessus |

## Couverture des features (73 cas)

| Catégorie | Cas | Bons→OK | Mauvais→rejetés |
|---|---|---|---|
| basic app | 4 | 1 | 3 |
| execution.mode | 5 | 4 | 1 |
| workspace_mode | 5 | 4 | 1 |
| session_mode | 3 | 2 | 1 |
| tool_injection | 4 | 3 | 1 |
| context.strategy | 3 | 2 | 1 |
| hooks `on` (YAML bool trap) | 2 | 1 | 1 |
| hooks event typo | 1 | 0 | 1 |
| hooks conditions (9 types) | 9 | 5 | 4 |
| hooks actions (8 types) | 8 | 4 | 4 |
| capabilities.grant cross-ref | 4 | 2 | 2 |
| agents.delegate_to | 3 | 1 | 2 |
| brain provider/backend | 3 | 1 | 2 |
| placeholders vars | 6 | 3 | 3 |
| filters `{{x \| f}}` | 2 | 1 | 1 |
| channels.type | 2 | 1 | 1 |
| middleware | 3 | 2 | 1 |
| modules.X.config (CONFIG_MODEL) | 4 | 2 | 2 |
| trigger.method | 2 | 1 | 1 |

## Bugs trouvés et corrigés pendant le stress

1. **`{{x | filter}}` dans prompts résolus à la compile** — variables.py cherchait `"name | upper"` comme variable. Fix : skip le resolve si `|` présent (runtime template).
2. **`{{input}}`, `{{steps[N]}}` dans pipeline** non reconnus comme namespace réservé. Fix : ajoutés à `_RESERVED_ROOT`.

## Ce qui est prouvé

- Le compilateur catch **tous** les typos testés (field names, enum values, module IDs, credential providers/fields, hook events, conditions/actions param names, channel types, middleware names, trigger methods).
- Zero faux positif : aucun YAML cassé n'est passé à travers.
- Zero faux négatif : tous les YAMLs valides compilent et se déploient.
- Messages d'erreur incluent `file:line:col` + suggestions fuzzy (`Did you mean: X?`).

## Ce qui n'est pas prouvé

- Le chat live avec LLM sur les 40 apps valides (problème infra, pas compilateur).
- Les features marquées "Not yet implemented" dans les docs (flows, macros, expose) sont correctement rejetées par `extra=forbid`.
- Les 13 modules sans `CONFIG_MODEL` acceptent encore tout silencieusement (warning log).

## Fichiers

- `runner.py` : générateur de cas + runner
- `results/results.json` : détails par cas
- `logs/daemon.log` : log du daemon stress

Les apps stress ont été purgées de la DB partagée à la fin.
