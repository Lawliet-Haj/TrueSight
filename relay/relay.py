"""TrueSight Remote — relais WebSocket de bureau à distance (cf. REMOTE.md §2).

Serveur asyncio autonome (lib ``websockets``) écoutant sur 0.0.0.0:8765.
Nginx proxifie ``/ws/remote/agent`` et ``/ws/remote/viewer`` vers ce service.

Rôle :
- valider le jeton de session passé en query string (``?token=<session_token>``)
  contre la table ``remote_sessions`` (lecture SQL directe via psycopg, pas d'ORM) ;
- apparier exactement 1 agent + 1 viewer par ``session_id`` ;
- quand les deux sont connectés : passer la session à ``active`` (started_at = now) ;
- relayer SANS transformation : trames BINAIRES agent→viewer, entrées TEXTE viewer→agent ;
- à la déconnexion de l'un : fermer l'autre, passer la session à ``ended`` (ended_at = now).

Le relais ne décode jamais le contenu : c'est un tuyau. Il est robuste — un client lent
ne bloque pas l'autre session, et toute erreur sur une connexion est isolée (try/except).

Sécurité (CONTRAT REMOTE / REMOTE.md §7) :
- jeton accepté seulement si ``sha256(token) == token_hash`` ET ``status in (requested, active)``
  ET ``(now - requested_at) < 60 s`` ;
- appariement strict 1+1 : toute connexion surnuméraire (2e agent / 2e viewer) est rejetée.
"""
import asyncio
import hashlib
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import psycopg
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s relay: %(message)s",
)
logger = logging.getLogger("truesight.relay")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
LISTEN_HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RELAY_PORT", "8765"))

# Fenêtre d'appariement : une session « requested » au-delà de ce délai est
# refusée à la connexion, ET une connexion restée seule (non appariée) est fermée
# au-delà de ce même délai (auto-nettoyage des orphelines). Élargi à 120 s : la
# connexion wss de l'agent depuis certains postes est lente/instable (timeouts +
# ré-essais) ; 60 s ne laissait pas toujours le temps de s'apparier.
PAIRING_TTL_SECONDS = 120

# Taille max d'un message accepté (protège contre une trame aberrante).
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def _dsn_from_env() -> str:
    """Construit le DSN psycopg à partir de DATABASE_URL.

    SQLAlchemy utilise un préfixe ``postgresql+psycopg://`` ; psycopg attend
    ``postgresql://`` (sans ``+psycopg``). On normalise donc le DSN.
    """
    url = os.environ.get("DATABASE_URL", "postgresql://truesight:truesight@db:5432/truesight")
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgres+psycopg://", "postgresql://"
    )


DSN = _dsn_from_env()


def _sha256(token: str) -> str:
    """Hash SHA-256 hexadécimal (identique au serveur Flask)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    """Horodatage UTC « aware »."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Appariement en mémoire : 1 session_id -> { "agent": ws, "viewer": ws }
# --------------------------------------------------------------------------
class Session:
    """État d'appariement d'une session (un agent + un viewer)."""

    __slots__ = ("session_id", "agent", "viewer", "lock", "activated")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.agent = None       # websocket de l'agent
        self.viewer = None      # websocket du viewer
        self.lock = asyncio.Lock()
        self.activated = False  # passé à True après mise en statut "active"


