"""Expiration des commandes PERDUES (cf. ``tests/test_command_expiry.py``).

Une commande passe à ``dispatched`` quand l'agent vient la chercher. Si l'agent
disparaît pendant l'exécution — écran bleu, redémarrage, coupure réseau — aucun
résultat n'arrive jamais. Sans ce balayage, la commande restait « en cours »
pour toujours : interface trompeuse, lot qui ne se termine jamais, et
auto-remédiation bloquée (``alerts.py`` refuse une nouvelle tentative tant
qu'une commande précédente est « en vol »). Vécu le 28/09/2026 sur PF1D0V19,
écran bleu huit secondes après la remise d'une commande.

ÉCHÉANCE. L'agent exécute les commandes d'un même envoi L'UNE APRÈS L'AUTRE
(``runner._command_loop``) : elles partagent le même ``dispatched_at``, mais la
n-ième ne démarre qu'à la fin des précédentes. L'échéance est donc cumulée :

    dispatched_at + Σ (timeout_seconds + grâce) des commandes du même envoi
                    passées avant elle, elle comprise

quel que soit l'état actuel de ces commandes : une première commande terminée
normalement a quand même retardé le départ de la suivante. La grâce
(``COMMAND_RESULT_GRACE_SECONDS``) couvre le lancement du processus et l'envoi
du résultat. À ``created_at`` égal (l'horloge de Windows avance par pas de
~15 ms), l'ordre réel est inconnu : on compte toutes les ex aequo. L'échéance ne
peut alors qu'être plus tardive, jamais trop précoce.

CONSTAT. Statut ``timeout`` — état final que l'interface, les lots et la
remédiation connaissent déjà — et un résultat synthétique dont le ``stderr``
dit la cause. Un résultat TARDIF reste accepté par ``post_result`` et remplace
ce constat : la perte n'est qu'une présomption, la vraie sortie fait foi.
"""
import logging
from datetime import timedelta, timezone

from flask import current_app

from .extensions import db
from .models import Command, CommandResult, utcnow

_logger = logging.getLogger("truesight.commands")

LOST_STDERR = (
    "[TrueSight] aucun résultat reçu : l'agent a disparu pendant l'exécution "
    "(poste éteint, redémarré ou déconnecté). La commande a pu s'exécuter en "
    "tout ou partie : vérifier l'état du poste avant de la relancer."
)
LOST_REDACTED_TEXT = "[secret purgé : aucun résultat reçu]"


def _aware(ts):
    """SQLite rend des dates naïves, PostgreSQL des dates avec fuseau."""
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _same_send(cmd: Command) -> list[Command]:
    """Toutes les commandes remises en même temps que ``cmd``, QUEL QUE SOIT leur état.

    ``_collect_agent_work`` pose le même instant sur toutes les commandes d'un
    envoi : même agent + même ``dispatched_at`` = même envoi.
    """
    if cmd.dispatched_at is None:  # donnée ancienne ou incohérente : envoi isolé
        return [cmd]
    return (
        db.session.query(Command)
        .filter(Command.agent_id == cmd.agent_id, Command.dispatched_at == cmd.dispatched_at)
        .all()
    )


def _deadline(cmd: Command, send: list[Command], grace: float):
    """Instant au-delà duquel le résultat de ``cmd`` n'arrivera plus."""
    start = _aware(cmd.dispatched_at) or _aware(cmd.created_at)
    created = _aware(cmd.created_at)

    def passe_avant(other: Command) -> bool:
        # Ex aequo ou date manquante : on la compte (échéance plus tardive,
        # jamais trop précoce).
        other_created = _aware(other.created_at)
        return created is None or other_created is None or other_created <= created

    budget = sum((o.timeout_seconds or 0) + grace for o in send if passe_avant(o))
    return start + timedelta(seconds=budget)


def _mark_lost(cmd: Command, now) -> bool:
    """Passe ``cmd`` en ``timeout`` si elle est TOUJOURS ``dispatched``.

    UPDATE conditionnel : si le résultat vient d'arriver (``post_result``), ou si
    un autre processus a déjà fait le constat, la ligne n'est plus
    ``dispatched`` et on ne touche à rien.
    """
    values = {"status": "timeout", "completed_at": now}
    if cmd.redact_after_run:
        # Le secret est normalement purgé à la réception du résultat. Une
        # commande perdue n'en recevra jamais : sans cette purge, le mot de
        # passe resterait en base indéfiniment.
        values["command_text"] = LOST_REDACTED_TEXT
    claimed = (
        db.session.query(Command)
        .filter(Command.id == cmd.id, Command.status == "dispatched")
        .update(values, synchronize_session=False)
    )
    if not claimed:
        return False
    if db.session.get(CommandResult, cmd.id) is None:
        db.session.add(CommandResult(
            command_id=cmd.id, exit_code=None, stdout="", stderr=LOST_STDERR,
            duration_seconds=None, received_at=now,
        ))
    db.session.commit()
    return True


def expire_lost_commands(now=None) -> int:
    """Passe en ``timeout`` les commandes remises dont le résultat n'arrivera plus.

    Renvoie le nombre de commandes expirées. S'exécute dans un contexte
    d'application : le cycle de fond (``tasks``), ou un test qui injecte ``now``.
    """
    now = _aware(now) or utcnow()
    grace = float(current_app.config.get("COMMAND_RESULT_GRACE_SECONDS", 60))

    candidates = db.session.query(Command).filter(Command.status == "dispatched").all()
    expired = 0
    for cmd in candidates:
        send = _same_send(cmd)
        deadline = _deadline(cmd, send, grace)
        if now <= deadline:
            continue
        if _mark_lost(cmd, now):
            expired += 1
            _logger.warning(
                "Commande %s perdue (agent %s) : aucun résultat depuis sa remise à %s, "
                "échéance %s dépassée.", cmd.id, cmd.agent_id,
                _aware(cmd.dispatched_at), deadline,
            )
    return expired
