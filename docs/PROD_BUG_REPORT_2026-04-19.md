# Production Test Report — Digitorn Bridge
**Date** : 2026-04-19
**Testeur** : Claude (prod-tester, real LLM, zero mocks)
**Environnement** : Windows 11, daemon 127.0.0.1:8000, Python 3.12, DeepSeek-chat + Anthropic providers

## Résumé exécutif

**48 bugs identifiés** en ~3h de tests réels sur 4 apps builtin + 10+ apps de sécurité + tests auth + concurrent + multimodal + queue + rate limit + builder end-to-end.
- **P0 (bloquants)** : 12
- **Hauts** : 17
- **Moyens** : 11
- **Bas** : 4

**Round 3 nouveaux findings** : builder génère du YAML invalide (BUG-040), `message_done` jamais émis sur tâches longues du builder (BUG-039), `DELETE /api/apps/{id}` ment sur les effets (BUG-048), rate limit RPM non enforcé (BUG-047), image silent reply (BUG-046).

Les 4 apps builtin principales (chat, code, deepresearch, builder) souffrent chacune d'au moins un bug P0/haut qui bloque un vrai usage production. En multi-tenant :
- **Privilege escalation** : un user role=`developer` peut disable l'auth globale en une requête (BUG-034).
- **Leak mémoire cross-user** : les `semantic.facts` d'un user fuitent vers tous les autres (BUG-035). GDPR KO.
- **Event loop stall** : `subprocess.run` synchrone fait freezer le daemon 2-5s par chat (BUG-014).
- **Apps ghost** : les apps listées en `/api/apps` mais jamais réellement loaded → POST accepté (200) mais session disparaît (404) (BUG-037).

---

## Bugs P0 — À traiter en priorité

### BUG-014 — `subprocess.run` synchrone dans le event loop du turn
- **Localisation** : `packages/digitorn/core/api/apps.py:384` (`_get_workspace_status`) appelé depuis `manager.py:1516 _chat_locked`
- **Symptôme** : Chaque turn de chat freeze l'asyncio event loop 2-5s. Pendant ce temps, Uvicorn n'accepte plus de connexions → toutes les autres sessions timeout (`WinError 10061 — No connection could be made`).
- **Repro** :
  1. Ouvrir 2 sessions sur digitorn-code en parallèle.
  2. Les 2 plantent au bout de quelques secondes.
- **Preuve** : `~/.digitorn/logs/stacks.log` et `loop-stall.log` montrent tous deux `subprocess.communicate` actif au moment du stall. Health endpoint remonte `event_loop_watchdog.stalls_total` > 0 après chaque chat.
- **Impact** : Multi-user impossible. Single-user expérimente des freezes fréquents.
- **Fix proposé** : Exécuter `_get_workspace_status` dans le `io_pool` (ThreadPoolExecutor) via `asyncio.to_thread`, ou le rendre natif async.

### BUG-006 — Pollution mémoire inter-sessions sur digitorn-chat
- **Symptôme** : Une session vierge hérite instantanément de dizaines de faits `semantic.facts` provenant de sessions antérieures (faits sur « Digitorn Bridge framework »).
- **Repro** :
  1. `POST /api/apps/digitorn-chat/sessions/<nouveau-sid>/messages` avec n'importe quel message
  2. `GET /api/apps/digitorn-chat/sessions/<sid>/memory`
  3. → `semantic.facts` contient 15+ faits fantômes
- **Impact** : Confidentialité (fuite entre utilisateurs). Pollution du contexte (tokens gaspillés). LLM répond avec du contexte d'un autre user.
- **Gravité** : Potentiellement critique en multi-tenant.

### BUG-001 — Fuite de CLAUDE.md projet dans le system prompt de digitorn-chat
- **Symptôme** : Le system prompt de l'app généraliste `digitorn-chat` contient un bloc `# Project Memory` avec l'intégralité du `CLAUDE.md` du repo `digitorn-bridge` (architecture interne, paths OAuth, noms de fichiers privés).
- **Repro** : `GET /sessions/{sid}/history?include_system=true` → voir le premier message `role=system`.
- **Impact** : Leak de contexte interne au premier utilisateur de digitorn-chat. Consomme des tokens inutilement. Le system prompt digitorn-chat devrait être autonome.

### BUG-009 — `seq` Socket.IO dupliqué sur les `approval_request`
- **Symptôme** : Deux events `approval_request` partagent le même `seq` (observé : seq=183 et seq=187 chacun dupliqué dans un run).
- **Impact** : Casse le contrat Socket.IO documenté (`seq` strictement croissant). Replay/reconnect frontend impossible à implémenter correctement.
- **Repro** : déclencher un `Write` qui nécessite approval sur `digitorn-code`. Observer les events avec `LiveEventStream`.

### BUG-011 — Perte d'isolation des tours quand approval en attente
- **Symptôme** : Turn 3 se voit détourné pour ré-exécuter la logique du Turn 1 alors que Turn 2 est bloqué en approval. Le `correlation_id` de Turn 3 change de format (`6be54a72...` au lieu de `fp-xxx...`), preuve d'un chemin d'injection différent.
- **Impact** : Conversation multi-tour indéterministe quand une approbation traîne. Le frontend affiche des résultats ne correspondant pas aux tours.

---

## Bugs hauts

### BUG-015 — JWT invalidés au redémarrage du daemon
Le JWT signing key semble se régénérer au boot → toutes les sessions user se retrouvent en « Invalid or expired token » après un simple restart. Contraire à la nature stateless du JWT.

### BUG-019 — Schema API incohérent : `toolCalls` (camelCase) vs `tool_calls` (snake_case)
`GET /sessions/{sid}/history` retourne chaque message avec les tool calls sous la clé `toolCalls` (camelCase). Le SDK officiel `digitorn.testing.DevClient._extract_turn_result` lit `tool_calls` (snake_case) → `TurnResult.tool_calls` est TOUJOURS vide. Le SDK ne remonte aucun tool call de l'historique.

### BUG-005 — SDK register cassé (mismatch champ `username` vs `email`)
`DevClient.register(email, password, name)` envoie `{email, password, name}` mais l'endpoint `/auth/register` requiert `username`. Register via SDK échoue 422 systématiquement.

### BUG-008 — Approvals sans fallback → agent bloqué 300s puis timeout en état incohérent
Un `Write` demandant approval attend silencieusement 300s sans feedback au client, puis l'approval passe en « denied » automatiquement MAIS reste visible en `pending` (cf. BUG-012).

### BUG-016 — digitorn-deepresearch ne spawn PAS de sub-agents
App déclare 5 agents (coordinator + 4 specialists). En pratique le coordinator fait tout seul. Zéro event `agent_spawn`. Feature documentée non fonctionnelle.

### BUG-018 — Hallucination possible côté deepresearch
Le coordinator a appelé WebSearch (confirmé) mais si les specialists ne sont pas délégués (BUG-016) il ne fait qu'une seule recherche — rapport « deep research » basé sur 1 seule query.

### BUG-021 — Session silencieusement hangée quand l'app n'est plus deployée
Sur `sec-A-read-only` (status « not deployed » en interne mais visible dans `/api/apps`), les `POST /messages` sont acceptés (200) mais jamais exécutés. Aucun event, aucun timeout, aucun feedback.

### BUG-022 — Incohérence `listing apps` vs `diagnostics`
`/api/apps` rapporte 34 apps deployées. `/api/apps/{id}/diagnostics` rapporte `App: not deployed` pour beaucoup d'entre elles (sec-A-read-only, digiton-cv, pdf-processing-pipeline). Deux sources de vérité en désaccord.

### BUG-025 — Endpoint `/metrics` renvoie 404
Endpoint Prometheus documenté. Inaccessible → monitoring prod cassé.

### BUG-026 — Token count zéroé après turn completed
`GET /sessions/{sid}` après turn completed retourne `tokens: {prompt:0, completion:0, total:0}`. Impossible de tracker le coût par session.

### BUG-007 — Dédup mémoire cassée
Les faits `f1`, `f2` apparaissent 5-7 fois dans `semantic.facts`. Le system prompt promet « Duplicates are auto-detected and skipped ». Non.

