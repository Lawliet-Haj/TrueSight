# TrueSight — Procédures de déploiement

Ce dossier regroupe tout le nécessaire pour mettre TrueSight en production :

- `nginx.conf` — configuration du reverse proxy TLS.
- `gpo-install.ps1` — déploiement de masse de l'agent par GPO.
- ce `README.md` — procédures pas à pas.

Deux parties indépendantes :

1. [Déploiement du serveur sur un VPS](#1-déploiement-du-serveur-vps) (Docker + TLS).
2. [Déploiement de l'agent sur le parc](#2-déploiement-de-lagent-par-gpo) (GPO).

---

## 1. Déploiement du serveur (VPS)

### 1.1 Prérequis

- Un VPS **Ubuntu 24.04 LTS** (OVH, Scaleway…) avec accès root/sudo.
- Un **nom de domaine** pointant vers l'IP du VPS (ex. `parc.medicofi.fr`,
  enregistrement DNS `A` / `AAAA`).
- **Docker** et le plugin **Docker Compose** installés :

  ```bash
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"   # se reconnecter ensuite
  docker compose version            # vérifie le plugin compose
  ```

- **Pare-feu** : n'ouvrir que les ports nécessaires (HTTP pour le challenge ACME,
  HTTPS pour l'application) :

  ```bash
  sudo ufw allow 80/tcp
  sudo ufw allow 443/tcp
  sudo ufw allow OpenSSH
  sudo ufw enable
  ```

### 1.2 Récupérer le code

```bash
git clone https://github.com/AlphaConseils/parc-monitoring.git
cd parc-monitoring
```

### 1.3 Configurer les secrets (`.env`)

Copiez l'exemple puis renseignez les valeurs réelles :

```bash
cp .env.example .env
```

Générez des secrets aléatoires forts :

```bash
# SECRET_KEY (sessions Flask)
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# ENROLLMENT_TOKEN (secret d'enrôlement partagé, à pousser aux agents)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Éditez `.env` et renseignez au minimum :

- `SECRET_KEY` et `ENROLLMENT_TOKEN` (valeurs générées ci-dessus) ;
- `ADMIN_EMAIL` / `ADMIN_PASSWORD` (compte admin initial) ;
- éventuellement `N8N_WEBHOOK_URL` (alertes) et les seuils.

> Le `.env` n'est jamais commité (présent dans `.gitignore`).

### 1.4 Obtenir le certificat TLS (Let's Encrypt)

Le `nginx.conf` fourni attend des certificats dans
`/etc/letsencrypt/live/<domaine>/`. Procédure recommandée (webroot + certbot
sur l'hôte), en deux temps.

**a) Premier démarrage sans HTTPS (pour servir le challenge ACME)**

Démarrez d'abord la pile : nginx servira le bloc HTTP (port 80) qui expose
`/.well-known/acme-challenge/` via le volume partagé `certbot-www`.

```bash
docker compose up -d db web nginx
```

> Si nginx refuse de démarrer car les fichiers de certificat n'existent pas
> encore, commentez temporairement le bloc `server { listen 443 ... }` dans
> `deploy/nginx.conf`, démarrez, obtenez le certificat (étape b), puis
> décommentez et relancez `docker compose restart nginx`.

**b) Émettre le certificat avec certbot (mode webroot)**

Installez certbot sur l'hôte et pointez le webroot vers le volume utilisé par
nginx :

```bash
sudo apt-get update && sudo apt-get install -y certbot

# Récupère le point de montage du volume certbot-www utilisé par le conteneur :
WEBROOT=$(docker volume inspect parc-monitoring_certbot-www -f '{{ .Mountpoint }}')

sudo certbot certonly --webroot \
  -w "$WEBROOT" \
  -d parc.medicofi.fr \
  --email admin@medicofi.fr \
  --agree-tos --no-eff-email
```

Les certificats sont écrits dans `/etc/letsencrypt/live/parc.medicofi.fr/`,
déjà monté en lecture seule dans le conteneur nginx.

**c) Activer le HTTPS**

Vérifiez que `server_name` et les chemins `ssl_certificate*` de
`deploy/nginx.conf` correspondent à votre domaine, puis :

```bash
docker compose restart nginx
```

### 1.5 Lancer la pile complète

```bash
docker compose up -d
docker compose ps          # les 3 services doivent être « healthy » / « running »
docker compose logs -f web # vérifie l'init du schéma + création de l'admin
```

Au premier démarrage, l'application :

- crée le schéma PostgreSQL (`db.create_all()`),
- crée le compte admin initial (`ADMIN_EMAIL` / `ADMIN_PASSWORD`),
- insère les règles d'alerte par défaut.

Accédez ensuite au dashboard : `https://parc.medicofi.fr/`, connectez-vous,
**changez le mot de passe** et **activez la MFA (TOTP)**.

### 1.6 Renouvellement automatique du certificat

`certbot` installe une tâche de renouvellement (timer systemd). Pour que nginx
recharge le nouveau certificat, ajoutez un hook de rechargement :

```bash
# Renouvellement (test à blanc)
sudo certbot renew --dry-run

# Recharge nginx après renouvellement effectif
echo 'docker compose -f /chemin/vers/parc-monitoring/docker-compose.yml exec nginx nginx -s reload' \
  | sudo tee /etc/letsencrypt/renewal-hooks/deploy/truesight-reload.sh
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/truesight-reload.sh
```

### 1.7 Exploitation

- **Sauvegardes PostgreSQL** (quotidiennes, à planifier via cron) :

  ```bash
  docker compose exec -T db pg_dump -U truesight truesight | gzip > truesight-$(date +%F).sql.gz
  ```

- **Mises à jour applicatives** :

  ```bash
  git pull
  docker compose build web
  docker compose up -d
  ```

- **Mises à jour de sécurité de l'OS** : activez `unattended-upgrades` sur le VPS.

---

## 2. Déploiement de l'agent par GPO

L'agent est un exécutable Windows (`truesight-agent.exe`, construit via PyInstaller
— voir `agent/build.ps1`) qui tourne en **service Windows** sous le compte
**SYSTEM**. Il n'ouvre aucun port entrant : il **sort** en HTTPS vers le serveur.

