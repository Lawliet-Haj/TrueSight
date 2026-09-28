#!/usr/bin/env bash
# =============================================================================
# TrueSight - Sauvegarde du serveur
# -----------------------------------------------------------------------------
# Sauvegarde ce qui est IRREMPLACABLE :
#   - la base PostgreSQL (postes enroles, empreintes de jetons, sites, comptes,
#     journal d'audit) : sans elle, tous les agents doivent etre re-enroles ;
#   - le fichier .env (SECRET_KEY, ENROLLMENT_TOKEN, mot de passe admin).
# Les paquets d'agent (volume agent_releases) sont RECONSTRUCTIBLES depuis le
# depot : sauvegarde optionnelle (-r) pour eviter un archivage inutile.
#
# Usage (sur le VPS) :
#   ./backup.sh                      # base + .env dans /opt/truesight/backups
#   ./backup.sh -r                   # + paquets d'agent
#   BACKUP_DIR=/mnt/nas ./backup.sh  # ailleurs
#
# Cron quotidien (3h15) :
#   15 3 * * * cd /opt/truesight && ./deploy/backup.sh >> /var/log/truesight-backup.log 2>&1
#
# Surveillance de la sauvegarde elle-meme (fortement recommande) : definir
# BACKUP_PING_URL (healthchecks.io ou equivalent). Le signal n'est emis QUE si
# la sauvegarde a reussi ET a ete verifiee -> si les sauvegardes s'arretent ou
# deviennent vides, le service tiers vous alerte. Sans cela, on ne decouvre le
# probleme que le jour ou l'on a besoin de restaurer.
# =============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/opt/truesight/backups}"
KEEP="${KEEP:-14}"                    # nombre de sauvegardes conservees
DB_CONTAINER="${DB_CONTAINER:-truesight-db}"
DB_USER="${DB_USER:-truesight}"
DB_NAME="${DB_NAME:-truesight}"
ENV_FILE="${ENV_FILE:-/opt/truesight/.env}"
RELEASES_VOLUME="${RELEASES_VOLUME:-truesight_agent_releases}"
BACKUP_PING_URL="${BACKUP_PING_URL:-}"

# Taille plancher du dump (octets). Une base TrueSight reelle depasse largement
# ce seuil ; en dessous, c'est le signe d'un dump tronque ou vide.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-20000}"

WITH_RELEASES=0
while getopts "r" opt; do
  case "$opt" in
    r) WITH_RELEASES=1 ;;
    *) echo "Option inconnue" >&2; exit 2 ;;
  esac
done

stamp="$(date +%Y%m%d-%H%M%S)"
dest="$BACKUP_DIR/$stamp"
mkdir -p "$dest"

log() { echo "[$(date +%FT%T)] $*"; }
fail() { log "ECHEC : $*"; exit 1; }

log "=== Sauvegarde TrueSight -> $dest"

# --- 1. Base de donnees ------------------------------------------------------
# Format « custom » (-Fc) : compresse, et permet une restauration selective.
if ! docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -d "$DB_NAME" >/dev/null 2>&1; then
  fail "la base $DB_CONTAINER ne repond pas (rien n'a ete ecrit)"
fi

dump="$dest/truesight-$stamp.dump"
if ! docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$dump"; then
  fail "pg_dump a echoue"
fi

size=$(stat -c%s "$dump" 2>/dev/null || stat -f%z "$dump")
if [ "$size" -lt "$MIN_DUMP_BYTES" ]; then
  fail "dump suspect : $size octets (< $MIN_DUMP_BYTES). Sauvegarde REJETEE."
fi
log "base sauvegardee : $(basename "$dump") ($size octets)"

# --- 2. Verification de LISIBILITE du dump -----------------------------------
# pg_restore --list lit l'en-tete et le sommaire : un fichier corrompu echoue
# ici, AVANT qu'on croie avoir une sauvegarde valide.
if ! docker exec -i "$DB_CONTAINER" pg_restore --list < "$dump" > "$dest/sommaire.txt" 2>/dev/null; then
  fail "le dump n'est pas relisible par pg_restore (corrompu)"
fi
tables=$(grep -c "TABLE DATA" "$dest/sommaire.txt" || true)
log "dump relisible : $tables tables avec donnees"
[ "$tables" -ge 5 ] || fail "seulement $tables tables : la base semble incomplete"

# --- 3. Secrets (.env) -------------------------------------------------------
if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$dest/env.backup"
  chmod 600 "$dest/env.backup"
  log ".env sauvegarde"
else
  log "AVERTISSEMENT : $ENV_FILE introuvable (secrets NON sauvegardes)"
fi

# --- 4. Paquets d'agent (optionnel) ------------------------------------------
if [ "$WITH_RELEASES" -eq 1 ]; then
  if docker volume inspect "$RELEASES_VOLUME" >/dev/null 2>&1; then
    docker run --rm -v "$RELEASES_VOLUME":/src:ro -v "$dest":/out alpine \
      tar czf "/out/agent-releases-$stamp.tar.gz" -C /src . && log "paquets d'agent sauvegardes"
  else
    log "AVERTISSEMENT : volume $RELEASES_VOLUME introuvable"
  fi
fi

# --- 5. Empreintes + rotation ------------------------------------------------
( cd "$dest" && sha256sum ./* > SHA256SUMS 2>/dev/null || true )

cd "$BACKUP_DIR"
# On ne supprime QUE des dossiers horodates, jamais autre chose.
count=$(ls -1d 20*-* 2>/dev/null | wc -l || echo 0)
if [ "$count" -gt "$KEEP" ]; then
  ls -1d 20*-* | sort | head -n "$((count - KEEP))" | while read -r old; do
    log "rotation : suppression de $old"
    rm -rf -- "$old"
  done
fi

log "=== Sauvegarde REUSSIE ($stamp)"

# --- 6. Signal a la veille externe (seulement en cas de succes) --------------
if [ -n "$BACKUP_PING_URL" ]; then
  curl -fsS -m 10 "$BACKUP_PING_URL" >/dev/null 2>&1 \
    && log "veille externe notifiee" \
    || log "veille externe injoignable (sauvegarde OK malgre tout)"
fi
