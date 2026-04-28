# Migration - Déploiement Hetzner du daemon digitorn-bridge

Document de référence pour déployer le daemon sur un nouveau VPS Linux
(testé sur **Hetzner Cloud / Ubuntu 24.04**) avec auto-déploiement
GitHub Actions sur chaque push `main`.

> Tu peux refaire ce process from scratch sur une box vierge en suivant
> ce doc - pas besoin de moi. Les bugs qu'on a rencontrés la première
> fois sont déjà patchés dans les scripts.

---

## Architecture cible

```
┌─────────────────────────────────────────────────────────────┐
│  ton-domaine.com  ─►  Hetzner VPS (Ubuntu 24.04)            │
│                       ┌──────────────────────────────────┐  │
│                       │  Caddy 2 (TLS Let's Encrypt)     │  │
│                       │     ▼ reverse_proxy              │  │
│                       │  digitorn-daemon (systemd)       │  │
│                       │  ├─ FastAPI + Socket.IO :8000    │  │
│                       │  ├─ user: digitorn               │  │
│                       │  └─ venv: /opt/digitorn-bridge   │  │
│                       │  Redis 7 (local, :6379)          │  │
│                       └──────────────────────────────────┘  │
│                                  ▲                          │
│                                  │ asyncpg (?ssl=require)   │
│                                  ▼                          │
│  Neon EU Postgres (cloud) ◄─────┘                           │
└─────────────────────────────────────────────────────────────┘

Auto-deploy: GitHub push → SSH box → /opt/digitorn-bridge/scripts/deploy.sh
            (git pull + restart + health-check) - ~30s.
```

---

## Pré-requis

Sur ton ordi local :

- Repo `digitorn-bridge` clone, branche `main` à jour, tu peux push
- Une clé SSH personnelle utilisable pour te connecter à la box

Compte Hetzner :

- Une box (CX22 minimum recommandé : 2 vCPU, 4 GB RAM, 40 GB disque)
- Image **Ubuntu 24.04** (noble)
- Ta clé SSH ajoutée au moment de la création (Hetzner Cloud Console
  ▸ Security ▸ SSH Keys) pour que tu puisses te connecter en `root`

Côté DNS :

- Un domaine ou sous-domaine que tu contrôles (ex. `api.digitorn.ai`)
- Un record `A` pointant sur l'IP publique de la box

Comptes externes :