# Registre global des sessions en cours d'appariement / actives.
_sessions: dict[str, Session] = {}
_sessions_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# Accès base (psycopg, requêtes SQL directes — pas d'ORM)
# --------------------------------------------------------------------------
async def _db_validate_token(token: str) -> str | None:
    """Valide un jeton et renvoie le ``session_id`` (str) si la session est exploitable.

    Critères (CONTRAT REMOTE) :
    - ``sha256(token) == token_hash`` ;
    - ``status in ('requested', 'active')`` ;
    - ``now - requested_at < 60 s``.
    Renvoie None sinon. Exécuté en thread pour ne pas bloquer la boucle asyncio.
    """
    token_hash = _sha256(token)

    def _query() -> str | None:
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, requested_at "
                    "FROM remote_sessions WHERE token_hash = %s "
                    "ORDER BY requested_at DESC LIMIT 1",
                    (token_hash,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        session_id, status, requested_at = row
        # Usage unique : seul l'état « requested » autorise l'appariement. Une fois
        # la session « active » (1+1 apparié) ou terminée/expirée, aucune nouvelle
        # connexion n'est acceptée (anti-rejeu, anti-résurrection d'une session close).
        if status != "requested":
            return None
        if requested_at is None:
            return None
        if requested_at.tzinfo is None:
            requested_at = requested_at.replace(tzinfo=timezone.utc)
        if (_utcnow() - requested_at) >= timedelta(seconds=PAIRING_TTL_SECONDS):
            return None
        return str(session_id)

    try:
        return await asyncio.to_thread(_query)
    except Exception:  # pragma: no cover - robustesse réseau/DB
        logger.exception("Échec de validation du jeton en base")
        return None


async def _db_mark_active(session_id: str) -> None:
    """Passe la session à ``active`` et fixe ``started_at = now`` (idempotent)."""

    def _update():
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE remote_sessions SET status = 'active', "
                    "started_at = COALESCE(started_at, %s) "
                    "WHERE id = %s AND status = 'requested'",
                    (_utcnow(), session_id),
                )
            conn.commit()

    try:
        await asyncio.to_thread(_update)
    except Exception:  # pragma: no cover
        logger.exception("Échec MAJ statut active (session %s)", session_id)


async def _db_mark_ended(session_id: str) -> None:
    """Passe la session à ``ended`` et fixe ``ended_at = now`` (sauf si déjà terminée)."""

    def _update():
        with psycopg.connect(DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE remote_sessions SET status = 'ended', "
                    "ended_at = COALESCE(ended_at, %s) "
                    "WHERE id = %s AND status NOT IN ('ended', 'expired', 'error')",
                    (_utcnow(), session_id),
                )
            conn.commit()

    try:
        await asyncio.to_thread(_update)
    except Exception:  # pragma: no cover
        logger.exception("Échec MAJ statut ended (session %s)", session_id)


# --------------------------------------------------------------------------
# Cycle de vie d'une connexion
# --------------------------------------------------------------------------
def _extract_token(path: str) -> str | None:
    """Extrait le paramètre ``token`` de la query string du chemin WebSocket."""
    query = urlparse(path).query
    values = parse_qs(query).get("token")
    if not values:
        return None
    token = values[0].strip()
    return token or None


async def _register(session_id: str, role: str, ws) -> Session | None:
    """Enregistre une connexion (agent ou viewer) dans la session.

    Refuse une connexion surnuméraire (rôle déjà occupé) en renvoyant None.
    Si les deux rôles sont désormais présents, déclenche le passage à ``active``.
    """
    async with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess is None:
            sess = Session(session_id)
            _sessions[session_id] = sess

    async with sess.lock:
        existing = getattr(sess, role)
        if existing is not None:
            logger.warning(
                "Connexion %s surnuméraire pour la session %s — rejetée", role, session_id
            )
            return None
        setattr(sess, role, ws)
        both_present = sess.agent is not None and sess.viewer is not None
        should_activate = both_present and not sess.activated
        if should_activate:
            sess.activated = True

    if should_activate:
        await _db_mark_active(session_id)
        logger.info("Session %s appariée (agent + viewer) → active", session_id)

    return sess


async def _teardown(session_id: str, sess: Session, role: str) -> None:
    """Démantèle la session à la déconnexion d'un participant : ferme l'autre + ended."""
    other = None
    async with sess.lock:
        if getattr(sess, role) is not None:
            setattr(sess, role, None)
        other = sess.viewer if role == "agent" else sess.agent

    # Ferme l'autre participant s'il est encore connecté.
    if other is not None:
        try:
            await other.close(code=1000, reason="session terminée")
        except Exception:  # pragma: no cover
            pass

    # Retire la session du registre (premier des deux à nettoyer).
    async with _sessions_lock:
        current = _sessions.get(session_id)
        if current is sess and sess.agent is None and sess.viewer is None:
            _sessions.pop(session_id, None)

    await _db_mark_ended(session_id)
    logger.info("Session %s terminée (déconnexion %s)", session_id, role)


