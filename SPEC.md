# TrueSight — SPEC (contrat technique, source de vérité)

> Ce document fait foi pour TOUS les composants (serveur, agent, déploiement).
> Toute divergence entre le code et ce SPEC est un bug. Version V1.

---

## 0. Conventions globales

- **API base path** : `/api/v1`
- **Format** : JSON UTF-8, dates en **ISO 8601 UTC** (suffixe `Z`).
- **Identifiants `agent_id`, `command_id`** : UUID v4 (string).
- **Auth agent** : en-tête `Authorization: Bearer <agent_token>` sur tous les endpoints agent SAUF `/enroll`.
- **Auth dashboard** : session cookie Flask (login + mot de passe + MFA TOTP).
- **Codes** : 200 OK, 201 créé, 400 requête invalide, 401 non authentifié, 403 interdit, 404 introuvable, 409 conflit, 429 rate-limit.
- **Versions** : `AGENT_VERSION = "1.0.0"`, `API_VERSION = "v1"`.
- **Python serveur** : 3.12 (conteneur Linux). **Python agent** : 3.12 (Windows).

---

## 1. Modèle de données (PostgreSQL 16)

Le serveur initialise le schéma via SQLAlchemy (`db.create_all()` au démarrage) ET fournit
un `server/init.sql` équivalent pour référence/déploiement manuel. Les noms de tables/colonnes
ci-dessous sont **normatifs**.

