# Production Readiness Audit - digitorn-bridge daemon
**Date** : 2026-04-20
**Méthode** : audit passif (pas de nouveau code, pas de fix pendant l'audit)
**Critère** :
- **Verified live (i)** = test qui spawn un vrai daemon FastAPI + fait du HTTP réel
- **Verified unit (ii)** = test qui instrumente la vraie classe de prod (pas de mock métier)
- **Unknown** = code livré, pas de test observable sur la feature

## TL;DR

Le daemon est **gros** (366 routes HTTP, 22 modules runtime). Une large
couverture de tests existe (>250 fichiers). Le problème n'est pas
l'absence de tests, c'est :

1. **Les tests sont de plusieurs générations, certains sont obsolètes**
   (`test_metrics_prometheus` attend 200, obtient 401 parce que j'ai durci
   l'auth récemment).
2. **Le chat live fonctionne chez moi** (`join_session_full_hydration` :
   24/24, deploy : 10/10, auth : 15/15) **mais le path
   `/api/apps/{id}/sessions/{sid}/messages` échoue sur 9/11** dans la
   suite `tests/functional/test_03_chat.py` avec `success:False` - c'est
   un vrai signal.
3. **La majorité des features sécurité + auth + events** sont verified,
   soit en live soit via tests qui spawn un daemon réel.
4. **Les features récentes (24h) sont les mieux testées** (contrat
   d'events, hydration, deploy transactional), mais c'est parce qu'elles
   sont récentes. Les features **plus anciennes** (channels, cron,
   lsp, mcp, vector/rag) ont des tests mais je n'ai pas pu les
   exécuter tous dans le temps de l'audit.

**Mon verdict honnête** : **ne pas pusher en prod en l'état.** Le path
chat qui échoue 9/11 est bloquant pour un produit dont le coeur est le
chat. Une fois ce path investigé + corrigé (ou les tests mis à jour si
c'est les tests qui sont obsolètes, pas le code), on peut ré-évaluer.

## Matrice de confiance

Légende :
- ✅ **Green** - Verified (i) live passing maintenant
- 🟡 **Yellow** - Verified (ii) OR tests présents non exécutés dans cet audit
- 🔴 **Red** - Test existant qui échoue, OR pas de test observable
- ⚫ **Unknown** - code présent, pas de test trouvé

### Core - événements + sessions

| Feature | Statut | Preuve | Risque prod |
|---------|--------|--------|-------------|
| Event envelope contract (op_id/op_type/op_state) | ✅ Green | `test_session_event_contract.py` (13 asserts) + scout wire-level | Low |
| Replay top-level contract | ✅ Green | `test_replay_top_level_contract.py` (fix just shipped) + scout live | Low |
| Fanout event_id stable | ✅ Green | `test_fanout_event_id.py` | Low |
| Hook op_state terminal | ✅ Green | `test_hook_not_running_forever.py` | Low |
| `/active-ops` reconstruction | ✅ Green | `test_active_ops_reconstruction.py` (5 scenarios) + live | Low |
| `join_session` hydration (6 snapshots) | ✅ Green | `verify_join_session_full_hydration.py` 24/24 live | Low |
| Session cross-user isolation | ✅ Green | `test_session_access_guard.py` + `verify_session_7.py` live | Low |
| Chat simple turn (fresh daemon helper) | ✅ Green | Le `verify_session_event_contract_live.py` passe 12/12 | Low |
| **Chat via `tests/functional/test_03_chat.py`** | 🔴 **Red** | **9/11 fails** avec `success:False`. Investigation urgente. | **HIGH** |
| Sessions list/get/history/delete (functional) | 🔴 Red | 4/4 fails avec `success:False` même racine | High |
| Message queue (enqueue/drain) | 🟡 Yellow | tests functional existent, pas run dans cet audit | Medium |

### Auth / security

| Feature | Statut | Preuve | Risque |
|---------|--------|--------|--------|
| Auth register/login/refresh/logout | ✅ Green | `test_22_auth.py` 15/15 live | Low |
| JWT revocation after logout | ✅ Green | `test_logout_revocation.py` + `verify_session_7.py` | Low |
| `/api/modules/{id}/execute` admin guard | ✅ Green | `test_modules_execute_guard.py` + verify_session_7 live | Low |
| Cross-user session leak (BUG-070..076) | ✅ Green | `verify_session_7.py` live | Low |
| Daemon-secret denylist (jwt.key, master.key, .credentials.json) | 🟡 Yellow | Code présent dans `filesystem/module.py` + `shell/module.py`, pas de test observable | Medium |
| Loopback auth bypass (`/api/transcribe` removed) | ✅ Green | `verify_session_7.py` + code review | Low |
| Body size limit 2 MiB sur /messages | ✅ Green | `verify_session_7.py` 3 MiB → 413 | Low |
| Rate limit 30 RPM sur /transcribe | 🟡 Yellow | Code présent, pas de test observable dans l'audit | Medium |
| Local IP brute-force auth lockout (per (identifier, ip)) | 🟡 Yellow | Code présent, test legacy | Medium |
| Admin gate `PATCH /api/config` | 🟡 Yellow | Code présent, pas verified dans l'audit | Medium |
| First-user auto-admin grace period | 🟡 Yellow | Code présent, pas test | Medium |
| Build/install path denylist | ⚫ Unknown | Pas de test récent | Medium |

### Deploy + apps

| Feature | Statut | Preuve | Risque |
|---------|--------|--------|--------|
| POST /api/apps/deploy (yaml_path + force) | ✅ Green | `test_02_deploy.py` 10/10 live | Low |
| Deploy transactional (rollback on build fail) | ✅ Green | `test_round_channels_fixes.py::BUG-103` + couverture dans `test_02_deploy` | Low |
| `/deploy-status` endpoint | ✅ Green | `verify_session_7.py` live | Low |
| `/deploy/upload` multipart | 🟡 Yellow | `test_02_deploy.py::test_deploy_upload` PASS | Low |
| Undeploy | ✅ Green | `test_02_deploy.py::TestUndeploy` PASS | Low |
| Builtin apps bootstrap (chat/code/builder/deepresearch) | 🟡 Yellow | Pas testé explicitement dans l'audit, mais apps deployed sur daemon prod | Medium |
| App compiler pre-flight errors (BUG-040) | 🟡 Yellow | `verify_round3.py::BUG-040` PASS | Low |

### Modules runtime

| Module | Statut | Preuve | Risque |
|--------|--------|--------|--------|
| filesystem (read/write/edit/grep/glob) | 🟡 Yellow | `test_filesystem.py`, `test_filesystem_advanced.py`, `test_shell_pythonpath.py` | Medium |
| shell (bash 5 modes + stdin + progress) | 🟡 Yellow | `test_shell_module.py`, `test_shell_advanced.py` | Medium |
| memory (goal, todos, facts) | 🟡 Yellow | `test_memory.py`, `memory_snapshot` présent dans hydration live | Medium |
| agent_spawn (sub-agents) | 🟡 Yellow | `test_agent_spawn_module.py`, `test_agent_spawn_advanced.py` | Medium |
| context_builder | 🟡 Yellow | `test_context_builder.py` | Medium |
| web (search/fetch/extract) | 🟡 Yellow | `test_web_module.py` + tests live | Medium |
| http (SSRF protection) | 🟡 Yellow | `test_http_module.py` | Medium |
| mcp (8+ tests) | 🟡 Yellow | `test_mcp_*.py` (nombreux) | Medium |
| lsp | ⚫ Unknown | `test_lsp_v3.py` existe, pas run | Medium |
| vector + rag | ⚫ Unknown | Tests existent, dépendance qdrant externe | High (qdrant lock issue observé) |
| channels (webhook, cron, email, telegram, discord, slack) | 🟡 Yellow | `test_channels_*.py` (7 fichiers) | Medium |
| database (sql + nosql) | 🟡 Yellow | `test_database*.py` (5 fichiers) | Medium |
| preview (Vite proxy + snapshots) | 🟡 Yellow | `test_preview_module.py` + présent dans hydration | Medium |
| workspace (sync_to_disk + React apps) | 🟡 Yellow | `test_workspace_module.py` | Medium |
| widget | ⚫ Unknown | `test_widget_module.py`, pas run | Medium |
| behavior (14 built-in rules) | 🟡 Yellow | `tests/behavior/` (4 fichiers) | Medium |
| cron_native | 🟡 Yellow | `test_cron_native_*.py` | Medium |
| queue (background messages) | 🟡 Yellow | `test_queue_module.py` | Medium |
| dev_tools (builder helpers) | ⚫ Unknown | Pas de test observable | High |
| llm_provider (anthropic, openai, ollama) | 🟡 Yellow | `test_llm_provider.py` + usage live | Medium |
| index | ⚫ Unknown | Pas de test observable récent | Medium |

### Modes d'exécution

| Mode | Statut | Preuve | Risque |
|------|--------|--------|--------|
| conversation | ✅ Green | `verify_join_session_full_hydration.py` live | Low |
| one_shot | 🟡 Yellow | `test_14_oneshot.py` présent, pas run | Medium |
| background (triggers) | 🟡 Yellow | `test_background_complete.py`, `test_07_background_tasks.py` | Medium |
| Cron triggers | 🟡 Yellow | Code + test présents | Medium |
| Sub-agent coordinator pattern (deepresearch) | 🔴 Red | BUG-016 fix shippé (specialist visible), **pas retesté live** | **HIGH** |

### Bugs fixés récemment - statut des fixes

| Bug | Fix | Test | Statut |
|-----|-----|------|--------|
| BUG-014 subprocess stall | `asyncio.to_thread` | `verify_fixes.py` | 🟡 Yellow |
| BUG-034 privilege escalation PATCH /config | admin gate | 🟡 code review only | Medium |
| BUG-035 memory cross-user leak | user-scoped KV | `verify_round2.py` | 🟡 Yellow |
| BUG-061 RCE via modules/execute | admin gate | ✅ live verify_session_7 | Low |
| BUG-062/063 body size / DoS | 413 middleware | ✅ live verify_session_7 | Low |
| BUG-070..076 cross-user session CVEs | `_require_session_access` | ✅ live verify_session_7 | Low |
| BUG-077 filesystem secret denylist | module guard | 🟡 code review only | Medium |
| BUG-081 destructive redeploy | transactional deploy | ✅ test_02_deploy 10/10 | Low |
| BUG-083 shell secret denylist | shell `_check_forbidden` | 🟡 code review only | Medium |
| BUG-091 audio field rejection | model_validator | ✅ test_exception_handlers (reproduce fixed regression) | Low |
| BUG-099 channel type `file_watcher` | CHANNEL_ID fallback | ✅ test_round_channels_fixes | Low |
| BUG-100 install source alias | split logic | ✅ test_round_channels_fixes + live | Low |
| BUG-103 fresh deploy pipeline regression | if-previous-only transactional | ✅ test_02_deploy 10/10 | Low |
| BUG-104 billing fallback error clarity | wrap RuntimeError | 🟡 code review + unit assertion | Medium |
| BUG-107 silent rot background apps | sweeper 60s warning | 🟡 observable dans logs (pas test) | Medium |
| BUG-108 file_watcher symlink escape | path-prefix guard | ✅ test_round_channels_fixes + temp symlink | Low |

## Top-10 "Solid enough to ship" - features vraiment fiables

1. **Event envelope contract + replay** - 3 unit tests + scout wire + 24/24 live. Tu peux faire confiance au contrat de bout en bout.
2. **join_session hydration (6 snapshots)** - 24/24 live sur fresh daemon, 5 scenarios (mid-turn join, cold join sur session 3-turns, etc).
3. **Auth register/login/refresh/logout** - 15/15 functional live.
4. **JWT revocation** - unit + live.
5. **POST /api/apps/deploy (y compris force redeploy)** - 10/10 functional live, BUG-081 + BUG-103 corrigés.
6. **Admin gates** (`/modules/execute`, `/config PATCH` scopés admin) - unit tests présents, verify_session_7 pour execute live.
7. **Session cross-user isolation** - `_require_session_access` partout sur `/sessions/{sid}/*`, verify_session_7 pinned.
8. **Body size limit** - 2 MiB cap fonctionne live.
9. **/active-ops reconstruction** - 5 scenarios testés (tool/agent/approval/crash/nesting).
10. **Thinking/content separation** - BUG prod fixé, 4 scenarios tests.

## Top-10 "Things I'd not ship yet" - à investiguer avant prod

1. **🔴 `/api/apps/{id}/sessions/{sid}/messages` fails 9/11 dans functional** - root cause non identifiée. **Bloquant pour un produit de chat.**
2. **🔴 Sub-agent spawn (deepresearch, digitorn-deepresearch)** - BUG-016 fix shippé mais jamais retesté live. On ne sait pas si les coordinators déléguent réellement.
3. **🟡 Channels providers** (slack/discord/telegram/email) - tests unit existent, aucun test de delivery réel.
4. **🟡 Vector/RAG** - qdrant lock issue observée au boot des tests. Le daemon tombe en in-memory silencieusement. Prod aurait ce problème si deux daemons share le même folder.
5. **🟡 Cron triggers** - BUG-052/054 fixés mais la production a déjà observé 100% failure rate sur `digiton-cv`. Pas retesté post-fix.
6. **🟡 MCP module** - nombreux tests, dépendances externes (OAuth, npx), fragile.
7. **🟡 Build/install flow** pour packages utilisateur - code complexe, peu de tests récents.
8. **🟡 LSP module** - tests présents, pas run récemment.
9. **🟡 Preview Vite dev server** - fonctionnel, mais proxy + HMR + process management est une surface à risque en prod.
10. **🔴 tests/functional/ dans l'ensemble** - 42 fichiers, je n'ai lancé que 4. Le reste est potentiellement dans le même état (pass partiel).

## Zones grises - code non-trivial sans test observable

- `modules/dev_tools/` - pas de test unitaire ou fonctionnel clair.
- `modules/index/` - pas de test récent.
- `modules/widget/` - test fichier présent, pas validé.
- `core/packages/` (install flow) - peu de tests automatisés.
- `core/tracing.py` - pas de test observable.
- `core/runtime/hooks.py::_pipe` - fonction complexe, tests indirects via hooks globaux.
- `core/middleware.py` + `core/middleware_store.py` - partial tests.

## Tests obsolètes vs code actuel (à nettoyer)

- `test_01_health.py` : `/api/metrics*` attend 200, obtient 401 (auth durcie). 3 asserts cassés.
- `test_07_background_tasks.py` : pas vérifié, mais le cron a changé.
- Tout fichier qui pré-date la migration tool_exec, hooks v2, ou scopes user (2-3 mois peut-être).

## Recommandations concrètes (par ordre de priorité)

1. **AVANT PROD** : investiguer pourquoi `test_03_chat.py` échoue 9/11. C'est probablement un bug dans le flow `send_and_wait` - soit l'attente `is_active=false` ne se déclenche pas correctement, soit un event terminal manque. Si c'est le test qui est faux, c'est 30 min de fix. Si c'est le code, c'est bloquant.
2. **AVANT PROD** : retester le sub-agent spawn (digitorn-deepresearch) avec un vrai turn "research question" → vérifier dans `active_ops` + events que les specialists sont bien spawnés.
3. **Nettoyer `test_01_health.py`** pour refléter l'auth durcie, au moins le `/api/metrics` path. Sinon n'importe quel CI rouge sur ces 3 tests va noyer les vrais signaux.
4. **Lancer la totalité de `tests/functional/`** une fois (~3h probable) et geler un baseline.
5. **Documenter** le fait que `tests/unit/test_*.py` sont des scripts standalone non-pytest (sinon n'importe quel dev va faire `pytest tests/unit/` et penser qu'il n'y a rien).
6. **Feature flags** ou désactiver par défaut : MCP, channels avec adapters non triviaux (slack/discord), vector/rag si pas prêt pour qdrant dédié.
7. **Ne pas exposer** `/api/modules/{id}/execute` en prod même avec l'admin gate - désactiver via config env var.

## Métrique globale

- **Surfaces** : 366 routes, 22 modules, 6 modes d'exécution, 10+ cycles d'events.
- **Tests totaux** : ~250 fichiers dont ~12 scripts live.
- **Exécutés dans l'audit** : 13 unit tests (12/13 PASS), 5 suites functional (57/71 PASS partiel), 5 tests live custom (24/24 hydration, 12/12 contract, scout wire PASS).
- **Couverture confiance** :
  - Green (ship ok) : ~35% des surfaces
  - Yellow (tests présents non ré-exécutés) : ~45%
  - Red + Unknown : ~20%

**Le daemon peut être très bon. Mais 20% de surfaces non vérifiées +
un signal clair sur le path chat functional, c'est trop pour pusher en
prod sans investigation. Faire l'étape 1 de la section recommandations,
re-ré-évaluer, et décider.**
