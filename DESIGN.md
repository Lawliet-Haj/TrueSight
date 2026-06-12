# ParcVue — Document de conception

> Outil web de supervision et de gestion du parc PC de Medicofi / Tire-Lait Express.
> Un « mini-NinjaOne » sur-mesure, limité aux fonctions réellement utiles.
>
> **Statut** : brouillon à valider — *2026-06-11*
> **Nom de code** : ParcVue (provisoire)

---

## 1. Objectif & périmètre

### 1.1 Ce que l'outil fait (périmètre validé)

| Fonction | Détail |
|---|---|
| **Inventaire matériel** | CPU, RAM, disques, n° de série, fabricant/modèle, cartes réseau (MAC) |
| **Inventaire logiciel** | Logiciels installés + versions + éditeur + date d'installation |
| **Supervision live** | Usage CPU / RAM / disque, uptime, utilisateur connecté, état en ligne/hors-ligne |
| **Lignes de commande à distance** | Exécution de commandes PowerShell/cmd sur un poste (ou un groupe) depuis le dashboard, avec retour stdout/exit code |
| **Alertes** | Notifications via n8n (PC hors-ligne, disque plein, CPU saturé…) |

### 1.2 Ce que l'outil ne fait PAS (volontairement)

- ❌ **Bureau à distance / prise de contrôle graphique** → si besoin un jour, on intègre **RustDesk** ou **MeshCentral** plutôt que de le réinventer.
- ❌ **Patch management automatisé** (déploiement de MAJ Windows piloté).
- ❌ **Déploiement de logiciels** (MSI push).

Ces exclusions gardent le projet maîtrisable. La ligne de commande à distance couvre déjà 80 % des besoins d'intervention (un `winget upgrade`, un redémarrage de service, un nettoyage disque… se font en une commande).

### 1.3 Contexte & contraintes

- **Parc** : 100+ PC, tous **Windows**.
- **Hébergement** : VPS / cloud (OVH ou Scaleway), accessible publiquement en HTTPS.
- **Criticité** : environnement médical → la sécurité du canal de commande à distance est prioritaire (voir §6).

---

## 2. Vue d'ensemble de l'architecture

```
                            ┌────────────────────────────────────┐
   100+ PC Windows          │           VPS (OVH/Scaleway)         │
 ┌───────────────────┐      │                                      │
 │  Agent ParcVue    │      │   ┌──────────────────────────────┐   │
 │  (service Windows)│ HTTPS│   │  Nginx (TLS / Let's Encrypt)  │   │
 │                   │◄────►│   └───────────────┬──────────────┘   │
 │  • inventaire     │ poll │                   │                  │
 │  • métriques      │      │   ┌───────────────▼──────────────┐   │      ┌──────────┐
 │  • exécute cmd    │      │   │      API Flask (gunicorn)     │───┼─────►│   n8n    │
 └───────────────────┘      │   │  - /enroll  - /heartbeat      │   │webhook│ alertes  │
                            │   │  - /inventory  - /commands    │   │      │ email/   │
                            │   │  - /results                   │   │      │ Teams    │
                            │   └───────────────┬──────────────┘   │      └──────────┘
                            │                   │                  │
                            │   ┌───────────────▼──────────────┐   │
                            │   │         PostgreSQL            │   │
                            │   └──────────────────────────────┘   │
                            │   ┌──────────────────────────────┐   │
                            │   │   Dashboard web (admin)       │   │
                            │   └──────────────────────────────┘   │
                            └────────────────────────────────────┘
```

### 2.1 Principe directeur : l'agent *interroge*, il n'écoute jamais

Aucun port n'est ouvert sur les postes. L'agent **sort** en HTTPS vers le VPS et demande
« as-tu une commande pour moi ? ». Ça résout deux problèmes d'un coup :

1. **Réseau** : pas de souci de firewall/NAT/IP dynamique sur 100+ postes (télétravail compris).
2. **Sécurité** : aucune surface d'attaque entrante sur les postes.

C'est le modèle des RMM sérieux (Tactical RMM, NinjaOne…).

---

## 3. Stack technique

