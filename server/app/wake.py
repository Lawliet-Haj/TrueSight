"""Réveil immédiat d'un agent en attente longue (long-polling des commandes).

Sans cela, un agent découvre une nouvelle commande — ou une demande de bureau à
distance — au prochain sondage périodique : jusqu'à ~8 s d'attente après le clic.
Ici, l'agent peut demander à **attendre** (``GET /agents/<id>/commands?wait=N``)
et le serveur le relâche **dès** qu'un travail apparaît pour lui : la prise en
main devient quasi instantanée.

Mécanique : une table en mémoire ``agent_id -> threading.Event``. Les écritures
en base déclenchent le réveil via un hook SQLAlchemy ``after_commit`` (cf.
``install_hooks``) — donc **tous** les chemins de création de commande sont
couverts, y compris l'auto-remédiation et ceux ajoutés plus tard, sans avoir à
penser à appeler quoi que ce soit.

Pourquoi de la mémoire de processus suffit : l'application tourne en **un seul
worker** gunicorn (choix assumé, cf. ``server/Dockerfile``), donc tous les
threads partagent cette table. C'est le même pattern que
``security._remote_session_tokens``.

**Dégradation sûre** : si l'application était un jour lancée en plusieurs
workers, un réveil pourrait survenir dans un processus différent de celui qui
attend. Aucune commande n'est perdue pour autant (elle reste en base) : l'attente
expire au bout de ``wait`` secondes et l'on retombe simplement sur le
comportement historique (sondage périodique).
"""

from __future__ import annotations

import logging
import threading
import time

_logger = logging.getLogger("truesight.wake")

# Clé de session SQLAlchemy où l'on accumule les agents à réveiller au commit.
_SESSION_KEY = "_truesight_wake_agents"

# Purge des événements inutilisés (agent désinstallé, redéploiement…) : borne la
# table en mémoire sans jamais gêner un agent actif.
_STALE_SECONDS = 3600.0

_lock = threading.Lock()
_events: dict[str, threading.Event] = {}
_last_used: dict[str, float] = {}


def _prune_locked(now: float) -> None:
    """Retire les entrées inutilisées depuis longtemps (appelé sous ``_lock``)."""
    stale = [k for k, ts in _last_used.items() if now - ts > _STALE_SECONDS]
    for key in stale:
        _events.pop(key, None)
        _last_used.pop(key, None)


def arm(agent_id) -> threading.Event:
    """Prépare l'attente pour un agent et renvoie son événement, **remis à zéro**.

    À appeler **AVANT** de lire la base : c'est ce qui évite la course classique
    « une commande est créée juste après ma lecture, avant que je me mette en
    attente » — l'événement serait alors positionné et l'attente retournerait
    immédiatement, au lieu de dormir pour rien.
    """
    key = str(agent_id)
    now = time.monotonic()
    with _lock:
        event = _events.get(key)
        if event is None:
            event = threading.Event()
            _events[key] = event
        _last_used[key] = now
        _prune_locked(now)
    event.clear()
    return event


def notify(agent_id) -> None:
    """Réveille l'agent s'il est en attente (sans effet sinon)."""
    key = str(agent_id)
    with _lock:
        event = _events.get(key)
        _last_used[key] = time.monotonic()
    if event is not None:
        event.set()


def wait(event: threading.Event, timeout: float) -> bool:
    """Attend au plus ``timeout`` secondes. True si réveillé, False si expiré."""
    if timeout <= 0:
        return False
    return event.wait(timeout)


def install_hooks(db) -> None:
    """Branche le réveil automatique sur les écritures de ``Command`` /
    ``RemoteSession``.

    On collecte les agents concernés à l'insertion, puis on ne réveille qu'**au
    commit** : avant le commit, l'agent qui se réveillerait ne verrait pas encore
    la ligne (elle n'est pas visible hors de la transaction) et repartirait les
    mains vides.
    """
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import object_session

    from .models import Command, RemoteSession

    def _collect(_mapper, _connection, target) -> None:
        agent_id = getattr(target, "agent_id", None)
        if agent_id is None:
            return
        session = object_session(target)
        if session is None:
            return
        session.info.setdefault(_SESSION_KEY, set()).add(str(agent_id))

    def _release(session) -> None:
        pending = session.info.pop(_SESSION_KEY, None)
        if not pending:
            return
        for agent_id in pending:
            notify(agent_id)

    for model in (Command, RemoteSession):
        sa_event.listen(model, "after_insert", _collect)
    sa_event.listen(db.session, "after_commit", _release)
    # Transaction annulée : on jette la collecte, sinon un réveil surviendrait
    # pour un travail qui n'existe pas.
    sa_event.listen(db.session, "after_rollback", lambda s: s.info.pop(_SESSION_KEY, None))
    _logger.info("Réveil immédiat des agents actif (long-polling des commandes).")
