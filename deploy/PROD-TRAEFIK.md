# TrueSight — passage en HTTPS derrière le Traefik existant du VPS

Bascule de la pile de test (HTTP :8080) vers une pile **HTTPS/wss** branchée sur le
**Traefik déjà présent** (v3, réseau `root_default`, certresolver Let's Encrypt
`mytlschallenge`). On ne touche **ni aux ports 80/443 ni à n8n**.

- Dashboard : **https://srv778935.hstgr.cloud**
- Bureau à distance / terminal : **wss://srv778935.hstgr.cloud/ws/remote/…**
- Fichier : [`docker-compose.prod.yml`](../docker-compose.prod.yml)

## Pré-requis (déjà vérifiés sur ce VPS)
- Traefik écoute 80/443, provider Docker, réseau `root_default`, redirection
  HTTP→HTTPS globale, certresolver `mytlschallenge` (TLS-ALPN sur 443).
- `srv778935.hstgr.cloud` résout vers le VPS et est **libre** (n8n est sur
  `n8n.srv778935.hstgr.cloud`).
- `.env` présent dans `/opt/truesight` (ADMIN_*, ENROLLMENT_TOKEN, DATABASE_URL,
  N8N_WEBHOOK_URL…). **Inutile** d'y changer `SESSION_COOKIE_SECURE` : la pile prod
  force déjà les cookies sécurisés via `environment:` (le .env reste utilisable
  tel quel par la pile de test si besoin).

## Bascule (depuis `/opt/truesight`)
```bash
git pull
docker compose -f docker-compose.test.yml down          # libère :8080 (le VOLUME de données est conservé)
docker compose -f docker-compose.prod.yml up -d --build  # web + relay branchés sur Traefik
docker compose -f docker-compose.prod.yml ps
```
Traefik détecte les labels et **émet le certificat Let's Encrypt** au premier accès
HTTPS (quelques secondes ; un certificat Traefik par défaut peut apparaître très
brièvement avant). La base réutilise le volume `truesight_pgdata_test` : **les
agents déjà enrôlés et l'historique sont conservés** (projet Compose `truesight`,
identique à la pile de test — à confirmer au besoin avec `docker volume ls | grep pgdata`).

## Mettre l'agent en HTTPS (poste pilote et futurs postes)
Le serveur n'est plus sur `:8080`. Éditer le `config.ini` de l'agent
(`C:\Users\Haja\Documents\TrueSightAgent\config.ini` sur le pilote) :
```ini
[server]
url = https://srv778935.hstgr.cloud
enrollment_token = <inchangé>
verify_tls = true
```
Puis relancer la tâche : `Stop-ScheduledTask -TaskName "TrueSight Agent"; Start-ScheduledTask -TaskName "TrueSight Agent"`.
(Le token de l'agent est conservé en base → pas de ré-enrôlement.)

## Vérifications
```bash
curl -sI https://srv778935.hstgr.cloud/healthz        # 200 + en-têtes de sécurité (HSTS présent en HTTPS)
curl -s  https://srv778935.hstgr.cloud/healthz        # {"status":"ok"}
docker logs truesight-web --tail 30
docker logs truesight-relay --tail 30
```
Dans le navigateur : `https://srv778935.hstgr.cloud` → connexion, puis tester le
**bureau à distance** et le **terminal** (le viewer ouvre une `wss://…/ws/remote/viewer`).

## Étapes de durcissement post-bascule
1. **Activer le MFA** sur ton compte (Réglages) — le dashboard est désormais exposé sur Internet.
2. **CSP** : une fois l'UI vérifiée en HTTPS, décommenter la ligne
   `CONTENT_SECURITY_POLICY` du service `web` dans `docker-compose.prod.yml`, puis
   `docker compose -f docker-compose.prod.yml up -d web`. Vérifier que graphiques,
   terminal et bureau à distance fonctionnent toujours (la politique fournie
   autorise les CDN, le QR MFA et le WebSocket).
3. (Optionnel) **Restreindre l'accès par IP** via un middleware Traefik
   `ipallowlist` sur le routeur `truesight` pour réserver le dashboard à vos réseaux.

## Retour arrière
```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.test.yml up -d        # revient en HTTP :8080 (même volume)
```
