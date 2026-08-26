# Sauvegarde & restauration du serveur TrueSight

> Une sauvegarde qu'on n'a jamais restaurée n'est pas une sauvegarde : c'est une
> hypothèse. Ce document sert autant à **répéter** la restauration (§3) qu'à la
> faire en urgence (§4).

Enjeu : une fois NinjaOne parti, ce serveur est le **seul** moyen d'atteindre le
parc. S'il disparaît sans sauvegarde restaurable, il faut **re-enrôler chaque
poste à la main** — donc passer physiquement sur 100+ machines.

---

## 1. Ce qui est sauvegardé, et pourquoi

| Élément | Pourquoi c'est critique |
|---|---|
| **Base PostgreSQL** | Postes enrôlés + **empreintes de leurs jetons**. Sans elle, aucun agent ne peut s'authentifier → re-enrôlement manuel de tout le parc. Contient aussi sites, étiquettes, comptes, journal d'audit. |
| **`.env`** | `SECRET_KEY`, `ENROLLMENT_TOKEN`, mot de passe admin initial. Perdre `ENROLLMENT_TOKEN` invalide les installeurs déjà distribués. |
| **Paquets d'agent** (option `-r`) | Reconstructibles depuis le dépôt (`build.ps1`) — sauvegarde facultative. |

## 2. Mettre en place

```bash
cd /opt/truesight
./deploy/backup.sh          # test manuel : doit finir par « Sauvegarde REUSSIE »
```

Puis en tâche planifiée quotidienne :

```bash
crontab -e
# 15 3 * * * cd /opt/truesight && ./deploy/backup.sh >> /var/log/truesight-backup.log 2>&1
```

Le script **refuse** une sauvegarde suspecte : il vérifie que la base répond, que
le dump dépasse une taille plancher, et qu'il est **relisible par `pg_restore`**
avec au moins 5 tables de données. Un dump vide ou tronqué provoque un échec
bruyant au lieu de s'accumuler silencieusement.

### Surveiller la sauvegarde elle-même

Créez un contrôle sur [healthchecks.io](https://healthchecks.io) (gratuit) et
exportez son URL :

```bash
echo 'BACKUP_PING_URL=https://hc-ping.com/VOTRE-UUID' >> /etc/environment
```

Le signal n'est émis **qu'en cas de succès vérifié**. Si les sauvegardes
s'arrêtent — ou deviennent invalides — c'est le service tiers qui vous alerte.
Sans cela, on découvre le problème le jour où l'on a besoin de restaurer.

> ⚠️ Stockez au moins une copie **hors du VPS** (`rsync` vers un NAS, `rclone`
> vers un stockage objet). Une sauvegarde qui vit sur la machine qu'elle protège
> disparaît avec elle.

---

## 3. Répétition à blanc — à faire MAINTENANT, puis 2×/an

L'objectif : prouver que le dump est restaurable, **sans toucher à la production**.
On restaure dans un conteneur PostgreSQL jetable, sur un réseau séparé.

```bash
# 1. Choisir la sauvegarde la plus récente
DUMP=$(ls -1d /opt/truesight/backups/20*-*/ | tail -1)*.dump
echo "Test de : $DUMP"

# 2. Base jetable (port non exposé, aucun lien avec la prod)
docker run -d --name ts-restore-test \
  -e POSTGRES_USER=truesight -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=truesight postgres:16
sleep 10

# 3. Restauration
docker exec -i ts-restore-test pg_restore -U truesight -d truesight --no-owner < $DUMP

# 4. VÉRIFIER le contenu (c'est l'étape qui compte)
docker exec ts-restore-test psql -U truesight -d truesight -c \
  "SELECT (SELECT count(*) FROM agents) AS postes,
          (SELECT count(*) FROM users)  AS comptes,
          (SELECT count(*) FROM sites)  AS sites,
          (SELECT max(last_seen_at) FROM agents) AS dernier_contact;"

# 5. Nettoyer
docker rm -f ts-restore-test
```

**Critère de réussite** : le nombre de postes correspond à ce que montre le
dashboard, et `dernier_contact` est proche de l'heure de la sauvegarde. Si l'un
des deux ne colle pas, la sauvegarde ne vous sauvera pas — corrigez avant d'aller
plus loin.

Notez la date de la dernière répétition réussie : ______________

---

## 4. Restauration réelle (le serveur est perdu)

```bash
# 1. Remonter la pile sur la nouvelle machine
git clone https://github.com/Lawliet-Haj/TrueSight.git /opt/truesight
cd /opt/truesight
cp /chemin/vers/backup/env.backup .env        # secrets d'origine : indispensable

# 2. Démarrer SEULEMENT la base (l'application créerait des tables vides)
docker compose -f docker-compose.prod.yml up -d db
sleep 15

# 3. Restaurer
docker exec -i truesight-db pg_restore -U truesight -d truesight --clean --if-exists \
  --no-owner < /chemin/vers/backup/truesight-<horodatage>.dump

# 4. Démarrer le reste
docker compose -f docker-compose.prod.yml up -d --build

# 5. Vérifier
curl -fsS https://srv778935.hstgr.cloud/healthz && echo OK
```

**Ordre important** : la base d'abord, restaurée, *puis* l'application. Si
l'application démarre avant, `db.create_all()` crée des tables vides et la
restauration entre en conflit avec elles.

Ensuite, côté parc : rien à faire. Les agents continuent de sonder le même nom
d'hôte et leurs jetons figurent dans la base restaurée — ils se reconnectent
seuls.

### Si le nom d'hôte change

Les agents ne trouveront plus le serveur. Deux options : faire pointer l'ancien
nom DNS vers la nouvelle machine (de loin le plus simple), ou pousser un nouveau
`config.ini` par GPO.

---

## 5. Détecter que le serveur est tombé

Les alertes TrueSight partent **du** serveur : s'il est mort, elles ne partent
plus, et le silence ressemble à « tout va bien ». Deux dispositifs
complémentaires :

| Dispositif | Détecte | Mise en place |
|---|---|---|
| **Signal de vie sortant** (intégré) | application ou base en panne, VPS éteint, réseau coupé | `WATCHDOG_PING_URL` dans `.env` → contrôle healthchecks.io |
| **Sonde HTTP externe** | serveur injoignable depuis Internet (TLS, Traefik, DNS) | UptimeRobot/healthchecks sur `https://.../healthz` |

Le signal de vie n'est émis que si le cycle de fond a **réussi** : un serveur
dont la base ne répond plus cesse de se déclarer en bonne santé, au lieu de
mentir par omission.

```bash
# .env
WATCHDOG_PING_URL=https://hc-ping.com/VOTRE-UUID
```

Réglez la période du contrôle sur ~10 min (le signal part toutes les 5 min par
défaut, `WATCHDOG_PING_INTERVAL_SECONDS`).