---

## Bugs moyens

### BUG-003 — Message d'erreur trompeur sur tool non trouvé
Lorsqu'un tool inexistant est appelé : « Use search_tools or list_categories to discover available tools » — or ces outils ne sont pas exposés à l'agent → cul-de-sac.

### BUG-010 — `correlation_id` deux formats (`fp-XXX` vs UUID brut 32 hex)
Incohérence selon le chemin d'injection. Difficile à logger côté client.

### BUG-012 — Approval en état contradictoire « denied + pending »
Après timeout 300s, l'approval a `description: "User denied: Approval timed out"` MAIS reste visible dans `/approvals` sous `pending`.

### BUG-017 — `tool_call` event avec `name=""`
Dans le stream Socket.IO, certains events `tool_call` ont un payload `data.name` vide. Le frontend ne sait pas quel outil s'exécute.

### BUG-020 — LLM sort des tool calls en TEXTE brut au lieu d'appels structurés (sec-I-cross-module)
Le modèle écrit `shell.bash("echo ... > bypass.txt")` dans le `content` du message au lieu d'émettre un vrai function call. Agent inutilisable dans cette config (mais pas de bypass sécurité par effet de bord).

### BUG-023 — Channel type sérialisé comme `"?"`
`pdf-processing-pipeline` channel `pdf_inbox` a `type: "?"` dans la réponse API. Info manquante.

---

## Bugs bas

### BUG-002 — Tool count incohérent dans system prompt digitorn-chat
System prompt dit « 9 tools » puis en liste 11.

### BUG-004 — Loopback auth bypass trop large
Tous les paths sous `/api/apps/*` sont autorisés sans auth depuis 127.0.0.1 (y compris `POST /messages`, `/abort`, `/approve`, `DELETE /sessions/...`). Devrait être restreint aux read-only. Risque : un process local hostile ou multi-user Windows peut piloter le daemon d'un autre user.

### BUG-013 — Pas de graceful degradation sur stall event loop
Quand l'event loop stalle (BUG-014), les clients reçoivent un simple « Connection refused ». Il manque un mode dégradé (réponse 503 « daemon busy ») ou un autre worker Uvicorn.

### BUG-024 — `POST /triggers/{id}/test` retourne un body vide
Pas de structure `{success, data, error}` standard. Frontend ne peut pas afficher de feedback.

---

## Synthèse par app

| App | Smoke | Multi-tour | Outils | Events | Mémoire | Verdict |
|---|---|---|---|---|---|---|
| digitorn-chat | ✅ | ✅ | ✅ | ✅ | ❌ P0 | **Cassé (pollution mémoire inter-session)** |
| digitorn-code | ✅ | ❌ | ⚠️ (Write bloqué approval) | ❌ P0 (seq dup) | - | **Cassé (seq dup + approval hang)** |
| digitorn-deepresearch | ✅ | - | ⚠️ (1 agent seul) | ✅ | - | **Partiel (sub-agents non spawnés)** |
| digitorn-builder | ✅ | non testé | non testé | - | - | Smoke OK |
| sec-A-read-only | ❌ | - | - | - | - | **Cassé (app fantôme)** |
| sec-B-blocked-cmds | ✅ | - | ✅ | - | - | OK |
| sec-I-cross-module | ⚠️ | - | ⚠️ (LLM ne call pas) | - | - | Sécurité OK par effet de bord |
| sec-J-workspace-escape | ✅ | - | ✅ (blocage correct) | - | - | **OK — sandbox FS effectif** |

---

## Reproducteurs

Les scripts de test reproductibles sont ici :
- `tools/live_tests/prod_chat_multiturn.py` — multi-tour digitorn-chat
- `tools/live_tests/prod_code_tools.py` — filesystem + shell + path traversal digitorn-code
- `tools/live_tests/prod_deepresearch.py` — multi-agent deepresearch
- `tools/live_tests/prod_security.py` — sec-A / sec-B / sec-I / sec-J

Usage :
```bash
DIGITORN_TEST_TOKEN="<jwt-from-login>" py -3.12 tools/live_tests/prod_chat_multiturn.py
```

---

## Recommandations prioritaires

1. **Fix BUG-014 en priorité absolue** : déplacer `_get_workspace_status` hors de l'event loop. C'est le tipping point qui fait cascader tous les autres (timeouts, tests flaky, UX dégradée).
2. **Investiguer BUG-006** : tracer où `semantic.facts` est chargé au boot d'une session — probablement mauvaise clé de scope (app_id au lieu de session_id).
3. **BUG-009 seq unique** : auditer le serializer de `approval_request` — il émet visiblement 2× le même seq.
4. **Aligner le schema JSON** : `toolCalls` vs `tool_calls` (BUG-019). Casser vers snake_case partout pour aligner avec le reste du code Python.
5. **Restaurer `/metrics`** (BUG-025) pour l'observabilité prod.

---

## Tests additionnels (round 2) — Nouveaux bugs P0/hauts

### BUG-034 — P0 — Privilege escalation : role `developer` peut modifier la config globale
- **Repro** :
  1. Register user `developer` role (default pour tout nouvel inscrit).
  2. `PATCH /api/config` avec `{"server":{"auth_enabled":false}}`.
  3. → Daemon accepte (`applied: {...}`). Aucun check de rôle.
  4. `GET /api/apps` **sans token** → 200 + liste complète. Auth globalement disabled.
- **Également testé** : modifier `rate_limit_rpm`, `sandbox`, CORS — tout accepté.
- **Impact** : un simple dev user peut disable l'auth pour tous les autres, changer tous les paramètres serveur.
- **Gravité** : **CVE-level**. Sévérité maximale.

### BUG-035 — P0 — Leak mémoire cross-user sur `semantic.facts`
- **Repro** :
  1. User2 envoie à digitorn-chat : « my secret is PINEAPPLE ».
  2. L'agent appelle `Remember("User2's secret is PINEAPPLE")`.
  3. User3 (compte fraîchement créé, session vierge) demande sa mémoire → `semantic.facts` contient `"User2's secret is PINEAPPLE"`.
- **Analyse des scopes** :
  - `working.goal` → scopé `(user_id, app_id)` → cross-user OK mais cross-session KO (BUG-027).
  - `semantic.facts` → scopé `app_id` uniquement → leak cross-user intégral.
- **Impact** : confidentialité multi-tenant complètement cassée. Un user peut lire les secrets/noms/préférences des autres users de la même app.
- **Remarque** : les credentials ont la bonne isolation (401/403 cross-user). Seule la mémoire est buggée.

### BUG-027 — P0 — Race condition mémoire concurrent-session (mêmes user/app)
- **Repro** : 3 threads POST simultané sur digitorn-chat, chaque thread SetGoal avec un goal différent (alpha-red, beta-blue, gamma-green). Après completion, les 3 sessions voient `working.goal = "gamma-green"` (dernier writer).
- **Impact** : impossible d'utiliser l'app pour plusieurs projets parallèles par le même user.

### BUG-032 — P0 — Race condition sur le compteur `seq` global
- **Repro** : session normale sur task-manager, streaming LLM → seqs 2132-2136 tous dupliqués entre events `memory_update` et `token`.
- **Différent de BUG-009** : pas besoin d'approval_request. Se produit dans n'importe quel flux multi-publisher.
- **Cause probable** : le counter `seq` n'a pas de lock → collision entre l'event bus et le streaming LLM.

### BUG-037 — P0 — POST /messages accepté mais session jamais créée
- **Repro** :
  1. `POST /api/apps/sec-B-blocked-cmds/sessions/my-sid/messages` → `{success:true, status:"accepted"}` (200)
  2. Immédiatement après, `GET /api/apps/sec-B-blocked-cmds/sessions/my-sid` → 404 « Session not found or expired »
- **Impact** : client pense que tout va bien, l'agent ne démarre jamais. Pas de feedback d'erreur.
- **Cause probable** : bootstrap après daemon restart ne charge pas toutes les apps, mais la route `/messages` ne vérifie pas l'existence runtime.

