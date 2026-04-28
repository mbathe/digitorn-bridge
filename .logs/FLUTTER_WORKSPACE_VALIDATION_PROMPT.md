# Flutter client - Workspace validation flow integration

Brief pour le `digitorn_client` Flutter. Tu reçois ici le contrat
complet côté daemon après le sprint de fixes (BUG #1 / #2 / GAP #3-6 /
`auto_approve` / BUG #20). Tout est testé end-to-end (scouts green,
vrai LLM inclus). Adapte l'UI en suivant ce contrat - pas d'autre
source de vérité.

---

## 1. Détection du mode

Les apps peuvent être en **manual** ou **auto_approve**. L'UI doit
s'adapter : pas de boutons approve/reject/hunks en auto_approve,
juste un indicateur `AUTO` discret.

### Source de vérité - endpoint dédié UI-config

**NE PAS** exposer le YAML complet côté client - il peut contenir des
api_keys inline, des system_prompts (IP agent), des URLs internes /
webhook paths, la liste des secrets requis. Utiliser l'endpoint
dédié qui allow-list les champs safe :

```
GET /api/apps/{app_id}/ui-config
→ {
    app_id,
    workspace_config: {
      render_mode?, entry_file?, title?,
      sync_to_disk?, lint?, auto_approve?
    },
    preview_config: {enabled?, port?},
    workspace: {render_mode?, entry_file?, title?}   // top-level block
  }
```

Seuls les champs de l'allow-list sont renvoyés - prompts/secrets/hooks/capabilities
restent côté serveur.

```dart
final resp = await dio.get('/api/apps/$appId/ui-config');
final wsConfig = resp.data['data']['workspace_config'] as Map? ?? {};
final autoApprove = wsConfig['auto_approve'] as bool? ?? false;
final renderMode = resp.data['data']['workspace']?['render_mode'] as String? ?? 'auto';
final previewEnabled = resp.data['data']['preview_config']?['enabled'] as bool? ?? false;
```

Cache par `app_id` dans `AppRepository` - le config change seulement
au redeploy, invalide le cache sur l'event SSE `app_redeployed` ou sur
un bouton "Reload app".

Si tu as besoin d'exposer un nouveau flag UI (ex : `theme_color`,
`default_language`), ajoute-le à l'allow-list `_WS_ALLOW` ou
`_PREVIEW_ALLOW` dans `core/api/apps.py::get_app_ui_config` -
décision explicite, pas de dump global.

### Règles UI par mode

| Mode | Diff gutters | Bouton Approve | Approve-hunks | Badge header |
|---|---|---|---|---|
| manual (défaut) | oui | oui | oui | "N files pending" |
| auto_approve | **non** - tout est toujours baseline | **caché** | **caché** | "AUTO" chip gris |