async def _relay_loop(ws, peer_getter):
    """Boucle de relais d'une extrémité vers l'autre, SANS filtrage de type.

    Le canal est bidirectionnel : trames BINAIRES (agent→viewer) comme messages
    TEXTE (entrées viewer→agent, et métadonnées de confort agent→viewer :
    latence pong, liste des moniteurs, utilisateur connecté). Le relais reste un
    simple tuyau : il ne décode ni ne transforme jamais le contenu.
    """
    async for message in ws:
        peer = peer_getter()
        if peer is None:
            # Pair pas encore connecté : le viewer arrive TOUJOURS avant l'agent
            # (qui ne se connecte qu'après avoir reçu la signalisation). On ignore
            # le message et on garde la connexion ouverte en attente d'appariement,
            # au lieu de fermer. Les messages d'amorçage éventuellement perdus
            # (ex. request_keyframe) sont sans conséquence : l'agent envoie une
            # keyframe dès sa connexion.
            continue
        try:
            await peer.send(message)
        except Exception:
            # Pair lent/déconnecté : on arrête cette extrémité, le teardown suit.
            break


async def handle_connection(ws):
    """Gestionnaire principal d'une connexion WebSocket entrante.

    Détermine le rôle depuis le chemin (``/ws/remote/agent`` ou ``/ws/remote/viewer``),
    valide le jeton, apparie, puis relaie jusqu'à déconnexion. Tout est isolé par
    try/except pour qu'une connexion fautive n'affecte pas les autres sessions.
    """
    # websockets >= 11 : le chemin (avec query string) est dans ws.request.path.
    # Repli sur ws.path pour les versions plus anciennes.
    request = getattr(ws, "request", None)
    path = request.path if request is not None else getattr(ws, "path", "")
    parsed_path = urlparse(path).path

    if parsed_path == "/ws/remote/agent":
        role = "agent"
    elif parsed_path == "/ws/remote/viewer":
        role = "viewer"
    else:
        await ws.close(code=4404, reason="chemin inconnu")
        return

    token = _extract_token(path)
    if not token:
        await ws.close(code=4401, reason="jeton manquant")
        return

    session_id = await _db_validate_token(token)
    if session_id is None:
        await ws.close(code=4401, reason="jeton invalide ou expiré")
        return

    sess = await _register(session_id, role, ws)
    if sess is None:
        await ws.close(code=4409, reason="rôle déjà occupé")
        return

    logger.info("Connexion %s acceptée pour la session %s", role, session_id)

    # Chien de garde d'appariement : si cette connexion reste SEULE (le pair ne
    # rejoint jamais) au-delà du TTL, on la ferme — sinon une connexion orpheline
    # (agent sans viewer, ou l'inverse) resterait ouverte indéfiniment, occuperait
    # le rôle et bloquerait toute nouvelle session. Auto-nettoyage.
    async def _pairing_watchdog():
        try:
            await asyncio.sleep(PAIRING_TTL_SECONDS)
        except asyncio.CancelledError:
            return
        if not sess.activated:
            logger.info(
                "Session %s non appariée après %ss (%s seul) : fermeture.",
                session_id, PAIRING_TTL_SECONDS, role,
            )
            try:
                await ws.close(code=4408, reason="non apparié (délai dépassé)")
            except Exception:  # pragma: no cover
                pass

    watchdog = asyncio.create_task(_pairing_watchdog())
    try:
        if role == "agent":
            await _relay_loop(ws, lambda: sess.viewer)
        else:
            await _relay_loop(ws, lambda: sess.agent)
    except websockets.ConnectionClosed:
        pass
    except Exception:  # pragma: no cover - isole toute erreur par connexion
        logger.exception("Erreur dans la boucle de relais (session %s, %s)", session_id, role)
    finally:
        watchdog.cancel()
        await _teardown(session_id, sess, role)


async def main():
    """Démarre le serveur WebSocket et tourne jusqu'à signal d'arrêt."""
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    logger.info("Relais TrueSight Remote en écoute sur %s:%s", LISTEN_HOST, LISTEN_PORT)
    async with websockets.serve(
        handle_connection,
        LISTEN_HOST,
        LISTEN_PORT,
        max_size=MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ):
        await stop.wait()
    logger.info("Relais TrueSight Remote arrêté")


if __name__ == "__main__":
    asyncio.run(main())
