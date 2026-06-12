# TrueSight Remote — Bureau à distance sur-mesure (spec)

> Prise de contrôle du bureau d'un poste depuis le navigateur, intégrée à TrueSight.
> Sur-mesure, **outbound-only** (l'agent ne reçoit jamais de connexion entrante).
> Cible : ~20 images/s (plus si possible). Latence acceptable, pas de temps réel strict.
>
> Statut : à valider — *2026-06-11*

---

## 1. Principe

L'agent **sort** vers le VPS (comme pour le monitoring). Pour le bureau à distance, on ajoute
un **relais WebSocket** sur le VPS qui apparie deux participants d'une session :

```
  Navigateur admin                 VPS                         Poste cible
 ┌────────────────┐         ┌──────────────────┐         ┌──────────────────┐
 │  <canvas>      │  wss    │   RELAIS (relay) │  wss    │  Agent : module  │
 │  rend les      │◄───────►│   asyncio        │◄───────►│  remote          │
 │  trames        │ frames  │   websockets     │ frames  │  • capture écran │
 │  capture clavier│ + input │   apparie 1 admin│ + input │  • encode (JPEG) │
 │  /souris       │         │   + 1 agent      │         │  • injecte input │
 └────────────────┘         └──────────────────┘         └──────────────────┘
```

Le relais ne décode rien : il transfère des messages binaires entre les deux WebSocket de la session.

---

## 2. Composant serveur : `relay`

- **Conteneur dédié** (Python `asyncio` + lib `websockets`), car gunicorn sync ne gère pas bien le WebSocket. Le Flask reste synchrone et simple.
- Nginx proxifie `/ws/remote/...` vers le relais.
- Le relais valide les jetons de session (lecture DB ou Redis partagé), apparie exactement **1 viewer + 1 agent** par session, et démantèle à la déconnexion de l'un des deux.
- Endpoints WebSocket :
  - `wss://parc.medicofi.fr/ws/remote/agent?token=<session_token>`
  - `wss://parc.medicofi.fr/ws/remote/viewer?token=<session_token>`

---

## 3. Démarrage d'une session (signalisation)

1. Admin clique **« Bureau à distance »** sur la fiche poste → `POST /api/v1/agents/{id}/remote-session` (admin only).
2. Le serveur crée une ligne `remote_sessions` (jeton à usage unique, TTL court, lié à l'agent + l'admin), écrit l'audit (`remote.start`).
3. L'agent apprend la demande via le **canal de poll existant** (réponse au heartbeat / GET commands enrichie d'un `pending_remote_session: {session_token}`). Délai de démarrage : quelques secondes (intervalle de poll resserré à ~3 s tant qu'une session est demandée).
4. L'agent ouvre la wss `/ws/remote/agent?token=...` ; le navigateur ouvre `/ws/remote/viewer?token=...` (le dashboard a authentifié l'admin et délivré le jeton viewer).
5. Le relais les apparie → le flux démarre.

---

## 4. Protocole de streaming (binaire sur WebSocket)

### Agent → viewer (trames)
Pour tenir ~20 i/s sans saturer le WAN : **encodage par tuiles avec delta**.
- Capture via `mss` (rapide).
- Écran découpé en tuiles (ex. 256×256). À chaque trame, ne transmettre que les **tuiles modifiées** (comparaison de hash vs trame précédente). Un écran majoritairement statique → très peu de données.
- Encodage JPEG par tuile via **libjpeg-turbo** (`PyTurboJPEG`) si dispo, repli **Pillow**. Qualité 60–75, adaptative.
- Message binaire : en-tête compact (n° de trame, x, y, w, h, codec) + octets JPEG. Les tuiles d'une même trame peuvent être groupées.
- **Keyframe** : à la connexion et sur demande du viewer (toutes les tuiles).
- **Backpressure** : si le viewer prend du retard (`bufferedAmount` élevé / acks), baisser la qualité puis le framerate.
- Option **downscale** (cap largeur ~1600 px) pour les liaisons lentes.

### Viewer → agent (entrées)
JSON compact ou binaire :
- `mousemove` (x, y normalisés 0–1), `mousedown`/`mouseup` (bouton), `wheel` (delta)
- `keydown`/`keyup` (code touche + modificateurs), saisie unicode
- contrôle : `set_quality`, `request_keyframe`, `set_monitor` (multi-écran)

---

## 5. Côté agent (Windows)

- **Module `remote/`** séparé : `capture.py` (mss + tuiles + diff + encode), `inject.py` (souris/clavier via `SendInput` ctypes ou `pydirectinput`), `session.py` (boucle wss, négociation, backpressure).
- **Capture du bureau utilisateur depuis un service SYSTEM** : le service tourne en session 0 ; pour capturer/injecter dans la session interactive de l'utilisateur, le service lance un **helper dans la session active** (`WTSGetActiveConsoleSessionId` + `CreateProcessAsUser`/`DuplicateTokenEx`). Le helper fait la capture + injection ; il communique avec le service par pipe local, ou ouvre lui-même la wss. (C'est le point délicat — bien isolé dans `remote/`.)
- Multi-écran : énumérer les moniteurs (mss), exposer le choix au viewer.

---

## 6. Modèle de données (ajout)

### Table `remote_sessions`
| Colonne | Type |
|---|---|
| `id` | UUID PK |
| `agent_id` | UUID FK→agents |
| `admin_user_id` | UUID FK→users |
| `token_hash` | text (jeton de session hashé) |
| `status` | text — `requested|active|ended|expired|error` |
| `requested_at` | timestamptz |
| `started_at` | timestamptz (nullable) |
| `ended_at` | timestamptz (nullable) |

---

## 7. Sécurité (contexte médical)

- Jeton de session **à usage unique, TTL court** (~60 s pour s'apparier), lié à 1 agent + 1 admin.
- Admin authentifié + `role=admin` obligatoire.
- **wss/TLS** de bout en bout.
- **Audit** : début/fin de chaque session (`remote.start`, `remote.end`), qui, quel poste, durée.
- **Indicateur de visionnage** côté poste (bandeau/icône) + option de **consentement utilisateur** configurable (RGPD/vie privée — un poste médical peut afficher des données sensibles).
- Le relais n'accepte qu'un appariement strict ; toute connexion surnuméraire est rejetée.

---

## 8. Performance & évolution

- Cible 20 i/s atteinte par : tuiles+delta, turbojpeg, downscale adaptatif, threads capture/encode/envoi séparés.
- **Évolution future** (si besoin de fluidité supérieure / multi-écran lourd / transfert de fichiers) : passer le transport en **WebRTC** (H.264 matériel, latence < 1 s) via `aiortc` + STUN/TURN, ou basculer cette brique sur **MeshCentral**. Le reste de TrueSight est inchangé.

---

## 9. Découpage de livraison (rapide)

1. **R1 — Tuyau** : relais + signalisation + wss agent/viewer + capture pleine trame JPEG + rendu canvas (sans input). Valide le flux écran de bout en bout.
2. **R2 — Contrôle** : injection souris/clavier + helper session utilisateur.
3. **R3 — Perf** : tuiles+delta, turbojpeg, backpressure, downscale adaptatif → ~20 i/s.
4. **R4 — Confort** : multi-écran, indicateur de visionnage + consentement, audit complet.

*TrueSight Remote — à valider, puis intégration après la V1.*