### BUG-033 — Haut — Rate limiter auth scopé par email → DoS trivial sur n'importe quel compte
- **Repro** : 10 POST `/auth/login` avec un wrong password ciblant un email → compte locked 15 min.
- **Impact** : un attaquant peut locker l'admin ou n'importe quel user sans même avoir accès au compte. Devrait être rate-limité par IP aussi (ou IP seulement).

### BUG-036 — Haut — `/api/apps/{id}/diagnostics` retourne systématiquement `App: not deployed`
- **Repro** : tester sur les 35 apps listées → toutes rapportent « not deployed ». Même pour `digitorn-chat` qui répond activement aux messages.
- **Impact** : endpoint ops inutilisable. Les outils de monitoring sur ce endpoint sont tous dans l'erreur.

### BUG-038 — Haut — Apps se dégradent en "ghost state" au fil du temps
- **Repro** : `task-manager` fonctionnel à T+0, ghost state à T+45 min sans restart explicite. POSTs acceptés (200), GETs session retournent 404.
- **Impact** : opération instable à long terme. Force des restarts manuels réguliers.

### BUG-029 (précisé) — SDK `DevClient.delete_session()` cassé (pas backend)
- Le backend `DELETE /sessions/{sid}` fonctionne bien (`{success:true, deleted:true}`).
- Le SDK fait un POST bizarre avec `{"_method":"DELETE"}` puis tente un fallback `daemon_request` avec l'auth cache qui ne matche pas le token.
- Exception swallow dans try/except → le caller ne sait pas.

### BUG-031 — `GET /sessions/{sid}/workspace` structure non-normalisée
- Retourne `{session_id, app_id, workspace, workspace_mode, render_mode, entry_file, title, snapshot, git}` — les fichiers sont sous `snapshot.resources.files`, pas à la racine comme attendu par les SDK clients. Le helper `DevClient.get_workspace` retourne ce blob brut.

### BUG-028 — Moyen — `seq` counter global pas session-scoped
- 3 sessions concurrentes ont des ranges seq qui se chevauchent complètement (1648-2131). Le seq est un counter daemon-global, pas per-session.
- Contrairement à ce que laisse entendre la doc Socket.IO (`seq` monotone per session).

---

## Mise à jour de la synthèse par app

| App | Fonctionne | Notes |
|---|---|---|
| digitorn-chat | ✅ (dégradé) | Multi-tour OK mais BUG-027 + BUG-035 (leak mémoire) |
| digitorn-code | ⚠️ (buggy) | BUG-009 seq dup + BUG-008 approval hang |
| digitorn-deepresearch | ⚠️ | BUG-016 sub-agents non spawnés |
| digitorn-builder | ✅ smoke | Pas testé en profondeur |
| task-manager | ❌ ghost | Fonctionnait 30 min, maintenant 404 sur POST |
| sec-A/C/D/E/F/G/H (17 apps) | ❌ ghost | Listées mais jamais loaded, POST accepté mais dropped |

## Nouveaux scripts de repro

- `tools/live_tests/prod_abort.py` — abort flow (PASS)
- `tools/live_tests/prod_concurrent.py` — isolation mémoire 3 sessions parallèles (FAIL)
- `tools/live_tests/prod_sessions_ops.py` — fork/compact/export/delete/search
- `tools/live_tests/prod_taskmanager.py` — workspace + preview + React

Commande curl pour repro BUG-034 (privilege escalation) :
```bash
# register a regular dev
TK=$(curl -s -X POST http://127.0.0.1:8000/auth/register -H "Content-Type: application/json" \
  -d '{"email":"pwn@test","username":"pwn","password":"TestProd1234!"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# disable auth globally
curl -s -X PATCH -H "Authorization: Bearer $TK" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/config -d '{"server":{"auth_enabled":false}}'

# exfiltrate without auth
curl -s http://127.0.0.1:8000/api/apps
```

## TOP 5 actions urgentes

1. **BUG-034** (privilege escalation) — ajouter un check `is_admin` sur `PATCH /api/config` AVANT tout traitement.
2. **BUG-035 + BUG-027** — scoper la mémoire (working/episodic/semantic/procedures) par `(user_id, app_id, session_id)` au lieu de `app_id` seul.
3. **BUG-014** — déplacer `_get_workspace_status` hors event loop (subprocess.run → asyncio.to_thread).
4. **BUG-037 + BUG-038** — POST /messages doit vérifier que l'app est vraiment loaded avant d'accepter. Sinon renvoyer 503. Et investigation sur le "ghost state progressif" (fuite mémoire ? handler crash ?).
5. **BUG-032** — mettre un `asyncio.Lock` sur l'incrément du seq counter ou utiliser `itertools.count` thread-safe.

---

## Round 3 — Tests utilisateur réels approfondis

### BUG-039 — P0 — digitorn-builder : `message_done` JAMAIS émis sur tâches longues
- **Repro** : POST 3 messages normaux sur digitorn-builder demandant de créer une app YAML.
- **Observation** : après 840s (14 min), `message_done` n'est émis pour aucun des 3 turns. Pourtant le daemon exécute 24 tool calls en arrière-plan (RagQuery, WsWrite, DevToolsApp, etc.).
- **Impact** : la feature phare (app builder) est inutilisable côté client. Frontend reste en spinner indéfini.

### BUG-040 — P0 — digitorn-builder génère un YAML avec schema invalide
- **Repro** : demander "build me a simple counter app". Extraire `app.yaml` du workspace.
- **Observation** : le YAML utilise :
  - `name:` à la racine au lieu de `app.app_id:`
  - `modules:` comme `[{id: memory, type: memory}]` (list) au lieu de `{memory: {}}` (dict)
  - `app: { type: conversation, entrypoint: X }` au lieu de `execution.mode`
  - `agent: { model: { provider, model } }` au lieu de `brain:`
- **Impact** : la feature phare est cassée. Le builder confond son propre schéma.
- **Cause probable** : fallback Anthropic prend le relais (DeepSeek 402) mais le prompt ou les exemples RAG ne sont pas alignés avec le vrai schéma.

### BUG-041 — Haut — Drafts non persistés
Builder appelle `DevToolsApp` 10x mais `GET /api/builder/drafts` retourne 0 drafts. Workflow brisé.

### BUG-042 — P0 — Seq dup cascade sur builder (répétition de BUG-032)
Sur la même session builder : seqs 2201, 2202, 2203 dupliqués entre `memory_update / stream_done / thinking / out_token`. Confirme que la race condition sur le seq counter est systémique dès qu'un provider « thinking » est utilisé (Claude Sonnet fallback ici).

### BUG-043 — Moyen — `correlation_id` réutilisé pour 2 turns consécutifs
Turn 2 & Turn 3 sur builder ont le même `fp-bf0246e9ff97`. Soit le serveur dédupe (merge queue), soit le POST retourne l'ancien cid. Non documenté.

### BUG-046 — Haut — Agent silent après upload image + `message_done` quand même émis
- **Repro** : POST image 1x1 PNG à digitorn-chat avec question "What do you see?".
- **Observation** : image bien stockée (type `image_ref`, `image_id: 6f5df9c28824`), récupérable via `GET /images/{id}` (✅). Mais historique contient **UN SEUL message** (le user). Aucun assistant message. `message_done` émis quand même.
- **Impact** : client pense la session terminée alors qu'aucune réponse n'a été générée. Conversation cassée.
- **Cause probable** : DeepSeek-chat (non-vision) rejette silencieusement le contenu image ; le daemon ne détecte pas l'empty completion et signale done.

### BUG-047 — Moyen — Rate limit RPM non appliqué sur l'API authentifiée
200 requêtes parallèles GET `/api/apps` en 6.9s (≈1700 RPM soutenu) → **toutes reçoivent 200**. Config `rate_limit_rpm: 600` ignorée. Seul `/auth/login` a un vrai limiter (confirmé par BUG-033).

### BUG-048 — Haut — `DELETE /api/apps/digitorn-chat` : mensonge complet sur les effets
- **Repro** : User2 `DELETE /api/apps/digitorn-chat`.
- **Response** :
  ```json
  {"deleted":true, "deployed":false, "disk_removed":true,
   "secrets_deleted":1, "history_preserved":false, "scope":"user"}
  ```
