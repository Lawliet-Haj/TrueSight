"""Évaluation des règles d'alerte et notification n8n (cf. SPEC §1 / §4.1).

Types de règles supportés : ``offline``, ``disk_low``, ``cpu_high``, ``ram_high``.
Pour chaque règle active, on crée une ligne ``alerts`` quand la condition devient
vraie (et qu'aucune alerte non résolue n'existe déjà), et on la résout quand la
condition redevient fausse. Toute nouvelle alerte est notifiée à n8n si
``N8N_WEBHOOK_URL`` est défini — un échec réseau ne fait jamais planter la boucle.
"""
import logging
import re
from datetime import timedelta, timezone

import requests

from .extensions import db
from .models import (
    Agent,
    AgentServices,
    Alert,
    AlertRule,
    Command,
    Metric,
    RemediationAttempt,
    ServiceWatch,
)
from .models import utcnow
from .security import write_audit

_logger = logging.getLogger("truesight.alerts")

# Délai d'attente court pour ne jamais bloquer la boucle de fond.
_N8N_TIMEOUT_SECONDS = 5

# Nom de service Windows autorisé pour une remédiation (liste blanche stricte) :
# alphanumérique + . _ - espace. Bloque toute injection PowerShell.
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9._\- ]{1,128}$")


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
        # Services supervisés (préchargés une fois par cycle) — utilisés par la
        # règle service_down. Petite table, on évite N requêtes par agent.
        watches = (
            db.session.query(ServiceWatch).filter_by(is_active=True).all()
            if any(r.type == "service_down" for r in rules) else []
        )

        for agent in agents:
            latest = _latest_metric(agent.id)
            for rule in rules:
                _evaluate_rule(app, agent, rule, latest, offline_threshold, watches)

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


def _evaluate_rule(app, agent: Agent, rule: AlertRule, metric, offline_threshold: int, watches=None):
    """Évalue une règle pour un agent et gère le cycle déclenchement/résolution."""
    # La règle service_down est multi-services (1 poste → N services attendus) et
    # déclenche l'auto-remédiation : traitement dédié.
    if rule.type == "service_down":
        _evaluate_service_down(app, agent, rule, watches or [])
        return

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


# --------------------------------------------------------------------------
# Supervision des services Windows + auto-remédiation
# --------------------------------------------------------------------------
def _watch_applies(watch: ServiceWatch, agent: Agent) -> bool:
    """Le service surveillé s'applique-t-il à ce poste (scope global/site/tag) ?"""
    scope = (watch.scope or "global").lower()
    if scope == "global":
        return True
    if scope == "site":
        return bool(agent.site_id) and str(agent.site_id) == (watch.scope_value or "")
    if scope == "tag":
        return (watch.scope_value or "") in (agent.tags or [])
    return False


def _down_watched_services(agent: Agent, watches) -> list:
    """[(watch, service_name)] des services attendus NON démarrés sur ce poste.

    Si l'agent n'a pas encore remonté ses services (AgentServices absent), on ne
    déclenche rien (pas de faux positif au démarrage)."""
    applicable = [w for w in watches if _watch_applies(w, agent)]
    if not applicable:
        return []
    svc_row = db.session.get(AgentServices, agent.id)
    if svc_row is None or not isinstance(svc_row.services, list):
        return []
    by_name = {}
    for s in svc_row.services:
        if isinstance(s, dict) and s.get("name"):
            by_name[str(s["name"]).lower()] = str(s.get("state") or "").lower()
    down = []
    for w in applicable:
        state = by_name.get((w.service_name or "").lower())
        if state != "running":  # absent OU arrêté → en panne
            down.append((w, w.service_name))
    return down