| Composant | Choix | Justification |
|---|---|---|
| **Agent** | Python 3.12 + `psutil` + `pywin32`/`wmi`, packagé en **.exe** (PyInstaller) tournant en **service Windows** (NSSM ou service pywin32) | Tourne en continu pour le polling ; déployable en masse par GPO/Intune |
| **API + dashboard** | **Flask** + gunicorn | Cohérent avec ton stack actuel ; la charge est minime (~2–4 req/s pour 100 PC) |
| **Base de données** | **PostgreSQL 16** | Robuste, gère l'historique des métriques ; déjà familier (Odoo) |
| **Reverse proxy / TLS** | **Nginx** + Let's Encrypt (certbot) | HTTPS obligatoire, renouvellement auto |
| **Alertes** | **n8n** (webhook) | Déjà en place et maîtrisé — route vers email/Teams/SMS |
| **Conteneurisation** | **Docker Compose** | Reproductible, déploiement/maj simples sur le VPS |

> **Pourquoi pas Odoo ?** Odoo n'est pas conçu pour encaisser de la télémétrie haute fréquence
> (100 agents × heartbeat/30 s = ~3 req/s en continu, plus les pics de commandes). On garde
> ParcVue **totalement séparé** pour ne pas alourdir l'ERP. Une passerelle de lecture seule
> vers Odoo reste possible plus tard (afficher l'inventaire dans une fiche).

> **Pourquoi Flask et pas FastAPI ?** Le modèle est du *polling* court, pas du WebSocket
> temps réel → la concurrence asynchrone n'apporte rien d'indispensable, et Flask colle à
> ton existant. Si un jour on veut des commandes quasi-instantanées par WebSocket, on
> basculera ce point précis vers de l'ASGI.

---

## 4. Modèle de données (PostgreSQL)

```
agents
  id              uuid        PK
  hostname        text
  agent_version   text
  os_version      text
  enrolled_at     timestamptz
  last_seen_at    timestamptz          -- mis à jour à chaque heartbeat
  token_hash      text                 -- secret unique par agent (hashé)
  is_active       boolean              -- révocation
  tags            text[]               -- ex. {"siège","compta","portable"}

hardware_inventory                     -- 1 ligne courante par agent (snapshot)
  agent_id        uuid        FK → agents
  manufacturer    text
  model           text
  serial_number   text
  cpu_model       text
  cpu_cores       int
  ram_total_mb    int
  disks           jsonb                -- [{drive, total_gb, free_gb}]
  mac_addresses   jsonb
  collected_at    timestamptz

software_inventory
  id              bigserial   PK
  agent_id        uuid        FK
  name            text
  version         text
  publisher       text
  install_date    date
  collected_at    timestamptz
  -- on remplace l'ensemble du jeu à chaque collecte (par agent)

metrics                                -- série temporelle
  id              bigserial   PK
  agent_id        uuid        FK
  ts              timestamptz
  cpu_pct         numeric(5,2)
  ram_used_pct    numeric(5,2)
  disk_free       jsonb                -- {C: 42.1, D: 870.3} en Go
  uptime_seconds  bigint
  logged_in_user  text
  -- purge auto > 90 jours (cron) ; option : downsampling au-delà

commands                               -- Phase 3
  id              uuid        PK
  agent_id        uuid        FK
  created_by      uuid        FK → users
  shell           text                 -- 'powershell' | 'cmd'
  command_text    text
  status          text                 -- pending|dispatched|running|done|error|timeout
  timeout_seconds int         default 120
  created_at      timestamptz
  dispatched_at   timestamptz
  completed_at    timestamptz

command_results
  command_id      uuid        FK → commands
  exit_code       int
  stdout          text
  stderr          text
  received_at     timestamptz

users                                  -- accès dashboard
  id              uuid        PK
  email           text        unique
  password_hash   text
  role            text                 -- 'admin' | 'viewer'
  mfa_secret      text
  created_at      timestamptz

audit_log                              -- immuable
  id              bigserial   PK
  ts              timestamptz
  user_id         uuid
  action          text                 -- 'command.create', 'agent.revoke', 'login'…
  target_agent    uuid
  ip              inet
  details         jsonb

alert_rules
  id              serial      PK
  type            text                 -- 'offline' | 'disk_low' | 'cpu_high'
  threshold       numeric              -- ex. 90 (% CPU), 10 (% disque), 15 (min hors-ligne)
  is_active       boolean

alerts
  id              bigserial   PK
  agent_id        uuid        FK
  rule_id         int         FK
  triggered_at    timestamptz
  resolved_at     timestamptz
  notified        boolean              -- envoyé à n8n ?
```

---

## 5. API — endpoints

### 5.1 Côté agent (auth : token unique par agent, en-tête `Authorization: Bearer <token>`)

| Méthode | Endpoint | Rôle |
|---|---|---|
| `POST` | `/api/v1/enroll` | 1er contact : `enrollment_token` + empreinte machine → renvoie `agent_id` + `agent_token` |
| `POST` | `/api/v1/agents/{id}/heartbeat` | Ping + métriques courantes (toutes les 30–60 s) |
| `POST` | `/api/v1/agents/{id}/inventory` | Snapshot matériel + logiciel (6–24 h, ou sur changement) |
| `GET`  | `/api/v1/agents/{id}/commands` | Récupère les commandes en attente (poll) |
| `POST` | `/api/v1/commands/{id}/result` | Renvoie exit code + stdout/stderr |

### 5.2 Côté dashboard (auth : session utilisateur + MFA)

| Méthode | Endpoint | Rôle |
|---|---|---|
| `GET`  | `/api/v1/agents` | Liste + état du parc (filtres, tags) |
| `GET`  | `/api/v1/agents/{id}` | Détail : inventaire, métriques, historique |
| `GET`  | `/api/v1/agents/{id}/metrics?from=&to=` | Séries pour les graphiques |
| `POST` | `/api/v1/agents/{id}/commands` | **Met une commande en file** (admin uniquement) |
| `GET`  | `/api/v1/commands/{id}` | Statut + résultat d'une commande |
| `GET`  | `/api/v1/audit` | Journal d'audit |
| `GET`/`POST` | `/api/v1/alert-rules` | Gestion des règles d'alerte |

### 5.3 Cadence de l'agent

| Tâche | Fréquence | Méthode de collecte |
|---|---|---|
| Heartbeat + métriques | 30–60 s | `psutil` (CPU/RAM/disque), `LastBootUpTime` (uptime) |
| Poll des commandes | 5–10 s (backoff à 60 s si inactif) | `GET /commands` |
| Inventaire matériel | 6–24 h | `Win32_BIOS`, `Win32_ComputerSystem`, `Win32_Processor` |
| Inventaire logiciel | 1×/jour | clés registre `Uninstall` (HKLM + HKCU, 32/64 bits) |

---

## 6. Sécurité (section critique — contexte médical)

> Un serveur capable de lancer des commandes sur 100+ postes est une cible à très forte valeur.
> Ces mesures ne sont pas optionnelles.

### 6.1 Enrôlement & authentification des agents
- **Token d'enrôlement** partagé, à durée/usage limité, poussé par GPO. Au 1er contact, l'agent l'échange contre un **token unique par poste**.
- Le token unique est stocké côté poste protégé par **DPAPI** (chiffrement lié à la machine) ; côté serveur on ne garde qu'un **hash**.
- Token **révocable** par poste (`is_active = false`) → coupe l'accès d'une machine compromise/volée.

### 6.2 Transport
- **HTTPS obligatoire** (Let's Encrypt), HSTS, redirection/refus du HTTP en clair.
- Domaine dédié, ex. `parc.medicofi.fr`.

### 6.3 Accès au dashboard
- Login + **mot de passe fort + MFA (TOTP)**, sessions courtes.
- **Recommandé** : dashboard derrière **VPN** ou **liste blanche d'IP** (l'API agent reste publique, le panneau d'admin non).
- Deux rôles : `viewer` (lecture) / `admin` (peut émettre des commandes).

### 6.4 Commandes à distance — garde-fous
- Émission réservée au rôle **admin**.
- **Journal d'audit immuable** : qui, quelle commande, sur quel poste, quand, quel résultat.
- **Confirmation explicite** dans l'UI ; double validation envisageable pour les commandes destructives.
- **One-shot avec timeout** — pas de shell interactif persistant en v1.
- ⚠️ **Niveau de privilège** : l'agent tourne en **SYSTEM** (nécessaire pour l'inventaire complet et les interventions). C'est puissant → on l'assume mais on restreint *qui* peut enrôler et émettre.
- **Rate limiting** sur l'API + validation stricte des entrées.

### 6.5 Exploitation
- Sauvegardes Postgres quotidiennes + rotation.
- Firewall VPS : seul le **443** entrant ouvert.
- Mises à jour de sécurité OS automatiques sur le VPS.

---

## 7. Déploiement

### 7.1 Serveur (VPS)
- **Ubuntu 24.04 LTS**, Docker Compose : `nginx` + `gunicorn/flask` + `postgres`.
- Certificat Let's Encrypt (certbot), renouvellement auto.
- Variables/secrets via fichier `.env` (hors git).

### 7.2 Agent sur 100+ postes
- Build d'un **.exe signé** (PyInstaller) → idéalement empaqueté en **MSI**.
- Déploiement de masse par **GPO** (script de démarrage / tâche planifiée) ou **Intune**.
- Configuration poussée par GPO : URL du serveur + token d'enrôlement (registre/fichier protégé).
- Au 1er lancement : enrôlement automatique → le poste apparaît dans le dashboard.
- Mises à jour de l'agent : champ `agent_version` suivi côté serveur ; mécanisme d'auto-update à prévoir en Phase 2/3.

---

## 8. Plan de réalisation par phases

> Principe : livrer de la valeur tôt (lecture seule, faible risque), et ne brancher la partie
> sensible (commandes) qu'une fois le socle d'auth/enrôlement solide et éprouvé.

### Phase 1 — Socle read-only *(faible risque)*
- Squelette projet (Docker Compose, Flask, Postgres, Nginx/TLS).
- Enrôlement sécurisé des agents.
- Agent : inventaire matériel + logiciel + heartbeat + métriques.
- Dashboard de consultation : liste du parc, état en ligne/hors-ligne, fiche poste, graphiques CPU/RAM/disque.
- **Résultat : tu vois tout ton parc en temps quasi réel.**

### Phase 2 — Alertes *(facile)*
- Règles d'alerte (hors-ligne, disque bas, CPU saturé).
- Émission vers webhook **n8n** → email/Teams.
- Vue « alertes actives ».

### Phase 3 — Commandes à distance *(sensible)*
- File de commandes + poll agent + retour de résultat.
- Auth renforcée, **journal d'audit**, confirmation UI.
- Console dans le dashboard (par poste et par groupe/tag).
- (Option) auto-update de l'agent.

> **Décision (2026-06-11)** : la V1 livrée intègre dès maintenant le **socle read-only (Phase 1)**,
> les **alertes n8n (Phase 2)** ET l'**exécution de lignes de commande à distance (Phase 3)** avec
> audit + rôle admin, car c'est un besoin d'intervention prioritaire. Seul le **bureau à distance**
> reste hors V1.

### Pistes ultérieures (hors périmètre v1) — explicitement souhaitées « à terme »
- **Bureau à distance / prise de contrôle graphique** — *demandé par l'utilisateur* : à intégrer via **RustDesk** ou **MeshCentral** (pas réinventé). Le dashboard ParcVue déclenchera/affichera la session.
- **Durcissement de la ligne de commande admin** (groupes de postes, double validation, bibliothèque de scripts).
- État antivirus / Windows Update.
- Lecture seule de l'inventaire dans une fiche Odoo.

---

## 9. Points à trancher avant de coder

1. **Nom de domaine** du serveur (ex. `parc.medicofi.fr`) — existe-t-il déjà une zone DNS exploitable ?
2. **Rétention** des métriques : 90 jours suffisent ? Besoin d'un historique long (capacity planning) ?
3. **Groupes/tags** des postes : quelle segmentation utile (site, service, fixe/portable) ?
4. **n8n** : instance déjà en place réutilisable, ou à héberger sur le même VPS ?
5. **Signature de code** de l'agent : certificat de signature disponible (réduit les alertes SmartScreen/AV) ?
6. **Accès dashboard** : combien d'admins, et VPN/liste blanche d'IP envisageable ?

---

*Document de conception ParcVue — à valider puis on attaque la Phase 1.*