- **Neon Postgres** (https://console.neon.tech) - projet créé,
  branch principale, connection string copiée
- (Optionnel) Clés API : DeepSeek, Anthropic, OpenAI selon les apps
  builtins que tu actives

---

## Étape 1 - Bootstrap one-shot sur la box

Connecte-toi à la box en `root` :

```bash
ssh root@<IP_BOX>
# OU via le domaine si DNS déjà pointé :
ssh root@api.ton-domaine.com
```

Lance le bootstrap depuis GitHub :

```bash
DEPLOY_SUDO_USER=root \
curl -fsSL https://raw.githubusercontent.com/mbathe/digitorn-bridge/main/scripts/bootstrap.sh \
  | bash
```

> **Pourquoi `DEPLOY_SUDO_USER=root` est passé après `|`** : le pipe
> exécute `bash` dans un nouveau process - l'env var doit s'appliquer
> à `bash`, pas à `curl`. Sans ça, le script utilise le défaut `ubuntu`
> et créé une rule sudoers pour un user inexistant.

Le bootstrap (~3-5 min) installe :

| Composant | Version | Rôle |
|---|---|---|
| Python 3.12 + venv + dev | 3.12.x | runtime du daemon |
| Redis 7 | 7.0.x | KV backend + Socket.IO pub/sub + queue |
| Caddy 2 | 2.x | reverse proxy + TLS auto |
| UFW | latest | firewall (ports 22, 80, 443) |
| `digitorn` system user | - | runtime user du daemon |

Et configure :

- Repo cloné dans `/opt/digitorn-bridge`
- Venv dans `/opt/digitorn-bridge/.venv` avec `pip install -e ".[postgres,redis,rss,pdf,presentation]"`
- Config app : `/home/digitorn/.digitorn/config.yaml`
- Secrets stub : `/etc/digitorn/digitorn.env` (à remplir étape 2)
- Unit systemd : `/etc/systemd/system/digitorn-daemon.service`
- Caddyfile : `/etc/caddy/Caddyfile`
- Sudoers rule pour le deploy user (CI/CD)
- Firewall ouvert sur 22, 80, 443

Le bootstrap est **idempotent** - tu peux le relancer sans rien casser.
Il met juste à jour ce qui a bougé.

---

## Étape 2 - Remplir les secrets

```bash
sudo -e /etc/digitorn/digitorn.env
```

Remplace les placeholders par tes vraies valeurs :

```env
# Neon EU - récupère depuis console.neon.tech ▸ ton projet ▸ Connect.
# Format ATTENDU (3 modifs depuis le format Neon brut) :
#   1. postgresql://      →  postgresql+asyncpg://      (driver async requis)
#   2. ?sslmode=require   →  ?ssl=require               (asyncpg parse différemment)
#   3. retire &channel_binding=require                  (asyncpg ne supporte PAS le SCRAM channel binding)
DIGITORN_DATABASE__URL=postgresql+asyncpg://neondb_owner:PASSWORD@ep-xxx.eu-central-1.aws.neon.tech/neondb?ssl=require

# JWT secret - génère avec : openssl rand -hex 64
DIGITORN_AUTH__JWT_SECRET=<colle ici>

# Clés API LLM (selon ce que tu utilises)
DEEPSEEK_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=
```

Sauvegarde + ferme.

> **Erreur classique** : ne mets **pas** de guillemets autour des
> valeurs. Le format `.env` de systemd `EnvironmentFile=` est
> `KEY=value` cru - les guillemets seraient inclus dans la valeur.

---

## Étape 3 - DNS

Chez ton registrar (Cloudflare, OVH, Gandi…) :

```
A    api.digitorn.ai    <IP_BOX>    TTL 300
```

Vérifie depuis ton local :

```bash
dig +short api.digitorn.ai
# → doit afficher l'IP de ta box
```

> Caddy émet le cert Let's Encrypt à la **première requête** sur le
> domaine. Ça prend 5-15 s. Si le DNS n'est pas encore propagé, le
> cert ne sera pas émis → erreur TLS - patiente quelques minutes.

---

## Étape 4 - Démarrer le daemon

```bash
systemctl start digitorn-daemon
systemctl status digitorn-daemon --no-pager
```

Tu dois voir :

```
Active: active (running) since ... ; Xs ago
Memory: 1.2G ...
Tasks: 24
```

(Memory ~360 KB pendant les 5 premières secondes du boot, puis monte
à ~1 GB. Si Memory reste à <500 KB après 30s, c'est qu'il crash en
boucle - voir [Troubleshooting](#troubleshooting).)

Tail les logs :

```bash
journalctl -u digitorn-daemon -f
```

`Ctrl+C` pour quitter le tail.

Test :

```bash
# Local (sur la box)
curl http://127.0.0.1:8000/health

# Public (depuis ton local ou la box)
curl https://api.digitorn.ai/health
```

Les deux doivent retourner :

```json
{"status":"ok","version":"1.0.0","socketio":true,"warming_up":false,...}
```

Si **public** échoue mais **local** marche → souci Caddy ou DNS.
Si **les deux** échouent → souci daemon ou config.

---

## Étape 5 - Configurer GitHub Actions pour l'auto-deploy

### 5.1 Générer une clé SSH dédiée au déploiement

Sur ton **local Windows** (PowerShell) :

```powershell
ssh-keygen -t ed25519 -f "$HOME\.ssh\digitorn_deploy" -N '""' -C "github-actions-digitorn"
```

Crée 2 fichiers :

- `~/.ssh/digitorn_deploy` (privée - secret GitHub)
- `~/.ssh/digitorn_deploy.pub` (publique - autorisée sur la box)

> Ne réutilise **pas** ta clé personnelle. Une clé dédiée limite le
> blast-radius si elle fuite (pas d'accès à tes autres serveurs ou
> repos GitHub).

### 5.2 Ajouter la clé publique sur la box

Affiche la publique :

```powershell
type "$HOME\.ssh\digitorn_deploy.pub"
```

Tu vois une ligne `ssh-ed25519 AAAA... github-actions-digitorn`.
Copie-la entière.

Sur la **box** (ta session SSH ouverte en root) :

```bash
mkdir -p /root/.ssh
chmod 700 /root/.ssh
echo "ssh-ed25519 AAAA... github-actions-digitorn" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

(Si la ligne précédente existait déjà avec une autre clé, utilise
`>>` pour ajouter ; si tu veux remplacer, utilise `>` à la place.)

### 5.3 Tester depuis ton local

**Nouvelle fenêtre PowerShell** (pas la session SSH) :

```powershell
ssh -i "$HOME\.ssh\digitorn_deploy" -o StrictHostKeyChecking=accept-new root@<IP_BOX> "echo OK && hostname"
```

Doit afficher `OK` et le hostname **sans demander password ni
passphrase**. Si ça pète, vérifie que tu as bien copié la clé entière
dans `authorized_keys` (pas de retour à la ligne au milieu).

### 5.4 Ajouter les 3 secrets GitHub

Sur **GitHub** : `mbathe/digitorn-bridge` ▸ **Settings** ▸ **Secrets
and variables** ▸ **Actions** ▸ **New repository secret**.

Crée 3 secrets :

| Nom | Valeur |
|---|---|
| `HETZNER_HOST` | l'IP publique de la box (ex. `46.225.124.155`) |
| `HETZNER_USER` | `root` |
| `HETZNER_SSH_KEY` | le contenu **complet** de `~/.ssh/digitorn_deploy` (clé privée), incluant les lignes `-----BEGIN/END OPENSSH PRIVATE KEY-----` |

Pour récupérer la clé privée :

```powershell
type "$HOME\.ssh\digitorn_deploy"
```

Copie tout, colle dans le champ `HETZNER_SSH_KEY`.

### 5.5 Déclencher le premier auto-deploy

Sur GitHub : **Actions** ▸ **Deploy to Hetzner** ▸ **Run workflow**
▸ branch `main` ▸ **Run**.

Ou par push :

```bash
git commit --allow-empty -m "ci: trigger deploy"
git push origin main
```

Les 5 steps attendus, tous verts :

1. **Sanity - required secrets are set** (~1s)
2. **Load SSH key into the agent** (~1s)
3. **Trust the box's host key** (~2s)
4. **Run deploy.sh on the box** (~10-30s) - l'output doit afficher
   `[deploy ...] head=<sha>` puis `[deploy ...] daemon healthy after Xs`
5. **Health check from outside** (~3-15s) - `✓ public health check passed`

À partir de maintenant, **chaque push sur `main`** redéploie auto.
Tu peux aussi lancer manuellement via le bouton "Run workflow".

---

## Comment ça marche (interne)

### Lifecycle d'un déploiement

```
Tu fais : git push origin main
   │
   ▼
GitHub déclenche .github/workflows/deploy.yml
   │
   ▼
GH Actions runner Ubuntu :
   1. Lit les 3 secrets
   2. Charge la clé privée dans ssh-agent
   3. ssh-keyscan pour trust le host
   4. ssh root@HOST "sudo /opt/digitorn-bridge/scripts/deploy.sh"
   │
   ▼
Sur la box, deploy.sh tourne en root :
   1. cd /opt/digitorn-bridge
   2. sudo -u digitorn git fetch origin main
   3. sudo -u digitorn git reset --hard origin/main
   4. Compute hash de pyproject.toml + poetry.lock + requirements*.txt
   5. Si hash a changé : pip install -e ".[postgres,redis,rss,pdf,presentation]"
   6. Si scripts/digitorn-daemon.service a changé : daemon-reload
   7. Si scripts/Caddyfile a changé : caddy reload
   8. systemctl restart digitorn-daemon
   9. Boucle health check pendant 60s : curl http://127.0.0.1:8000/health
       │
       ▼ si ok
   10. Exit 0 - étape verte
   ▼
GH Actions lance "Health check from outside" :
   curl https://api.digitorn.ai/health
   ▼ si ok
   Workflow vert
```

### Idempotence

- `bootstrap.sh` peut être relancé N fois sans tout casser. Il vérifie
  l'existence avant de créer / installer.
- `deploy.sh` ne réinstalle pip que si `pyproject.toml` ou `poetry.lock`
  ou un `requirements*.txt` a changé (hash dans `.last_deploy_deps_hash`).
- Le `chmod +x` sur les scripts shell est tracké en git (mode 100755)
  - clones suivants l'auront direct.

### Sécurité

- Le daemon tourne sous user `digitorn` (système, no-shell), pas root.
- Le `.env` (`/etc/digitorn/digitorn.env`) est en `640 root:digitorn`
  - seul le daemon peut le lire.
- Sudoers rule restreinte : l'user de déploiement (root dans ce setup)
  ne peut lancer NOPASSWD que `deploy.sh`, `systemctl restart digitorn-daemon`,
  `systemctl status digitorn-daemon`, `systemctl daemon-reload`,
  `systemctl reload caddy`. Pas un sudo all-access.
- Caddy gère le TLS automatiquement via Let's Encrypt - auto-renew
  inclus.
- UFW ne laisse passer que 22, 80, 443.
- L'unit systemd inclut du hardening : `NoNewPrivileges`,
  `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`,
  `ProtectKernelTunables`, etc.

---

## Opérations courantes

### Voir les logs du daemon

```bash
# Live tail
journalctl -u digitorn-daemon -f

# 50 dernières lignes
journalctl -u digitorn-daemon -n 50 --no-pager

# Depuis 5 min
journalctl -u digitorn-daemon --since "5 minutes ago" --no-pager

# Erreurs uniquement
journalctl -u digitorn-daemon -p err --no-pager
```

### Restart manuel

```bash
systemctl restart digitorn-daemon
systemctl status digitorn-daemon --no-pager
```

### Déploiement manuel (sans GitHub Actions)

Si CI est cassée ou tu veux tester un déploy en urgence sans push :

```bash
sudo /opt/digitorn-bridge/scripts/deploy.sh
```

### Rollback à un commit précédent

Si une release casse la prod :

```bash
cd /opt/digitorn-bridge
sudo -u digitorn git fetch --all
sudo -u digitorn git log --oneline -10  # trouve un SHA stable
sudo -u digitorn git reset --hard <SHA>
sudo systemctl restart digitorn-daemon
```

Ensuite, **revert le commit fautif sur `main`** côté GitHub pour que
le prochain auto-deploy ne ré-applique pas la régression :

```bash
# Sur ton local
git revert <SHA>
git push origin main
```

### Mettre à jour les secrets / config

```bash
sudo -e /etc/digitorn/digitorn.env   # éditer secrets
sudo systemctl restart digitorn-daemon

# OU pour la config YAML :
sudo -u digitorn -e /home/digitorn/.digitorn/config.yaml
sudo systemctl restart digitorn-daemon
```

### Modifier le domaine

Si tu changes `api.digitorn.ai` pour un autre domaine, édite le
Caddyfile :

```bash
nano /etc/caddy/Caddyfile
# remplace "api.digitorn.ai" par ton nouveau domaine
systemctl reload caddy
```

Puis modifie le DNS pour pointer le nouveau domaine sur l'IP de la box.

### Restart Redis / Caddy

```bash
systemctl restart redis-server
systemctl restart caddy
systemctl reload caddy   # reload config sans drop des connexions
```

---

## Troubleshooting

### Le daemon ne démarre pas - `Active: failed (exit-code)`

```bash
journalctl -u digitorn-daemon --since "5 minutes ago" --no-pager | tail -100
```

Cherche les premières lignes d'`Error` ou `Exception`. Causes les plus
fréquentes :

#### `asyncpg.exceptions.InvalidPasswordError: password authentication failed`

Le `DIGITORN_DATABASE__URL` dans `/etc/digitorn/digitorn.env` est faux.
Vérifie :

1. C'est bien le format `postgresql+asyncpg://` (pas `postgresql://`)
2. C'est bien `?ssl=require` (pas `?sslmode=require`)
3. Tu as **retiré** `&channel_binding=require` (asyncpg ne le supporte pas)
4. Le password dans la string ne contient pas de caractères qui
   nécessitent un escape (`@`, `#`, `/`, `+`, `&` → URL-encode-les)

Récupère la string canonique sur https://console.neon.tech ▸ ton projet
▸ **Connect**, applique les 3 transformations, re-mets dans le `.env`.

#### `unable to open database file` ou `Permission denied`

L'unit systemd ne charge pas le `.env`, donc fallback sur SQLite par
défaut, qui ne peut pas écrire au CWD courant. Vérifie :

```bash
ls -la /etc/digitorn/digitorn.env
# Doit être: -rw-r----- 1 root digitorn ... digitorn.env
grep -c DIGITORN_DATABASE__URL /etc/digitorn/digitorn.env
# Doit afficher: 1 (une seule occurrence)
```

Si plusieurs lignes `DIGITORN_DATABASE__URL=`, garde-en une seule :

```bash
sed -i '/^DIGITORN_DATABASE__URL=/d' /etc/digitorn/digitorn.env
echo 'DIGITORN_DATABASE__URL=postgresql+asyncpg://...' >> /etc/digitorn/digitorn.env
```

#### `RecursionError: maximum recursion depth exceeded`

Patché. Si tu vois encore ça, c'est que le repo cloné n'a pas la
version corrigée de `process_group.py`. Force un pull :

```bash
cd /opt/digitorn-bridge
sudo -u digitorn git pull origin main
sudo systemctl restart digitorn-daemon
```

#### `ensurepip is not available`

Le paquet `python3.12-venv` n'est pas installé. Patché dans la version
récente du bootstrap, mais si tu as une vieille box :

```bash
apt-get install -y python3.12-venv
rm -rf /opt/digitorn-bridge/.venv
DEPLOY_SUDO_USER=root \
curl -fsSL https://raw.githubusercontent.com/mbathe/digitorn-bridge/main/scripts/bootstrap.sh \
  | bash
```

### Le daemon démarre mais `/health` ne répond pas

Vérifie qu'il bind bien sur `127.0.0.1:8000` :

```bash
ss -tlnp | grep 8000
# Doit afficher quelque chose qui écoute sur 127.0.0.1:8000

curl http://127.0.0.1:8000/health
# Si ko : daemon en train de boot encore - attends 30s
```

### `https://api.digitorn.ai/health` retourne 502 Bad Gateway

Caddy écoute mais le daemon ne répond pas - soit pas démarré, soit
bind sur la mauvaise interface :

```bash
systemctl status digitorn-daemon --no-pager | head -5
curl http://127.0.0.1:8000/health
journalctl -u caddy -n 30 --no-pager
```

### `https://api.digitorn.ai/health` retourne erreur TLS / cert invalide

Caddy n'a pas réussi à émettre le cert Let's Encrypt :

```bash
journalctl -u caddy -n 50 --no-pager
```

Erreurs courantes :

- **DNS pas propagé** : Caddy reçoit `acme: error: DNS validation failed`.
  Solution : attends 5-10 min de propagation DNS.
- **Port 80 bloqué** : Let's Encrypt fait le challenge HTTP-01 sur :80.
  Vérifie UFW : `ufw status | grep 80`.
- **Rate limit Let's Encrypt** : si tu as relancé le test trop souvent,
  tu peux atteindre le rate limit (5 certs / domaine / semaine).
  Solution : attends 1-7 jours OU utilise le staging Let's Encrypt
  pendant les tests (édite Caddyfile avec `acme_ca https://acme-staging-v02...`).

### GitHub Actions deploy step échoue

Clique sur le job rouge ▸ étape rouge. Causes les plus fréquentes :

#### `Permission denied (publickey)` à l'étape SSH

Le secret `HETZNER_SSH_KEY` n'a pas la clé privée complète ou est
mal formaté. Re-vérifie :

```powershell
# Sur ton local
type "$HOME\.ssh\digitorn_deploy"
```

Tu dois copier **TOUT**, incluant les lignes `-----BEGIN OPENSSH
PRIVATE KEY-----` et `-----END OPENSSH PRIVATE KEY-----`.

Et la clé publique correspondante doit être dans
`/root/.ssh/authorized_keys` sur la box.

#### `sudo: /opt/digitorn-bridge/scripts/deploy.sh: command not found`

Le fichier n'a pas le bit exécutable. Sur la box :

```bash
chmod +x /opt/digitorn-bridge/scripts/deploy.sh
```

Et côté repo, vérifie que git tracke le bit `+x` :

```powershell
# Sur ton local
git ls-files --stage scripts/deploy.sh
# Doit afficher: 100755 ...
```

Si c'est `100644`, fais :

```powershell
git update-index --chmod=+x scripts/deploy.sh
git commit -m "fix: mark deploy.sh as executable"
git push origin main
```

#### `[deploy ...] FAIL - daemon not healthy after 60s`

Le daemon a redémarré mais `/health` ne répond pas dans le temps.
Connecte-toi à la box et regarde les logs comme indiqué plus haut.

---

## Évolutions possibles

À considérer plus tard, pas critique pour le setup actuel :

### Multi-instances zero-downtime
Aujourd'hui : `systemctl restart digitorn-daemon` coupe l'API ~3-5 s.
Pour zero-downtime, faudrait Docker + 2 instances + Caddy LB devant.

### Workers > 1 (parallélisme HTTP)
Aujourd'hui : `workers: 1` dans `~/.digitorn/config.yaml`. Pour scaler,
passe à `workers: 4` ou plus. Vérifie d'abord que tous les modules sont
multi-worker safe (Redis pub/sub gère déjà le cas Socket.IO).

### Backup de la base
Neon Postgres a son propre backup, mais tu peux scripter un dump
nightly via cron :

```bash
# /etc/cron.daily/digitorn-db-dump
pg_dump "$DIGITORN_DATABASE__URL" > /var/backups/digitorn-$(date +%Y%m%d).sql
gzip /var/backups/digitorn-*.sql
find /var/backups/ -name "digitorn-*.sql.gz" -mtime +30 -delete
```

### Monitoring / alerting
Aujourd'hui aucun. À considérer :

- **Uptime** : UptimeRobot ou Cronitor sur `https://api.digitorn.ai/health`
  (gratuit jusqu'à 50 monitors)
- **Logs** : la stack journald est ok pour debug, mais centralise
  sur Loki / Grafana Cloud / Datadog si tu as plusieurs box
- **Error tracking** : Sentry - ajouter `sentry-sdk` dans
  `pyproject.toml` et l'init dans `server.py`

### Bascule sur une SSH dédiée moins privilégiée
Aujourd'hui le déploiement SSH se fait en `root`. Pour durcir :

1. Crée un user `deploy` sur la box (pas root)
2. Ajoute-le aux sudoers avec NOPASSWD restreint à `deploy.sh` + `systemctl ...`
3. Ajoute la clé publique GH Actions à `~deploy/.ssh/authorized_keys`
4. Change le secret `HETZNER_USER` à `deploy`

Le compromis : un peu plus de setup, mais si la clé GH Actions fuite,
elle n'a accès qu'à des commandes très limitées, pas root.

---

## Annexe - Fichiers et leurs rôles

```
scripts/
├── bootstrap.sh             One-shot setup d'une box vierge
├── deploy.sh                Pull + restart, appelé par GH Actions
├── digitorn-daemon.service  Unit systemd
├── Caddyfile                Reverse proxy + TLS
└── MIGRATION.md             ← ce document

.github/workflows/
└── deploy.yml               GH Actions auto-deploy on push main

/etc/digitorn/digitorn.env       Secrets (DB, JWT, API keys)
/etc/systemd/system/digitorn-daemon.service   Unit installé
/etc/caddy/Caddyfile                          Caddy installé
/etc/sudoers.d/digitorn-deploy                Sudoers pour CI

/opt/digitorn-bridge/                         Repo cloné
/opt/digitorn-bridge/.venv/                   Python venv
/opt/digitorn-bridge/.last_deploy_deps_hash   Hash deps pour skip pip install

/home/digitorn/.digitorn/config.yaml          Config app (non-secrets)
/var/log/digitorn/                            Logs (vide - daemon log via journald)
/var/log/caddy/                               Logs HTTP de Caddy
```

---

## Annexe - Bugs rencontrés en première migration (références historiques)

Tous patchés dans la version actuelle des scripts. Tu ne devrais plus
les rencontrer si tu pars d'une box vierge avec le repo `main` à jour.

| # | Symptôme | Cause | Patch |
|---|---|---|---|
| 1 | `Permission denied: /opt/digitorn-bridge` au clone | `sudo -u digitorn git clone /opt/...` mais `digitorn` n'a pas write sur `/opt/` | Clone en root + chown après |
| 2 | `ensurepip is not available` | Ubuntu 24.04 ship `python3.12` sans le module `venv` (paquet séparé) | Install `python3.12-venv` inconditionnellement |
| 3 | `pip not found` au 2ᵉ run | Premier run a laissé un `.venv/` partiel (python symlinked, pas pip) | `rm -rf .venv` avant recréation, check sur `bin/pip` |
| 4 | Sudoers rule pour user inexistant | Default `DEPLOY_SUDO_USER=ubuntu` même si on bootstrap en root | Override `DEPLOY_SUDO_USER=root` au lancement |
| 5 | Recap mentait avec `/home/root/.ssh` | `/home/$USER` est faux pour root (qui est `/root`) | `eval echo "~$USER"` à la place |
| 6 | `RecursionError: max recursion depth exceeded` au shutdown | `_handler` reset `SIG_DFL` après `_kill_children`, killpg renvoie SIGTERM à soi → handler re-rentre | Reset SIG_DFL **avant** `_kill_children` |
| 7 | Idem en atexit | `_cleanup_at_exit` appelle `_kill_children` sans reset des handlers | Reset les handlers avant `_kill_children` aussi |
| 8 | CI deploy fail après `head=...` | `sha256sum pyproject.toml requirements*.txt` quand glob ne match pas → exit 1 → `set -euo pipefail` tue le script | `shopt -s nullglob` autour du tableau de fichiers |
| 9 | CI fail `command not found: deploy.sh` | git Windows ne tracke pas le bit `+x` par défaut → fichier cloné en `100644` | `git update-index --chmod=+x` puis commit |

---

**Doc à jour au 2026-04-27.** Contributions / mises à jour bienvenues.
