"""Évaluation des règles d'alerte et notification n8n (cf. SPEC §1 / §4.1).

Types de règles supportés : ``offline``, ``disk_low``, ``cpu_high``, ``ram_high``.
Pour chaque règle active, on crée une ligne ``alerts`` quand la condition devient
vraie (et qu'aucune alerte non résolue n'existe déjà), et on la résout quand la
condition redevient fausse. Toute nouvelle alerte est notifiée à n8n si
``N8N_WEBHOOK_URL`` est défini — un échec réseau ne fait jamais planter la boucle.
"""
import logging
from datetime import timezone

import requests

from .extensions import db
from .models import Agent, Alert, AlertRule, Metric
from .models import utcnow

_logger = logging.getLogger("parcvue.alerts")

# Délai d'attente court pour ne jamais bloquer la boucle de fond.
_N8N_TIMEOUT_SECONDS = 5


def evaluate_all(app):
    """Évalue toutes les règles actives pour tous les agents.

    Doit être appelé dans un contexte d'application (``app.app_context()``).
    Ne lève jamais : toute exception est journalisée et avalée.
    """
    try:
        rules = db.session.query(AlertRule).filter_by(is_active=True).all()
        if not rules:
            return
        agents = db.session.query(Agent).all()
        offline_threshold = app.config["OFFLINE_THRESHOLD_SECONDS"]

        for agent in agents:
            latest = _latest_metric(agent.id)
            for rule in rules:
                _evaluate_rule(app, agent, rule, latest, offline_threshold)

        db.session.commit()
    except Exception:  # pragma: no cover - robustesse boucle de fond
        _logger.exception("Échec de l'évaluation des alertes")
        db.session.rollback()


def _latest_metric(agent_id):
    """Dernier point de métriques d'un agent (ou None)."""
    return (
        db.session.query(Metric)
        .filter(Metric.agent_id == agent_id)
        .order_by(Metric.ts.desc())
        .first()
    )


def _active_alert(agent_id, rule_id):
    """Retourne l'alerte non résolue existante pour (agent, règle), ou None."""
    return (
        db.session.query(Alert)
        .filter(
            Alert.agent_id == agent_id,
            Alert.rule_id == rule_id,
            Alert.resolved_at.is_(None),
        )
        .first()
    )


def _evaluate_rule(app, agent: Agent, rule: AlertRule, metric, offline_threshold: int):
    """Évalue une règle pour un agent et gère le cycle déclenchement/résolution."""
    breached, value = _check_condition(agent, rule, metric, offline_threshold)
    existing = _active_alert(agent.id, rule.id)

    if breached and existing is None:
        alert = Alert(
            agent_id=agent.id,
            rule_id=rule.id,
            triggered_at=utcnow(),
            notified=False,
        )
        db.session.add(alert)
        db.session.flush()
        # Notification n8n (best-effort).
        notified = _notify_n8n(
            app,
            alert_type=rule.type,
            hostname=agent.hostname,
            agent_id=str(agent.id),
            value=value,
            threshold=float(rule.threshold) if rule.threshold is not None else None,
        )
        alert.notified = notified

    elif not breached and existing is not None:
        existing.resolved_at = utcnow()


def _check_condition(agent: Agent, rule: AlertRule, metric, offline_threshold: int):
    """Retourne (condition_atteinte, valeur_observée) pour une règle donnée."""
    threshold = float(rule.threshold) if rule.threshold is not None else 0.0

    if rule.type == "offline":
        # Hors-ligne si pas vu depuis > OFFLINE_THRESHOLD_SECONDS.
        if agent.last_seen_at is None:
            return True, None
        last = agent.last_seen_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        seconds = (utcnow() - last).total_seconds()
        return seconds >= offline_threshold, round(seconds, 1)

    if metric is None:
        return False, None

    if rule.type == "cpu_high":
        cpu = _num(metric.cpu_pct)
        if cpu is None:
            return False, None
        return cpu >= threshold, cpu

    if rule.type == "ram_high":
        ram = _num(metric.ram_used_pct)
        if ram is None:
            return False, None
        return ram >= threshold, ram

    if rule.type == "disk_low":
        # threshold = % d'espace libre minimum ; on compare au % libre le plus bas.
        disk_free = metric.disk_free or {}
        worst_pct = _worst_free_pct(agent, disk_free)
        if worst_pct is None:
            return False, None
        return worst_pct <= threshold, worst_pct

    return False, None


def _worst_free_pct(agent: Agent, disk_free: dict):
    """Calcule le plus faible pourcentage d'espace libre parmi les disques.

    ``disk_free`` donne les Go libres par lecteur ; la capacité totale provient
    de l'inventaire matériel (``disks``). Si la capacité est inconnue, le disque
    est ignoré pour ce calcul.
    """
    hw = agent.hardware
    totals = {}
    if hw is not None and hw.disks:
        for d in hw.disks:
            if isinstance(d, dict) and d.get("drive") and d.get("total_gb"):
                try:
                    totals[d["drive"]] = float(d["total_gb"])
                except (TypeError, ValueError):
                    continue

    worst = None
    for drive, free_gb in (disk_free or {}).items():
        try:
            free = float(free_gb)
        except (TypeError, ValueError):
            continue
        total = totals.get(drive)
        if not total or total <= 0:
            continue
        pct_free = (free / total) * 100.0
        if worst is None or pct_free < worst:
            worst = round(pct_free, 2)
    return worst


def _num(value):
    """Convertit un Numeric/Decimal en float (ou None)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _notify_n8n(app, alert_type, hostname, agent_id, value, threshold) -> bool:
    """Poste l'alerte vers n8n. Best-effort : retourne True si envoyé, False sinon."""
    url = app.config.get("N8N_WEBHOOK_URL", "")
    if not url:
        return False
    payload = {
        "type": alert_type,
        "hostname": hostname,
        "agent_id": agent_id,
        "value": value,
        "threshold": threshold,
        "ts": utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    try:
        resp = requests.post(url, json=payload, timeout=_N8N_TIMEOUT_SECONDS)
        if resp.status_code >= 400:
            _logger.warning("n8n a répondu %s pour l'alerte %s", resp.status_code, alert_type)
            return False
        return True
    except requests.RequestException as exc:
        # n8n injoignable : on journalise et on continue (jamais d'exception remontée).
        _logger.warning("n8n injoignable (%s) : %s", url, exc)
        return False