### 2.1 Préparer le partage réseau

Sur un serveur de fichiers du domaine, créez un dossier de déploiement, par ex.
`\\srv-fichiers\Deploiement\TrueSight`, contenant :

- `truesight-agent.exe` — l'exécutable de l'agent ;
- `config.ini` — la configuration de référence poussée aux postes.

Exemple de `config.ini` (voir `agent/config.example.ini`) :

```ini
[server]
url = https://parc.medicofi.fr
enrollment_token = <le ENROLLMENT_TOKEN défini dans le .env serveur>
verify_tls = true

[agent]
heartbeat_interval = 45
command_poll_interval = 8
inventory_interval_hours = 12
```

**Droits du partage** : autorisez la **lecture** au groupe
« Ordinateurs du domaine » (le script s'exécute sous le compte machine SYSTEM,
pas sous l'utilisateur connecté).

### 2.2 Adapter le script

Dans `deploy/gpo-install.ps1`, ajustez la variable `$SourceShare` pour qu'elle
pointe vers votre partage :

```powershell
$SourceShare = '\\srv-fichiers\Deploiement\TrueSight'
```

### 2.3 Créer la GPO

1. Console **Gestion des stratégies de groupe** (`gpmc.msc`).
2. Créez une GPO (ex. « Déploiement TrueSight ») liée à l'OU des postes cibles.
3. **Configuration ordinateur → Stratégies → Paramètres Windows → Scripts
   (démarrage/arrêt) → Démarrage**.
4. Onglet **Scripts PowerShell** → **Ajouter** → désignez `gpo-install.ps1`
   (copiez-le dans le dossier `Scripts\Startup` de la GPO, ou référencez le
   partage `NETLOGON`).

> Le script s'exécute au **démarrage de la machine**, en compte SYSTEM. Il est
> **idempotent** : il ne recopie l'exe/config que s'ils sont plus récents et ne
> réinstalle pas le service s'il existe déjà.

### 2.4 Déclencher et vérifier

- Forcer l'application sur un poste de test : `gpupdate /force` puis redémarrer.
- Vérifier le service : `Get-Service TrueSightAgent`.
- Consulter le journal local : `C:\ProgramData\TrueSight\gpo-install.log`.
- Au premier lancement, l'agent s'**enrôle** automatiquement (échange du
  `enrollment_token` contre un token unique) puis apparaît dans le dashboard.

### 2.5 Mises à jour de l'agent

Déposez simplement une nouvelle version de `truesight-agent.exe` sur le partage.
Au prochain démarrage des postes, le script détecte le fichier plus récent, le
recopie et **redémarre** le service automatiquement.

### 2.6 Révocation d'un poste

Depuis le dashboard (admin), révoquez l'agent (`is_active = false`) : tout appel
de cet agent renverra alors `401`. Utile en cas de poste compromis ou volé.