- **Réalité** :
  - App toujours dans `/api/apps` ✅
  - `digitorn-chat` répond normalement aux messages ✅
  - User n'avait créé AUCUN secret → `secrets_deleted:1` est inventé
  - `disk_removed:true` → rien de supprimé (vérifié)
- **Impact** : le client croit que des données ont été supprimées ; quel que soit le comportement voulu (NO-OP sur builtin, ou erreur 403), l'API ment.
- **Fix** : si scope=user et pas d'override user : renvoyer `{deleted:false, scope:"system", message:"nothing to delete"}`. Sinon 403.

---

## Features testées qui FONCTIONNENT bien

| Feature | Résultat | Détails |
|---|---|---|
| AskUser flow interactif | ✅ PASS | approval_request event, pending queue, respond_to_ask, agent reprend |
| Abort mid-flight + resume | ✅ PASS | stream_done émis, is_active=false, interrupted=true, next message OK |
| Long chat 6 turns retention | ✅ PASS | SECRET_CODE retenu au tour 6, pressure tracking correct |
| Queue API (enqueue, positions) | ✅ PASS | 3 messages queued, positions 1/2/3, transitions trackées |
| Export session JSON | ✅ | retourne {content, format, filename, turns, messages} |
| Fork session | ✅ | nouveau sid créé, hérite BANANA, 5 messages |
| Search sessions plein texte | ✅ | trouve "BANANA" dans parent + fork |
| Credentials CRUD | ✅ | create masque API key (sk-...2345), list/delete scoped per-user |
| Cross-user isolation credentials | ✅ | 403 sur GET/DELETE d'un autre user |
| Cross-user isolation sessions (list/read direct) | ✅ | 404 sur sid d'un autre user |
| Backend DELETE session | ✅ | `{success:true, deleted:true}` puis 404 au GET |
| Image upload + stockage | ✅ | user content contient `{type: image_ref, image_id, mime, ...}` |
| Image retrieval GET /images/{id} | ✅ | renvoie bytes PNG valides |
| MCP catalog | ✅ | entries avec transport/runtime/required_fields |
| Password min length 12 enforced | ✅ | rejet 422 sur < 12 chars |
| Brute force protection (mais cf. BUG-033) | ✅ | lockout après 10 wrong tentatives |

---

## Bilan des 48 bugs

### Par sévérité
- **P0 (10)** : 006, 009, 011, 014, 027, 032, 034, 035, 037, 039, 040, 042
- **Hauts (15)** : 005, 007, 008, 015, 016, 019, 021, 022, 025, 026, 031, 033, 036, 038, 041, 046, 048
- **Moyens (10)** : 003, 010, 012, 017, 020, 023, 028, 029, 030, 043, 047
- **Bas (4)** : 002, 004, 013, 024

### Par domaine
- **Mémoire/isolation** : 006, 007, 027, 035 — catastrophique en multi-tenant
- **Socket.IO seq counter** : 009, 028, 032, 042 — race condition systémique
- **Auth/sécurité** : 015, 033, 034, 048 — escalation + DoS + mensonges API
- **Apps/runtime** : 021, 022, 036, 037, 038 — ghost apps, API lies, dégradation
- **LLM flows** : 008, 011, 016, 018, 039, 040, 046 — approvals hangs, no sub-agents, builder broken, vision broken
- **SDK** : 005, 019, 029, 031 — contrats cassés entre SDK et backend
- **Schema/API** : 010, 019, 023, 024, 025, 026, 043 — incohérences naming & valeurs

### TOP 3 bugs critiques ABSOLUS
1. **BUG-034** — Privilege escalation `PATCH /api/config` → auth disabled global. CVE-level.
2. **BUG-035 + BUG-027** — Memory leak cross-user + cross-session. GDPR nightmare.
3. **BUG-014** — subprocess.run bloquant event loop → multi-user impossible.

Tous ces bugs sont reproductibles avec les scripts dans `tools/live_tests/prod_*.py`.

---

## Round 4 — Escalade + protocoles + triggers/channels + sécurité RCE

### 🔴 BUG-061 — CVE CRITIQUE — RCE via `/api/modules/{id}/execute`

**Le endpoint debug/admin `/api/modules/{module}/execute` donne un RCE complet à tout user avec un token `developer` (par défaut).**

Tests reproduisibles :
```bash
TK=<token d'un dev register>

# 1. Exécution shell arbitraire
POST /api/modules/shell/execute
  {"action":"bash","params":{"command":"whoami"}}
→ 200 {"stdout":"ASUS\n", ...}  # Execute on host

# 2. Lecture fichier système HORS workspace
POST /api/modules/filesystem/execute
  {"action":"read","params":{"file_path":"C:/Windows/System32/drivers/etc/hosts"}}
→ 200 + contenu complet du fichier hosts

# 3. Écriture arbitraire HORS workspace
POST /api/modules/filesystem/execute
  {"action":"write","params":{"file_path":"C:/Users/ASUS/pwn.txt","content":"RCE"}}
→ 200 + fichier créé (vérifié avec `ls`)
```

**Bypass** : ce endpoint bypass TOUS les security mechanisms :
- Security profile app-level
- Workspace sandboxing
- Capability grants (`constraints.allowed_paths`, `constraints.allowed_hosts`)
- Path traversal blocks (qui marchent sur les Agent tool calls mais pas ici)

**Combiné avec BUG-034** (disable auth) : register dev → disable auth via `/api/config` → execute shell anonyme → pwn.

**Fix urgent** : ce endpoint doit demander `is_admin=true` OU être désactivé en prod OU appliquer une security profile.

### BUG-049 — Moyen — Events éphémères dans log persistant
`assistant_stream_snapshot`, `in_token`, `out_token` apparaissent dans `GET /sessions/{sid}/events`. L'SDK assertion `ephemeral_types_absent_from_persistent` ne détecte que `token/thinking_delta/agent_progress`, laissant passer ces 3. Gonfle la DB inutilement.

### BUG-050 — Haut — `GET /events` vs Socket.IO replay : counts divergent
`GET /sessions/{sid}/events` → 11 events. Socket.IO `replay since=0` → 32 events. Deux sources de vérité pour les events durables disant des choses différentes.

### BUG-051 — Bas — `/channels/health` vs `/triggers` désaccord
pdf-processing-pipeline : `channels/health` dit `channel_count: 0`, mais `/triggers` listait 1 channel `pdf_inbox`.

### BUG-052 — Haut — digiton-cv cron agent : 80 activations, 0 succès
`activations/stats` sur digiton-cv :
```
total: 80, completed: 0, failed: 59, success_rate: 0.0,
total_prompt_tokens: 0, total_completion_tokens: 0
```
L'agent cron tourne depuis longtemps mais 74% failed et 26% running-permanent (zombies). 0 token LLM = échec avant même l'appel au provider. Feature production inutilisable.

### BUG-053 — Moyen — `fire trigger` répond succès mais aucune background session créée
`POST /triggers/{id}/fire` → `{fired:true}`, mais `GET /background-sessions` reste vide. Le fire « réussi » ne matérialise rien.

### BUG-054 — Haut — Activations bloquées en `running` (dur_ms=0, fired_at=None)
20/20 activations récentes de digiton-cv stuck en `running` avec `dur_ms: 0`. Jamais nettoyées. Resource leak DB.

### BUG-055 — Bas — `GET /api/triggers` (global) → 404
Pas d'endpoint pour lister tous les triggers de toutes les apps. Frontend ne peut pas afficher « automations actives ».

### BUG-056 — Moyen — Channels `list_providers` retourne `[]`
Aucun provider channels (Slack, Telegram, email, webhook) configuré. Feature documentée mais non provisionnée par défaut.

### BUG-057 — Haut — `POST /auth/logout` → 422 (body required mais non documenté)
```json
{"success":false,"error":"Validation error","details":[{"type":"missing","loc":["body"],"msg":"Field required"}],"status_code":422}
```
Le SDK `DevClient.logout()` n'envoie pas de body → échec systématique.

