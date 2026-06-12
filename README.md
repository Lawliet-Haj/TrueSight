# TrueSight

> Mini-RMM sur-mesure pour superviser et administrer le parc PC Windows de
> Medicofi / Tire-Lait Express. Un « mini-NinjaOne » limité aux fonctions
> réellement utiles.

TrueSight donne une vue temps quasi réel de 100+ postes Windows : inventaire
matériel et logiciel, métriques live (CPU / RAM / disque / uptime / utilisateur
connecté), état en ligne / hors-ligne, alertes (via n8n) et exécution de lignes
de commande à distance (PowerShell / cmd) avec journal d'audit.

---

## Objectif

| Fonction | Détail |
|---|---|
| **Inventaire matériel** | CPU, RAM, disques, n° de série, fabricant/modèle, cartes réseau (MAC) |
| **Inventaire logiciel** | Logiciels installés + versions + éditeur + date d'installation |
| **Supervision live** | Usage CPU / RAM / disque, uptime, utilisateur connecté, état en ligne/hors-ligne |
| **Commandes à distance** | Exécution PowerShell/cmd sur un poste depuis le dashboard, retour stdout + exit code |
| **Alertes** | Notifications via n8n (PC hors-ligne, disque plein, CPU saturé…) |

---

## Architecture en bref

```
   100+ PC Windows                       VPS (Docker)
 ┌───────────────────┐            ┌──────────────────────────────┐
 │  Agent TrueSight    │   HTTPS    │  Nginx (TLS / Let's Encrypt)  │
 │ (service Windows) │ ─────────► │            │                 │
 │  • inventaire     │   (poll)   │   API Flask (gunicorn)        │ ──► n8n
 │  • métriques      │ ◄───────── │   + Dashboard web             │   (alertes)
 │  • exécute cmd    │            │            │                 │
 └───────────────────┘            │        PostgreSQL 16          │
                                  └──────────────────────────────┘
```

**Principe directeur — l'agent interroge le serveur, il n'écoute jamais.**
Aucun port n'est ouvert sur les postes : l'agent **sort** en HTTPS vers le VPS
et demande régulièrement « as-tu une commande pour moi ? ». Cela élimine les
problèmes de firewall/NAT/IP dynamique (télétravail compris) et supprime toute
surface d'attaque entrante sur les postes. C'est le modèle des RMM sérieux
(Tactical RMM, NinjaOne…).

- **Agent** : Python 3.12 + `psutil` + `pywin32`/`wmi`, packagé en `.exe`
  (PyInstaller), tournant en service Windows (compte SYSTEM).
- **Serveur** : Flask 3 + gunicorn, SQLAlchemy 2 (psycopg 3), PostgreSQL 16.
- **Reverse proxy / TLS** : Nginx + Let's Encrypt (certbot).
- **Alertes** : webhook n8n (route vers email / Teams / SMS).
- **Conteneurisation** : Docker Compose (services `db`, `web`, `nginx`).

---

## Arborescence

```
parc-monitoring/
├── DESIGN.md                  ← document de conception (le « pourquoi »)
├── SPEC.md                    ← contrat technique normatif (la source de vérité)
├── README.md                  ← ce fichier
├── docker-compose.yml         ← orchestration db + web + nginx
├── .env.example               ← variables d'environnement serveur (à copier en .env)
├── .gitignore
├── server/                    ← application Flask (API agent + API dashboard + pages)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── wsgi.py
│   ├── init.sql               ← schéma SQL de référence
│   └── app/                   ← code applicatif (models, security, blueprints, tasks…)
├── agent/                     ← agent Windows (Python → .exe)
│   ├── requirements.txt
│   ├── config.example.ini
│   ├── build.ps1              ← build PyInstaller → truesight-agent.exe
│   ├── install-service.ps1    ← installation du service Windows
│   └── truesight_agent/         ← code de l'agent (collectors, client, runner…)
└── deploy/                    ← déploiement
    ├── nginx.conf             ← reverse proxy TLS
    ├── gpo-install.ps1        ← déploiement de masse de l'agent par GPO
    └── README.md              ← procédures VPS + GPO détaillées
```

---

## Démarrage rapide (DEV)

> Pour un déploiement de **production** (VPS, TLS Let's Encrypt, GPO), voir
> [`deploy/README.md`](deploy/README.md).

Prérequis : Docker + Docker Compose.

```bash
# 1. Cloner le dépôt
git clone https://github.com/AlphaConseils/parc-monitoring.git
cd parc-monitoring

# 2. Préparer la configuration
cp .env.example .env
#    Générer les secrets et les coller dans .env :
python -c "import secrets; print('SECRET_KEY=', secrets.token_urlsafe(48))"
python -c "import secrets; print('ENROLLMENT_TOKEN=', secrets.token_urlsafe(32))"
#    Renseigner aussi ADMIN_EMAIL / ADMIN_PASSWORD.

# 3. Lancer la base + l'application (sans nginx en dev)
docker compose up -d db web
docker compose logs -f web   # vérifie l'init du schéma + création de l'admin
```

En dev, l'application web écoute sur le port interne `8000`. Pour y accéder
directement sans nginx, ajoutez temporairement un mapping de port au service
`web` (ex. `ports: ["8000:8000"]`) puis ouvrez `http://localhost:8000/`.
Connectez-vous avec `ADMIN_EMAIL` / `ADMIN_PASSWORD`.

Pour tester un agent en local (machine Windows), voir `agent/config.example.ini`
et lancez `python -m truesight_agent` depuis le dossier `agent/` (mode console).

---

## Documentation

- **[SPEC.md](SPEC.md)** — contrat technique **normatif** : modèle de données,
  payloads JSON, endpoints, configuration, arborescence. **Source de vérité.**
- **[DESIGN.md](DESIGN.md)** — document de conception : objectifs, choix de
  stack, sécurité, plan par phases, points tranchés.
- **[deploy/README.md](deploy/README.md)** — procédures de déploiement (VPS +
  GPO).

---

## Périmètre V1

Inclus dès la V1 :

- **Socle read-only** : enrôlement sécurisé, inventaire matériel + logiciel,
  heartbeat + métriques, dashboard de consultation (liste du parc, état, fiche
  poste, graphiques CPU/RAM/disque).
- **Alertes** : règles (hors-ligne, disque bas, CPU saturé) → webhook n8n.
- **Commandes à distance** : file de commandes, poll agent, retour de résultat,
  rôle **admin** requis, **journal d'audit** immuable, confirmation dans l'UI.

Volontairement **hors V1** :

- Bureau à distance / prise de contrôle graphique.
- Patch management automatisé (déploiement de MAJ Windows).
- Déploiement de logiciels (push MSI).

---

## Pistes futures

- **Bureau à distance** — explicitement souhaité « à terme » : à intégrer via
  **RustDesk** ou **MeshCentral** (on ne réinvente pas la roue). Le dashboard
  TrueSight déclenchera et affichera la session.
- Durcissement des commandes admin : groupes de postes, double validation des
  commandes destructives, bibliothèque de scripts.
- État antivirus / Windows Update.
- Auto-update de l'agent (suivi du champ `agent_version`).
- Passerelle de lecture seule de l'inventaire vers une fiche Odoo.
