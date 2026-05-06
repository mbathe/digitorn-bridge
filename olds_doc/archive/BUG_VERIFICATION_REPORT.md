# Bug-by-Bug Verification Report

**Date** : 2026-04-20
**Méthode** : Re-test de chaque bug identifié avec `DevClient` (vrai client) + vrai LLM + vrais events Socket.IO. Aucun mock.
**Harness** : `tools/live_tests/verify_bugs.py` (12 groupes, 97 bugs couverts)
**Données brutes** : `docs/BUG_VERIFICATION.json`

## Scoreboard global

| Verdict | Count | % |
|---|---|---|
| **FIXED** | **63** | 65% |
| NOT_FIXED | 12 | 12% |
| PARTIAL | 6 | 6% |
| UNCLEAR | 7 | 7% |
| SKIP | 9 | 9% |
| **Total vérifié** | **97** | |

**+ cron_native** : 11 bugs fixés séparément (commits appliqués, 31/31 tests unitaires passent).

---

## Bugs encore NOT_FIXED (12) - à traiter

| ID | Severity | Summary | Evidence |
|---|---|---|---|
| **BUG-103** | **P0** | Deploy pipeline pour user `developer` retourne 200 "deploying" puis 404 permanent | Testé sur YAML valide minimal, 60s+ pas d'errors visibles, package n'apparaît pas dans /packages/list. Deploy async se plante silencieusement |
| **BUG-016** | Haut | `digitorn-deepresearch` ne spawn pas de sub-agents (0 `agent_spawn` events sur 459) | 1-sentence report test |
| **BUG-052** | Haut | `digiton-cv` cron : 102 activations, 0% success_rate | Stats activations digiton-cv |
| ~~BUG-005~~ | ~~Haut~~ | ~~SDK `DevClient.register()`~~ | **Actually FIXED** - premier test utilisait un email existant, un fresh register marche |
| **BUG-046** | Haut | Upload d'une image → `message_done` émis mais assistant silent | DeepSeek no-vision probably |
| **BUG-002** | Moyen | System prompt digitorn-chat dit 11 tools, en liste 17 | `claimed=11` vs `listed=17` |
| **BUG-027** | P0 | Concurrent sessions même user : goals vides (pas écrasés mais pas set) | Race/provider issue |
| **BUG-051** | Bas | `channels/health.channel_count=0` alors que `/triggers` list 1 channel | Endpoint disagreement persiste |
| **BUG-055** | Bas | `GET /api/triggers` (global) → 404 | Endpoint non implémenté |
| **BUG-069** | Bas | CORS `ACA-Credentials:true` sans `ACAO` sur Origin disallowed | Header noise, non bloquant |
| **BUG-096** | Moyen | Transcribe non-audio MIME rejeté par rate-limit (429) plutôt que MIME filter (415) | Bypass logique possible |
| **BUG-100** | Moyen | SDK `install_package(source)` vs backend `source_uri` | Backend attend `bundle://digitorn/<id>` mais SDK envoie `builtin:<id>` - schéma divergent |

---

## Bugs PARTIAL (6) - améliorés mais incomplets

| ID | Summary | État |
|---|---|---|
| **BUG-033** | Rate limit auth scopé par email | attack account locked, tester2 OK - toujours email-scoped |
| **BUG-081** | Deploy override builtin | app builtin désormais intacte, mais deploy toujours 200 au lieu 400 |
| **BUG-019** | Schema `toolCalls` vs `tool_calls` | les 2 shapes retournés ensemble |
| **BUG-028** | Seq counter scope | ranges mixtes session/global (48 overlap a_b) |
| **BUG-050** | `/events` REST vs Socket.IO replay | sio=5 rest=7 (proche) |
| **BUG-085** | Transcribe error messages | "not_an_audio_file" (spécifique) au lieu de RuntimeError générique |

---

## Bugs UNCLEAR (7) - nécessitent investigation manuelle

| ID | Raison |
|---|---|
| BUG-024 | `POST /triggers/{id}/test` timeout (daemon lent à répondre) |
| BUG-031 | `/workspace` pas de `files` key ni en root ni nested |
| BUG-039 | Builder session not found (pas booté ?) |
| BUG-062 | 30MB body → `ReadError` (connection refused - daemon rejette via nginx? ou crash?) |
| BUG-073 | Anon `/events` → 404 (pas de session → ne peut distinguer auth-block de not-found) |
| BUG-091/092 | Audio `/messages` rejeté 422/415 (mieux que silent drop, à considérer FIXED?) |

---

## Bugs SKIP (9)

| ID | Raison |
|---|---|
| BUG-003, BUG-020 | Symptômes LLM-specific, non-deterministic |
| BUG-015 | Nécessite daemon restart manuel |
| BUG-018 | Subsumé par BUG-016 |
| BUG-040 | Test builder long, coûteux |
| BUG-042 | Subsumé par BUG-032 (FIXED) |
| BUG-068 | Retracté (false positive curl) |
| BUG-082 | Module admin-gated, schema OK |
| BUG-108 | Nécessite access filesystem watched path |

---

## Highlights - Ce qui marche très bien maintenant

### Sécurité (CVE blocks)
- ✅ `/api/modules/*/execute` → 403 admin_required (BUG-061)
- ✅ `PATCH /api/config` → 403 (BUG-034)
- ✅ Filesystem execute → 403 (BUG-077)
- ✅ Cross-user endpoints (6 endpoints testés) → tous 404/403 (BUG-070/071/072/074/075/076)
- ✅ Anon `POST /messages` → 401 (BUG-004)
- ✅ Cross-user inbox, credentials → isolation totale

### Apps / Deploy
- ✅ Apps registry cohérent (`/apps` vs `/diagnostics`)
- ✅ Ghost state résolu (task-manager, digitorn-chat actifs)
- ✅ POST /messages sur app inexistante → 404 upfront
- ✅ Deploy YAML invalide → 400 avec détails clairs
- ✅ SDK `delete_session` fonctionne

### Mémoire
- ✅ Fresh session = mémoire vide
- ✅ Dedup facts fonctionne
- ✅ Cross-user memory leak bloqué (BUG-035)

### Events / Socket.IO
- ✅ Correlation IDs uniques et format consistent
- ✅ Seq unique dans session (0 dup observé sur 62 events)
- ✅ Tool call events avec noms
- ✅ Ephemeral events (token/thinking_delta) absents du log persistant

### Auth
- ✅ Logout 200 + token révoqué
- ✅ Registration errors spécifiques (email vs username)
- ✅ Name field preserved at register
- ✅ Approval workflow (AskUser)
- ✅ Drafts persistés

### Performance
- ✅ 0 event-loop stalls sur 3 chats consécutifs (BUG-014 subprocess stall FIXED)
- ✅ Rate limit actif (429 au-delà de 10 RPS)
- ✅ Daemon résilient post-stress
- ✅ 10 concurrent transcribe : 0 stall

### Transcribe
- ✅ Anon rejeté 401
- ✅ Zip bomb rejeté 415
- ✅ Empty filename 422
- ✅ Health endpoint auth-gated
- ✅ Rate limit transcribe actif

---

## Détail par ID (tri croissant, extrait court)

Voir `docs/BUG_VERIFICATION.json` pour le détail complet de chaque check.