def _evaluate_service_down(app, agent: Agent, rule: AlertRule, watches):
    """Alerte service_down (1 alerte/poste, contexte = services en panne) +
    auto-remédiation par service (garde-fous anti-boucle)."""
    down = _down_watched_services(agent, watches)
    existing = _active_alert(agent.id, rule.id)
    down_names = [name for (_w, name) in down]

    if down and existing is None:
        alert = Alert(
            agent_id=agent.id, rule_id=rule.id, triggered_at=utcnow(),
            notified=False, context={"services": down_names},
        )
        db.session.add(alert)
        db.session.flush()
        alert.notified = _notify_n8n(
            app, alert_type="service_down", hostname=agent.hostname,
            agent_id=str(agent.id), value=", ".join(down_names), threshold=None,
        )
    elif down and existing is not None:
        if (existing.context or {}).get("services") != down_names:
            existing.context = {"services": down_names}
    elif not down and existing is not None:
        existing.resolved_at = utcnow()

    # Auto-remédiation : pour chaque service en panne avec auto_restart activé.
    # Appelée à chaque cycle ; les garde-fous (cooldown/max/anti-doublon) évitent
    # les rafales et couvrent un service tombé alors que l'alerte est déjà active.
    if down and app.config.get("REMEDIATION_AUTO_RESTART_ENABLED", True):
        for watch, name in down:
            if watch.auto_restart:
                try:
                    _maybe_remediate(app, agent, watch, name)
                except Exception:  # noqa: BLE001 - jamais bloquant.
                    _logger.exception("Auto-remédiation %s/%s en échec", agent.id, name)


def _maybe_remediate(app, agent: Agent, watch: ServiceWatch, service_name: str):
    """Garde-fous anti-boucle puis, si autorisé, file une commande de redémarrage."""
    now = utcnow()
    cooldown = app.config.get("REMEDIATION_COOLDOWN_SECONDS", 600)
    window = app.config.get("REMEDIATION_WINDOW_SECONDS", 3600)
    global_max = int(app.config.get("REMEDIATION_MAX_ATTEMPTS", 3))
    max_attempts = min(int(watch.max_attempts or global_max), global_max)

    recent = (
        db.session.query(RemediationAttempt)
        .filter(RemediationAttempt.agent_id == agent.id,
                RemediationAttempt.service_name == service_name)
        .order_by(RemediationAttempt.created_at.desc())
        .all()
    )
    # (1) Anti-doublon : une commande de remédiation encore en vol ?
    for att in recent:
        if att.command_id is not None:
            cmd = db.session.get(Command, att.command_id)
            if cmd is not None and cmd.status in ("pending", "dispatched"):
                return
    # (2) Cooldown : une tentative trop récente ?
    if recent:
        last = recent[0].created_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < cooldown:
                return
    # (3) Plafond de tentatives sur la fenêtre glissante.
    cutoff = now - timedelta(seconds=window)
    in_window = 0
    for att in recent:
        ts = att.created_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts is not None and ts >= cutoff:
            in_window += 1
    if in_window >= max_attempts:
        _logger.info("Remédiation plafonnée pour %s/%s (%d/%d).",
                     agent.id, service_name, in_window, max_attempts)
        return
    _queue_remediation_command(app, agent, service_name, in_window + 1)


def _queue_remediation_command(app, agent: Agent, service_name: str, attempt_n: int):
    """Crée une Command système (created_by=NULL) de redémarrage du service + une
    RemediationAttempt + un audit. Valide STRICTEMENT le nom (anti-injection)."""
    if not _SERVICE_NAME_RE.match(service_name or ""):
        _logger.warning("Nom de service invalide, remédiation refusée : %r", service_name)
        return
    timeout = int(app.config.get("REMEDIATION_COMMAND_TIMEOUT", 120))
    lit = "'" + service_name.replace("'", "''") + "'"  # littéral PowerShell sûr
    command_text = (
        "try { Restart-Service -Name " + lit + " -Force -ErrorAction Stop; "
        "Write-Output 'OK' } catch { Write-Error $_; exit 1 }"
    )
    cmd = Command(
        agent_id=agent.id, created_by=None, shell="powershell",
        command_text=command_text, status="pending", timeout_seconds=timeout,
        created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()
    db.session.add(RemediationAttempt(
        agent_id=agent.id, service_name=service_name, command_id=cmd.id,
        created_at=utcnow(), outcome="queued",
    ))
    write_audit(
        action="remediation.auto", user_id=None, target_agent=agent.id,
        details={"service": service_name, "command_id": str(cmd.id), "attempt": attempt_n},
        commit=False,
    )
    _logger.info("Auto-remédiation filée : %s -> redémarrage de %s (tentative %d).",
                 agent.hostname, service_name, attempt_n)


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