En auto_approve, les endpoints approve/reject/*-hunks restent
callable mais no-op (le baseline = current content sur chaque write).
**Ne pas** les appeler depuis l'UI - ça pollue les logs et risque
de masquer un vrai bug plus tard.

---

## 2. Contrat payload `files` channel

Le client accumule déjà les events `resource_patched`. Voici les
champs à **lire tels quels** (zéro recalcul côté client) :

```dart
class WorkspaceFile {
  final String path;
  final String content;
  final int size;
  final int lines;
  final String language;
  final String validation;            // "pending" | "approved"
  final int insertionsPending;        // delta vs baseline (0 si approved)
  final int deletionsPending;         // delta vs baseline
  final int totalInsertions;          // cumul session
  final int totalDeletions;           // cumul session
  final int baselineLines;
  final String? gitStatus;            // untracked | unstaged | staged | conflict | committed
  final String? source;               // "user" si PUT writeback, sinon absent
  final String? unifiedDiff;          // per-op diff (edit only)
  final String? diff;                 // short diff preview
  final double updatedAt;
  final String status;                // added | modified | deleted
}
```

**IMPORTANT** : `insertions_pending` et `deletions_pending` sont
**delta-vs-baseline** depuis le sprint de fixes (BUG #1 corrigé). Un
fichier approuvé puis édité d'une seule ligne montre `1/1`, pas la
somme cumulée. Retire tout code client qui recalcule pending à partir
du stream - fais confiance au payload.

---

## 3. Diff rendering

Le `unified_diff_pending` revient via :

```
GET /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}?include_baseline=true
→ {
    path,
    payload: { content, validation, insertions_pending, … },
    baseline: "last-approved content as string",
    unified_diff_pending: "--- a/path\n+++ b/path\n@@ …"
  }
```

Depuis BUG #2 : chaque ligne est terminée par `\n`, parseable
directement par un unified-diff parser (`unified_diff_parser` package
Dart). Aucune ligne fusionnée du style `-line three+LINE TWO`.

### Parsing pour UI par-hunk

```dart
// Parse les hunks pour offrir approve/reject granulaire
class Hunk {
  final int index;         // 0-based position
  final String hash;       // 12-char stable SHA-256
  final String header;     // "@@ -1,3 +1,4 @@"
  final int oldStart;
  final int oldLen;
  final int newStart;
  final int newLen;
  final List<String> body; // lignes " ", "-", "+"
}
```

Le hash est calculable côté client avec `sha256(header + "\n" + body.join("\n")).substring(0,12)`.
Le daemon le calcule exactement pareil (voir `_finalize_hunk` dans
`workspace/module.py`) - utilise le hash pour passer à
`approve-hunks` / `reject-hunks` pour survivre à un race où un agent
écrirait pendant que le user regarde son diff.

---

## 4. Actions UI - endpoints à câbler

### Stage whole file

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/approve
Body: {"path": "src/App.tsx"}
→ 200 {success: true, data: {path, validation: "approved"}}
→ 400 {detail: {error: "file not found in workspace: …"}}
```

### Reject whole file

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/reject
Body: {"path": "src/App.tsx"}
→ 200 {data: {path, reverted: "baseline" | "deleted"}}
```

`deleted` = le fichier n'avait pas de baseline (première écriture
jamais approuvée) → reject l'a supprimé du workspace.

### Partial stage - per-hunk approve

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/approve-hunks
Body: {"path": "src/App.tsx", "hunks": ["a8f3bc0e", 2]}
→ 200 {
    path,
    approved_hunks: [{index, hash}, …],
    remaining_hunks: [{index, hash}, …],
    validation: "approved" | "pending"
  }
```

Le `hunks` array accepte int (index) OU string hash - peut mélanger.
Si `remaining_hunks: []`, valide devient `"approved"` et l'UI doit
retirer le fichier de la liste "pending".

### Partial revert - per-hunk reject

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/reject-hunks
Body: {"path": "src/App.tsx", "hunks": [1]}
→ 200 {path, reverted_hunks: [{index, hash}, …]}
```

Après cet appel, le serveur émet automatiquement un
`resource_patched` sur le canal `files` avec le nouveau content +
pending recalculé. **Ne mets pas à jour l'UI de façon optimiste** -
attends l'event Socket.IO.

### User writeback - édition manuelle / résolution de conflit / drag-drop

```
PUT /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}
Body: {"content": "…nouveau contenu…", "auto_approve": false, "source": "user"}
→ 200 {path, size, validation}
```

Cas d'usage :
- **ConflictPane** : user résout `<<<<<<< / ======= / >>>>>>>`
  → PUT le contenu résolu, `auto_approve: true` optionnel pour
  court-circuiter l'approve.
- **Monaco editor avec readOnly: false** : on PUT à chaque `onBlur`
  ou `Ctrl+S`.
- **Drag-drop d'un fichier externe** : PUT direct avec le contenu.
- **Scripts externes qui seedent le workspace** : PUT avant le premier
  message à l'agent.

Le payload émis côté stream aura `source: "user"` - utile pour
afficher un tag "edited by you" dans l'UI.

### Ship to git

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/commit
Body: {"message": "feat: add task list", "files": null, "push": false}
→ 200 {commit_sha, branch, files_committed: [paths], pushed: bool, commit_stdout}
→ 400 {detail: {error: "workspace is not a git repo: /…"}}
→ 400 {detail: {error: "no files to commit (all approved = none, or list was empty)"}}
```

Par défaut `files: null` commit tous les fichiers approved. Passer
une liste explicite pour en commiter un sous-ensemble. `push: true`
fait un `git push` derrière le commit.

**Pré-requis** : le workspace doit être un repo git initialisé.
Bouton UI conseillé : "Init git" en dehors du flow si pas de `.git/`,
pour que l'utilisateur voie bien qu'il doit initier le repo avant.

### Approval history

```
GET /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}/history
→ 200 {
    path,
    revisions: [
      {revision: 1, approved_at: 1776611337.5, approved_by: "user",
       tokens_delta_ins: 3, tokens_delta_del: 0, bytes: 54},
      {revision: 2, approved_at: 1776611400.9, approved_by: "auto",
       tokens_delta_ins: 1, tokens_delta_del: 1, bytes: 58},
    ]
  }
```

`approved_by: "auto"` signifie que c'est `auto_approve` module/call.
`"user"` = approve explicite.

Usage UI : "Changes over time" vue chronologique, bouton "Restore
revision N" (qu'on câblera plus tard - endpoint pas encore livré).

---

## 5. Socket.IO events à écouter

Toutes les mutations émettent un event sur la room `session:{sid}`.
Le client les accumule dans son cache `files` map :

| event_type | action |
|---|---|
| `resource_set` | Remplace intégralement le payload d'un path |
| `resource_patched` | Merge un patch (change uniquement certains champs) |
| `resource_deleted` | Retire le path du cache |
| `resource_bulk_set` | Remplace plusieurs paths d'un coup |
| `channel_cleared` | Vide tout le cache `files` |

Event shape (JSON) :
```json
{
  "session_id": "…",
  "channel": "files",
  "id": "src/App.tsx",
  "payload": { /* full WorkspaceFile */ },
  "seq": 42
}
```

`seq` est monotone - utile pour détecter les pertes après reconnect
(le client peut replayer depuis son `last_seq` via le hydrate snapshot).

---

## 6. UI flow suggéré

### Panneau file tree (explorateur)

- Liste des paths triés par nom.
- Chaque entrée :
  - icône langage (via `language` field)
  - nom du fichier
  - **badge `+N -M`** si `validation == "pending"` et (ins > 0 OR del > 0)
  - **chevron vert** si `validation == "approved"`
  - **rien** si `auto_approve` mode (validation toujours approved)
  - `git_status` dot (orange = unstaged, rouge = conflict, etc.)
- Clic → ouvre le fichier via
  `GET /workspace/files/{path}?include_baseline=true`.

### Panneau diff

- Onglets : "Current" (payload.content) | "Baseline" (baseline) | "Diff".
- "Diff" rendu via le `unified_diff_pending` parsé.
- Boutons par hunk :
  - ✓ **Stage** → `POST /files/approve-hunks {hunks: [hunk.hash]}`
  - ✗ **Revert** → `POST /files/reject-hunks {hunks: [hunk.hash]}`
- Bouton global : **Stage all** (`POST /files/approve`), **Revert all**
  (`POST /files/reject`).
- En `auto_approve` : cacher tous ces boutons, juste afficher le diff
  à titre informatif (il sera toujours vide de toute façon).

### Header de session

- Compteur "N files pending" (count des files où `insertions_pending > 0
  OR deletions_pending > 0`).
- Bouton **Commit** → ouvre un dialog avec message + liste des files
  approved + checkbox `push`.
- Chip "AUTO" si detected auto_approve mode.

### Palette de commandes

- `Ctrl+Shift+P` → "Approve current file", "Reject current file",
  "Commit session", "Refresh git status".

---

## 7. Gotchas et ordre des opérations

1. **Restart du daemon** : clear `packages/digitorn/**/__pycache__`
   avant sinon le daemon charge des `.pyc` stales qui peuvent masquer
   les fixes. C'est un bug connu (pas encore corrigé côté daemon).

2. **Route order FastAPI** : `/files/{path}/history` est maintenant
   déclarée AVANT `/files/{path}` catch-all (BUG #20 fix). Si tu
   ajoutes de nouvelles sous-routes `/files/...`, mets-les avant le
   catch-all aussi.

3. **Hunks identification** : préfère le hash (string 12-char) à
   l'index (int). Un agent peut écrire dans le workspace entre le
   moment où le user voit son diff et le moment où il clique
   approve - le hash survit, l'index change.

4. **`PUT /files/{path}` + `auto_approve` per-call** : le flag dans le
   body override le module-level flag *vers le bas* uniquement. Si le
   module est en `auto_approve: true`, un PUT avec `auto_approve: false`
   reste approved (c'est le comportement attendu).

5. **Soft-failures** : les endpoints retournent `400 + detail.error`
   plutôt que `200 + success:false` (BUG-065 fix). Branche ton
   error-handling sur `statusCode >= 400`, pas sur `body.success`.

6. **Thinking bleed** : DeepSeek-reasoner peut parfois émettre le
   début de la réponse dans `reasoning_content` → `thinking` snapshot.
   Le workaround client (`ChatMessage.stripThinkingOverlap`) est
   encore en place - garde-le. Le daemon fait de son mieux pour
   séparer mais certains modèles mixent.

---

## 8. Scouts à maintenir

Dans `digitorn_client/scout/` tu peux reprendre la structure de
`digitorn-bridge/.logs/scout_pending_counts.py` et
`scout_auto_approve.py`. Chaque PR daemon qui touche au workspace
doit passer ces scouts avant merge. On te conseille un
`scout_all_workspace.sh` qui :

1. Démarre un daemon sur un port de test.
2. Run les 3 scouts Python (pending_counts, auto_approve, llm_e2e).
3. Affiche `PASS` / `FAIL` et exit-code.

Pour le CI Flutter : wrap ça derrière un `./scripts/check-daemon-contract.sh`
appelé avant le `flutter test` et le `flutter analyze`. Si le contrat
change, les scouts cassent côté daemon ET l'UI échoue - on sait
immédiatement qu'il faut actualiser le client.

---

## 9. Questions à te poser avant de merger

- [ ] UI cache toute action approve/reject quand le module est en `auto_approve`.
- [ ] Diff gutters utilisent `insertions_pending` / `deletions_pending` directement (pas de recalcul).
- [ ] Per-hunk approve uses `hash` (string) plutôt que `index` (int).
- [ ] `PUT writeback` déclenché sur Monaco `onBlur` avec `auto_approve: false` par défaut (le user flippe un toggle explicite pour auto).
- [ ] Commit dialog vérifie `workspace is git repo` avant d'offrir le bouton (feature-detect via 400 response ou pre-check).
- [ ] Le client ne poll pas `/history` agressivement (cache avec TTL 30s).
- [ ] Error-handling branche sur `statusCode >= 400` et affiche `detail.error` quand présent.