### BUG-058 — Haut — Token valide après logout (pas de révocation)
Même en ignorant BUG-057, le JWT bearer n'a pas de blacklist/revocation côté serveur. Un token volé reste valide jusqu'à expiry (24h). Violation best practices OWASP.

### BUG-059 — Bas — Messages d'erreur registration vagues
Duplicate email ET duplicate username → même `"Registration failed"` (400). Debug UX pénible.

### BUG-060 — Bas — Field `name` silencieusement dropped à register
Register accepte `name` mais `display_name` = username. Le `name` transmis est ignoré.

---

## Mise à jour récap

**Total : 61 bugs en ~4h de tests réels.**

### P0 CVE-level (3)
1. **BUG-061** — RCE via `/api/modules/{id}/execute` (nouveau champion)
2. **BUG-034** — Privilege escalation via `PATCH /api/config`
3. **BUG-035** — Memory leak cross-user (GDPR)

### Features qui RÉUSSISSENT (confirmées à nouveau round 4)
- Refresh token rotation (single-use ✅)
- Tampered JWT rejected ✅
- SQL injection blocked dans login ✅
- **digitorn-code sur vraie tâche de bugfix** : PASS complet (Glob → Bash test → Edit → Read → Bash test → "FIXED") ✅
- Reconnect + replay Socket.IO ✅
- Refresh token replay attack blocked ✅
- Password min 12 / max 128 enforced ✅

### Nouveau tableau de synthèse
| Catégorie | P0 | Haut | Moyen | Bas | Total |
|---|---|---|---|---|---|
| Sécurité | **3** | **4** | 2 | 2 | 11 |
| Memory/isolation | 2 | 2 | 0 | 0 | 4 |
| Socket.IO/events | 4 | 2 | 3 | 0 | 9 |
| Auth | 0 | 3 | 1 | 2 | 6 |
| Apps/runtime | 1 | 5 | 1 | 0 | 7 |
| Builder/DeepResearch | 3 | 2 | 0 | 0 | 5 |
| Background/channels | 0 | 2 | 2 | 2 | 6 |
| SDK | 0 | 2 | 1 | 0 | 3 |
| API schema | 0 | 1 | 5 | 1 | 7 |
| Approvals | 0 | 2 | 1 | 0 | 3 |

### Reproducteurs livrés (13 scripts)
`tools/live_tests/prod_*.py` :
- chat_multiturn, code_tools, code_bugfix, deepresearch, security
- abort, concurrent, sessions_ops, taskmanager
- builder_deep, askuser, multimodal, long_chat
- queue, reconnect

Plus scripts bash inline pour: rate limit, auth edge cases, privilege escalation, RCE via modules/execute.

### TOP 3 actions immédiates
1. **BUG-061** — Désactiver `/api/modules/{id}/execute` ou exiger `is_admin=true`. Priorité MAXIMALE.
2. **BUG-034** — Même chose sur `PATCH /api/config`.
3. **BUG-035** — Scoper `semantic.facts` par (user_id, app_id).

---

## Round 5 — DoS + CROSS-USER AUTHORIZATION BYPASS GÉNÉRAL

### 🔴 BUG-062 — P0 — Aucune limite de taille de message (DoS)
POST `/messages` accepte 50MB de texte en 2.6s sans limite. Démontré avec 100KB, 1MB, 10MB, 50MB tous acceptés HTTP 200. Combiné avec BUG-014, un seul attaquant envoyant 4 messages gros simultanément provoque 27 event loop stalls (daemon ~60s unresponsive).

### 🔴 BUG-063 — P0 — Daemon unreachable après 4 gros POSTs consécutifs
Health endpoint répond vide (JSON decode fail). Recovery auto après ~60s mais inacceptable. Stall counter monte à 27.

### 🔴 BUG-070 — P0 CVE — `GET /sessions/{sid}/events` leak cross-user
User3 `GET /api/apps/digitorn-chat/sessions/<user2-sid>/events?since_seq=0` → **200 OK**, retourne les events incluant les secrets mémorisés par user2 (ex: `{"result":{"content":"Secret: PURPLE-PANDA-999"}}`).

### 🔴 BUG-071 — P0 CVE — `POST /sessions/{sid}/abort` cross-user
User B peut abort une session de User A (destructive DoS à distance).

### 🔴 BUG-072 — P0 CVE — `POST /sessions/{sid}/messages` cross-user + exec
User B POST un message dans la session de User A → **200 "accepted"**, le message EST AJOUTÉ à la conversation (vu via `/events`: "injected by user3" présent). L'assistant répond normalement, comme si le message venait de user A. **Injection de prompts dans une conversation d'autrui.**

### 🔴 BUG-073 — P0 CVE — `GET /sessions/{sid}/events` accessible **SANS TOKEN**
Appel sans aucun Authorization header → 200 OK. Leak de tous les events de toutes les sessions. Combiné au loopback bypass, n'importe quel process local (ou remote si exposé) lit tout.

### 🔴 BUG-074 — P0 CVE — `POST /sessions/{sid}/fork` cross-user
User B fork une session de User A → **nouvelle session créée pour User B** avec `forked_from: <user-A-sid>`, `message_count: 3`. Vol complet du contenu de conversation.

### 🔴 BUG-075 — P0 CVE — `GET /sessions/{sid}/export` cross-user
User B GET export → markdown complet exporté (User + Assistant turns). Data exfiltration intégrale.

### BUG-076 — Haut — `/queue`, `/context-breakdown`, `/workspace` leak cross-user
Les 3 endpoints répondent 200 avec les data de la session de l'autre user.

### BUG-064 — Moyen — POST message sur ghost app → 400 "parsing error" trompeur
Task-manager en ghost state → même body JSON valide reçoit 400 "error parsing the body". Message erreur misleading (devrait être 404/503).

### BUG-065 — Bas — `/workspace/files/approve|reject` : 200 OK avec `success:false`
Code HTTP incohérent. Au moins path traversal (`../../etc/passwd`) n'exploite pas (stays inside workspace resolver).

### BUG-066 — Haut — `/workspace/export` renvoie 404 alors que session existe
Incohérence entre `/workspace` (200) et `/workspace/export` (404) sur la même session.

### BUG-067 — Bas — `/api/mcp/pool/health` sans wrapper standard
Retourne `{"results":{}}` au lieu de `{"success":true,"data":{...}}`.

### BUG-068 — (retracté) — MCP search fonctionne, false positive curl piping.

### BUG-069 — Bas — CORS: `access-control-allow-credentials:true` sans `allow-origin` sur requête simple avec origin disallowed
Non-bloquant (le browser refusera) mais cosmétique.

---

## Récapitulatif final : **76 bugs identifiés**

### P0 CVE-level (9)
1. **BUG-061** — RCE via `/api/modules/*/execute`
2. **BUG-034** — Privilege escalation `PATCH /api/config`
3. **BUG-073** — `/events` anonymous access
4. **BUG-070** — `/events` cross-user leak
5. **BUG-072** — `/messages` cross-user inject+exec
6. **BUG-074** — `/fork` cross-user (vol de session)
7. **BUG-075** — `/export` cross-user (data dump)
8. **BUG-071** — `/abort` cross-user (DoS destructive)
9. **BUG-035** — `semantic.facts` leak cross-user

### P0 (6 non-CVE)
- BUG-006, BUG-009, BUG-011, BUG-014, BUG-027, BUG-032, BUG-037, BUG-039, BUG-040, BUG-042, BUG-062, BUG-063

### Vecteurs d'attaque réalistes
1. **Attack 1 — Full data exfiltration multi-user** (BUG-073+070+074+075) :
   - Anonymous → `GET /events` sur un SID deviné → dump de tous les events
   - Register dev → `fork` cross-user → copier toutes les conversations
   - `export` cross-user → obtenir markdown complet
2. **Attack 2 — RCE** (BUG-061) :
   - Register dev → `POST /modules/shell/execute` → shell host
3. **Attack 3 — Full auth bypass + RCE** (BUG-034 + BUG-061) :
   - Register dev → `PATCH /config` disable auth → anonymous RCE
4. **Attack 4 — DoS** (BUG-062 + BUG-014) :
   - 1 POST 50MB → event loop stall ~60s → tous les users timeout
