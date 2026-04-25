# Cahier de Charge — Tests de Production Digitorn Bridge

**Version:** 1.0  
**Date:** 2026-04-02  
**Portee:** Tests d'integration et de validation avec applications YAML  
**Objectif:** Couvrir 100% des fonctionnalites, configurations et chemins critiques avant mise en production

---

## Table des matieres

1. [Infrastructure Daemon](#1-infrastructure-daemon)
2. [Authentification & Autorisation](#2-authentification--autorisation)
3. [Deploiement & Cycle de Vie des Apps](#3-deploiement--cycle-de-vie-des-apps)
4. [Modes d'Execution](#4-modes-dexecution)
5. [Modules — Filesystem](#5-modules--filesystem)
6. [Modules — Shell](#6-modules--shell)
7. [Modules — Git](#7-modules--git)
8. [Modules — HTTP & Web](#8-modules--http--web)
9. [Modules — Database](#9-modules--database)
10. [Modules — MCP](#10-modules--mcp)
11. [Modules — Memory](#11-modules--memory)
12. [Modules — Agent Spawn](#12-modules--agent-spawn)
13. [Modules — Notebook](#13-modules--notebook)
14. [Modules — PDF, Presentation, Spreadsheet](#14-modules--pdf-presentation-spreadsheet)
15. [Modules — Browser & Computer Use](#15-modules--browser--computer-use)
16. [Modules — Channels](#16-modules--channels)
17. [Modules — RAG & Vector](#17-modules--rag--vector)
18. [Modules — Cache, Queue, Cron](#18-modules--cache-queue-cron)
19. [Modules — Context Builder](#19-modules--context-builder)
20. [Securite — Security Gate (7 portes)](#20-securite--security-gate-7-portes)
21. [Securite — Sandbox OS](#21-securite--sandbox-os)
22. [Securite — Profils & Policies](#22-securite--profils--policies)
23. [Middleware Pipeline](#23-middleware-pipeline)
24. [Systeme d'Evenements & Streaming](#24-systeme-devenements--streaming)
25. [Workflow d'Approbation](#25-workflow-dapprobation)
26. [Taches de Fond & Watchers](#26-taches-de-fond--watchers)
27. [Rate Limiting & Quotas](#27-rate-limiting--quotas)
28. [Sessions & Persistence](#28-sessions--persistence)
29. [Workspace & Project Memory](#29-workspace--project-memory)
30. [Skills](#30-skills)
31. [Multi-Agent & Coordination](#31-multi-agent--coordination)
32. [Configuration LLM & Providers](#32-configuration-llm--providers)
33. [API REST Exhaustive](#33-api-rest-exhaustive)
34. [Socket.IO & Temps Reel](#34-socketio--temps-reel)
35. [Metrics & Observabilite](#35-metrics--observabilite)
36. [Resilience & Graceful Shutdown](#36-resilience--graceful-shutdown)
37. [Multi-Plateforme](#37-multi-plateforme)
38. [Scenarios End-to-End Complets](#38-scenarios-end-to-end-complets)

---

## Conventions

- **[YAML]** : Application YAML a creer pour le test
- **[API]** : Appel API a effectuer
- **[VERIFY]** : Verification attendue
- **[PREREQ]** : Prerequis avant le test
- **Priorite** : P0 (bloquant), P1 (critique), P2 (important), P3 (souhaitable)

---

## 1. Infrastructure Daemon

### 1.1 Demarrage et Arret — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| D-001 | Demarrage par defaut | `digitorn start` | Daemon demarre sur 127.0.0.1:8000, DB SQLite creee, migrations executees |
| D-002 | Demarrage avec port custom | `digitorn start --port 9000` | Daemon accessible sur port 9000 |
| D-003 | Demarrage avec host custom | `digitorn start --host 0.0.0.0 --no-sandbox` | Refuse sans auth_enabled=true, sauf si --insecure |
| D-004 | Demarrage host non-local sans auth | `digitorn start --host 0.0.0.0` sans auth | RuntimeError: "Refusing to bind to 0.0.0.0 without authentication" |
| D-005 | Demarrage avec config YAML | `digitorn start --config custom.yaml` | Charge la config depuis le fichier |
| D-006 | Demarrage avec app bootstrap | `digitorn start --app app.yaml` | App deployee automatiquement au demarrage |
| D-007 | Demarrage avec TLS | `digitorn start --tls-cert cert.pem --tls-key key.pem` | HTTPS actif, HSTS header present |
| D-008 | Demarrage multi-workers | `digitorn start --workers 4` | 4 workers Uvicorn, DB pool partage |
| D-009 | Arret gracieux | `digitorn stop` | Drain des requetes actives (30s), undeploy apps, fermeture DB |
| D-010 | Arret force | Envoyer SIGTERM pendant activite | Drain 30s puis exit, pas de corruption DB |
| D-011 | Health check | `GET /health` | `{"status": "ok", "version": "1.0.0", "socketio": true}` |
| D-012 | Readiness check | `GET /readyz` | `{"status": "ready", "database": true, "deployed_apps": N}` |
| D-013 | Liveness check | `GET /healthz` | `{"status": "alive"}` |
| D-014 | Reload dev mode | `digitorn start --reload` | Rechargement auto sur modification fichiers |
| D-015 | Premier demarrage setup | Lancer sur repertoire vierge | Prompt interactive DB (SQLite/PostgreSQL), creation config.yaml |

### 1.2 Configuration — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| D-020 | Config par defaut | Demarrer sans config | host=127.0.0.1, port=8000, workers=1, auth=true, sandbox=true |
| D-021 | Env vars override | `DIGITORN_SERVER__PORT=9090 digitorn start` | Port 9090 utilise |
| D-022 | Priorite config | System + user + env var pour meme cle | Env var gagne |
| D-023 | CORS origins | Configurer `cors_origins: ["https://app.example.com"]` | Seul ce domaine accepte, wildcard * rejete |
| D-024 | CORS wildcard rejete | `cors_origins: ["*"]` | ValueError leve au demarrage |
| D-025 | KV backend DiskCache | Pas de kv_backend configure | DiskCache dans ~/.digitorn/kv/, operations atomiques |
| D-026 | KV backend Redis | `kv_backend: redis://localhost:6379/0` | Redis utilise, fallback DiskCache si Redis down |
| D-027 | KV backend Redis resilient | Couper Redis pendant operation | Circuit breaker s'ouvre, fallback sur DiskCache, recovery auto |
| D-028 | Database SQLite | `database.url: sqlite+aiosqlite:///digitorn.db` | DB SQLite creee, migrations OK |
| D-029 | Database PostgreSQL | `database.url: postgresql+asyncpg://...` | Pool (5 connexions), pre_ping, migrations |
| D-030 | Runtime config | Configurer max_consecutive_failures=5, tool_timeout=60 | Agent loop respecte ces limites |
| D-031 | Log level | `logging.level: debug` | Logs debug visibles |
| D-032 | Log format JSON | `logging.format: json` | Logs en JSON structure |
| D-033 | Config runtime PATCH | `PATCH /api/config {"logging": {"level": "debug"}}` | Applique a chaud, restart_required vide |
| D-034 | Config runtime restart required | `PATCH /api/config {"server": {"port": 9000}}` | Retourne `restart_required: ["server.port"]` |

### 1.3 Base de Donnees — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| D-040 | Migration auto | Ajouter colonne a un modele, redemarrer | ALTER TABLE execute, pas de perte de donnees |
| D-041 | Migration colonnes manquantes | DB existante sans nouvelle colonne | _migrate_missing_columns() ajoute la colonne avec DEFAULT |
| D-042 | Pool exhaustion | Lancer 50 requetes paralleles sur pool_size=5 | File d'attente, pas de crash, timeout 30s |
| D-043 | DB disconnect recovery | Couper PostgreSQL puis reconnecter | pre_ping detecte, reconnexion auto |

---

## 2. Authentification & Autorisation

### 2.1 Auth Locale — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-001 | Login admin par defaut | `POST /auth/login {"username": "admin", "password": "admin1234admin"}` | 200, access_token + refresh_token retournes |
| A-002 | Login credentials invalides | `POST /auth/login {"username": "admin", "password": "wrong"}` | 401 Unauthorized |
| A-003 | Register nouvel utilisateur | `POST /auth/register {"username": "dev1", "password": "securePwd12345", "email": "dev@test.com"}` | 201, tokens retournes, role "developer" |
| A-004 | Register mot de passe trop court | `POST /auth/register {"username": "x", "password": "short"}` | 400, validation error (min 12 chars) |
| A-005 | Register username duplique | Re-register meme username | 400, "Username already exists" |
| A-006 | Lockout progressif | 5 logins echoues en 15 min | 6eme tentative: "Too many failed login attempts. Try again in Xs" |
| A-007 | Recovery apres lockout | Attendre 15 min apres lockout | Login reussi |
| A-008 | Login par email | `POST /auth/login {"email": "dev@test.com", "password": "..."}` | Login reussi (cherche email) |

### 2.2 Tokens JWT — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-010 | Access token valide | Utiliser token dans `Authorization: Bearer <token>` | 200, request.state.user_id rempli |
| A-011 | Access token expire | Attendre 15 min (ou manipuler TTL) | 401 "Invalid or expired token" |
| A-012 | Refresh token exchange | `POST /auth/refresh {"refresh_token": "..."}` | Nouveau access + refresh token |
| A-013 | Refresh token one-time use | Re-utiliser un refresh token deja utilise | 401, token revoque |
| A-014 | Refresh token expire | Token de 7+ jours | 401, token expire |
| A-015 | Logout revocation | `POST /auth/logout {"refresh_token": "..."}` puis refresh | Refresh echoue (token revoque) |
| A-016 | Token sans issuer | JWT forge sans iss="digitorn" | 401, verification echoue |
| A-017 | Token signe avec mauvaise cle | JWT signe avec autre cle | 401, signature invalide |
| A-018 | Me endpoint | `GET /auth/me` avec token valide | Retourne user_id, email, roles, permissions |

### 2.3 API Keys — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-020 | Generer API key | Via API de creation | Retourne `dk_xxxx_yyyy...`, prefix stocke, hash en DB |
| A-021 | Auth avec API key | `X-API-Key: dk_xxxx_yyyy...` | Authentifie, permissions appliquees |
| A-022 | API key invalide | `X-API-Key: dk_fake_value` | 401 |
| A-023 | API key expiree | Key avec expires_at passe | 401 "API key expired" |
| A-024 | API key desactivee | Key avec is_active=false | 401 |
| A-025 | API key scoped a app | Key avec app_id specifique | Acces uniquement a cette app |
| A-026 | API key permissions | Key avec permissions limitees | Seules ces permissions accordees |

### 2.4 OAuth2/OIDC — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-030 | Authorize URL Google | `GET /auth/oauth/google` | Redirect vers accounts.google.com avec state HMAC |
| A-031 | Callback valide | Simuler callback avec code + state | Token echange, user cree/mis a jour |
| A-032 | State invalide | Callback avec state forge | "Invalid state parameter" |
| A-033 | State expire | Callback apres 10 min | "State expired" |
| A-034 | State replay | Re-utiliser un state deja consomme | "State already used or expired" |
| A-035 | Auto-provision | Premier login OAuth | User cree automatiquement |
| A-036 | Login OAuth existant | Re-login meme user OAuth | Email/display_name mis a jour, meme user_id |
| A-037 | GitHub provider | Configurer client_id/secret GitHub | Flow OAuth complet |
| A-038 | Azure AD provider | Configurer Azure AD | Flow OAuth complet |
| A-039 | Custom OIDC provider | Fournir authorize_url, token_url, userinfo_url custom | Flow OAuth complet |

### 2.5 LDAP — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-040 | Login LDAP | Credentials LDAP valides | Bind reussi, user provisionne |
| A-041 | Login LDAP invalide | Mauvais mot de passe | "Invalid credentials" |
| A-042 | LDAP user search | Username avec caracteres speciaux | Echappement LDAP correct |
| A-043 | LDAP auto-provision | Premier login | User cree en DB |

### 2.6 Roles & Permissions — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| A-050 | Admin wildcard | User admin accede a tout | Toutes les permissions via `["*"]` |
| A-051 | Developer permissions | User developer tente deploy | Autorise (apps:deploy dans role) |
| A-052 | Viewer read-only | User viewer tente deploy | 403 Forbidden |
| A-053 | Permission check endpoint | `_require_permission(request, "apps:deploy")` | 403 si permission manquante |
| A-054 | Multi-role union | User avec developer + custom role | Permissions = union des deux roles |
| A-055 | Role scope par app | User avec role different par app | Permissions varient selon l'app |
| A-056 | Permission glob matching | Permission `"fs.*"` vs check `"fs.read"` | Match via fnmatch |

---

## 3. Deploiement & Cycle de Vie des Apps

### 3.1 Deploy — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| APP-001 | Deploy minimal | [YAML] `app: {app_id: test, name: Test}` + 1 module + 1 agent | Deploy reussi, app accessible |
| APP-002 | Deploy avec path | `POST /api/apps/deploy {"yaml_path": "/path/to/app.yaml"}` | Compile + deploy |
| APP-003 | Deploy upload | `POST /api/apps/deploy/upload` multipart avec fichier YAML | Upload + deploy |
| APP-004 | Deploy upload trop gros | Fichier > 1 MB | 413 Payload Too Large |
| APP-005 | Deploy YAML invalide | YAML avec erreurs de syntaxe | 400, message d'erreur clair |
| APP-006 | Deploy module inconnu | YAML referant module inexistant | 400, "Module xyz not found" |
| APP-007 | Deploy action inconnue | YAML avec action inexistante dans setup | 400, erreur de validation |
| APP-008 | Deploy force | `POST /api/apps/deploy {"yaml_path": "...", "force": true}` sur app deja deployee | Redeploy reussi |
| APP-009 | Deploy avec secrets | `POST /api/apps/deploy {..., "secrets": {"API_KEY": "xxx"}}` | Secrets stockes (Fernet), accessibles via {{secret.API_KEY}} |
| APP-010 | Deploy avec variables | [YAML] `variables: {workspace: "{{env.PWD}}"}` | Variables resolues dans toute la config |
| APP-011 | Deploy avec setup steps | [YAML] setup avec `database.connect` | Connexion etablie au bootstrap |
| APP-012 | Deploy setup step echoue | Setup step avec parametres invalides | BootstrapResult.success=false, erreur logguee |

### 3.2 Undeploy & List — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| APP-020 | Undeploy | `DELETE /api/apps/{app_id}` | App retiree, sessions nettoyees |
| APP-021 | List apps | `GET /api/apps/` | Liste de toutes les apps deployees |
| APP-022 | Get app detail | `GET /api/apps/{app_id}` | AppSummary avec modules, agents, tools |
| APP-023 | Get app non deployee | `GET /api/apps/inexistant` | 404 |
| APP-024 | Redeploy auto au demarrage | Deployer app, redemarrer daemon | App redeployee depuis DB |

### 3.3 Secrets — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| APP-030 | Set secret | `PUT /api/apps/{id}/secrets/MY_KEY {"value": "secret"}` | Secret chiffre en DB (Fernet) |
| APP-031 | List secrets | `GET /api/apps/{id}/secrets` | Liste des noms de cles (pas les valeurs) |
| APP-032 | Check secret exists | `GET /api/apps/{id}/secrets/MY_KEY` | `{"exists": true}` |
| APP-033 | Delete secret | `DELETE /api/apps/{id}/secrets/MY_KEY` | Secret supprime |
| APP-034 | Secret dans YAML | `{{secret.MY_KEY}}` dans config | Valeur resolue au deploy |
| APP-035 | Secret manquant dans YAML | `{{secret.MISSING}}` | Erreur de compilation claire |

---

## 4. Modes d'Execution

### 4.1 One-Shot — P0

```yaml
# [YAML] test-oneshot.yaml
app:
  app_id: test-oneshot
  name: One Shot Test

modules:
  filesystem: {}
  web: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You are a helpful assistant."
    modules: [filesystem, web]

execution:
  mode: one_shot
  workspace: "/tmp/test-workspace"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: web
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| EX-001 | Run one-shot | `POST /api/apps/test-oneshot/run {"input": "What is 2+2?"}` | Reponse textuelle, tool_calls_count >= 0 |
| EX-002 | One-shot avec tools | Input demandant recherche web | Tool calls executes, resultat integre |
| EX-003 | One-shot timeout | Configurer timeout: 5, tache longue | Timeout respecte, erreur retournee |
| EX-004 | One-shot max_turns | Configurer max_turns: 2 | Arret apres 2 tours |

### 4.2 Conversation — P0

```yaml
# [YAML] test-conversation.yaml
app:
  app_id: test-conversation
  name: Conversation Test

modules:
  filesystem: {}
  memory: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You are a helpful assistant. Use memory to remember important things."
    modules: [filesystem, memory]

execution:
  mode: conversation
  greeting: "Hello! How can I help you today?"
  workspace: "/tmp/test-workspace"
  max_turns: 50

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: memory
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| EX-010 | Chat premier message | `POST /api/apps/{id}/chat {"session_id": "s1", "message": "Hi"}` | Reponse + greeting envoye avant |
| EX-011 | Chat multi-tours | Envoyer 5 messages dans meme session | Contexte maintenu, messages accumules |
| EX-012 | Chat session differente | Meme app, session_id different | Historique isole |
| EX-013 | Chat streaming SSE | `POST /api/apps/{id}/chat/stream {"session_id": "s1", "message": "Hi"}` | Events SSE: token, tool_call, result |
| EX-014 | Chat concurrent meme session | 2 messages simultanes sur meme session_id | Lock per-session, serialisation |
| EX-015 | Chat workspace | Specifier workspace dans la requete | Outils filesystem confines a ce workspace |
| EX-016 | Context compaction | Envoyer messages jusqu'a depasser context_pressure_threshold (0.75) | Compaction auto, historique resume |

### 4.3 Background — P1

```yaml
# [YAML] test-background.yaml
app:
  app_id: test-background
  name: Background Test

modules:
  filesystem: {}
  shell: {}

agents:
  - id: monitor
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You monitor the system."
    modules: [filesystem, shell]

execution:
  mode: background
  workspace: "/tmp/test-bg"
  triggers:
    - id: health-check
      type: cron
      schedule: "*/5 * * * *"
      message: "Run health checks."

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: shell
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| EX-020 | Lancer background app | Deploy + activer | App tourne en arriere-plan |
| EX-021 | Background + channels | Ajouter trigger cron | Agent active periodiquement |

### 4.4 Pipeline — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| EX-030 | Pipeline 2 etapes | `POST /api/apps/{id}/pipeline` avec 2 steps | Execution sequentielle, templates resolus |
| EX-031 | Pipeline avec erreur | Step 1 echoue, on_error="stop" | Pipeline arrete, steps suivants non executes |
| EX-032 | Pipeline continue on error | Step 1 echoue, on_error="continue" | Steps suivants executes |
| EX-033 | Pipeline template resolution | `{{steps[0].output}}` dans step 2 | Output de step 1 injecte dans input de step 2 |

---

## 5. Modules — Filesystem

```yaml
# [YAML] test-filesystem.yaml
app:
  app_id: test-fs
  name: Filesystem Test

modules:
  filesystem:
    constraints:
      allowed_actions: [read, write, edit, glob, grep]

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You manage files."
    modules: [filesystem]

execution:
  mode: conversation
  workspace: "/tmp/test-fs-workspace"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| FS-001 | Read file | Agent lit un fichier existant | Contenu retourne avec numeros de ligne |
| FS-002 | Read file avec range | start_line=5, end_line=10 | Lignes 5-10 seulement |
| FS-003 | Read fichier inexistant | Chemin invalide | Erreur claire |
| FS-004 | Read PDF | Fichier .pdf | Extraction texte |
| FS-005 | Read image | Fichier .png/.jpg | Rendu visuel pour LLM multimodal |
| FS-006 | Write file | Agent cree un nouveau fichier | Fichier cree dans workspace |
| FS-007 | Write hors workspace | Tenter ecriture dans /etc/ | Refuse par contrainte paths |
| FS-008 | Edit file | old_string → new_string | Remplacement exact |
| FS-009 | Edit old_string non unique | Deux occurrences de old_string | Erreur "not unique" |
| FS-010 | Edit replace_all | replace_all=true | Toutes les occurrences remplacees |
| FS-011 | Ls directory | Liste un repertoire | Noms, types, tailles retournes |
| FS-012 | Ls recursive | recursive=true | Arborescence complete |
| FS-013 | Grep regex | pattern="TODO.*fix" | Lignes matchant avec contexte |
| FS-014 | Grep output modes | output_mode: content, files_with_matches, count | 3 formats differents |
| FS-015 | Glob pattern | pattern="**/*.py" | Fichiers Python tries par mtime |
| FS-016 | Mv fichier | Renommer/deplacer | Fichier deplace |
| FS-017 | Cp fichier | Copier fichier | Copie creee |
| FS-018 | Rm fichier | Supprimer fichier | Fichier supprime |
| FS-019 | Rm recursive | Supprimer repertoire | Repertoire et contenu supprimes |
| FS-020 | Undo apres edit | Modifier puis undo | Fichier restaure a la version precedente |
| FS-021 | Checkpoint limit | Creer 6 checkpoints (max=5) | Plus ancien supprime |
| FS-022 | Path traversal | Tenter `../../etc/passwd` | Refuse par _resolve() + allowed_roots |
| FS-023 | Symlink escape | Creer symlink vers hors workspace | resolve() suit le symlink, _check_path() refuse |
| FS-024 | Max file size | Tenter lecture fichier > max_file_size | Refuse par contrainte |

---

## 6. Modules — Shell

```yaml
# [YAML] test-shell.yaml
app:
  app_id: test-shell
  name: Shell Test

modules:
  shell:
    constraints:
      allowed_paths: ["/tmp/test-shell-workspace"]

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You execute shell commands."
    modules: [shell]

execution:
  mode: conversation
  workspace: "/tmp/test-shell-workspace"

capabilities:
  default_policy: auto
  grant:
    - module: shell
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SH-001 | Commande simple | `echo "hello"` | stdout: "hello", exit_code: 0 |
| SH-002 | Commande avec erreur | `ls /nonexistent` | exit_code != 0, stderr capture |
| SH-003 | Timeout | Commande avec timeout=2, `sleep 10` | Timeout apres 2s |
| SH-004 | Timeout par defaut | Commande longue sans timeout explicite | Timeout 30s par defaut |
| SH-005 | Commande blacklistee | `rm -rf /` | Refuse par forbidden patterns |
| SH-006 | Fork bomb | `:(){ :|:& };:` | Bloque par blacklist |
| SH-007 | Path confinement | `cat /etc/passwd` | Refuse (chemin absolu hors workspace) |
| SH-008 | CWD persistence | `cd subdir` puis `pwd` | CWD change persiste entre appels |
| SH-009 | Output sanitization | Commande affichant $ANTHROPIC_API_KEY | Valeur redactee dans output |
| SH-010 | Output truncation | Commande generant > 1 MB | Tronque a max_output_bytes |
| SH-011 | Large output persistence | Output > 30 KB | Sauvegarde sur disque, lien retourne |
| SH-012 | Background task | `bash_background` avec commande longue | task_id retourne, execution async |
| SH-013 | Background status | `bash_status` avec task_id | Status + tail du output |
| SH-014 | Background kill | `bash_status` avec kill=true | Process termine |
| SH-015 | Sleep detection | `sleep 30` (bare) | Bloque (> 2s nu) |
| SH-016 | Sed interception | `sed -i 's/old/new/' file.txt` | Converti en filesystem.edit |
| SH-017 | Safe system paths | `/usr/bin/python3 --version` | Autorise (dans _SAFE_SYSTEM_PATHS) |

---

## 7. Modules — Git

```yaml
# [YAML] test-git.yaml
app:
  app_id: test-git
  name: Git Test

modules:
  filesystem: {}
  shell: {}     # Git is done via `Bash("git ...")` — the git module was removed.

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You manage a git repository via `Bash`."
    modules: [filesystem, shell]

execution:
  mode: conversation
  workspace: "/tmp/test-git-repo"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: shell
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| GIT-001 | Status | `git.status` sur repo initialise | Branche, staged, unstaged, untracked |
| GIT-002 | Diff unstaged | Modifier fichier, `git.diff` | Diff affiche |
| GIT-003 | Diff staged | Stager fichier, `git.diff target=staged` | Diff staged |
| GIT-004 | Log | `git.log limit=10` | 10 derniers commits |
| GIT-005 | Blame | `git.blame file=README.md` | Auteur/date par ligne |
| GIT-006 | Show | `git.show ref=HEAD` | Info commit + diff |
| GIT-007 | Add + Commit | Creer fichier, add, commit | Commit cree avec message |
| GIT-008 | Branch create + checkout | Creer branche, switch | Nouvelle branche active |
| GIT-009 | Stash push/pop | Modifier, stash push, verifier clean, stash pop | Modifications restaurees |
| GIT-010 | Tag create/list | Creer tag, lister | Tag present |
| GIT-011 | Push | Push vers remote | Commits envoyes (necessite remote) |
| GIT-012 | Pull | Pull depuis remote | Commits recus |
| GIT-013 | Reset soft | `git.reset mode=soft ref=HEAD~1` | Commit annule, changes staged |
| GIT-014 | Reset hard | `git.reset mode=hard ref=HEAD~1` | Tout annule (dangereux) |
| GIT-015 | Merge | Merge branche | Merge fast-forward ou 3-way |
| GIT-016 | PR create | `git.pr_create title="..." body="..."` | PR creee (necessite GitHub remote) |
| GIT-017 | Branch list | `git.branch_list all=true` | Branches locales + remote |

---

## 8. Modules — HTTP & Web

```yaml
# [YAML] test-http.yaml
app:
  app_id: test-http
  name: HTTP Test

modules:
  http: {}
  web:
    config:
      search_backend: duckduckgo
      cache_ttl: 300

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You fetch web content and make HTTP requests."
    modules: [http, web]

execution:
  mode: conversation
  workspace: "/tmp/test-http"

capabilities:
  default_policy: auto
  grant:
    - module: http
    - module: web
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| HTTP-001 | GET request | `http.get url="https://httpbin.org/get"` | 200, JSON body |
| HTTP-002 | POST JSON | `http.post url="https://httpbin.org/post" json_body={...}` | 200, echo body |
| HTTP-003 | PUT/PATCH/DELETE | Requetes vers httpbin | Methodes correctes |
| HTTP-004 | Headers custom | `headers: {"X-Custom": "value"}` | Header envoye |
| HTTP-005 | Timeout | `timeout=1` vers endpoint lent | Timeout erreur |
| HTTP-006 | Download file | `http.download url="..." path="/tmp/file"` | Fichier telecharge |
| HTTP-007 | Fetch page | `http.fetch_page url="..."` | HTML parse en texte/markdown |
| HTTP-008 | Upload file | `http.upload_file url="..." file_path="..."` | Multipart upload |
| HTTP-009 | SSRF protection private IP | `http.get url="http://192.168.1.1"` | Refuse (IP privee) |
| HTTP-010 | SSRF protection localhost | `http.get url="http://127.0.0.1:8000"` | Refuse (loopback) |
| HTTP-011 | SSRF DNS rebinding | URL pointant vers IP privee apres resolution | IP pinnee, refuse |
| HTTP-012 | Allowed hosts | Configurer allowed_hosts, tenter autre domaine | Refuse |
| HTTP-013 | Blocked hosts | Configurer blocked_hosts | Bloque |
| HTTP-014 | Sensitive header masking | Request avec Authorization header | Masque dans les logs |
| WEB-001 | Web search | `web.search query="python tutorial"` | Resultats de recherche |
| WEB-002 | Web fetch | `web.fetch url="https://example.com"` | Contenu parse |
| WEB-003 | Web search backend fallback | Backend principal down | Fallback vers secondaire |
| WEB-004 | Web fetch cache | Fetch meme URL 2x | 2eme appel depuis cache (TTL 300s) |
| WEB-005 | Web extract CSS | `web.extract url="..." selector="h1"` | Elements h1 extraits |

---

## 9. Modules — Database

```yaml
# [YAML] test-database.yaml
app:
  app_id: test-db
  name: Database Test

modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "/tmp/test.db"

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You manage a SQLite database."
    modules: [database]

execution:
  mode: conversation
  workspace: "/tmp/test-db"

capabilities:
  default_policy: auto
  grant:
    - module: database
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| DB-001 | Connect SQLite | Setup step connect | Connexion active |
| DB-002 | Connect PostgreSQL | Driver postgres, URL valide | Connexion active |
| DB-003 | List tables | `database.list_tables` | Tables listees avec metadata |
| DB-004 | Execute CREATE | `CREATE TABLE test (id INTEGER, name TEXT)` | Table creee |
| DB-005 | Execute INSERT | `INSERT INTO test VALUES (1, 'hello')` | Row inseree |
| DB-006 | Fetch SELECT | `SELECT * FROM test` | Rows retournees |
| DB-007 | Paginated fetch | `database.fetch_paginated page_size=10` | Pagination cursor |
| DB-008 | Bulk insert | `database.bulk_insert table=test rows=[...]` | Insertion batch |
| DB-009 | Upsert | `database.upsert table=test unique_keys=[id]` | Insert ou update |
| DB-010 | Explain query | `database.explain_query` | Plan d'execution |
| DB-011 | Schema introspect | `database.introspect` | Schema complet en une requete |
| DB-012 | Sample | `database.sample table=test limit=5` | 5 rows aleatoires |
| DB-013 | Read-only policy | `set_policy read_only=true`, tenter INSERT | Refuse |
| DB-014 | Table blacklist | Blacklist "secret_table", tenter SELECT | Refuse |
| DB-015 | Column redaction | Blacklist colonne "password_hash" | Valeur remplacee par ***REDACTED*** |
| DB-016 | Max rows | `max_rows_returned=10`, SELECT sans LIMIT | LIMIT 10 injecte |
| DB-017 | Query timeout | `max_query_time_seconds=1`, requete lente | Timeout |
| DB-018 | SQL injection via params | `SELECT * FROM test WHERE id = :id` avec params | Parametre bind, pas d'injection |
| DB-019 | SQL injection via identifiant | Table name avec injection | validate_sql_identifier() refuse |
| DB-020 | Transaction begin/commit | BEGIN, INSERT, COMMIT | Transaction atomique |
| DB-021 | Transaction rollback | BEGIN, INSERT, ROLLBACK | Pas de changement |
| DB-022 | Blocked statements | `blocked_statements: [DROP]`, tenter DROP | Refuse |
| DB-023 | Audit log | Executer requetes, consulter audit | Logs structures avec timing |
| DB-024 | Disconnect | `database.disconnect` | Connexion fermee |

---

## 10. Modules — MCP

```yaml
# [YAML] test-mcp.yaml
app:
  app_id: test-mcp
  name: MCP Test

modules:
  mcp:
    servers:
      github:
        token: "{{secret.GITHUB_TOKEN}}"
    config:
      cache:
        scope: auto
        ttl: 300

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You use MCP tools."
    modules: [mcp]

execution:
  mode: conversation
  workspace: "/tmp/test-mcp"

capabilities:
  default_policy: auto
  grant:
    - module: mcp
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MCP-001 | Install server | `POST /api/mcp/servers {"server_id": "github"}` | Serveur installe |
| MCP-002 | Test server | `POST /api/mcp/servers/github/test` | connection_ok, tools_discovered |
| MCP-003 | Connect pool | `POST /api/mcp/pool/github/connect` | Serveur connecte dans le pool |
| MCP-004 | List servers | `GET /api/mcp/servers` | Liste avec status |
| MCP-005 | Search servers | `GET /api/mcp/search?q=github` | Resultats catalogue + registry |
| MCP-006 | Server config update | `PUT /api/mcp/servers/github/config` | Config mise a jour |
| MCP-007 | Disconnect server | `POST /api/mcp/pool/github/disconnect` | Deconnecte |
| MCP-008 | Remove server | `DELETE /api/mcp/servers/github` | Serveur supprime |
| MCP-009 | Pool health check | `GET /api/mcp/pool/health` | Health status par serveur |
| MCP-010 | Env sandboxing | Verifier env du subprocess MCP | Pas de DIGITORN_DB_URL, DATABASE_URL, etc. |
| MCP-011 | Safe env inherited | Verifier PATH, HOME dans subprocess | Presents |
| MCP-012 | Blocked env | Verifier PRIVATE_KEY dans subprocess | Absent |
| MCP-013 | OAuth MCP flow | `GET /api/apps/{id}/oauth/authorize?server_id=...` | URL d'autorisation generee |
| MCP-014 | OAuth callback | Simuler callback | Token injecte dans serveur |
| MCP-015 | Tool cache | Appeler meme outil 2x | 2eme depuis cache |
| MCP-016 | Ref counting pool | 2 apps acquire meme serveur, 1 release | Serveur reste ouvert |
| MCP-017 | Pool auto-reconnect | Serveur crash | Reconnexion auto par health monitor |

---

## 11. Modules — Memory

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MEM-001 | Set goal | `memory.set_goal goal="Deploy the app"` | Goal stocke |
| MEM-002 | Remember fact | `memory.remember content="API key is in vault"` | Fact stocke avec ID |
| MEM-003 | Recall | `memory.recall query="API key"` | Fact retrouve |
| MEM-004 | Forget | `memory.forget fact_id="..."` | Fact supprime |
| MEM-005 | Add todo | `memory.add_todo content="Fix bug"` | Todo ajoute |
| MEM-006 | Update todo | `memory.update_todo todo_id="..." status="done"` | Status mis a jour |
| MEM-007 | Memory persistence | Redemarrer daemon, reload session | Memory restauree |
| MEM-008 | Memory dans compaction | Declencher compaction contexte | Memory survit a la compaction |

---

## 12. Modules — Agent Spawn

```yaml
# [YAML] test-multi-agent.yaml
app:
  app_id: test-agents
  name: Multi Agent Test

modules:
  filesystem: {}
  web: {}
  memory: {}

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You coordinate tasks by spawning specialist agents."
    pool:
      max_workers: 3
    modules: [filesystem, web, memory]

  - id: researcher
    role: specialist
    specialty: "Research topics on the web"
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You research topics."
    modules: [web]

execution:
  mode: conversation
  workspace: "/tmp/test-agents"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: web
    - module: memory
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| AG-001 | Spawn agent | `agent_spawn.spawn_agent task="Research X"` | Agent cree, agent_id retourne |
| AG-002 | Agent status | `agent_spawn.agent_status agent_id="..."` | Status running/completed |
| AG-003 | Agent result | `agent_spawn.agent_result agent_id="..."` | Resultat de l'agent |
| AG-004 | Agent cancel | `agent_spawn.agent_cancel agent_id="..."` | Agent annule |
| AG-005 | Agent wait | `agent_spawn.agent_wait agent_id="..." timeout=60` | Bloque jusqu'a fin |
| AG-006 | Agent wait_all | Spawn 3 agents, wait_all | Attend tous |
| AG-007 | Agent list | `agent_spawn.agent_list` | Liste avec status |
| AG-008 | Specialist explore | `spawn_agent specialist="explore"` | Agent rapide recherche |
| AG-009 | Specialist plan | `spawn_agent specialist="plan"` | Agent architecture |
| AG-010 | Agent timeout | `spawn_agent timeout=5`, tache longue | Timeout, status="timeout" |
| AG-011 | Max workers | Spawn 4 agents avec max_workers=3 | 4eme attend un slot |
| AG-012 | Agent isolation | Agent essaie de modifier etat parent | Isole, pas d'effet |
| AG-013 | Agent events SSE | Spawn agent pendant stream | Events agent_event (spawned, completed) |
| AG-014 | Reassign agent | `agent_spawn.reassign_agent` | Cancel + respawn |

---

## 13. Modules — Notebook

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| NB-001 | Execute code | `notebook.execute_cell code="print(42)"` | stdout: "42" |
| NB-002 | State persistence | Definir variable, lire dans cellule suivante | Variable persistee |
| NB-003 | Import modules | `import math; print(math.pi)` | Import reussi |
| NB-004 | Matplotlib capture | `import matplotlib.pyplot as plt; plt.plot([1,2,3])` | Image base64 capturee |
| NB-005 | Error handling | `1/0` | ZeroDivisionError capture |
| NB-006 | Restart kernel | `notebook.restart_kernel` | Namespace reinitialise |
| NB-007 | Get variables | `notebook.get_variables` | Variables du namespace |
| NB-008 | Timeout (si implemente) | Code avec boucle infinie | Timeout respecte (actuellement un gap) |

---

## 14. Modules — PDF, Presentation, Spreadsheet

### PDF — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| PDF-001 | Read PDF | `pdf.read path="doc.pdf"` | Texte extrait |
| PDF-002 | Read pages | `pdf.read path="doc.pdf" pages="1-3"` | Pages 1-3 seulement |
| PDF-003 | Generate PDF | `pdf.generate title="..." sections=[...]` | PDF cree |
| PDF-004 | Merge PDFs | `pdf.merge input_paths=[...] output_path="..."` | PDFs fusionnes |
| PDF-005 | Extract tables | `pdf.read_tables path="..."` | Tables en CSV/JSON |
| PDF-006 | Metadata | `pdf.metadata path="..."` | Auteur, sujet, etc. |

### Presentation — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| PRES-001 | New presentation | `presentation.new_presentation title="..."` | Presentation creee |
| PRES-002 | Add slides | `presentation.add_slide layout=... content=...` | Slides ajoutees |
| PRES-003 | Finalize PPTX | `presentation.finalize_presentation format="pptx"` | Fichier .pptx cree |
| PRES-004 | Finalize PDF | `format="pdf"` | Fichier .pdf cree |

### Spreadsheet — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SS-001 | Create workbook | `spreadsheet.create path="..." sheets=[...]` | Fichier Excel cree |
| SS-002 | Read workbook | `spreadsheet.read path="..."` | Donnees lues |
| SS-003 | Edit cells | `spreadsheet.edit path="..." range="A1:B2"` | Cellules modifiees |

---

## 15. Modules — Browser & Computer Use

### Browser — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| BR-001 | Browse URL | `browser.browse url="https://example.com"` | Page chargee |
| BR-002 | Screenshot | `browser.screenshot` | Image base64 |
| BR-003 | Click element | `browser.click selector="button#submit"` | Click execute |
| BR-004 | Type text | `browser.type selector="input#search" text="query"` | Texte saisi |
| BR-005 | Extract text | `browser.extract selector="h1"` | Texte extrait |

### Computer Use — P3

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| CU-001 | Screenshot desktop | `computer_use.screenshot` | Capture ecran |
| CU-002 | Mouse click | `computer_use.mouse_click x=100 y=200` | Click a la position |
| CU-003 | Keyboard type | `computer_use.keyboard_type text="hello"` | Texte tape |
| CU-004 | Display info | `computer_use.display_info` | Resolution, position souris |

---

## 16. Modules — Channels

```yaml
# [YAML] test-channels.yaml
app:
  app_id: test-channels
  name: Channels Test

modules:
  filesystem: {}
  channels:
    config:
      default_agent: main
      providers:
        cron_test:
          adapter: cron
          config:
            schedule: "*/5 * * * *"
          activation:
            message: "Run scheduled check"
        webhook_test:
          adapter: webhook
          config:
            inbound_path: "/hook/test"
          activation:
            message: "Webhook received: {{event.payload}}"
            filter:
              - field: event.payload.action
                equals: "trigger"

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You handle events."
    modules: [filesystem, channels]

execution:
  mode: background
  workspace: "/tmp/test-channels"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: channels
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| CH-001 | Cron trigger | Attendre declenchement cron | Agent active avec message |
| CH-002 | Webhook inbound | POST vers /hook/test avec payload | Agent active avec payload |
| CH-003 | Webhook filter match | Payload avec action="trigger" | Agent active |
| CH-004 | Webhook filter no match | Payload avec action="other" | Agent PAS active |
| CH-005 | File watcher | Modifier fichier surveille | Agent active avec nom fichier |
| CH-006 | Send message | `channels.send_message channel=... message=...` | Message envoye |
| CH-007 | Broadcast | `channels.broadcast message=... providers=[...]` | Message envoye a tous |
| CH-008 | HMAC webhook verification | Envoyer webhook avec signature HMAC | Signature verifiee |
| CH-009 | Webhook sans HMAC | Envoyer webhook sans signature (si requis) | Rejete |
| CH-010 | Payload sanitization | Webhook avec `__proto__` dans payload | Cle dangereuse supprimee |
| CH-011 | Outbound secret filtering | Reponse contenant cle API | Cle remplacee par [REDACTED] |
| CH-012 | Activation prepare | Prepare step avec database.fetch | Donnees injectees dans context |
| CH-013 | Activation route | Routing conditionnel par champ | Agent correct selectionne |
| CH-014 | Session strategy | `session: "per_event"` | Session unique par event |
| CH-015 | Provider status | `channels.provider_status provider=...` | Status du provider |

---

## 17. Modules — RAG & Vector

```yaml
# [YAML] test-rag.yaml
app:
  app_id: test-rag
  name: RAG Test

modules:
  filesystem: {}
  rag:
    chunking:
      strategy: paragraph
      size: 800
      overlap: 100
    pipeline:
      retrieval: hybrid
      bm25_weight: 0.4
      semantic_weight: 0.6
      final_top_k: 5
    backend:
      type: qdrant
      path: "/tmp/test-rag-data"
  vector: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You answer questions using the knowledge base."
    modules: [filesystem, rag, vector]

execution:
  mode: conversation
  workspace: "/tmp/test-rag"

capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: rag
    - module: vector
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| RAG-001 | Create KB | `rag.create_knowledge_base name="test_kb"` | KB creee |
| RAG-002 | Ingest file | `rag.ingest_file kb_name="test_kb" file_path="..."` | Fichier chunke et indexe |
| RAG-003 | Ingest directory | `rag.ingest_directory kb_name="test_kb" directory="..."` | Repertoire indexe |
| RAG-004 | Query KB | `rag.query kb_name="test_kb" query="..."` | Chunks pertinents retournes |
| RAG-005 | Hybrid search | Pipeline hybrid avec bm25 + semantic | Resultats combines |
| RAG-006 | Multi-query | `rag.multi_query queries=[...]` | Resultats de N requetes |
| RAG-007 | Delete KB | `rag.delete_knowledge_base name="test_kb"` | KB supprimee |
| VEC-001 | Create collection | `vector.create_collection name="test"` | Collection creee |
| VEC-002 | Add documents | `vector.add collection="test" documents=[...]` | Documents indexes |
| VEC-003 | Search | `vector.search collection="test" query="..."` | Resultats par similarite |
| VEC-004 | Hybrid search | `vector.hybrid_search` | Texte + vecteur |
| VEC-005 | Delete collection | `vector.delete_collection name="test"` | Collection supprimee |
| VEC-006 | Search multi | Chercher dans plusieurs collections | Resultats agreges |

---

## 18. Modules — Cache, Queue, Cron

### Cache — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| CA-001 | Set/Get | `cache.set key="k" value="v"` puis `cache.get key="k"` | Valeur retournee |
| CA-002 | TTL expiration | Set avec ttl=2, attendre 3s, get | null (expire) |
| CA-003 | Delete | `cache.delete key="k"` | Supprime |
| CA-004 | Increment | `cache.increment key="counter"` | Compteur atomique |
| CA-005 | Bulk operations | `cache.bulk_set` puis `cache.bulk_get` | Operations batch |
| CA-006 | Clear namespace | `cache.clear namespace="test"` | Namespace vide |

### Queue — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| QU-001 | Create queue | `queue.create_queue name="tasks"` | Queue creee |
| QU-002 | Publish/Receive | Publier message, recevoir | Message recu |
| QU-003 | Ack/Nack | Recevoir, ack ou nack | Confirme ou requeue |
| QU-004 | Priority queue | Messages avec priorites differentes | Ordre par priorite |
| QU-005 | Dead letter | Nack N fois | Message dans DLQ |

### Cron Native — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| CR-001 | Create schedule | `cron_native.create_schedule cron="*/1 * * * *"` | Schedule active |
| CR-002 | Validate cron | `cron_native.validate_cron cron="invalid"` | Erreur validation |
| CR-003 | Explain cron | `cron_native.explain_cron cron="0 9 * * 1-5"` | "Every weekday at 9:00" |
| CR-004 | Next runs | `cron_native.next_runs count=5` | 5 prochaines executions |
| CR-005 | Pause/Resume | Pause puis resume schedule | Respect des etats |
| CR-006 | Execution history | `cron_native.execution_history` | Historique des runs |
| CR-007 | Run now | `cron_native.run_now schedule_id="..."` | Execution immediate |

---

## 19. Modules — Context Builder

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| CB-001 | Tool discovery | Agent demande outil non charge | Context builder decouvre et propose |
| CB-002 | Tool search | `GET /api/apps/{id}/tools/search?query=file` | Outils pertinents trouves |
| CB-003 | Tool categories | `GET /api/apps/{id}/tools/categories` | Categories avec compteurs |
| CB-004 | Tool schema | `GET /api/apps/{id}/tools/filesystem.read` | JSON Schema complet |
| CB-005 | Tool direct execute | `POST /api/apps/{id}/tools/filesystem.read/execute` | Execution directe |
| CB-006 | Full index | `GET /api/apps/{id}/index` | Index complet des outils |

---

## 20. Securite — Security Gate (7 portes)

```yaml
# [YAML] test-security-gates.yaml
app:
  app_id: test-security
  name: Security Gate Test

modules:
  filesystem: {}
  shell: {}
  http: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You test security gates."
    modules: [filesystem, shell, http]

execution:
  mode: conversation
  workspace: "/tmp/test-security"

capabilities:
  default_policy: approve
  max_risk_level: medium
  grant:
    - module: filesystem
      actions: [read, grep, glob]
  approve:
    - module: filesystem
      actions: [write, edit]
    - module: shell
  deny:
    - module: http
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SG-001 | Gate 0 — App inactive | Desactiver le profil (is_active=false) | Toute action refuse |
| SG-002 | Gate 1 — Module hidden | Mettre module visibility="hidden" | Actions de ce module refusees |
| SG-003 | Gate 2 — Risk level | Action risk="high" avec max_risk_level="medium" | Refuse |
| SG-004 | Gate 2 — Risk OK | Action risk="low" avec max_risk_level="medium" | Passe |
| SG-005 | Gate 3 — Permission manquante | Action requiert "net.http", pas dans granted | Refuse |
| SG-006 | Gate 3 — Permission glob | Permission "fs.*" vs check "fs.read" | Autorise (fnmatch) |
| SG-007 | Gate 4 — Policy auto | Action dans grant list | Execution auto sans approbation |
| SG-008 | Gate 4 — Policy approve | Action dans approve list | ApprovalRequiredError leve |
| SG-009 | Gate 4 — Policy block | Action dans deny list (http) | PermissionDeniedError |
| SG-010 | Gate 4 — Action override | Override specifique dans module grant | Override prend precedence |
| SG-011 | Gate 4 — Risk approval rules | `risk_approval_rules: {high: block}` | Action high bloquee |
| SG-012 | Gate 5 — Data classification | Action "confidential" vs max "internal" | Refuse |
| SG-013 | Gate 6 — Rate limit | Depasser le rate limit par action | Refuse temporairement |
| SG-014 | Resolution cascade | Pas d'override → risk rule → module default → app default | Cascade correcte |
| SG-015 | Admin bypass | Profil is_admin=true | Toutes les portes passent |
| SG-016 | Audit logging | Chaque decision | Event audit avec gate, decision, reason |

---

## 21. Securite — Sandbox OS

### 21.1 Linux Sandbox — P0 (si deploiement Linux)

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SB-001 | Landlock filesystem | Worker sandbox tente lire /etc/shadow | EPERM (kernel refuse) |
| SB-002 | Landlock write restriction | Worker tente ecrire hors workspace | EPERM |
| SB-003 | Landlock degradation | Kernel < 5.13 | Fallback gracieux, warning log |
| SB-004 | seccomp mount | Worker tente mount() | EPERM (syscall bloque) |
| SB-005 | seccomp ptrace | Worker tente ptrace() | EPERM |
| SB-006 | seccomp reboot | Worker tente reboot() | EPERM |
| SB-007 | seccomp execve conditionnel | Shell module absent, tenter exec | Bloque |
| SB-008 | seccomp execve autorise | Shell module present | exec autorise |
| SB-009 | seccomp network conditionnel | Web/http/database absent | Socket bloque |
| SB-010 | seccomp arch validation | Tentative syscall 32-bit | Refuse (architecture mismatch) |
| SB-011 | Hardening no_new_privs | Worker tente suid | Refuse (PR_SET_NO_NEW_PRIVS) |
| SB-012 | Hardening no dumpable | Worker tente /proc/self/mem | Refuse |
| SB-013 | Hardening cap drop | Worker tente cap_sys_admin | Refuse (capabilities droppees) |
| SB-014 | cgroups memory | Worker depasse limite memoire | OOM killer, pas de crash daemon |
| SB-015 | cgroups CPU | Worker depasse quota CPU | Throttle |
| SB-016 | Namespace PID | Worker voit uniquement ses PIDs | Isolation PID |
| SB-017 | Namespace network | sandbox strict, tenter connexion externe | Loopback only (si net NS actif) |
| SB-018 | Sandbox level off | `sandbox.level: off` | Pas d'isolation OS (dev mode) |
| SB-019 | Sandbox level standard | `sandbox.level: standard` | Landlock + seccomp + hardening + cgroups |
| SB-020 | Sandbox level strict | `sandbox.level: strict` | + warm pool + user NS + PID NS |
| SB-021 | Sandbox level maximum | `sandbox.level: maximum` | + network NS + seccomp-notify + CoW |

### 21.2 Windows Sandbox — P1 (si deploiement Windows)

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SB-030 | Job Object memory | Worker depasse limite memoire | Process termine |
| SB-031 | Job Object process count | Worker fork excessif | Limite active processes |
| SB-032 | Process Mitigation DEP | Code en memoire writeable | Data Execution Prevention active |
| SB-033 | Kill on job close | Daemon ferme | Workers tues automatiquement |

### 21.3 macOS Sandbox — P2

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SB-040 | XPC sandbox | Worker sandbox macOS | Restrictions filesystem/network |

---

## 22. Securite — Profils & Policies

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SP-001 | Create profile | `POST /api/security/profiles {"app_id": "..."}` | Profil cree |
| SP-002 | Get profile | `GET /api/security/profiles/{app_id}` | Profil avec grants |
| SP-003 | Update profile | `PATCH /api/security/profiles/{app_id}` | Champs mis a jour |
| SP-004 | Delete profile | `DELETE /api/security/profiles/{app_id}` | Profil + grants supprimes |
| SP-005 | Create grant | `PUT /api/security/profiles/{id}/grants/filesystem` | Grant cree |
| SP-006 | Action overrides | Grant avec action_overrides | Overrides respectes |
| SP-007 | Delete grant | `DELETE /api/security/profiles/{id}/grants/filesystem` | Grant supprime |
| SP-008 | List grants | `GET /api/security/profiles/{id}/grants` | Liste complete |
| SP-009 | Module visibility hidden | Grant visibility="hidden" | Module invisible + actions bloquees |
| SP-010 | Max risk level | `max_risk_level: "low"` | Actions medium/high refusees |

---

## 23. Middleware Pipeline

### 23.1 App-Level Middleware — P1

```yaml
# Ajouter au YAML de test
app:
  middleware:
    - mask_secrets:
        patterns: ["password", "api_key"]
    - content_filter:
        block_patterns: ["DROP TABLE", "rm -rf"]
        rejection_message: "Blocked for safety."
    - prompt_inject:
        system: "Always respond in French."
    - rag_inject:
        max_chunks: 5
        max_chars: 2000
    - response_filter:
        max_length: 5000
        mask_secrets: true
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MW-001 | Mask secrets before | Message user contenant "password: abc123" | "abc123" masque avant envoi LLM |
| MW-002 | Content filter block | Message "DROP TABLE users" | Rejete avec message custom |
| MW-003 | Content filter pass | Message normal | Passe sans blocage |
| MW-004 | Prompt inject append | Verifier system prompt | Texte injecte a la fin |
| MW-005 | Prompt inject prepend | `position: prepend` | Texte injecte au debut |
| MW-006 | RAG inject | Question correspondant a la KB | Chunks injectes dans system prompt |
| MW-007 | Response filter length | Reponse LLM > 5000 chars | Tronquee avec "[Response truncated]" |
| MW-008 | Response filter secrets | Reponse contenant token | Token masque |

### 23.2 Module-Level Middleware — P1

```yaml
modules:
  database:
    middleware:
      - audit:
          log_params: true
      - retry:
          max_attempts: 3
          backoff: exponential
      - timeout:
          seconds: 30
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MW-010 | Module audit | Executer action database | Log structure avec timing |
| MW-011 | Module retry | Action echoue 2x puis reussit | 3 tentatives, succes final |
| MW-012 | Module retry all fail | Action echoue 3x | Erreur apres 3 tentatives |
| MW-013 | Module timeout | Action > 30s | TimeoutError |

### 23.3 MCP-Level Middleware — P1

```yaml
mcp:
  middleware:
    - retry:
        max_attempts: 3
    - timeout:
        seconds: 30
    - audit:
        log_params: true
    - budget:
        max_calls_per_hour: 100
    - circuit_breaker:
        failure_threshold: 3
        recovery_timeout: 60
    - dedup:
        window_seconds: 5
    - semantic_cache:
        similarity_threshold: 0.85
        ttl: 300
    - auto_heal:
        max_suggestions: 3
    - streaming:
        slow_threshold: 5
    - context:
        max_entries: 20
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MW-020 | MCP retry | Appel MCP echoue 1x | Retry reussi |
| MW-021 | MCP timeout | Appel MCP > 30s | Timeout |
| MW-022 | MCP audit | Appel MCP | Log mcp_audit avec timing |
| MW-023 | MCP budget limit | 101 appels en 1h | BudgetExceededError au 101eme |
| MW-024 | MCP budget per server | Limite serveur specifique | Limite respectee par serveur |
| MW-025 | Circuit breaker open | 3 echecs consecutifs | Circuit ouvert, appels suivants refusent immediatement |
| MW-026 | Circuit breaker recovery | Attendre recovery_timeout | Half-open → probe → close |
| MW-027 | Dedup window | Meme appel 2x en 5s | 2eme retourne cache |
| MW-028 | Semantic cache hit | Appel semantiquement similaire | Cache hit (similarity > 0.85) |
| MW-029 | Auto heal suggestions | Appel echoue | Suggestions d'alternatives |
| MW-030 | Streaming slow detection | Appel > 5s | Warning log, slow_call=true |
| MW-031 | Cross-server context | Appel server A puis B | Contexte de A disponible pour B |

---

## 24. Systeme d'Evenements & Streaming

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| EV-001 | SSE token events | Chat stream | Events `token` avec delta |
| EV-002 | SSE tool_start | Agent commence outil | Event `tool_start` avec nom et params |
| EV-003 | SSE tool_call | Agent finit outil | Event `tool_call` avec resultat |
| EV-004 | SSE thinking | Agent Claude avec extended thinking | Event `thinking` avec texte |
| EV-005 | SSE preview:resource_set (channel=files) | Agent lit/ecrit/cree fichier | Event `preview_delta` type=`resource_set` channel=`files` |
| EV-006 | SSE preview:resource_patched (channel=files) | Agent modifie fichier | Event `preview_delta` type=`resource_patched` channel=`files` |
| EV-007 | SSE preview:resource_set (channel=diagnostics) | LSP diagnostics apres write | Event `preview_delta` sur channel `diagnostics` (shape LSP) |
| EV-008 | SSE memory_update | Agent set_goal ou remember | Event `memory_update` |
| EV-009 | SSE agent_event | Agent spawn sub-agent | Event `agent_event` spawned/completed |
| EV-010 | SSE result | Tour termine | Event `result` avec metadata |
| EV-011 | SSE error | Erreur d'execution | Event `error` |
| EV-012 | SSE diagnostics | Fichier avec erreurs | Event `diagnostics` |
| EV-013 | Session event bus | Subscribe via GET /sessions/{id}/events | Events recus en temps reel |
| EV-014 | Causality chain | Event → child events | correlation_id, causation_id coherents |

---

## 25. Workflow d'Approbation

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| AP-001 | Approval required | Action avec policy="approve" | Agent bloque, event `approval_request` |
| AP-002 | Approve action | `POST /api/apps/{id}/approve {"request_id": "...", "approved": true}` | Agent reprend, outil execute |
| AP-003 | Deny action | `POST /api/apps/{id}/approve {"request_id": "...", "approved": false}` | Agent recoit message de refus |
| AP-004 | Approval timeout | Ne pas repondre pendant 5 min | Timeout, agent informe |
| AP-005 | List pending | `GET /api/apps/{id}/approvals` | Liste des approbations en attente |
| AP-006 | Approval concurrent | Approbation pendant que autre outil tourne | Pas de blocage des autres outils |
| AP-007 | Approval SSE | Approval request en streaming | Event `approval_request` dans SSE |

---

## 26. Taches de Fond & Watchers

### Taches de Fond — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| BG-001 | Launch task | `POST /api/apps/{id}/background-tasks {"tool": "...", "params": {...}}` | task_id retourne |
| BG-002 | Task status | `GET /api/apps/{id}/background-tasks/{task_id}` | Status pending/running/completed |
| BG-003 | Task result | Task complete, get result | Resultat disponible |
| BG-004 | Cancel task | `DELETE /api/apps/{id}/background-tasks/{task_id}` | Task annulee |
| BG-005 | Wait for task | `POST /api/apps/{id}/background-tasks/{task_id}/wait {"timeout": 30}` | Bloque jusqu'a fin |
| BG-006 | Notification injection | Task complete, poll /notifications | Agent recoit notification |
| BG-007 | Active check | `GET /api/apps/{id}/notifications/active` | `{"active": true/false}` |

### Watchers — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| WA-001 | Create watcher | `POST /api/apps/{id}/watchers` | Watcher cree, tourne |
| WA-002 | Watcher status | `GET /api/apps/{id}/watchers/{wid}` | Status, dernier run, metrics |
| WA-003 | Pause/Resume | POST pause puis resume | Watcher suspend puis reprend |
| WA-004 | Delete watcher | `DELETE /api/apps/{id}/watchers/{wid}` | Watcher arrete et supprime |
| WA-005 | Notify on_change | Watcher detecte changement | Notification declenchee |
| WA-006 | Notify always | `notify_when: always` | Notification a chaque check |
| WA-007 | Notify never | `notify_when: never` | Pas de notification |

---

## 27. Rate Limiting & Quotas

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| RL-001 | Default rate limit | 61 requetes en 1 min vers /chat | 61eme retourne 429 |
| RL-002 | Custom quota | `PUT /api/apps/{id}/quota {"rpm": 10}` | Limite a 10 RPM |
| RL-003 | User quota | `PUT /api/apps/{id}/quota/user/{uid} {"rpm": 5}` | Limite par user |
| RL-004 | Delete quota | `DELETE /api/apps/{id}/quota` | Retour au defaut |
| RL-005 | Get quota usage | `GET /api/apps/{id}/quota` | used, remaining, rpm |
| RL-006 | Admin endpoints 30 RPM | 31 requetes vers /api/mcp/ en 1 min | 31eme retourne 429 |
| RL-007 | Auth endpoints 60 RPM | Limites /auth/login | Limite respectee |
| RL-008 | Retry-After header | Apres 429 | Header Retry-After present |
| RL-009 | Action rate limiter | Rate limit per module.action dans YAML | Limite respectee |
| RL-010 | Sliding window | Requetes etalees dans le temps | Window glissant 60s |

---

## 28. Sessions & Persistence

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SE-001 | Create session | Premier chat avec session_id | Session creee |
| SE-002 | List sessions | `GET /api/apps/{id}/sessions` | Sessions listees avec pagination |
| SE-003 | Session detail | `GET /api/apps/{id}/sessions/{sid}` | Metadata session |
| SE-004 | Session history | `GET /api/apps/{id}/sessions/{sid}/history` | Messages complets |
| SE-005 | Delete session | `DELETE /api/apps/{id}/sessions/{sid}` | Session + historique supprimes |
| SE-006 | Session idle TTL | Attendre 30 min sans activite | Session expiree |
| SE-007 | Session absolute TTL | Session de 24h+ | Expiree apres TTL absolu |
| SE-008 | Session persistence DB | Redemarrer daemon, recharger session | Messages restaures depuis DB |
| SE-009 | Session lock | 2 chats simultanes meme session | Serialises (asyncio.Lock) |
| SE-010 | Auth session list | `GET /auth/sessions` | Sessions du user connecte |
| SE-011 | Session fork | `POST /auth/sessions/{sid}/fork` | Copie independante creee |
| SE-012 | Session history auth | `GET /auth/sessions/{sid}/history` | Historique du user |

---

## 29. Workspace & Project Memory

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| WS-001 | Workspace auto | `workspace_mode: auto` | .digitorn/ cree automatiquement |
| WS-002 | Workspace fixed | `workspace_mode: fixed` | Workspace specifique utilise |
| WS-003 | Project memory .digitorn.md | Creer .digitorn/apps/{id}/.digitorn.md | Contenu injecte dans system prompt |
| WS-004 | Project memory fallback | Pas de .digitorn.md, CLAUDE.md existe | CLAUDE.md utilise |
| WS-005 | Rules loading | Creer .digitorn/rules/*.md | Rules chargees et appliquees |
| WS-006 | App-specific rules | .digitorn/apps/{id}/rules/*.md | Rules specifiques chargees |
| WS-007 | Skills from workspace | .digitorn/skills/*.md | Skills disponibles |
| WS-008 | App-specific skills | .digitorn/apps/{id}/skills/*.md | Skills specifiques (override global) |

---

## 30. Skills

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SK-001 | Skill declare dans YAML | `skills: [{command: "/commit", path: "./skills/commit.md"}]` | Skill disponible |
| SK-002 | Skill invocation | Envoyer "/commit" comme message | Skill markdown charge, agent execute |
| SK-003 | Skill pas trouve | Envoyer "/inexistant" | Erreur ou message clair |
| SK-004 | Skill avec parametres | "/analyze --detailed" | Parametres passes au skill |

---

## 31. Multi-Agent & Coordination

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MA-001 | Coordinator + specialists | Deployer app multi-agent | Coordinator delegue, specialists executent |
| MA-002 | Pool max_workers | Spawner plus que max_workers | File d'attente respectee |
| MA-003 | Specialist isolation | Specialist tente acces module non autorise | Refuse (modules filtres) |
| MA-004 | Agent memory isolation | Specialist et coordinator | Memories separees |
| MA-005 | Progress relay | Specialist emet progress | Coordinator voit les events |
| MA-006 | Agent failure handling | Specialist echoue | Coordinator informe, peut reassigner |
| MA-007 | Parallel specialists | 3 specialists en parallele | Execution concurrente |

---

## 32. Configuration LLM & Providers

```yaml
# Tester chaque provider
agents:
  - id: test-anthropic
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"

  - id: test-openai
    brain:
      provider: openai
      model: gpt-4o
      config:
        api_key: "{{secret.OPENAI_API_KEY}}"

  - id: test-deepseek
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"

  - id: test-ollama
    brain:
      provider: ollama
      model: llama3.1
      backend: openai_compat
      config:
        base_url: "http://localhost:11434/v1"
```
| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| LLM-001 | Anthropic provider | Agent avec Claude | Reponses + tool_use native |
| LLM-002 | OpenAI provider | Agent avec GPT-4o | Reponses + function calling |
| LLM-003 | DeepSeek provider | Agent avec DeepSeek | OpenAI-compat, reponses OK |
| LLM-004 | Ollama local | Agent avec modele local | Reponses (pas de tool use natif) |
| LLM-005 | Temperature config | `temperature: 0.0` vs `temperature: 1.0` | Reponses plus/moins deterministes |
| LLM-006 | Max tokens | `max_tokens: 100` | Reponse tronquee a 100 tokens |
| LLM-007 | Context window | `context.max_tokens: 8000` | Compaction declenchee avant 8000 |
| LLM-008 | Plan first | `plan_first: true` | Agent planifie avant d'agir |
| LLM-009 | Circuit breaker LLM | Provider down | Circuit breaker apres 5 echecs, 30s recovery |
| LLM-010 | Provider switch | Changer provider dans YAML, redeploy | Nouveau provider utilise |

---

## 33. API REST Exhaustive

### Validation des Entrees — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| API-001 | ID validation regex | app_id avec caracteres speciaux "app/../../etc" | 400, _SAFE_ID_RE refuse |
| API-002 | ID trop long | app_id de 200 chars | 400, max 128 chars |
| API-003 | Body manquant | POST sans body | 422, validation error |
| API-004 | Champ type invalide | `{"rpm": "not_a_number"}` | 422, type error |
| API-005 | Path traversal dans ID | `../secret` comme session_id | Rejete par regex |

### Headers de Securite — P0

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| API-010 | X-Content-Type-Options | Toute reponse | `nosniff` |
| API-011 | X-Frame-Options | Toute reponse | `DENY` |
| API-012 | Referrer-Policy | Toute reponse | `strict-origin-when-cross-origin` |
| API-013 | CSP | Toute reponse | Policy restrictive complete |
| API-014 | Permissions-Policy | Toute reponse | camera/micro/geo/payment desactives |
| API-015 | HSTS | Reponse HTTPS | `max-age=31536000; includeSubDomains` |
| API-016 | X-Request-ID | Toute reponse | UUID present dans header |
| API-017 | OpenAPI docs cachees | Auth active | /docs, /redoc, /openapi.json → 404 |

### Error Handling — P1

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| API-020 | 404 Not Found | GET /api/apps/inexistant | JSON structure, pas de stack trace |
| API-021 | 500 Internal Error | Provoquer erreur interne | JSON generique, request_id, pas de details |
| API-022 | 422 Validation | Body invalide | Details Pydantic structures |

---

## 34. Socket.IO & Temps Reel

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| SIO-001 | Connect auth | Connexion avec Bearer token | Accepte, user_id en session |
| SIO-002 | Connect sans auth | Connexion sans token | Rejete |
| SIO-003 | Join app room | emit("join_app", {app_id: "..."}) | Rejoint room "app:{app_id}" |
| SIO-004 | Join session room | emit("join_session", {app_id, session_id}) | Rejoint room session |
| SIO-005 | Event isolation | 2 clients dans 2 sessions differentes | Chaque client recoit uniquement ses events |
| SIO-006 | Broadcast room | Event systeme | Tous les clients broadcast recoivent |
| SIO-007 | Leave room | emit("leave_app") | Plus d'events pour cette app |
| SIO-008 | Reconnection | Client deconnecte puis reconnecte | Re-authentifie, rejoint rooms |

---

## 35. Metrics & Observabilite

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MT-001 | JSON metrics | `GET /api/metrics` | Counters, gauges, histograms |
| MT-002 | Prometheus metrics | `GET /api/metrics/prometheus` | Format Prometheus 0.0.4 |
| MT-003 | Session metrics | `GET /api/metrics/sessions` | Metrics par session active |
| MT-004 | App metrics | `GET /api/metrics/apps/{app_id}` | Resume par app |
| MT-005 | Request latency | Requetes API | Histogram http_request_duration_seconds |
| MT-006 | Active requests gauge | Pendant requete | Gauge active_requests incremente |
| MT-007 | Tool call counter | Apres tool calls | Counter tool_calls_total incremente |
| MT-008 | Cardinality limit | 5000+ combinaisons labels | Dropped count incremente |

---

## 36. Resilience & Graceful Shutdown

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| RS-001 | Graceful shutdown | `digitorn stop` pendant activite | Drain 30s, requetes completent, 503 pour nouvelles |
| RS-002 | 503 pendant shutdown | Requete pendant drain | `{"status": "shutting_down"}` sauf health |
| RS-003 | Health pendant shutdown | GET /health pendant drain | 200 OK (pas bloque) |
| RS-004 | Agent turn drain | Shutdown pendant agent turn | 30s pour finir les turns |
| RS-005 | Redis circuit breaker | Redis down pendant operation | Fallback DiskCache, pas de crash |
| RS-006 | Redis recovery | Redis revient | Circuit half-open → close, Redis reutilise |
| RS-007 | LLM circuit breaker | Provider LLM down | Circuit s'ouvre apres 5 echecs |
| RS-008 | LLM recovery | Provider revient | Half-open → probe → close |
| RS-009 | DB pool exhaustion | 50 requetes paralleles | File d'attente, pas de crash |
| RS-010 | Undeploy cleanup | Undeploy app | MCP deconnecte, sidecars fermes, sessions nettoyees |

---

## 37. Multi-Plateforme

| ID | Test | Procedure | Resultat attendu |
|----|------|-----------|------------------|
| MP-001 | Linux sandbox complet | Deployer sur Linux 5.13+ | Landlock + seccomp + cgroups |
| MP-002 | Linux ancien kernel | Linux < 5.13 | Degradation gracieuse, warnings |
| MP-003 | Windows sandbox | Deployer sur Windows 10+ | Job Objects + Process Mitigation |
| MP-004 | macOS sandbox | Deployer sur macOS | XPC sandbox |
| MP-005 | Paths cross-platform | Verifier ~/.digitorn/ | Chemins corrects par OS |
| MP-006 | Shell commands cross-platform | Commandes shell basiques | Adaptees a l'OS |
| MP-007 | Shell blacklist platform | `rm -rf /` (Linux) vs `del /f /s c:\` (Windows) | Blacklist adaptee |

---

## 38. Scenarios End-to-End Complets

### E2E-001 — Assistant Code Complet — P0

```yaml
app:
  app_id: e2e-code-assistant
  name: Code Assistant
modules:
  filesystem: {}
  shell: {}     # git commands via `Bash("git ...")`
  web: {}
  memory: {}
agents:
  - id: main
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: |
      You are a code assistant. You can read/write files, run commands (incl. git), and search the web.
    modules: [filesystem, shell, web, memory]
execution:
  mode: conversation
  workspace: "/tmp/e2e-code"
  greeting: "Hello! I'm your code assistant. What would you like to work on?"
capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: shell
    - module: web
    - module: memory
```
**Scenario:**
1. Deployer l'app
2. Chat: "Create a Python Flask API with a /health endpoint"
3. Verifier: fichier cree, code valide
4. Chat: "Run it and test the health endpoint"
5. Verifier: serveur lance, curl reussi
6. Chat: "Commit the changes"
7. Verifier: commit git cree
8. Chat: "Remember that we use Flask for APIs"
9. Verifier: memory.remember appele
10. Chat: "What framework do we use?"
11. Verifier: memory.recall retourne Flask

### E2E-002 — Multi-Agent Research — P1

```yaml
app:
  app_id: e2e-research
  name: Research Team
modules:
  filesystem: {}
  web: {}
  memory: {}
agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You coordinate research tasks by spawning specialist agents."
    pool:
      max_workers: 3
    modules: [filesystem, web, memory]
  - id: researcher
    role: specialist
    specialty: web research
    brain:
      provider: anthropic
      model: claude-sonnet-4-5-20241022
      config:
        api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: "You research topics on the web."
    modules: [web]
execution:
  mode: conversation
  workspace: "/tmp/e2e-research"
capabilities:
  default_policy: auto
  grant:
    - module: filesystem
    - module: web
    - module: memory
```
**Scenario:**
1. Deployer l'app
2. Chat: "Research the latest trends in AI and write a summary"
3. Verifier: coordinator spawn des agents researcher
4. Verifier: events `agent_event` emis
5. Verifier: resultats agreges en un resume

### E2E-003 — Data Pipeline RAG — P1

**Scenario:**
1. Deployer app avec modules database + rag + filesystem
2. Connecter base SQLite avec donnees
3. Ingest donnees dans KB RAG
4. Chat: "What products have the highest revenue?"
5. Verifier: requete RAG + SQL combinee, reponse accurate

### E2E-004 — Background Monitoring avec Channels — P1

**Scenario:**
1. Deployer app background avec cron + webhook
2. Verifier: cron declenche a l'intervalle
3. Envoyer webhook POST
4. Verifier: agent active, filtre respecte
5. Verifier: notification envoyee via channel

### E2E-005 — Security Complet — P0

**Scenario:**
1. Deployer app avec profil de securite restrictif
2. Tenter action granttee → reussit
3. Tenter action approve → approval_request emis
4. Approuver → action execute
5. Tenter action deny → refuse
6. Verifier audit log complet
7. Verifier que sandbox OS est actif (si Linux)

### E2E-006 — Session Persistence & Resume — P0

**Scenario:**
1. Chat avec agent, 5 echanges
2. Redemarrer daemon
3. `GET /auth/sessions/{sid}/history` → historique complet
4. Reprendre chat meme session_id
5. Verifier: contexte maintenu, agent se souvient

### E2E-007 — Pipeline Multi-App — P2

**Scenario:**
1. Deployer app1 (extracteur) et app2 (analyseur)
2. `POST /api/apps/app1/pipeline` avec steps vers app1 puis app2
3. Verifier: output app1 injecte comme input app2
4. Verifier: resultat final combine

### E2E-008 — Full Middleware Stack — P1

**Scenario:**
1. Deployer app avec tous les middlewares (mask_secrets, content_filter, rag_inject, etc.)
2. Envoyer message avec secret → masque
3. Envoyer message dangereux → bloque
4. Envoyer question RAG → context injecte
5. Verifier reponse filtree et limitee

### E2E-009 — MCP Integration Complete — P1

**Scenario:**
1. Installer serveur MCP (ex: github)
2. Configurer token
3. Deployer app avec mcp.servers.github
4. Chat: "List my recent repos"
5. Verifier: outil MCP appele, resultats retournes
6. Verifier: cache, retry, audit middleware actifs

### E2E-010 — Stress Test Concurrent — P0

**Scenario:**
1. Deployer app conversation
2. 20 clients simultanes, chacun sa session
3. Verifier: pas de corruption cross-session
4. Verifier: rate limiting fonctionne
5. Verifier: metrics coherentes
6. Verifier: pas de memory leak apres 100 tours

---

## Annexe A — Matrice de Couverture

| Composant | Tests P0 | Tests P1 | Tests P2 | Tests P3 | Total |
|-----------|----------|----------|----------|----------|-------|
| Daemon Infrastructure | 15 | 15 | 0 | 0 | 30 |
| Auth & Autorisation | 26 | 17 | 4 | 0 | 47 |
| Deploy & Lifecycle | 12 | 6 | 0 | 0 | 18 |
| Modes d'Execution | 8 | 5 | 4 | 0 | 17 |
| Filesystem | 24 | 0 | 0 | 0 | 24 |
| Shell | 17 | 0 | 0 | 0 | 17 |
| Git | 17 | 0 | 0 | 0 | 17 |
| HTTP & Web | 19 | 0 | 0 | 0 | 19 |
| Database | 24 | 0 | 0 | 0 | 24 |
| MCP | 17 | 0 | 0 | 0 | 17 |
| Memory | 8 | 0 | 0 | 0 | 8 |
| Agent Spawn | 14 | 0 | 0 | 0 | 14 |
| Notebook | 8 | 0 | 0 | 0 | 8 |
| PDF/Pres/Spreadsheet | 0 | 0 | 16 | 0 | 16 |
| Browser/Computer Use | 0 | 0 | 5 | 4 | 9 |
| Channels | 15 | 0 | 0 | 0 | 15 |
| RAG & Vector | 13 | 0 | 0 | 0 | 13 |
| Cache/Queue/Cron | 0 | 0 | 18 | 0 | 18 |
| Context Builder | 6 | 0 | 0 | 0 | 6 |
| Security Gate | 16 | 0 | 0 | 0 | 16 |
| Sandbox OS | 21 | 4 | 1 | 0 | 26 |
| Security Profiles | 10 | 0 | 0 | 0 | 10 |
| Middleware | 0 | 31 | 0 | 0 | 31 |
| Events & Streaming | 14 | 0 | 0 | 0 | 14 |
| Approbation | 7 | 0 | 0 | 0 | 7 |
| Background & Watchers | 0 | 14 | 0 | 0 | 14 |
| Rate Limiting | 10 | 0 | 0 | 0 | 10 |
| Sessions | 12 | 0 | 0 | 0 | 12 |
| Workspace & Memory | 8 | 0 | 0 | 0 | 8 |
| Skills | 4 | 0 | 0 | 0 | 4 |
| Multi-Agent | 7 | 0 | 0 | 0 | 7 |
| LLM Providers | 10 | 0 | 0 | 0 | 10 |
| API REST | 7 | 3 | 0 | 0 | 10 |
| Socket.IO | 8 | 0 | 0 | 0 | 8 |
| Metrics | 8 | 0 | 0 | 0 | 8 |
| Resilience | 10 | 0 | 0 | 0 | 10 |
| Multi-Plateforme | 7 | 0 | 0 | 0 | 7 |
| E2E Scenarios | 4 | 5 | 1 | 0 | 10 |
| **TOTAL** | **~370** | **~100** | **~49** | **~4** | **~523** |

---

## Annexe B — Environnement de Test Requis

| Composant | Requis | Optionnel |
|-----------|--------|-----------|
| Python 3.12+ | Oui | — |
| Linux (kernel 5.13+) | Pour tests sandbox | macOS/Windows pour cross-platform |
| PostgreSQL 15+ | Pour tests DB | SQLite par defaut |
| Redis 7+ | Pour tests KV/sessions | DiskCache par defaut |
| Node.js 18+ | Pour MCP servers | — |
| Clef API Anthropic | Pour tests LLM | OpenAI, DeepSeek optionnels |
| GitHub token | Pour tests MCP GitHub | — |
| Docker | Pour tests d'isolation | — |
| Playwright | Pour tests browser | — |
| LDAP server (OpenLDAP) | Pour tests LDAP | — |

---

## Annexe C — Criteres de Validation Production

- [ ] Tous les tests P0 passent (370 tests)
- [ ] 95%+ des tests P1 passent (95/100)
- [ ] Aucune regression securite
- [ ] Rate limiting fonctionne sous charge
- [ ] Graceful shutdown sans perte de donnees
- [ ] Sessions persistent au redemarrage
- [ ] Sandbox OS actif et verifie
- [ ] Audit log complet pour toute action
- [ ] Mot de passe admin par defaut change
- [ ] CORS configure pour domaine production
- [ ] TLS active si expose au reseau
- [ ] OpenAPI docs desactivees
- [ ] Metrics Prometheus accessibles