### Table `agents`
| Colonne | Type | Notes |
|---|---|---|
| `id` | UUID | PK (généré serveur à l'enrôlement) |
| `machine_id` | text | unique, empreinte stable du poste (fournie par l'agent) |
| `hostname` | text | |
| `agent_version` | text | |
| `os_version` | text | |
| `enrolled_at` | timestamptz | |
| `last_seen_at` | timestamptz | MAJ à chaque heartbeat |
| `token_hash` | text | hash du token agent (jamais le token en clair) |
| `is_active` | boolean | défaut true ; false = révoqué |
| `tags` | text[] | défaut `{}` |

### Table `hardware_inventory` (1 ligne courante par agent — upsert sur `agent_id`)
| Colonne | Type |
|---|---|
| `agent_id` | UUID PK, FK→agents (ON DELETE CASCADE) |
| `manufacturer` | text |
| `model` | text |
| `serial_number` | text |
| `cpu_model` | text |
| `cpu_cores` | int |
| `ram_total_mb` | int |
| `disks` | jsonb — `[{"drive":"C:","total_gb":237.5,"free_gb":42.1}]` |
| `mac_addresses` | jsonb — `["AA:BB:..", ...]` |
| `collected_at` | timestamptz |

### Table `software_inventory` (remplacé en bloc par agent à chaque collecte)
| Colonne | Type |
|---|---|
| `id` | bigserial PK |
| `agent_id` | UUID FK→agents (ON DELETE CASCADE) |
| `name` | text |
| `version` | text |
| `publisher` | text |
| `install_date` | date (nullable) |
| `collected_at` | timestamptz |

### Table `metrics` (série temporelle)
| Colonne | Type |
|---|---|
| `id` | bigserial PK |
| `agent_id` | UUID FK→agents (ON DELETE CASCADE) |
| `ts` | timestamptz (index) |
| `cpu_pct` | numeric(5,2) |
| `ram_used_pct` | numeric(5,2) |
| `disk_free` | jsonb — `{"C:":42.1}` (Go libres par lecteur) |
| `uptime_seconds` | bigint |
| `logged_in_user` | text |

Index : `(agent_id, ts DESC)`. Purge auto > `METRICS_RETENTION_DAYS`.

### Table `commands`
| Colonne | Type |
|---|---|
| `id` | UUID PK |
| `agent_id` | UUID FK→agents (ON DELETE CASCADE) |
| `created_by` | UUID FK→users (nullable) |
| `shell` | text — `'powershell'` ou `'cmd'` |
| `command_text` | text |
| `status` | text — `pending|dispatched|running|done|error|timeout` |
| `timeout_seconds` | int défaut 120 |
| `created_at` | timestamptz |
| `dispatched_at` | timestamptz (nullable) |
| `completed_at` | timestamptz (nullable) |

### Table `command_results`
| Colonne | Type |
|---|---|
| `command_id` | UUID PK, FK→commands (ON DELETE CASCADE) |
| `exit_code` | int (nullable) |
| `stdout` | text |
| `stderr` | text |
| `duration_seconds` | numeric(8,2) |
| `received_at` | timestamptz |

### Table `users`
| Colonne | Type |
|---|---|
| `id` | UUID PK |
| `email` | text unique |
| `password_hash` | text (werkzeug/`pbkdf2`) |
| `role` | text — `'admin'` ou `'viewer'` |
| `mfa_secret` | text (nullable ; base32 TOTP) |
| `mfa_enabled` | boolean défaut false |
| `is_active` | boolean défaut true |
| `created_at` | timestamptz |

### Table `audit_log` (append-only)
| Colonne | Type |
|---|---|
| `id` | bigserial PK |
| `ts` | timestamptz |
| `user_id` | UUID (nullable) |
| `action` | text — ex. `command.create`, `agent.revoke`, `login.success`, `login.fail` |
| `target_agent` | UUID (nullable) |
| `ip` | inet (nullable) |
| `details` | jsonb |

### Table `alert_rules`
| Colonne | Type |
|---|---|
| `id` | serial PK |
| `type` | text — `offline|disk_low|cpu_high|ram_high` |
| `threshold` | numeric |
| `is_active` | boolean défaut true |

### Table `alerts`
| Colonne | Type |
|---|---|
| `id` | bigserial PK |
| `agent_id` | UUID FK→agents |
| `rule_id` | int FK→alert_rules |
| `triggered_at` | timestamptz |
| `resolved_at` | timestamptz (nullable) |
| `notified` | boolean défaut false |

---

## 2. API — endpoints AGENT

Tous sous `/api/v1`. Auth `Bearer <agent_token>` sauf enroll.

### 2.1 `POST /api/v1/enroll`
Auth : **aucune** (utilise le token d'enrôlement dans le body).
```jsonc
// requête
{
  "enrollment_token": "<shared secret>",
  "machine_id": "stable-hardware-fingerprint",
  "hostname": "PC-COMPTA-03",
  "os_version": "Windows 11 Pro 26100",
  "agent_version": "1.0.0"
}
// réponse 200 (idempotent sur machine_id : si déjà enrôlé & actif → rotation token)
{
  "agent_id": "0f3c...uuid",
  "agent_token": "<token aléatoire 32+ octets en base64url>"
}
// 401 si enrollment_token invalide
```
Le serveur stocke `token_hash = sha256(agent_token)`. L'agent stocke `agent_id` + `agent_token` en local (state).

### 2.2 `POST /api/v1/agents/{agent_id}/heartbeat`
```jsonc
// requête
{
  "metrics": {
    "cpu_pct": 12.34,
    "ram_used_pct": 45.6,
    "ram_total_mb": 16384,
    "disk_free": {"C:": 42.1, "D:": 870.3},
    "uptime_seconds": 123456,
    "logged_in_user": "MEDICOFI\\jdupont"
  }
}
// réponse 200
{ "ok": true, "pending_commands": 0, "config": { "heartbeat_interval": 45, "command_poll_interval": 8 } }
```
Effets : MAJ `agents.last_seen_at`, insertion `metrics`. `config` permet un pilotage centralisé des intervalles (l'agent l'applique).

### 2.3 `POST /api/v1/agents/{agent_id}/inventory`
```jsonc
// requête
{
  "hardware": {
    "manufacturer": "Dell Inc.",
    "model": "Latitude 5520",
    "serial_number": "ABC123",
    "cpu_model": "Intel Core i5-1145G7",
    "cpu_cores": 8,
    "ram_total_mb": 16384,
    "disks": [{"drive":"C:","total_gb":237.5,"free_gb":42.1}],
    "mac_addresses": ["AA:BB:CC:DD:EE:FF"]
  },
  "software": [
    {"name":"Google Chrome","version":"125.0","publisher":"Google LLC","install_date":"2026-01-12"}
  ]
}
// réponse 200
{ "ok": true }
```
Effets : upsert `hardware_inventory` ; remplacement complet `software_inventory` de cet agent.

### 2.4 `GET /api/v1/agents/{agent_id}/commands`
Récupère les commandes `pending` → les passe à `dispatched`.
```jsonc
// réponse 200
{ "commands": [ { "id":"uuid", "shell":"powershell", "command_text":"Get-Service spooler", "timeout_seconds":120 } ] }
```

### 2.5 `POST /api/v1/commands/{command_id}/result`
```jsonc
// requête
{ "exit_code": 0, "stdout": "...", "stderr": "", "duration_seconds": 1.23 }
// réponse 200
{ "ok": true }
```
Effets : upsert `command_results`, `commands.status = done` (ou `error` si exit_code≠0 / `timeout`), `completed_at = now`. Tronquer stdout/stderr à 1 Mo.

---

## 3. API + pages DASHBOARD (session admin/viewer)

### Pages HTML (Jinja)
- `GET /` → redirige `/agents` (ou `/login` si non connecté)
- `GET /login`, `POST /login` (+ étape MFA si activée)
- `GET /logout`
- `GET /agents` → tableau du parc (live)
- `GET /agents/<agent_id>` → fiche poste : inventaire, graphiques, console commandes
- `GET /audit` → journal d'audit (admin)

### API JSON (consommée par le front)
- `GET /api/v1/agents` → `[{id,hostname,os_version,status,last_seen_at,cpu_pct,ram_used_pct,tags}]`
  - `status` calculé : `online` si `last_seen_at` < `OFFLINE_THRESHOLD_SECONDS`, sinon `offline`.
- `GET /api/v1/agents/<agent_id>` → détail complet (agent + hardware + dernier metrics + nb logiciels).
- `GET /api/v1/agents/<agent_id>/software` → liste logiciels.
- `GET /api/v1/agents/<agent_id>/metrics?hours=24` → `[{ts,cpu_pct,ram_used_pct,disk_free,uptime_seconds}]`
- `POST /api/v1/agents/<agent_id>/commands` (**admin only**) → body `{shell, command_text, timeout_seconds?}` → crée commande `pending`, écrit `audit_log(action=command.create)`, renvoie `{command_id}`.
- `GET /api/v1/commands/<command_id>` → `{status, command_text, shell, created_at, completed_at, result:{exit_code,stdout,stderr,duration_seconds}}`
- `GET /api/v1/audit?limit=200` (admin) → entrées d'audit.

---

## 4. Configuration

### 4.1 Serveur — variables d'environnement (`.env`)
| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://truesight:truesight@db:5432/truesight` | connexion Postgres |
| `SECRET_KEY` | (obligatoire) | sessions Flask |
| `ENROLLMENT_TOKEN` | (obligatoire) | secret d'enrôlement partagé |
| `N8N_WEBHOOK_URL` | (vide) | URL webhook alertes ; si vide → pas d'envoi |
| `OFFLINE_THRESHOLD_SECONDS` | `300` | seuil online/offline |
| `METRICS_RETENTION_DAYS` | `90` | purge métriques |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | (obligatoire au 1er boot) | crée l'admin initial si absent |
| `ALERT_DISK_LOW_PCT` | `10` | % disque libre déclenchant l'alerte |
| `ALERT_CPU_HIGH_PCT` | `90` | % CPU soutenu |

### 4.2 Agent — `config.ini` (poussé par GPO) + `state.json` (généré)
```ini
# config.ini
[server]
url = https://parc.medicofi.fr
enrollment_token = <shared secret>
verify_tls = true

[agent]
heartbeat_interval = 45
command_poll_interval = 8
inventory_interval_hours = 12
```
`state.json` (créé après enrôlement, protégé DPAPI en prod) :
```json
{ "agent_id": "uuid", "agent_token": "..." }
```
Emplacement prod : `C:\ProgramData\TrueSight\` (config + state + logs). En dev : dossier courant.

---

## 5. Sécurité (rappel normatif)
- `agent_token` : 32 octets aléatoires (`secrets.token_urlsafe`), stocké hashé SHA-256 côté serveur.
- HTTPS obligatoire en prod ; l'agent vérifie le certificat (`verify_tls=true`).
- Dashboard : mot de passe hashé (werkzeug), MFA TOTP (pyotp), sessions sécurisées (`SESSION_COOKIE_SECURE`, `HTTPONLY`, `SAMESITE=Lax`).
- Commandes : émission réservée `role=admin` ; chaque émission → `audit_log`. Pas de shell interactif persistant en V1 (one-shot + timeout).
- Rate-limiting léger sur `/enroll` (anti-bruteforce du token).
- Révocation : `agents.is_active=false` → tout appel agent renvoie 401.

---

## 6. Arborescence cible du dépôt

```
parc-monitoring/
├── DESIGN.md
├── SPEC.md                      ← ce fichier
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
├── server/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── wsgi.py
│   ├── init.sql
│   ├── app/
│   │   ├── __init__.py          ← app factory create_app()
│   │   ├── config.py
│   │   ├── extensions.py        ← db = SQLAlchemy()
│   │   ├── models.py            ← tous les modèles (§1)
│   │   ├── security.py          ← hash token, auth agent/dashboard, décorateurs
│   │   ├── api_agent.py         ← blueprint endpoints agent (§2)
│   │   ├── api_dashboard.py     ← blueprint API JSON dashboard (§3)
│   │   ├── web.py               ← blueprint pages HTML (login, agents, detail, audit)
│   │   ├── alerts.py            ← évaluation règles + POST n8n
│   │   ├── tasks.py             ← bg thread : détection offline, purge métriques, alertes
│   │   ├── seed.py              ← crée admin initial + règles d'alerte par défaut
│   │   ├── templates/
│   │   │   ├── base.html
│   │   │   ├── login.html
│   │   │   ├── mfa.html
│   │   │   ├── agents.html
│   │   │   ├── agent_detail.html
│   │   │   └── audit.html
│   │   └── static/
│   │       ├── css/app.css
│   │       └── js/{agents.js,agent_detail.js}
│   └── tests/
│       └── test_api.py
├── agent/
│   ├── requirements.txt
│   ├── config.example.ini
│   ├── build.ps1                ← PyInstaller → truesight-agent.exe
│   ├── install-service.ps1      ← installe le service (NSSM/pywin32)
│   └── truesight_agent/
│       ├── __init__.py
│       ├── __main__.py          ← `python -m truesight_agent`
│       ├── config.py            ← lecture config.ini + state.json
│       ├── client.py            ← session HTTP (requests), retries, auth
│       ├── enroll.py            ← flux d'enrôlement
│       ├── collectors.py        ← hardware / software / metrics (psutil + wmi/winreg)
│       ├── commands.py          ← exécution powershell/cmd avec timeout
│       ├── service.py           ← wrapper service Windows (pywin32) + mode console
│       └── runner.py            ← boucles heartbeat / poll commandes / inventaire
└── deploy/
    ├── nginx.conf
    ├── gpo-install.ps1          ← script de déploiement de masse de l'agent
    └── README.md
```

---

## 7. Détails d'implémentation imposés

- **Serveur** : Flask 3, SQLAlchemy 2 (`postgresql+psycopg`, psycopg 3), gunicorn. App factory.
  Au démarrage : `db.create_all()` puis `seed.ensure_admin()` + `seed.ensure_alert_rules()`.
  Thread de fond `tasks.start_background(app)` : toutes les 60 s → recalcul offline, évaluation alertes, et 1×/jour purge métriques.
- **Front dashboard** : Jinja + **Tailwind via CDN** + **Chart.js via CDN** + JS vanilla (fetch + setInterval 10 s pour le live). Pas de build node. Design sobre, lisible, FR.
- **Agent** : `requests` pour HTTP, `psutil` pour métriques, `wmi`+`pywin32` pour matériel, `winreg` pour logiciels installés (clés `Uninstall` HKLM 32/64 + HKCU). Robuste aux erreurs réseau (retry/backoff, ne crash jamais). `machine_id` = UUID machine via `MachineGuid` du registre (`HKLM\SOFTWARE\Microsoft\Cryptography`).
- **Exécution commandes** : `subprocess` avec `timeout`, capture stdout/stderr, encodage tolérant (`errors="replace"`). `powershell -NoProfile -NonInteractive -Command` ou `cmd /c`.
- **Docker Compose** : services `db` (postgres:16), `web` (build server/, gunicorn, dépend de db), `nginx` (proxy TLS, monte `deploy/nginx.conf`). Volume nommé pour Postgres.
- **Code** : commentaires et libellés UI en **français** ; noms de variables/fonctions en anglais.