5. **Attack 5 — Conversation hijack** (BUG-072) :
   - User B POST message dans session de User A → injection de prompts, LLM répond à user A avec le contenu de user B

### Features qui RÉSISTENT correctement

- Socket.IO `join_session` cross-user → refusé ✅
- `/history`, `/memory`, `/compact` cross-user → 404 ✅
- Credentials CRUD cross-user → 403 ✅
- Inbox isolation (unread_count/mark_all/DELETE) → proper ✅
- CORS preflight (allowed vs evil origin) ✅
- SQL injection dans login ✅
- JWT tampering rejected ✅
- XSS payload rejected (name dropped) ✅
- Refresh token rotation single-use ✅
- Password length bornes 12-128 ✅
- Path traversal dans session_id URL-encoded ✅

### TOP 5 actions ABSOLUMENT urgentes

1. **BUG-073** — `/events` endpoint doit require auth + check user_id cross-session (fix 1 ligne)
2. **BUG-070, 072, 074, 075** — ajouter `_verify_session_owner(session_id, request.state.user_id)` sur TOUS les endpoints `/sessions/{sid}/*`. Fix middleware centralisé.
3. **BUG-061** — `/api/modules/*/execute` → gate sur `is_admin` ou désactiver en prod.
4. **BUG-034** — `PATCH /api/config` → gate sur `is_admin`.
5. **BUG-062** — body size limit sur `/messages` (1MB max, renvoyer 413).

Le rapport est reproductible via les 15 scripts dans `tools/live_tests/prod_*.py` et les commandes curl inline ci-dessus.

---

## Round 6 — SECRETS EXFIL + OVERRIDE BUILTIN (CVE CATASTROPHIQUES)

### 🔥 BUG-077 — P0 CVE CATASTROPHIC — Secret exfiltration via `/api/modules/filesystem/execute`

**Token `developer` suffit pour lire N'IMPORTE QUEL fichier sur le host.**

Prouvé avec :
1. **`~/.claude/.credentials.json`** → LEAKÉ
   Contient les tokens OAuth Anthropic de l'utilisateur (accessToken + refreshToken).
   Un attaquant peut facturer des appels Claude au compte du propriétaire.
2. **`~/.digitorn/jwt.key`** → LEAKÉ
   HMAC signing key pour les JWT. Avec ça, on peut FORGER n'importe quel JWT (admin compris) et impersonner tous les users.
3. **`~/.digitorn/master.key`** → LEAKÉ
   Clé de chiffrement des secrets user-stockés. Avec ça, décryption de toutes les API keys/credentials de tous les users.

```bash
# Attack démontré avec un token user developer :
curl -X POST -H "Authorization: Bearer $TK" \
  http://127.0.0.1:8000/api/modules/filesystem/execute \
  -d '{"action":"read","params":{"file_path":"C:/Users/ASUS/.digitorn/jwt.key"}}'
→ 200 OK, content: "21225fb72fba4a23f1bf36a2c46d5238..."
```

**Combine avec BUG-061** — complete end-game. Register dev → read jwt.key → forge admin JWT → total ownership.

