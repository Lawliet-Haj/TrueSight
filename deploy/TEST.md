# TrueSight — Déploiement de TEST (HTTP, sans domaine, cohabite avec n8n)

> But : tester TrueSight rapidement sur un VPS qui héberge déjà n8n, **sans TLS**
> et **sans toucher aux ports 80/443**. Seul le port **8080** est publié.
> Pour la production (HTTPS), voir `deploy/README.md`.

## A. Côté VPS (Ubuntu, Docker déjà installé via le template n8n)

### 1. Récupérer le code
```bash
cd /opt
git clone https://github.com/Lawliet-Haj/TrueSight.git truesight
cd truesight
```
> Repo privé ? Utiliser un *Personal Access Token* : `git clone https://<TOKEN>@github.com/Lawliet-Haj/TrueSight.git truesight`

### 2. Créer le fichier `.env` (secrets générés à la volée)
```bash
cat > .env <<EOF
DATABASE_URL=postgresql+psycopg://truesight:truesight@db:5432/truesight
SECRET_KEY=$(openssl rand -hex 32)
ENROLLMENT_TOKEN=$(openssl rand -hex 24)
ADMIN_EMAIL=admin@truesight.local
ADMIN_PASSWORD=ChangeMoi-2026!
SESSION_COOKIE_SECURE=false
TRUST_PROXY=true
N8N_WEBHOOK_URL=
OFFLINE_THRESHOLD_SECONDS=300
METRICS_RETENTION_DAYS=90
EOF
```
Noter le jeton d'enrôlement (à reporter dans l'agent) :
```bash
grep ENROLLMENT_TOKEN .env
```

### 3. Démarrer la stack de test
```bash
docker compose -f docker-compose.test.yml up -d --build
docker compose -f docker-compose.test.yml ps
docker compose -f docker-compose.test.yml logs -f web   # Ctrl+C pour quitter
```

### 4. Ouvrir le port 8080
```bash
ufw status                 # si ufw est actif :
ufw allow 8080/tcp
```
> Vérifier aussi le **pare-feu Hostinger** (panneau VPS → Firewall) : autoriser le TCP 8080 entrant.

### 5. Accéder au dashboard
- `http://srv778935.hstgr.cloud:8080`  (ou `http://82.25.116.238:8080`)
- Connexion : `admin@truesight.local` / le mot de passe mis dans `.env`.

## B. Côté poste pilote (Windows) — enrôler ton PC

Dans le dépôt cloné **sur ton PC** (pas le VPS), dossier `agent\` :
```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```
Créer `agent\config.ini` :
```ini
[server]
url = http://srv778935.hstgr.cloud:8080
enrollment_token = <le ENROLLMENT_TOKEN du .env>
verify_tls = false

[agent]
heartbeat_interval = 30
command_poll_interval = 8
inventory_interval_hours = 12
```
Lancer l'agent en mode console (depuis le dossier `agent\`) :
```powershell
.\.venv\Scripts\python -m truesight_agent
```
Le poste s'enrôle, apparaît « en ligne » dans le dashboard, remonte inventaire + métriques.

> En mode console, l'agent tourne dans **ta** session → le bureau à distance
> fonctionne directement (pas de bascule session 0). C'est parfait pour valider
> le streaming écran + le contrôle clavier/souris.

## C. Tester
1. Dashboard → le poste apparaît → ouvrir sa fiche.
2. **Console de commande** : lancer p.ex. `Get-Date` (PowerShell) → vérifier le retour.
3. **Bureau à distance** : « Prendre la main » → l'écran s'affiche → activer le contrôle → souris/clavier.

## D. Arrêter / nettoyer
```bash
docker compose -f docker-compose.test.yml down          # stoppe (garde les données)
docker compose -f docker-compose.test.yml down -v       # + supprime le volume Postgres
```

## Notes
- **HTTP en clair** : test uniquement. Le mot de passe et les jetons circulent non chiffrés → réseau de confiance, et on passe en HTTPS pour un vrai usage.
- **n8n intact** : aucun port système ni conteneur n8n touché. La stack de test est isolée (réseau `truesight_test`, conteneurs `truesight-test-*`).
- **Production HTTPS** ensuite : sur `srv778935.hstgr.cloud` (qui résout déjà), en cohabitant avec le reverse-proxy de n8n.