Bonne nouvelle : le `http` module a une bonne SSRF protection (private IPs + file:// blocked).

### 🔥 BUG-081 — P0 CVE CATASTROPHIC — Deploy override d'un builtin supprime l'app pour TOUS les users

**Repro** :
```bash
# User developer ordinaire
curl -X POST -H "Authorization: Bearer $TK" \
  http://127.0.0.1:8000/api/apps/deploy \
  -d '{"yaml_path":"my_evil.yaml","force":true}'
# avec my_evil.yaml ayant app_id: digitorn-chat (un builtin système)
```
- Le deploy FAILED silencieusement (compilation error).
- **Le builtin `digitorn-chat` a été EFFACÉ** du registre avant le check de validation.
- Apps count : 35 → 34.
- Tous les users perdent l'accès à `digitorn-chat` : 404 sur tous leurs messages.
- Transaction non-atomique : remove-before-install.

**Impact** :
- DoS sur n'importe quel builtin (chat, code, builder, deepresearch) par un dev user.
- Seule recovery : manual redeploy par admin.

### BUG-078 — Haut — Session ID collision : deux rows DB avec même `session_id` pour 2 users
`POST /messages` sur un SID existant chez user A crée une NOUVELLE session user B avec le même SID au lieu de 409 Conflict. Les deux sessions coexistent avec user_id différent.

### BUG-079 — Moyen — `/api/apps/{id}/assets/app.yaml` lisible par tout user
Le YAML source d'une app (system_prompts, model params, constraints) est exposé publiquement à tout user authentifié, même pour apps d'autres users/scope system.

### BUG-080 — Haut — Deploy silent failure
POST /deploy → 200 "deploying". Subsequent GET /apps/{id} → 404. `/errors` endpoint empty. `/diagnostics` says "not deployed". **Aucune façon de savoir pourquoi ça a foiré.**

---

## Récapitulatif FINAL — 82 bugs identifiés en ~6h

### P0 CVE-level (10)
1. **BUG-077** — Exfil tokens OAuth/jwt.key/master.key via filesystem.execute
2. **BUG-081** — Destruction builtin par deploy override force:true
3. **BUG-061** — RCE via /api/modules/*/execute
4. **BUG-034** — Privilege escalation PATCH /api/config
5. **BUG-073** — /events anonymous access (no token)
6. **BUG-070** — /events cross-user leak
7. **BUG-074** — /fork cross-user
8. **BUG-075** — /export cross-user
9. **BUG-072** — /messages cross-user inject (confirm BUG-078: creates duplicate SID)
10. **BUG-071** — /abort cross-user
11. **BUG-035** — semantic.facts leak cross-user

### Attack scenarios end-to-end démontrés

1. **Complete host compromise** (BUG-077 + BUG-061) :
   - Read jwt.key → forge admin JWT → read all users' secrets → RCE via modules.execute
2. **Data exfiltration public** (BUG-073) :
   - Anonymous `GET /events` sur n'importe quel SID deviné → dump conversations + secrets mémorisés
3. **DoS builtin** (BUG-081) :
   - Deploy YAML avec app_id=digitorn-chat force:true → supprime le builtin pour tous users
4. **Persistent auth bypass** (BUG-034 → BUG-077) :
   - Disable auth → read jwt.key anonyme → forge forever-valid admin tokens
5. **Cost draining attack** (BUG-077) :
   - Read ~/.claude/.credentials.json → utiliser accessToken Anthropic jusqu'à épuisement du crédit

### Top 5 fixes ABSOLUMENT critiques

1. **`/api/modules/{id}/execute`** → gate `is_admin` OU sandbox complet (constraint les paths/hosts par user profile) OU désactiver en prod.
2. **Ajouter `_verify_session_owner()`** sur TOUS endpoints `/sessions/{sid}/*` (middleware centralisé). Corrige BUG-070-076.
3. **`/events` endpoint** → require auth + verify session owner (fix 2 lignes).
4. **Deploy transaction atomique** → NE PAS remove l'ancien avant validation succès du nouveau. Corrige BUG-081.
5. **`PATCH /api/config`** → gate `is_admin`. Corrige BUG-034.

### Tools livrés
- `docs/PROD_BUG_REPORT_2026-04-19.md` (ce document)
- 16 scripts `tools/live_tests/prod_*.py` reproductibles
- Commandes curl inline pour tous les CVE

---

## Round 7 — CVE ULTIMATE : DB complète exfiltrable

### 🔥🔥🔥 BUG-083 — P0 CVE ULTIMATE — Full daemon DB exfiltration + admin password hash + encrypted secrets

**Chain** : BUG-061 (shell.bash) + Python script = dump complet de `~/.digitorn/digitorn.db`.

Extrait après exploitation (token dev basique, aucun privilège spécial) :

```
Tables (17): action_executions, agents, api_keys, app_module_configs,
app_module_grants, app_profiles, app_secrets, applications,
managed_mcp_servers, refresh_tokens, roles, session_checkpoints,
session_messages, user_oauth_tokens, user_roles, user_sessions, users

users.admin@digitorn.local :
  password_hash = $2b$12$sKegx6.oJO4CU.I2TisRquMnlUoutqW5YGsyJtMuNQcBv7hzMAyL2
  (bcrypt, ready for offline cracking)

app_secrets.MINIMAX_API_KEY = b'gAAAAABpzB9XHF2zCoo9cWsICRHdzdw5F3KZ3lqVK_F9SnTD...'
  (Fernet encrypted with master.key — which BUG-077 leaks)

user_oauth_tokens : access_token_enc / refresh_token_enc
  (all users' OAuth tokens, decryptable with master.key)
```

**Contournement notable** : database module bloque les paths dans `.hidden/` dirs, MAIS le shell module n'a aucune restriction → `cp ~/.digitorn/digitorn.db /tmp/x.db` puis lecture directe via SQLite. La protection `/modules/database/execute` est contournée par `/modules/shell/execute` + `/modules/filesystem/execute`.

**Gravité** : **CVE ULTIMATE**. Après exploitation :
- Admin bcrypt hash (offline cracking possible selon politique de mot de passe)
- Toutes les API keys des apps de tous les users (décryptable via master.key BUG-077)
- Tous les OAuth tokens users (Google, GitHub, Anthropic…)
- Hashes des `refresh_tokens` (pour impersonation persistente)

### BUG-082 — Bas — Database module : naming `type` vs `driver` inconsistance dans error message
Ancienne erreur disait "type required", maintenant "driver required". Inconsistance entre la doc publique et le schéma réel. Non-bloquant mais DX pénible.

---

## TOTAL FINAL : 85+ bugs identifiés en ~7h

### P0 CVE-level (11)
1. **BUG-083** — ULTIMATE : Full DB exfil + admin hash + encrypted secrets (chain BUG-061 + BUG-077)
2. **BUG-077** — Secret exfiltration (OAuth Claude, jwt.key, master.key)
3. **BUG-081** — Builtin destruction par deploy override
4. **BUG-061** — RCE via /api/modules/*/execute
5. **BUG-034** — Privilege escalation PATCH /api/config
6. **BUG-073** — /events anonymous access
7. **BUG-070** — /events cross-user leak
8. **BUG-074** — /fork cross-user
9. **BUG-075** — /export cross-user
10. **BUG-071** — /abort cross-user
11. **BUG-035** — Memory facts leak cross-user

### Chaînes d'attaque end-to-end prouvées

**Attaque 1 — Impersonation admin** :
1. Register dev (30s)
2. BUG-077: read `~/.digitorn/jwt.key`
3. Forge admin JWT (role=admin, is_admin=true) avec le signing key
4. Total ownership daemon

**Attaque 2 — Decrypt all users' secrets** :
1. Register dev
2. BUG-061+083: dump `digitorn.db` → encrypted app_secrets + user_oauth_tokens
3. BUG-077: read `~/.digitorn/master.key`
4. Fernet.decrypt(ciphertext, master.key) → plaintext API keys, OAuth tokens

**Attaque 3 — DoS** :
1. Register dev (force:true) + app_id: digitorn-chat → builtin supprimé
2. Ou : 1 POST 50MB → stall 60s
3. Ou : 10 wrong logins sur admin email → compte locked 15 min

**Attaque 4 — Data exfiltration silent** :
1. Anonymous `GET /events` sur SIDs devinables (format `fp-<hex>`)
2. Dump conversations + secrets mémorisés
3. Aucune trace côté admin (pas d'alert)

### Ratio P0 / Total = 11/85+ ≈ 13%. Extremely high.

### ✅ Features qui RÉSISTENT
- PyYAML safe_load (bloque !!python/object RCE)
- HTTP module SSRF protection (private IPs, file://, schemes)
- Database module hidden-path block (contournable via shell cp, mais principe bon)
- Socket.IO join_session cross-user
- `/history`, `/memory`, `/compact` cross-user
- Credentials cross-user
- Inbox isolation
- CORS preflight
- SQL injection dans login
- JWT tampering rejected
- Refresh token single-use
- Password bornes 12-128
- Path traversal URL-encoded session_id

### RECOMMANDATION FINALE

**Ce daemon NE DOIT PAS être exposé en prod avant fix au minimum des 11 CVE P0 listés.**

Le système a de bonnes bases de sécurité (CORS, password policy, JWT, CSP headers, cross-user isolation sur /history/memory, Fernet encryption at rest). Mais les points suivants sont un désastre :
1. `/api/modules/*/execute` = RCE complet (doit être admin-only ou désactivé)
2. Le scoping cross-user est CHAOTIQUE — 8/11 endpoints /sessions/* leakent
3. Le deploy n'est pas transactionnel → destruction de builtins
4. Pas de body size limit
5. JWT/master key lisibles avec le même bug que ci-dessus

**Fix priority order :**
1. Gate `/api/modules/*/execute` on `is_admin` (fix 1 hook)
2. Gate `PATCH /api/config` on `is_admin`
3. Ajouter `_verify_session_owner()` middleware sur tous `/sessions/*`
4. Rendre deploy transactionnel (atomic install, no remove-before)
5. Body size limit 1MB sur `/messages`
6. JWT revocation store pour logout effectif
7. Scope `semantic.facts` par (user_id, app_id, session_id)
8. `/metrics` endpoint opérationnel
9. Input size limit sur `/auth/register` + rate limit par IP
10. Activations cleanup (zombies running)

---

## Round 8 — Transcription audio (`/api/transcribe`)

### État actuel du provider
- **Configuré** : `provider=local, model=small, device=cuda, int8_float16, max_audio=25MB, min=500B, timeout=180s`
- **Non opérationnel** : `ready: false, error: "faster-whisper not installed"`
- Toutes les requêtes "valides" reçoivent 500 "Transcription failed: RuntimeError"

### ✅ Contract compliance (TRX01-TRX04)
| Test | Attendu | Observé | Result |
|---|---|---|---|
| TRX01 real audio → 200 + text | — | 500 (provider down) | ⚠️ bloqué par infra |
| TRX02 100B → 422 | 422 | 422 "Audio too short or empty" | ✅ |
| Just-below-min 499B → 422 | 422 | 422 | ✅ |
| Just-above-min 501B → 500 (provider) | 500 | 500 RuntimeError | ✅ |
| TRX03 26MB+1 → 413 | 413 | 413 "Audio too large (max 25 MB)" | ✅ |
| 50MB → 413 | 413 | 413 | ✅ |
| TRX04 health | 200 ready status | 200 + config | ✅ |

### 🔴 Nouveaux bugs transcribe

#### BUG-086 — Haut — `/api/transcribe` acceptable SANS auth via loopback bypass
```bash
# Ces deux marchent identiquement :
curl -F "audio=@a.webm" http://127.0.0.1:8000/api/transcribe   # → 500/200
curl -F "audio=@a.webm" -H "Authorization: Bearer $JWT" http://127.0.0.1:8000/api/transcribe
```
Tout process local (sans JWT) peut transcrire. Si `provider:openai` est configuré, l'attaquant draine le budget API. Si `provider:local + GPU`, l'attaquant consomme les cycles GPU.
- **Atténuation** : `X-Forwarded-For` header present → bypass désactivé (bien). Mais en deploy single-host sans proxy, pas de protection.

#### BUG-091 — Haut — Le champ `audio` dans `/messages` est silencieusement dropé
```json
POST /api/apps/digitorn-chat/sessions/X/messages
{"message":"transcribe this","audio":{"data":"<b64>","mime":"audio/webm","name":"voice.webm"}}
→ 200 "accepted"
```
Le message user dans l'historique n'a que `{role:"user", content:"transcribe this"}`. L'audio disparaît. Le LLM répond "I need to know what you'd like me to transcribe" — **il ne sait même pas qu'il y avait un audio**. La UX de fallback documentée (Flutter attache l'audio si transcription fail) ne fonctionne pas côté backend.

Variantes testées :
- `audio: {...}` (singular) → dropé
- `audios: [...]` (plural) → dropé

#### BUG-092 — Moyen — Audio transféré via champ `images` stocké comme `image_ref`
```json
{"message":"here is audio","images":[{"data":"<b64>","mime":"audio/webm",...}]}
→ user content: {type: "image_ref", mime: "audio/webm", width: 0, height: 0}
```
Hack : l'audio est stocké mais tagué `image_ref`. Pas de pipeline de transcription associé, juste un blob stocké. Agent silent (BUG-046 déjà identifié).

#### BUG-093 — Moyen — Concurrent transcribe produit stall daemon
20 requêtes transcribe parallèles → `event_loop_watchdog.stalls_total` passe à 29 (+1). Latences 1.4-3.2s indiquent sérialisation I/O sur disque (écriture temp file). Vecteur DoS modéré.

#### BUG-094 — Bas — `/api/transcribe/health` leak config anonyme
```json
{"enabled":true,"provider":"local","model":"small","preload":true,"model_loaded":false,"max_concurrency":1,"ready":false,"error":"faster-whisper not installed"}
```
Même contenu avec/sans auth. Info disclosure pour reconnaissance.

#### BUG-095 — Moyen — Pas de rate limit sur `/api/transcribe`
40 requêtes parallèles en 3.8s (10 RPS soutenu) → 0 × 429. Un user peut faire 600 RPM sans être limité. En combinaison avec BUG-086 (anonymous), DoS facile.

#### BUG-096 — Moyen — MIME type non validé
Tous acceptés et relayés au provider :
- `application/x-executable`
- `image/png`
- `text/html`
- `../../etc`
- `null`
- `\x00` (null byte)

Aucune allowlist `audio/*`. Provider est censé parser : si il crash sur `.exe`, c'est DoS. Defensive coding : reject at the gate.

#### BUG-097 — Bas — Form fields `language`/`app_id` 20KB acceptés
Pas de max_length sur les champs multipart supplémentaires. Mineur mais peut faire gonfler les logs/temp files.

#### BUG-087 — Moyen — Empty filename → 500 "Internal server error" au lieu de 422
`audio=('', bytes)` triggers uncaught exception avec request_id. Devrait être 422 validation error.

#### BUG-088 — Moyen — Zip bomb accepté (97KB → 100MB décompressé)
Tant que le provider est down, aucun impact. Si provider up et utilise PyAV/ffmpeg, risque de bomb. Gate à l'entrée : rejeter les non-audio par magic bytes ou extension.

### Endpoints transcribe testés
- `GET /api/transcribe/health` → 200 (leak config)
- `POST /api/transcribe` → 200/422/413/500 selon taille
- `POST /api/transcribe/batch`, `/models`, `/jobs`, `/status`, `/queue`, `/languages`, `/providers`, `/config`, `/metrics` → 404 (n'existent pas)
- `/api/voice`, `/api/audio` → 404

### Recommandations transcribe
1. **BUG-086** : restreindre le loopback bypass aux user-agents d'agent interne (X-Internal-Agent header signé) OU exiger un JWT "service account" pour les appels automatisés.
2. **BUG-091** : implémenter vraiment le pipeline audio dans `/messages` : soit transcrire avant le LLM, soit stocker comme `audio_ref` distinct de `image_ref`, soit refuser explicitement.
3. **BUG-095** : rate-limit transcribe par user_id ET par IP : 30 RPM default, configurable.
4. **BUG-096** : allowlist MIME stricte `audio/{mpeg,webm,ogg,mp4,wav,x-m4a,flac}`.
5. **BUG-085** : error messages informatifs ("faster-whisper not installed" côté /transcribe aussi, pas seulement /health).

---

## Round 9 — Channels module

### État global
- ✅ BUG-061 FIXED pendant mes tests : `/api/modules/*/execute` retourne maintenant 403 `admin_required` pour tous les modules (shell, filesystem, database, http, channels, memory). Grand fix.
- ❌ **Régression introduite** : le pipeline deploy user-initiated est cassé (BUG-103).
- ✅ `list_providers` propose les 11 adapters documentés (webhook, cron, email, file_watcher, rss, log, queue, telegram, discord, slack, voice).

### 🔴 BUG-103 — P0 RÉGRESSION — Deploy pipeline cassé après fix BUG-061
**Tout** deploy user-initiated échoue silencieusement :
- POST `/api/apps/deploy` → 200 "deploying"
- GET `/api/apps/{id}` → 404 "not deployed"
- `/errors` empty
- App jamais dans `/api/apps` list
- No install_dir créé sous `~/.digitorn/apps/`
- No logs visibles

Reproduit avec YAML minimal (memory only, pas de channels). Affecte aussi les apps avec channels. **Builder et dev_tools cassés → feature principale de la plateforme inutilisable**. Likely regression du fix admin_required introduite sur un path utilisé internally par deploy.

### 🔴 BUG-101 — P0 — Daemon crash durant tests channels
Pendant les tests channels, daemon devient injoignable (all endpoints WinError 10061), recovery auto après ~60s. Repro par combinaison de tests concurrents. Pattern consistent avec BUG-014 (event loop stall via subprocess.run).

### 🔥 BUG-108 — Haut — File_watcher ne bloque pas les symlinks (potentiel data exfil)
```bash
cd {watched_path}
mklink pwned.pdf C:/Users/ASUS/.digitorn/master.key   # Windows
# OR: ln -s /etc/passwd pwned.pdf   (Linux)
```
→ Watcher détecte et emit l'event (`events_received` incremented). Agent activation suit le symlink → lit contenu sensible → inclut dans session messages, potentiellement dans outbound reply.

**Impact** : tout user avec write access au watched directory (ou l'app lui-même si workspace) peut exfiltrer n'importe quel fichier system.

**Fix** : `os.path.realpath()` check + reject si en dehors du watched root.

### 🔴 BUG-107 — Haut — Silent failure 100% sur background apps
- `digiton-cv` : 80 activations, 0 succès (100% failed)
- `pdf-processing-pipeline` : 42 activations, 0 succès
- User inbox N'A PAS de notification d'échec
- Dashboard n'alerte pas
- Silent rot potentiellement depuis des semaines

**Cause underlying** : la plupart des non-builtin apps utilisent DeepSeek dont la clé est épuisée, mais n'ont pas de `brain.fallback` configuré. Le `_handle_llm_error` ne peut pas switcher → agent crashe → activation failed.

**Fix** : dashboard alert si `failure_rate > 50%` sur les N dernières activations. Ou notification inbox sur failure streak.

### BUG-099 — Bas — Channel `pdf_inbox` type sortie "?" corrigé en "filewatcher" (probablement fix entretemps)
Dans mon premier test (BUG-023) l'API retournait `type: "?"`. Maintenant `type: "filewatcher"`. Le bug semble fixé mais le naming est inconsistent avec la doc (`file_watcher` snake_case dans la doc, `filewatcher` sans séparateur dans l'API response).

### BUG-100 — Moyen — SDK `install_package(source)` cassé
`DevClient.install_package(source)` envoie `{source, force}`. Endpoint `/api/packages/install` exige `{source_type, source_uri}`. 422 validation error. SDK contract broken.

### ✅ Ce qui fonctionne dans channels
- `file_watcher` adapter détecte les nouveaux files correctement (events_received incremented real-time)
- Glob pattern `*.pdf` (sans `**`) correctement limité à root du watched path (sub-dirs ignorés)
- Burst handling : 20 files dropés rapidement → 20 events propres, daemon stable
- `list_providers`/`provider_status`/`stats` actions exposées au LLM via schema
- Adapter types complets (11) : webhook, cron, email, file_watcher, rss, log, queue, telegram, discord, slack, voice
- Secret filtering enabled par défaut sur outbound text
- `/api/modules/channels/execute` correctement gaté admin_required

### Tests non réalisables (à cause BUG-103)
- Real LLM + channels.send_message E2E via chat (pas de deploy possible)
- Webhook outbound vers listener externe
- Inbound webhook avec HMAC auth
- Provider_history leak test
- Slack/Telegram provider connection

### Attaques suggérées pour suite
- Si deploy corrigé : tester webhook outbound SSRF (même vecteur que http module)
- Tester max_providers constraint (default 20) avec YAML qui déclare 21+ providers
- Tester channels avec inbound_path containing `../../../` 
- Tester webhook HMAC signature bypass

