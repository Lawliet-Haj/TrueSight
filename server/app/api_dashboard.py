"""Blueprint API JSON du dashboard (cf. SPEC §3).

Toutes les routes nécessitent une session authentifiée (``login_required``).
La création de commandes et le journal d'audit sont réservés aux administrateurs
(``admin_required``).
"""
import base64
import csv
import io
import re
import uuid
from datetime import timedelta

import pyotp
import qrcode
from flask import Blueprint, current_app, g, jsonify, request, session

from .extensions import db
from .health import PROBLEM_LABELS, agent_health, is_online
from .models import (
    Agent,
    AgentSecurity,
    Alert,
    AlertRule,
    AuditLog,
    Command,
    CommandResult,
    HardwareInventory,
    Metric,
    PatchJob,
    RemoteSession,
    Site,
    SoftwareInventory,
    User,
)
from .models import utcnow
from .security import (
    admin_required,
    generate_session_token,
    hash_password,
    hash_token,
    login_required,
    store_session_token,
    verify_password,
    write_audit,
)

bp = Blueprint("api_dashboard", __name__, url_prefix="/api/v1")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _iso_utc(dt):
    """Formate un datetime en ISO 8601 UTC explicite avec suffixe Z."""
    if dt is None:
        return None
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _is_online(agent: Agent, threshold_seconds: int) -> bool:
    """Détermine si un agent est en ligne selon la dernière activité."""
    if agent.last_seen_at is None:
        return False
    from datetime import timezone

    last = agent.last_seen_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delta = (utcnow() - last).total_seconds()
    return delta < threshold_seconds


def _num(value):
    """Convertit un Numeric/Decimal en float pour la sérialisation JSON (ou None)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_uuid(value):
    """Convertit en UUID ou renvoie None."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _latest_metric(agent_id):
    """Renvoie le dernier point de métriques d'un agent (ou None)."""
    return (
        db.session.query(Metric)
        .filter(Metric.agent_id == agent_id)
        .order_by(Metric.ts.desc())
        .first()
    )


def _ws_base_url() -> str:
    """Construit la base WebSocket (ws:// ou wss://) depuis l'URL d'hôte de la requête.

    ``request.host_url`` reflète le scheme/host externes via ProxyFix
    (X-Forwarded-Proto / X-Forwarded-Host). https→wss, http→ws (cf. CONTRAT REMOTE).
    """
    base = request.host_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


# --------------------------------------------------------------------------
# GET /agents — liste du parc
# --------------------------------------------------------------------------
def _active_alert_types_map() -> dict:
    """Construit {agent_id: set(types d'alertes actives)} pour tout le parc."""
    rows = (
        db.session.query(Alert.agent_id, AlertRule.type)
        .join(AlertRule, Alert.rule_id == AlertRule.id)
        .filter(Alert.resolved_at.is_(None))
        .all()
    )
    out: dict = {}
    for agent_id, atype in rows:
        out.setdefault(agent_id, set()).add(atype)
    return out


def _sites_map() -> dict:
    """Renvoie {site_id: Site} pour résolution rapide."""
    return {s.id: s for s in db.session.query(Site).all()}


def _security_map() -> dict:
    """Renvoie {agent_id: AgentSecurity} pour tout le parc."""
    return {s.agent_id: s for s in db.session.query(AgentSecurity).all()}


def _sec_dict(sec):
    """Normalise un AgentSecurity en dict {defender, windows_update} (ou None)."""
    if sec is None:
        return None
    return {"defender": sec.defender or {}, "windows_update": sec.windows_update or {}}


def _security_summary(sec):
    """Résumé compact pour la liste : MAJ en attente + état antivirus."""
    if sec is None:
        return None
    wu = sec.windows_update or {}
    df = sec.defender or {}
    return {
        "pending_updates": wu.get("pending_count"),
        "pending_critical": wu.get("pending_critical"),
        "defender_enabled": df.get("enabled"),
        "defender_realtime": df.get("realtime"),
        "collected_at": _iso_utc(sec.collected_at),
    }


def _agent_display_name(agent: Agent) -> str:
    """Nom affiché : nom convivial s'il existe, sinon hostname, sinon id."""
    return agent.display_name or agent.hostname or str(agent.id)


@bp.get("/agents")
@login_required
def list_agents():
    """Liste des agents : statut, métriques, emplacement et santé calculée.

    Filtres optionnels : ``?site=<uuid>`` (ou ``none`` pour les non assignés) et
    ``?health=healthy|warning|critical|unknown``.
    """
    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    site_filter = (request.args.get("site") or "").strip()
    health_filter = (request.args.get("health") or "").strip()
    security_filter = (request.args.get("security") or "").strip()

    agents = db.session.query(Agent).order_by(Agent.hostname.asc()).all()
    sites = _sites_map()
    alert_map = _active_alert_types_map()
    sec_map = _security_map()

    out = []
    for agent in agents:
        if site_filter:
            if site_filter == "none" and agent.site_id is not None:
                continue
            if site_filter != "none" and str(agent.site_id) != site_filter:
                continue
        sec = sec_map.get(agent.id)
        if security_filter == "updates":
            wu = (sec.windows_update if sec else None) or {}
            if not (isinstance(wu.get("pending_count"), int) and wu["pending_count"] > 0):
                continue
        elif security_filter == "defender":
            df = (sec.defender if sec else None) or {}
            if df.get("enabled") is not False:
                continue
        metric = _latest_metric(agent.id)
        health, reasons = agent_health(
            agent, metric, alert_map.get(agent.id, set()), current_app.config, _sec_dict(sec)
        )
        if health_filter and health != health_filter:
            continue
        site = sites.get(agent.site_id)
        out.append(
            {
                "id": str(agent.id),
                "hostname": agent.hostname,
                "display_name": agent.display_name,
                "name": _agent_display_name(agent),
                "os_version": agent.os_version,
                "status": "online" if _is_online(agent, threshold) else "offline",
                "last_seen_at": _iso_utc(agent.last_seen_at),
                "cpu_pct": _num(metric.cpu_pct) if metric else None,
                "ram_used_pct": _num(metric.ram_used_pct) if metric else None,
                "tags": agent.tags or [],
                "is_active": agent.is_active,
                "site_id": str(agent.site_id) if agent.site_id else None,
                "site_name": site.name if site else None,
                "site_color": (site.color if site else None),
                "health": health,
                "health_reasons": reasons,
                "security": _security_summary(sec),
            }
        )
    return jsonify(out), 200


# --------------------------------------------------------------------------
# GET /agents/export.csv — export du parc (honore ?site / ?health / ?security)
# --------------------------------------------------------------------------
_HEALTH_FR = {"healthy": "Sain", "warning": "Attention", "critical": "Défectueux", "unknown": "Inconnu"}


@bp.get("/agents/export.csv")
@login_required
def export_agents_csv():
    """Export CSV du parc (séparateur ';', UTF-8 BOM pour Excel), filtres repris de la liste."""
    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    site_filter = (request.args.get("site") or "").strip()
    health_filter = (request.args.get("health") or "").strip()
    security_filter = (request.args.get("security") or "").strip()

    agents = db.session.query(Agent).order_by(Agent.hostname.asc()).all()
    sites = _sites_map()
    alert_map = _active_alert_types_map()
    sec_map = _security_map()

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "Nom", "Hote", "Emplacement", "Etat", "Sante", "Raisons", "Systeme",
        "CPU %", "RAM %", "MAJ en attente", "Antivirus", "Etiquettes", "Derniere activite",
    ])

    for agent in agents:
        if site_filter:
            if site_filter == "none" and agent.site_id is not None:
                continue
            if site_filter != "none" and str(agent.site_id) != site_filter:
                continue
        sec = sec_map.get(agent.id)
        if security_filter == "updates":
            wu = (sec.windows_update if sec else None) or {}
            if not (isinstance(wu.get("pending_count"), int) and wu["pending_count"] > 0):
                continue
        elif security_filter == "defender":
            df = (sec.defender if sec else None) or {}
            if df.get("enabled") is not False:
                continue
        metric = _latest_metric(agent.id)
        health, reasons = agent_health(
            agent, metric, alert_map.get(agent.id, set()), current_app.config, _sec_dict(sec)
        )
        if health_filter and health != health_filter:
            continue
        site = sites.get(agent.site_id)
        wu = (sec.windows_update if sec else None) or {}
        df = (sec.defender if sec else None) or {}
        av = "—"
        if df.get("enabled") is True:
            av = "OK"
        elif df.get("enabled") is False:
            av = "Désactivé"
        writer.writerow([
            _agent_display_name(agent),
            agent.hostname or "",
            site.name if site else "",
            "En ligne" if _is_online(agent, threshold) else "Hors ligne",
            _HEALTH_FR.get(health, health),
            ", ".join(reasons),
            agent.os_version or "",
            f"{_num(metric.cpu_pct):.0f}" if metric and metric.cpu_pct is not None else "",
            f"{_num(metric.ram_used_pct):.0f}" if metric and metric.ram_used_pct is not None else "",
            wu.get("pending_count") if isinstance(wu.get("pending_count"), int) else "",
            av,
            ", ".join(agent.tags or []),
            _iso_utc(agent.last_seen_at) or "",
        ])

    body = "﻿" + buf.getvalue()  # BOM : Excel ouvre l'UTF-8 correctement.
    return current_app.response_class(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=truesight-parc.csv"},
    )


# --------------------------------------------------------------------------
# GET /agents/<id> — détail
# --------------------------------------------------------------------------
@bp.get("/agents/<agent_id>")
@login_required
def get_agent(agent_id):
    """Détail complet d'un agent : agent + matériel + dernier metrics + nb logiciels."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    hw: HardwareInventory | None = db.session.get(HardwareInventory, aid)
    metric = _latest_metric(aid)
    software_count = (
        db.session.query(SoftwareInventory).filter_by(agent_id=aid).count()
    )

    hardware_payload = None
    if hw is not None:
        hardware_payload = {
            "manufacturer": hw.manufacturer,
            "model": hw.model,
            "serial_number": hw.serial_number,
            "cpu_model": hw.cpu_model,
            "cpu_cores": hw.cpu_cores,
            "ram_total_mb": hw.ram_total_mb,
            "disks": hw.disks or [],
            "mac_addresses": hw.mac_addresses or [],
            "collected_at": _iso_utc(hw.collected_at),
        }

    last_metric_payload = None
    if metric is not None:
        last_metric_payload = {
            "ts": _iso_utc(metric.ts),
            "cpu_pct": _num(metric.cpu_pct),
            "ram_used_pct": _num(metric.ram_used_pct),
            "disk_free": metric.disk_free or {},
            "uptime_seconds": metric.uptime_seconds,
            "logged_in_user": metric.logged_in_user,
        }

    alert_types = {
        t for (t,) in db.session.query(AlertRule.type)
        .join(Alert, Alert.rule_id == AlertRule.id)
        .filter(Alert.agent_id == aid, Alert.resolved_at.is_(None))
        .all()
    }
    sec = db.session.get(AgentSecurity, aid)
    health, reasons = agent_health(agent, metric, alert_types, current_app.config, _sec_dict(sec))
    site = db.session.get(Site, agent.site_id) if agent.site_id else None

    payload = {
        "id": str(agent.id),
        "machine_id": agent.machine_id,
        "hostname": agent.hostname,
        "display_name": agent.display_name,
        "name": _agent_display_name(agent),
        "agent_version": agent.agent_version,
        "os_version": agent.os_version,
        "enrolled_at": _iso_utc(agent.enrolled_at),
        "last_seen_at": _iso_utc(agent.last_seen_at),
        "is_active": agent.is_active,
        "tags": agent.tags or [],
        "site_id": str(agent.site_id) if agent.site_id else None,
        "site_name": site.name if site else None,
        "status": "online" if _is_online(agent, threshold) else "offline",
        "health": health,
        "health_reasons": reasons,
        "security": _security_summary(sec),
        "hardware": hardware_payload,
        "last_metric": last_metric_payload,
        "software_count": software_count,
    }
    return jsonify(payload), 200


# --------------------------------------------------------------------------
# GET /agents/<id>/software — logiciels
# --------------------------------------------------------------------------
@bp.get("/agents/<agent_id>/software")
@login_required
def get_agent_software(agent_id):
    """Liste des logiciels installés d'un agent."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    if db.session.get(Agent, aid) is None:
        return jsonify({"error": "agent introuvable"}), 404

    rows = (
        db.session.query(SoftwareInventory)
        .filter_by(agent_id=aid)
        .order_by(SoftwareInventory.name.asc())
        .all()
    )
    out = [
        {
            "name": r.name,
            "version": r.version,
            "publisher": r.publisher,
            "install_date": r.install_date.isoformat() if r.install_date else None,
        }
        for r in rows
    ]
    return jsonify(out), 200


# --------------------------------------------------------------------------
# GET /agents/<id>/metrics?hours=24 — séries
# --------------------------------------------------------------------------
@bp.get("/agents/<agent_id>/metrics")
@login_required
def get_agent_metrics(agent_id):
    """Séries temporelles de métriques sur ``hours`` heures (défaut 24)."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    if db.session.get(Agent, aid) is None:
        return jsonify({"error": "agent introuvable"}), 404

    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 24 * 31))  # borné entre 1h et ~1 mois

    since = utcnow() - timedelta(hours=hours)
    rows = (
        db.session.query(Metric)
        .filter(Metric.agent_id == aid, Metric.ts >= since)
        .order_by(Metric.ts.asc())
        .all()
    )
    out = [
        {
            "ts": _iso_utc(r.ts),
            "cpu_pct": _num(r.cpu_pct),
            "ram_used_pct": _num(r.ram_used_pct),
            "disk_free": r.disk_free or {},
            "uptime_seconds": r.uptime_seconds,
        }
        for r in rows
    ]
    return jsonify(out), 200


# --------------------------------------------------------------------------
# POST /agents/<id>/commands — création (admin only)
# --------------------------------------------------------------------------
@bp.post("/agents/<agent_id>/commands")
@admin_required
def create_command(agent_id):
    """Crée une commande ``pending`` pour un agent. Réservé aux administrateurs."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    shell = (data.get("shell") or "").strip().lower()
    command_text = data.get("command_text") or ""
    timeout_seconds = data.get("timeout_seconds", 120)

    if shell not in ("powershell", "cmd"):
        return jsonify({"error": "shell invalide (powershell ou cmd)"}), 400
    if not command_text.strip():
        return jsonify({"error": "command_text requis"}), 400
    try:
        timeout_seconds = int(timeout_seconds)
        if timeout_seconds <= 0 or timeout_seconds > 3600:
            timeout_seconds = 120
    except (TypeError, ValueError):
        timeout_seconds = 120

    cmd = Command(
        agent_id=aid,
        created_by=g.user.id,
        shell=shell,
        command_text=command_text,
        status="pending",
        timeout_seconds=timeout_seconds,
        created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()  # obtient l'id avant l'audit

    write_audit(
        action="command.create",
        user_id=g.user.id,
        target_agent=aid,
        details={
            "command_id": str(cmd.id),
            "shell": shell,
            "command_text": command_text,
            "timeout_seconds": timeout_seconds,
        },
        commit=False,
    )
    db.session.commit()

    return jsonify({"command_id": str(cmd.id)}), 201


# --------------------------------------------------------------------------
# GET /commands/<id> — statut + résultat
# --------------------------------------------------------------------------
@bp.get("/commands/<command_id>")
@login_required
def get_command(command_id):
    """Statut et résultat d'une commande."""
    cid = _parse_uuid(command_id)
    if cid is None:
        return jsonify({"error": "command_id invalide"}), 400
    cmd = db.session.get(Command, cid)
    if cmd is None:
        return jsonify({"error": "commande introuvable"}), 404

    result_payload = None
    result: CommandResult | None = db.session.get(CommandResult, cid)
    if result is not None:
        result_payload = {
            "exit_code": result.exit_code,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "duration_seconds": _num(result.duration_seconds),
        }

    payload = {
        "id": str(cmd.id),
        "agent_id": str(cmd.agent_id),
        "status": cmd.status,
        "command_text": cmd.command_text,
        "shell": cmd.shell,
        "created_at": _iso_utc(cmd.created_at),
        "dispatched_at": _iso_utc(cmd.dispatched_at),
        "completed_at": _iso_utc(cmd.completed_at),
        "result": result_payload,
    }
    return jsonify(payload), 200


# --------------------------------------------------------------------------
# GET /audit?limit=200 — journal (admin)
# --------------------------------------------------------------------------
@bp.get("/audit")
@admin_required
def get_audit():
    """Entrées du journal d'audit (les plus récentes d'abord)."""
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))

    rows = (
        db.session.query(AuditLog)
        .order_by(AuditLog.ts.desc())
        .limit(limit)
        .all()
    )

    # Pré-chargement des emails pour affichage.
    user_ids = {r.user_id for r in rows if r.user_id is not None}
    users = {}
    if user_ids:
        for u in db.session.query(User).filter(User.id.in_(user_ids)).all():
            users[u.id] = u.email

    out = [
        {
            "id": r.id,
            "ts": _iso_utc(r.ts),
            "user_id": str(r.user_id) if r.user_id else None,
            "user_email": users.get(r.user_id),
            "action": r.action,
            "target_agent": str(r.target_agent) if r.target_agent else None,
            "ip": str(r.ip) if r.ip else None,
            "details": r.details or {},
        }
        for r in rows
    ]
    return jsonify(out), 200


# --------------------------------------------------------------------------
# POST /agents/<id>/remote-session — démarrage d'une session bureau à distance (admin)
# --------------------------------------------------------------------------
@bp.post("/agents/<agent_id>/remote-session")
@admin_required
def create_remote_session(agent_id):
    """Crée une session de bureau à distance ``requested`` pour un agent (admin only).

    Génère un jeton de session aléatoire url-safe, persiste son hash SHA-256 dans
    ``remote_sessions``, écrit l'audit ``remote.start`` et renvoie 201 avec le jeton
    EN CLAIR + l'URL WebSocket viewer. Le jeton ne sera plus jamais renvoyé ensuite
    (TTL court, usage unique). Cf. CONTRAT REMOTE.

    Body JSON optionnel : ``{"kind": "remote"|"terminal", "shell": "powershell"|"cmd"}``.
    ``kind`` vaut 'remote' par défaut (absent ou inconnu → 'remote'). Quand
    ``kind == 'terminal'``, ``shell`` vaut 'powershell' par défaut. Le ``ws_url``
    (chemin ``/ws/remote/viewer``) reste INCHANGÉ quel que soit le ``kind``.
    """
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "remote").strip().lower()
    if kind not in ("remote", "terminal"):
        kind = "remote"

    shell = None
    if kind == "terminal":
        shell = (data.get("shell") or "powershell").strip().lower()
        if shell not in ("powershell", "cmd"):
            shell = "powershell"

    token = generate_session_token()
    sess = RemoteSession(
        agent_id=aid,
        admin_user_id=g.user.id,
        token_hash=hash_token(token),
        status="requested",
        kind=kind,
        shell=shell,
        requested_at=utcnow(),
    )
    db.session.add(sess)
    db.session.flush()  # obtient l'id avant l'audit + le cache jeton

    # Mémorise le jeton en clair (la base ne stocke que le hash) afin de pouvoir
    # le transmettre à l'agent via la signalisation (réponse heartbeat / commands).
    store_session_token(str(sess.id), token)

    write_audit(
        action="remote.start",
        user_id=g.user.id,
        target_agent=aid,
        details={"session_id": str(sess.id), "kind": kind},
        commit=False,
    )
    db.session.commit()

    ws_url = f"{_ws_base_url()}/ws/remote/viewer?token={token}"
    return (
        jsonify(
            {
                "session_id": str(sess.id),
                "token": token,
                "ws_url": ws_url,
                "kind": kind,
                "shell": shell,
            }
        ),
        201,
    )


# --------------------------------------------------------------------------
# GET /remote-sessions/<id> — statut d'une session bureau à distance (admin)
# --------------------------------------------------------------------------
@bp.get("/remote-sessions/<session_id>")
@admin_required
def get_remote_session(session_id):
    """Statut d'une session de bureau à distance (admin only)."""
    sid = _parse_uuid(session_id)
    if sid is None:
        return jsonify({"error": "session_id invalide"}), 400
    sess = db.session.get(RemoteSession, sid)
    if sess is None:
        return jsonify({"error": "session introuvable"}), 404

    return (
        jsonify(
            {
                "status": sess.status,
                "started_at": _iso_utc(sess.started_at),
                "ended_at": _iso_utc(sess.ended_at),
            }
        ),
        200,
    )


# --------------------------------------------------------------------------
# POST /agents/<id>/quick-action — action rapide (admin)
# --------------------------------------------------------------------------
# Actions rapides : chaque action est traduite en une commande shell exécutée
# par l'agent via le pipeline de commandes existant (le résultat se lit comme
# une commande normale via GET /api/v1/commands/<id>). Toutes utilisent ``cmd``.
_QUICK_ACTIONS = {
    "lock": "rundll32.exe user32.dll,LockWorkStation",
    "restart": 'shutdown /r /t 5 /c "TrueSight: redemarrage demande"',
    "logoff": "shutdown /l",
    # 'message' est construit dynamiquement à partir du champ ``text``.
}


def _clean_message_text(value: str) -> str:
    """Nettoie le texte d'un message : retire guillemets doubles et caractères de
    contrôle/sauts de ligne, puis tronque à 240 caractères."""
    if not isinstance(value, str):
        return ""
    # Supprime les guillemets doubles (évite de casser la commande msg).
    cleaned = value.replace('"', "")
    # Supprime les caractères de contrôle (dont \r\n\t).
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    cleaned = cleaned.strip()
    return cleaned[:240]


@bp.post("/agents/<agent_id>/quick-action")
@admin_required
def quick_action(agent_id):
    """Lance une action rapide sur un agent (admin only).

    Body : ``{"action": "lock"|"restart"|"logoff"|"message", "text": "..."}``.
    ``text`` n'est requis que pour l'action 'message'. L'action est mappée vers
    une commande shell ``cmd`` puis matérialisée comme une ligne ``Command``
    ``pending`` (exactement comme la création de commande normale), avec un audit
    ``command.quickaction``. Renvoie 201 ``{"command_id": "..."}``.
    """
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()

    if action not in ("lock", "restart", "logoff", "message"):
        return jsonify({"error": "action invalide (lock, restart, logoff, message)"}), 400

    if action == "message":
        text = _clean_message_text(data.get("text") or "")
        if not text:
            return jsonify({"error": "text requis pour l'action message"}), 400
        command_text = f'msg * "{text}"'
        timeout_seconds = 15
    else:
        command_text = _QUICK_ACTIONS[action]
        timeout_seconds = 30

    cmd = Command(
        agent_id=aid,
        created_by=g.user.id,
        shell="cmd",
        command_text=command_text,
        status="pending",
        timeout_seconds=timeout_seconds,
        created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()  # obtient l'id avant l'audit

    write_audit(
        action="command.quickaction",
        user_id=g.user.id,
        target_agent=aid,
        details={
            "command_id": str(cmd.id),
            "action": action,
            "command_text": command_text,
        },
        commit=False,
    )
    db.session.commit()

    return jsonify({"command_id": str(cmd.id)}), 201


# --------------------------------------------------------------------------
# POST /agents/<id>/tags — étiquettes du poste (admin)
# --------------------------------------------------------------------------
def _normalize_tags(raw):
    """Normalise une liste d'étiquettes : trim, sans doublon (insensible à la casse),
    24 caractères max chacune, 15 au total."""
    seen = set()
    out = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip()[:24]
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= 15:
            break
    return out


@bp.post("/agents/<agent_id>/tags")
@admin_required
def set_agent_tags(agent_id):
    """Remplace les étiquettes d'un poste. Body : ``{tags: [...]}``."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    raw = data.get("tags")
    if not isinstance(raw, list):
        return jsonify({"error": "tags doit être une liste"}), 400

    tags = _normalize_tags(raw)
    agent.tags = tags
    write_audit(
        action="agent.tags", user_id=g.user.id, target_agent=aid,
        details={"tags": tags}, commit=False,
    )
    db.session.commit()
    return jsonify({"tags": tags}), 200


# --------------------------------------------------------------------------
# POST /agents/bulk — action groupée sur plusieurs postes (admin)
# --------------------------------------------------------------------------
@bp.post("/agents/bulk")
@admin_required
def bulk_action():
    """Exécute une commande ou une action rapide sur PLUSIEURS postes.

    Body : ``{agent_ids:[...], kind:"command"|"quick", ...}`` où
    - kind="command" → ``shell`` ('powershell'|'cmd'), ``command_text``, ``timeout_seconds`` ;
    - kind="quick"   → ``action`` ('lock'|'restart'|'logoff'|'message'), ``text`` (si message).
    Crée une commande ``pending`` par poste, audite ``command.bulk`` une fois, et
    renvoie 201 ``{count, results:[{agent_id, command_id|error}]}``.
    """
    data = request.get_json(silent=True) or {}
    agent_ids = data.get("agent_ids")
    kind = (data.get("kind") or "").strip().lower()

    if not isinstance(agent_ids, list) or not agent_ids:
        return jsonify({"error": "agent_ids requis (liste non vide)"}), 400
    if len(agent_ids) > 200:
        return jsonify({"error": "trop de postes (max 200)"}), 400
    if kind not in ("command", "quick"):
        return jsonify({"error": "kind invalide (command|quick)"}), 400

    if kind == "command":
        shell = (data.get("shell") or "").strip().lower()
        command_text = data.get("command_text") or ""
        if shell not in ("powershell", "cmd"):
            return jsonify({"error": "shell invalide (powershell ou cmd)"}), 400
        if not command_text.strip():
            return jsonify({"error": "command_text requis"}), 400
        try:
            timeout = int(data.get("timeout_seconds", 120))
            if timeout <= 0 or timeout > 3600:
                timeout = 120
        except (TypeError, ValueError):
            timeout = 120
    else:  # quick
        action = (data.get("action") or "").strip().lower()
        if action not in ("lock", "restart", "logoff", "message"):
            return jsonify({"error": "action invalide (lock, restart, logoff, message)"}), 400
        if action == "message":
            text = _clean_message_text(data.get("text") or "")
            if not text:
                return jsonify({"error": "text requis pour l'action message"}), 400
            shell, command_text, timeout = "cmd", f'msg * "{text}"', 15
        else:
            shell, command_text, timeout = "cmd", _QUICK_ACTIONS[action], 30

    results = []
    created = 0
    for raw_id in agent_ids:
        aid = _parse_uuid(raw_id)
        if aid is None:
            results.append({"agent_id": str(raw_id), "error": "id invalide"})
            continue
        agent = db.session.get(Agent, aid)
        if agent is None:
            results.append({"agent_id": str(raw_id), "error": "introuvable"})
            continue
        cmd = Command(
            agent_id=aid, created_by=g.user.id, shell=shell, command_text=command_text,
            status="pending", timeout_seconds=timeout, created_at=utcnow(),
        )
        db.session.add(cmd)
        db.session.flush()
        results.append({"agent_id": str(aid), "command_id": str(cmd.id)})
        created += 1

    write_audit(
        action="command.bulk", user_id=g.user.id,
        details={"kind": kind, "count": created, "command_text": command_text},
        commit=False,
    )
    db.session.commit()
    return jsonify({"count": created, "results": results}), 201


# --------------------------------------------------------------------------
# Nom convivial d'un poste (admin)
# --------------------------------------------------------------------------
@bp.post("/agents/<agent_id>/name")
@admin_required
def set_agent_name(agent_id):
    """Définit (ou efface) le nom convivial d'un poste. Body : ``{name: "..."}``."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:80]
    agent.display_name = name or None
    write_audit(
        action="agent.rename", user_id=g.user.id, target_agent=aid,
        details={"display_name": agent.display_name}, commit=False,
    )
    db.session.commit()
    return jsonify({"display_name": agent.display_name, "name": _agent_display_name(agent)}), 200


# --------------------------------------------------------------------------
# Affectation d'un poste à un emplacement (admin)
# --------------------------------------------------------------------------
def _resolve_site_id(value):
    """Valide un site_id : None (désaffecter), un UUID de site existant, ou ('err')."""
    if value in (None, "", "none"):
        return None, None
    sid = _parse_uuid(value)
    if sid is None or db.session.get(Site, sid) is None:
        return None, "emplacement introuvable"
    return sid, None


@bp.post("/agents/<agent_id>/site")
@admin_required
def set_agent_site(agent_id):
    """Affecte un poste à un emplacement (ou le désaffecte). Body : ``{site_id}``."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    sid, err = _resolve_site_id(data.get("site_id"))
    if err:
        return jsonify({"error": err}), 400
    agent.site_id = sid
    write_audit(
        action="agent.site", user_id=g.user.id, target_agent=aid,
        details={"site_id": str(sid) if sid else None}, commit=False,
    )
    db.session.commit()
    return jsonify({"site_id": str(sid) if sid else None}), 200


@bp.post("/agents/bulk-site")
@admin_required
def bulk_set_site():
    """Affecte PLUSIEURS postes à un emplacement. Body : ``{agent_ids:[...], site_id}``."""
    data = request.get_json(silent=True) or {}
    agent_ids = data.get("agent_ids")
    if not isinstance(agent_ids, list) or not agent_ids:
        return jsonify({"error": "agent_ids requis (liste non vide)"}), 400
    if len(agent_ids) > 500:
        return jsonify({"error": "trop de postes (max 500)"}), 400
    sid, err = _resolve_site_id(data.get("site_id"))
    if err:
        return jsonify({"error": err}), 400

    count = 0
    for raw_id in agent_ids:
        aid = _parse_uuid(raw_id)
        if aid is None:
            continue
        agent = db.session.get(Agent, aid)
        if agent is None:
            continue
        agent.site_id = sid
        count += 1
    write_audit(
        action="agent.site.bulk", user_id=g.user.id,
        details={"site_id": str(sid) if sid else None, "count": count}, commit=False,
    )
    db.session.commit()
    return jsonify({"count": count, "site_id": str(sid) if sid else None}), 200


# --------------------------------------------------------------------------
# Suppression d'un poste (admin) — retire l'enregistrement du parc
# --------------------------------------------------------------------------
@bp.delete("/agents/<agent_id>")
@admin_required
def delete_agent(agent_id):
    """Supprime un poste et toutes ses données (inventaire, métriques, commandes,
    alertes, sessions). Utile après désinstallation de l'agent (poste fantôme)."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    hostname = _agent_display_name(agent)
    # Nettoyage explicite des dépendances sans relation ORM en cascade côté Agent
    # (alertes + sessions distantes), pour un comportement identique SQLite/PostgreSQL.
    db.session.query(Alert).filter_by(agent_id=aid).delete(synchronize_session=False)
    db.session.query(RemoteSession).filter_by(agent_id=aid).delete(synchronize_session=False)
    db.session.delete(agent)  # cascade ORM : matériel, logiciels, métriques, commandes
    write_audit(
        action="agent.delete", user_id=g.user.id, target_agent=aid,
        details={"hostname": hostname}, commit=False,
    )
    db.session.commit()
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# Emplacements (sites) — CRUD
# --------------------------------------------------------------------------
@bp.get("/sites")
@login_required
def list_sites():
    """Liste des emplacements avec compteurs (postes, en ligne, santé)."""
    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    sites = db.session.query(Site).order_by(Site.name.asc()).all()
    agents = db.session.query(Agent).all()
    alert_map = _active_alert_types_map()

    def _blank():
        return {"total": 0, "online": 0,
                "health": {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}}

    stats = {str(s.id): _blank() for s in sites}
    stats["none"] = _blank()
    for agent in agents:
        key = str(agent.site_id) if agent.site_id else "none"
        bucket = stats.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        if _is_online(agent, threshold):
            bucket["online"] += 1
        metric = _latest_metric(agent.id)
        h, _ = agent_health(agent, metric, alert_map.get(agent.id, set()), current_app.config)
        bucket["health"][h] += 1

    out = [
        {
            "id": str(s.id),
            "name": s.name,
            "color": s.color,
            "notes": s.notes,
            **stats[str(s.id)],
        }
        for s in sites
    ]
    # Pseudo-emplacement « non assigné » (si des postes n'ont pas de site).
    if stats["none"]["total"]:
        out.append({"id": None, "name": "Non assigné", "color": None, "notes": None, **stats["none"]})
    return jsonify(out), 200


@bp.post("/sites")
@admin_required
def create_site():
    """Crée un emplacement. Body : ``{name, color?, notes?}``."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:60]
    if not name:
        return jsonify({"error": "nom requis"}), 400
    if db.session.query(Site).filter(db.func.lower(Site.name) == name.lower()).first():
        return jsonify({"error": "un emplacement porte déjà ce nom"}), 409
    site = Site(
        name=name,
        color=(data.get("color") or "").strip()[:16] or None,
        notes=(data.get("notes") or "").strip()[:240] or None,
    )
    db.session.add(site)
    db.session.flush()
    write_audit(action="site.create", user_id=g.user.id,
                details={"site_id": str(site.id), "name": name}, commit=False)
    db.session.commit()
    return jsonify({"id": str(site.id), "name": site.name, "color": site.color}), 201


@bp.patch("/sites/<site_id>")
@admin_required
def update_site(site_id):
    """Renomme / recolore un emplacement. Body : ``{name?, color?, notes?}``."""
    sid = _parse_uuid(site_id)
    if sid is None:
        return jsonify({"error": "site_id invalide"}), 400
    site = db.session.get(Site, sid)
    if site is None:
        return jsonify({"error": "emplacement introuvable"}), 404

    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = (data.get("name") or "").strip()[:60]
        if not name:
            return jsonify({"error": "nom requis"}), 400
        clash = (
            db.session.query(Site)
            .filter(db.func.lower(Site.name) == name.lower(), Site.id != sid)
            .first()
        )
        if clash:
            return jsonify({"error": "un emplacement porte déjà ce nom"}), 409
        site.name = name
    if "color" in data:
        site.color = (data.get("color") or "").strip()[:16] or None
    if "notes" in data:
        site.notes = (data.get("notes") or "").strip()[:240] or None

    write_audit(action="site.update", user_id=g.user.id,
                details={"site_id": str(sid), "name": site.name}, commit=False)
    db.session.commit()
    return jsonify({"id": str(site.id), "name": site.name, "color": site.color}), 200


@bp.delete("/sites/<site_id>")
@admin_required
def delete_site(site_id):
    """Supprime un emplacement (les postes associés deviennent « non assignés »)."""
    sid = _parse_uuid(site_id)
    if sid is None:
        return jsonify({"error": "site_id invalide"}), 400
    site = db.session.get(Site, sid)
    if site is None:
        return jsonify({"error": "emplacement introuvable"}), 404

    name = site.name
    db.session.query(Agent).filter_by(site_id=sid).update(
        {"site_id": None}, synchronize_session=False
    )
    db.session.delete(site)
    write_audit(action="site.delete", user_id=g.user.id,
                details={"site_id": str(sid), "name": name}, commit=False)
    db.session.commit()
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# GET /overview — KPI du parc (page d'accueil)
# --------------------------------------------------------------------------
@bp.get("/overview")
@login_required
def overview():
    """Vue d'ensemble : santé du parc, problèmes par catégorie, répartition par site."""
    from .releases import current_release_available, version_gt

    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    agents = db.session.query(Agent).all()
    sites = _sites_map()
    alert_map = _active_alert_types_map()
    sec_map = _security_map()

    health_counts = {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}
    online = 0
    updates_pending = 0   # postes avec des MAJ Windows en attente
    defender_off = 0      # postes dont l'antivirus est désactivé

    def _blank_site(name, color):
        return {"name": name, "color": color, "total": 0, "online": 0,
                "health": {"healthy": 0, "warning": 0, "critical": 0, "unknown": 0}}

    site_stats = {str(s.id): _blank_site(s.name, s.color) for s in sites.values()}
    site_stats["none"] = _blank_site("Non assigné", None)

    rel = current_release_available()
    updates_available = 0

    for agent in agents:
        metric = _latest_metric(agent.id)
        sec = sec_map.get(agent.id)
        health, _reasons = agent_health(
            agent, metric, alert_map.get(agent.id, set()), current_app.config, _sec_dict(sec)
        )
        health_counts[health] += 1
        is_on = _is_online(agent, threshold)
        if is_on:
            online += 1
        if rel and version_gt(rel.version, agent.agent_version):
            updates_available += 1
        if sec is not None:
            wu = sec.windows_update or {}
            if isinstance(wu.get("pending_count"), int) and wu["pending_count"] > 0:
                updates_pending += 1
            if (sec.defender or {}).get("enabled") is False:
                defender_off += 1

        key = str(agent.site_id) if agent.site_id else "none"
        bucket = site_stats.get(key)
        if bucket is not None:
            bucket["total"] += 1
            if is_on:
                bucket["online"] += 1
            bucket["health"][health] += 1

    total = len(agents)
    offline = total - online

    # Problèmes par catégorie : alertes actives groupées par type + hors-ligne.
    prob_rows = (
        db.session.query(AlertRule.type, db.func.count(Alert.id))
        .join(Alert, Alert.rule_id == AlertRule.id)
        .filter(Alert.resolved_at.is_(None))
        .group_by(AlertRule.type)
        .all()
    )
    problems = []
    for atype, count in prob_rows:
        if atype == "offline":
            continue  # géré par la présence live ci-dessous
        problems.append({
            "type": atype,
            "label": PROBLEM_LABELS.get(atype, atype),
            "count": int(count or 0),
        })
    problems.append({"type": "offline", "label": PROBLEM_LABELS["offline"], "count": offline})
    problems.append({"type": "updates", "label": "MAJ Windows en attente", "count": updates_pending})
    problems.append({"type": "defender", "label": "Antivirus désactivé", "count": defender_off})
    problems.sort(key=lambda p: p["count"], reverse=True)

    active_alerts = (
        db.session.query(Alert).filter(Alert.resolved_at.is_(None)).count()
    )

    # Liste des emplacements (incluant « non assigné » seulement s'il a des postes).
    sites_out = []
    for s in sorted(sites.values(), key=lambda x: x.name.lower()):
        st = site_stats[str(s.id)]
        sites_out.append({"id": str(s.id), **st})
    if site_stats["none"]["total"]:
        sites_out.append({"id": None, **site_stats["none"]})

    healthy_pct = round((health_counts["healthy"] / total) * 100, 1) if total else None

    return jsonify({
        "total": total,
        "online": online,
        "offline": offline,
        "healthy_pct": healthy_pct,
        "health": health_counts,
        "active_alerts": active_alerts,
        "updates_available": updates_available,
        "current_agent_version": rel.version if rel else None,
        "problems": problems,
        "sites": sites_out,
    }), 200


# --------------------------------------------------------------------------
# GET /scripts — bibliothèque de scripts prêts à l'emploi (admin)
# --------------------------------------------------------------------------
@bp.get("/scripts")
@admin_required
def list_scripts():
    """Catalogue des scripts 1-clic (exécutés via le pipeline de commandes)."""
    from .scripts_catalog import public_catalog

    return jsonify(public_catalog()), 200


# --------------------------------------------------------------------------
# Déploiement logiciel — installation / désinstallation silencieuse (admin)
#
# Pas de code agent dédié : on matérialise une commande PowerShell construite de
# façon sûre (cf. software_catalog) via le pipeline ``Command`` existant. Le
# résultat se lit comme une commande normale (GET /api/v1/commands/<id>).
# --------------------------------------------------------------------------
def _resolve_install_spec(data):
    """À partir du corps de requête, renvoie ``(spec, None)`` ou ``(None, msg)``.

    ``spec`` = ``{shell, command_text, timeout, target}``.
    Sources : ``catalog`` (clé du catalogue), ``winget`` (ID libre), ``url`` (MSI/EXE HTTPS).
    """
    from . import software_catalog as sc

    source = (data.get("source") or "").strip().lower()
    if source == "catalog":
        wid = sc.catalog_winget_id((data.get("key") or "").strip())
        if not wid:
            return None, "application inconnue dans le catalogue"
        shell, text, timeout = sc.build_winget_install(wid)
        return {"shell": shell, "command_text": text, "timeout": timeout, "target": wid}, None
    if source == "winget":
        wid = (data.get("winget_id") or "").strip()
        if not sc.valid_winget_id(wid):
            return None, "winget_id invalide"
        shell, text, timeout = sc.build_winget_install(wid)
        return {"shell": shell, "command_text": text, "timeout": timeout, "target": wid}, None
    if source == "url":
        url = (data.get("url") or "").strip()
        if not sc.valid_url(url):
            return None, "url invalide (HTTPS et extension .msi/.exe requis)"
        shell, text, timeout = sc.build_url_install(url, data.get("exe_args"))
        return {"shell": shell, "command_text": text, "timeout": timeout, "target": url}, None
    return None, "source invalide (catalog | winget | url)"


def _resolve_uninstall_spec(data):
    """Renvoie ``(spec, None)`` ou ``(None, msg)`` pour une désinstallation.

    Sources : ``registry`` (par nom affiché — défaut, correspond à l'inventaire)
    ou ``winget`` (par ID ou par nom).
    """
    from . import software_catalog as sc

    source = (data.get("source") or "registry").strip().lower()
    if source == "winget":
        wid = (data.get("winget_id") or "").strip()
        if wid:
            if not sc.valid_winget_id(wid):
                return None, "winget_id invalide"
            shell, text, timeout = sc.build_winget_uninstall(winget_id=wid)
            return {"shell": shell, "command_text": text, "timeout": timeout, "target": wid}, None
        name = sc.clean_name(data.get("name"))
        if name:
            shell, text, timeout = sc.build_winget_uninstall(name=name)
            return {"shell": shell, "command_text": text, "timeout": timeout, "target": name}, None
        return None, "winget_id ou name requis"
    if source == "registry":
        name = sc.clean_name(data.get("name"))
        if not name:
            return None, "name requis"
        shell, text, timeout = sc.build_registry_uninstall(name)
        return {"shell": shell, "command_text": text, "timeout": timeout, "target": name}, None
    return None, "source invalide (registry | winget)"


def _queue_software_command(aid, spec, audit_action, source):
    """Crée la commande ``pending`` + l'audit (sans commit). Renvoie l'id."""
    cmd = Command(
        agent_id=aid,
        created_by=g.user.id,
        shell=spec["shell"],
        command_text=spec["command_text"],
        status="pending",
        timeout_seconds=spec["timeout"],
        created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()
    write_audit(
        action=audit_action,
        user_id=g.user.id,
        target_agent=aid,
        details={"command_id": str(cmd.id), "source": source, "target": spec["target"]},
        commit=False,
    )
    return cmd.id


@bp.get("/software/catalog")
@admin_required
def software_catalog_list():
    """Catalogue d'applications installables en 1 clic."""
    from . import software_catalog as sc

    return jsonify(sc.public_catalog()), 200


@bp.post("/agents/<agent_id>/software/install")
@admin_required
def software_install(agent_id):
    """Installe silencieusement une application sur un poste (admin only)."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    spec, err = _resolve_install_spec(data)
    if err:
        return jsonify({"error": err}), 400

    cmd_id = _queue_software_command(aid, spec, "software.install", (data.get("source") or "").strip().lower())
    db.session.commit()
    return jsonify({"command_id": str(cmd_id)}), 201


@bp.post("/agents/<agent_id>/software/uninstall")
@admin_required
def software_uninstall(agent_id):
    """Désinstalle silencieusement une application sur un poste (admin only)."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    agent = db.session.get(Agent, aid)
    if agent is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    spec, err = _resolve_uninstall_spec(data)
    if err:
        return jsonify({"error": err}), 400

    cmd_id = _queue_software_command(aid, spec, "software.uninstall", (data.get("source") or "registry").strip().lower())
    db.session.commit()
    return jsonify({"command_id": str(cmd_id)}), 201


@bp.post("/software/bulk-install")
@admin_required
def software_bulk_install():
    """Installe une application sur PLUSIEURS postes. Body : ``{agent_ids:[...], source, ...}``."""
    return _software_bulk(_resolve_install_spec, "software.bulk-install", "")


@bp.post("/software/bulk-uninstall")
@admin_required
def software_bulk_uninstall():
    """Désinstalle une application sur PLUSIEURS postes. Body : ``{agent_ids:[...], source, ...}``."""
    return _software_bulk(_resolve_uninstall_spec, "software.bulk-uninstall", "registry")


def _software_bulk(resolver, audit_action, default_source):
    data = request.get_json(silent=True) or {}
    agent_ids = data.get("agent_ids")
    if not isinstance(agent_ids, list) or not agent_ids:
        return jsonify({"error": "agent_ids requis (liste non vide)"}), 400
    if len(agent_ids) > 200:
        return jsonify({"error": "trop de postes (max 200)"}), 400

    spec, err = resolver(data)
    if err:
        return jsonify({"error": err}), 400

    source = (data.get("source") or default_source).strip().lower()
    results = []
    created = 0
    for raw_id in agent_ids:
        aid = _parse_uuid(raw_id)
        if aid is None:
            results.append({"agent_id": str(raw_id), "error": "id invalide"})
            continue
        if db.session.get(Agent, aid) is None:
            results.append({"agent_id": str(raw_id), "error": "introuvable"})
            continue
        cmd_id = _queue_software_command(aid, spec, audit_action, source)
        results.append({"agent_id": str(aid), "command_id": str(cmd_id)})
        created += 1

    db.session.commit()
    return jsonify({"count": created, "results": results}), 201


# --------------------------------------------------------------------------
# Gestion des correctifs Windows (admin) — état / installation / rescan
#
# Pas de code agent dédié pour l'installation : on matérialise une commande
# PowerShell COM (cf. patch_catalog) via le pipeline ``Command`` existant. Un
# ``PatchJob`` relie la campagne à la commande ; son statut est DÉRIVÉ du
# résultat (exit_code 3010 = redémarrage requis). On ne redémarre JAMAIS le
# poste automatiquement (un utilisateur peut être en session).
# --------------------------------------------------------------------------
def _patch_job_payload(job: PatchJob) -> dict:
    """Sérialise un PatchJob avec son statut DÉRIVÉ de la commande liée.

    Statuts : ceux de la commande (pending|dispatched|done|error|timeout), plus
    ``reboot_pending`` quand ``exit_code == 3010`` et ``expired`` si la commande
    a été purgée (command_id devenu nul).
    """
    status = "pending"
    exit_code = None
    cmd = db.session.get(Command, job.command_id) if job.command_id else None
    if job.command_id is not None and cmd is None:
        status = "expired"
    elif cmd is not None:
        status = cmd.status
        res = db.session.get(CommandResult, cmd.id)
        if res is not None:
            exit_code = res.exit_code
            if exit_code == 3010:
                status = "reboot_pending"
            elif cmd.status == "error" and exit_code == 0:
                status = "done"
    return {
        "id": str(job.id),
        "mode": job.mode,
        "kb_list": job.kb_list or [],
        "command_id": str(job.command_id) if job.command_id else None,
        "status": status,
        "exit_code": exit_code,
        "created_at": _iso_utc(job.created_at),
    }


def _queue_patch_command(aid, spec, mode, kb_list):
    """Crée la commande ``pending`` + le ``PatchJob`` lié (sans commit). Renvoie
    ``(command_id, patch_job_id)``."""
    shell, command_text, timeout = spec
    cmd = Command(
        agent_id=aid,
        created_by=g.user.id,
        shell=shell,
        command_text=command_text,
        status="pending",
        timeout_seconds=timeout,
        created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()  # obtient l'id de la commande
    job = PatchJob(
        agent_id=aid,
        command_id=cmd.id,
        created_by=g.user.id,
        mode=mode,
        kb_list=(kb_list if mode == "selected" else None),
        created_at=utcnow(),
    )
    db.session.add(job)
    db.session.flush()
    return cmd.id, job.id


@bp.get("/agents/<agent_id>/patch")
@login_required
def get_agent_patch(agent_id):
    """État des correctifs d'un poste : MAJ en attente (liste enrichie) + dernières
    campagnes. Rétro-compatible avec les agents qui n'envoient que les compteurs."""
    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    if db.session.get(Agent, aid) is None:
        return jsonify({"error": "agent introuvable"}), 404

    sec = db.session.get(AgentSecurity, aid)
    wu = (sec.windows_update if sec else None) or {}
    updates = wu.get("updates") if isinstance(wu.get("updates"), list) else []

    jobs = (
        db.session.query(PatchJob)
        .filter_by(agent_id=aid)
        .order_by(PatchJob.created_at.desc())
        .limit(10)
        .all()
    )
    job_payloads = [_patch_job_payload(j) for j in jobs]
    reboot_pending = bool(job_payloads) and job_payloads[0]["status"] == "reboot_pending"

    return jsonify({
        "pending_count": wu.get("pending_count"),
        "pending_critical": wu.get("pending_critical"),
        "last_search_at": wu.get("last_search_at"),
        "collected_at": _iso_utc(sec.collected_at) if sec else None,
        "updates": updates,
        "jobs": job_payloads,
        "reboot_pending": reboot_pending,
    }), 200


@bp.post("/agents/<agent_id>/patch/install")
@admin_required
def patch_install(agent_id):
    """Installe les correctifs Windows d'un poste (admin only).

    Body : ``{"mode": "critical"|"all"|"selected", "kb_list": [...]}``.
    """
    from . import patch_catalog as pc

    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    if db.session.get(Agent, aid) is None:
        return jsonify({"error": "agent introuvable"}), 404

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "").strip().lower()
    kb_list = data.get("kb_list")
    try:
        spec = pc.build_install(mode, kb_list)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    cmd_id, job_id = _queue_patch_command(aid, spec, mode, kb_list)
    write_audit(
        action="patch.install",
        user_id=g.user.id,
        target_agent=aid,
        details={
            "command_id": str(cmd_id),
            "patch_job_id": str(job_id),
            "mode": mode,
            "kb_list": kb_list if mode == "selected" else None,
        },
        commit=False,
    )
    db.session.commit()
    return jsonify({"command_id": str(cmd_id), "patch_job_id": str(job_id)}), 201


@bp.post("/agents/<agent_id>/patch/rescan")
@admin_required
def patch_rescan(agent_id):
    """Relance une recherche des correctifs en attente (read-only) sur un poste.

    Le résultat (tableau lisible) s'affiche comme une commande normale ; la
    collecte de fond (~12 h) reste la source faisant autorité côté serveur.
    """
    from . import patch_catalog as pc

    aid = _parse_uuid(agent_id)
    if aid is None:
        return jsonify({"error": "agent_id invalide"}), 400
    if db.session.get(Agent, aid) is None:
        return jsonify({"error": "agent introuvable"}), 404

    shell, command_text, timeout = pc.build_rescan()
    cmd = Command(
        agent_id=aid, created_by=g.user.id, shell=shell, command_text=command_text,
        status="pending", timeout_seconds=timeout, created_at=utcnow(),
    )
    db.session.add(cmd)
    db.session.flush()
    write_audit(
        action="patch.rescan", user_id=g.user.id, target_agent=aid,
        details={"command_id": str(cmd.id)}, commit=False,
    )
    db.session.commit()
    return jsonify({"command_id": str(cmd.id)}), 201


def _resolve_patch_targets(data):
    """Résout les postes cibles d'un déploiement groupé.

    Accepte ``{agent_ids:[...]}`` OU ``{site:<uuid>}`` OU ``{tag:<str>}``.
    Renvoie une liste d'UUID, ou ``(None, message)`` en cas d'erreur.
    """
    raw_ids = data.get("agent_ids")
    if isinstance(raw_ids, list) and raw_ids:
        ids = []
        for raw in raw_ids:
            uid = _parse_uuid(raw)
            if uid is not None:
                ids.append(uid)
        return ids

    site = (data.get("site") or "").strip()
    if site:
        sid = _parse_uuid(site)
        if sid is None:
            return None, "site invalide"
        agents = db.session.query(Agent).filter(
            Agent.is_active.is_(True), Agent.site_id == sid
        ).all()
        return [a.id for a in agents]

    tag = (data.get("tag") or "").strip()
    if tag:
        # Filtrage en Python (portable PostgreSQL/SQLite ; le parc est modeste).
        agents = db.session.query(Agent).filter(Agent.is_active.is_(True)).all()
        return [a.id for a in agents if tag in (a.tags or [])]

    return None, "agent_ids, site ou tag requis"


@bp.post("/patch/bulk-install")
@admin_required
def patch_bulk_install():
    """Installe des correctifs sur PLUSIEURS postes (groupe site/tag ou liste).

    Body : ``{"mode": ..., "kb_list": [...], "agent_ids"|"site"|"tag": ...}``.
    Crée une commande + un PatchJob par poste, audite ``patch.bulk-install`` une
    fois. Plafond 200 postes.
    """
    from . import patch_catalog as pc

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "").strip().lower()
    kb_list = data.get("kb_list")
    try:
        spec = pc.build_install(mode, kb_list)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    targets = _resolve_patch_targets(data)
    if isinstance(targets, tuple):  # (None, message)
        return jsonify({"error": targets[1]}), 400
    if not targets:
        return jsonify({"error": "aucun poste cible"}), 400
    if len(targets) > 200:
        return jsonify({"error": "trop de postes (max 200)"}), 400

    results = []
    created = 0
    for aid in targets:
        if db.session.get(Agent, aid) is None:
            results.append({"agent_id": str(aid), "error": "introuvable"})
            continue
        cmd_id, _job_id = _queue_patch_command(aid, spec, mode, kb_list)
        results.append({"agent_id": str(aid), "command_id": str(cmd_id)})
        created += 1

    write_audit(
        action="patch.bulk-install", user_id=g.user.id,
        details={
            "mode": mode,
            "count": created,
            "kb_list": kb_list if mode == "selected" else None,
        },
        commit=False,
    )
    db.session.commit()
    return jsonify({"count": created, "results": results}), 201


# --------------------------------------------------------------------------
# Comptes utilisateurs locaux Windows (admin) — list / create / delete
#
# Mêmes garanties que le déploiement logiciel : on matérialise une commande
# PowerShell construite de façon sûre (cf. account_ops) via le pipeline Command.
# Le mot de passe de création N'EST JAMAIS journalisé dans l'audit.
# --------------------------------------------------------------------------
def _account_agent_or_error(agent_id):
    aid = _parse_uuid(agent_id)
    if aid is None:
        return None, (jsonify({"error": "agent_id invalide"}), 400)
    if db.session.get(Agent, aid) is None:
        return None, (jsonify({"error": "agent introuvable"}), 404)
    return aid, None


def _queue_account_command(aid, shell, command_text, timeout, audit_action, audit_details, redact=False):
    cmd = Command(
        agent_id=aid, created_by=g.user.id, shell=shell, command_text=command_text,
        status="pending", timeout_seconds=timeout, created_at=utcnow(),
        redact_after_run=redact,
    )
    db.session.add(cmd)
    db.session.flush()
    details = {"command_id": str(cmd.id)}
    details.update(audit_details or {})
    write_audit(action=audit_action, user_id=g.user.id, target_agent=aid, details=details, commit=False)
    db.session.commit()
    return cmd.id


@bp.post("/agents/<agent_id>/accounts/list")
@admin_required
def accounts_list(agent_id):
    """Liste les comptes locaux du poste (queue une commande, renvoie command_id)."""
    aid, err = _account_agent_or_error(agent_id)
    if err:
        return err
    from . import account_ops

    shell, text, timeout = account_ops.build_list()
    cmd_id = _queue_account_command(aid, shell, text, timeout, "account.list", {})
    return jsonify({"command_id": str(cmd_id)}), 201


@bp.post("/agents/<agent_id>/accounts/create")
@admin_required
def accounts_create(agent_id):
    """Crée un compte local. Body : ``{username, password, full_name?, administrator?}``."""
    aid, err = _account_agent_or_error(agent_id)
    if err:
        return err
    from . import account_ops

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    full_name = account_ops.clean_text(data.get("full_name"))
    administrator = bool(data.get("administrator"))
    if not account_ops.valid_username(username):
        return jsonify({"error": "nom d'utilisateur invalide (lettres, chiffres, . _ - ; 20 max)"}), 400
    if not isinstance(password, str) or len(password) < 4:
        return jsonify({"error": "mot de passe requis (4 caractères minimum)"}), 400

    shell, text, timeout = account_ops.build_create(username, password, full_name, administrator)
    # AUDIT : on journalise le nom et le rôle, JAMAIS le mot de passe.
    # redact=True : le command_text (qui contient le mot de passe) est purgé en base
    # une fois la commande exécutée (cf. post_result).
    cmd_id = _queue_account_command(
        aid, shell, text, timeout, "account.create",
        {"username": username, "administrator": administrator, "full_name": full_name},
        redact=True,
    )
    return jsonify({"command_id": str(cmd_id)}), 201


@bp.post("/agents/<agent_id>/accounts/delete")
@admin_required
def accounts_delete(agent_id):
    """Supprime un compte local. Body : ``{username, remove_profile?}``."""
    aid, err = _account_agent_or_error(agent_id)
    if err:
        return err
    from . import account_ops

    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    remove_profile = bool(data.get("remove_profile"))
    if not account_ops.valid_username(username):
        return jsonify({"error": "nom d'utilisateur invalide"}), 400

    shell, text, timeout = account_ops.build_delete(username, remove_profile)
    cmd_id = _queue_account_command(
        aid, shell, text, timeout, "account.delete",
        {"username": username, "remove_profile": remove_profile},
    )
    return jsonify({"command_id": str(cmd_id)}), 201


# --------------------------------------------------------------------------
# GET /alerts?status=active|all — liste des alertes du parc
# --------------------------------------------------------------------------
@bp.get("/alerts")
@login_required
def list_alerts():
    """Liste des alertes, triées par déclenchement décroissant (max 300).

    Le paramètre ``status`` vaut ``active`` (défaut, alertes non résolues) ou
    ``all`` (toutes). Le type et le seuil proviennent de la règle (``alert_rules``),
    le hostname de l'agent (``agents``). Le champ ``active`` est dérivé :
    ``resolved_at is null``.
    """
    status = (request.args.get("status") or "active").strip().lower()
    if status not in ("active", "all"):
        status = "active"

    query = (
        db.session.query(Alert, Agent, AlertRule)
        .outerjoin(Agent, Alert.agent_id == Agent.id)
        .outerjoin(AlertRule, Alert.rule_id == AlertRule.id)
    )
    if status == "active":
        query = query.filter(Alert.resolved_at.is_(None))

    rows = query.order_by(Alert.triggered_at.desc()).limit(300).all()

    out = [
        {
            "id": alert.id,
            "agent_id": str(alert.agent_id) if alert.agent_id else None,
            "hostname": agent.hostname if agent else None,
            "type": rule.type if rule else None,
            "threshold": _num(rule.threshold) if rule else None,
            "triggered_at": _iso_utc(alert.triggered_at),
            "resolved_at": _iso_utc(alert.resolved_at),
            "notified": bool(alert.notified),
            "active": alert.resolved_at is None,
        }
        for alert, agent, rule in rows
    ]
    return jsonify(out), 200


# --------------------------------------------------------------------------
# GET /inventory/software?q= — inventaire logiciel agrégé du parc
# --------------------------------------------------------------------------
@bp.get("/inventory/software")
@login_required
def inventory_software():
    """Inventaire logiciel agrégé du parc (max 500 lignes).

    Regroupe par (name, version, publisher) distincts et compte le nombre
    d'agents distincts portant chaque logiciel. Le filtre ``q`` (insensible à la
    casse) s'applique au nom OU à l'éditeur. Tri par name puis version.
    """
    q = (request.args.get("q") or "").strip()

    agent_count = db.func.count(db.func.distinct(SoftwareInventory.agent_id))
    query = db.session.query(
        SoftwareInventory.name,
        SoftwareInventory.version,
        SoftwareInventory.publisher,
        agent_count.label("agent_count"),
    )

    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            db.or_(
                db.func.lower(SoftwareInventory.name).like(like),
                db.func.lower(SoftwareInventory.publisher).like(like),
            )
        )

    rows = (
        query.group_by(
            SoftwareInventory.name,
            SoftwareInventory.version,
            SoftwareInventory.publisher,
        )
        .order_by(SoftwareInventory.name.asc(), SoftwareInventory.version.asc())
        .limit(500)
        .all()
    )

    out = [
        {
            "name": r.name,
            "version": r.version,
            "publisher": r.publisher,
            "agent_count": int(r.agent_count or 0),
        }
        for r in rows
    ]
    return jsonify(out), 200


# --------------------------------------------------------------------------
# Préférences UI de l'utilisateur courant — ordre des onglets de la fiche poste
# --------------------------------------------------------------------------
# Catalogue canonique des onglets de la zone de travail (clé + libellé). Source
# de vérité partagée : l'UI Réglages l'affiche, la fiche poste réordonne selon
# l'ordre enregistré, et le POST valide contre l'ensemble des clés connues.
WORKZONE_TABS = [
    {"key": "remote", "label": "Bureau à distance"},
    {"key": "terminal", "label": "Terminal"},
    {"key": "command", "label": "Commande ponctuelle"},
    {"key": "processes", "label": "Processus"},
    {"key": "activity", "label": "Activité"},
    {"key": "accounts", "label": "Comptes"},
    {"key": "patches", "label": "Correctifs"},
    {"key": "hardware", "label": "Matériel"},
    {"key": "copilot", "label": "Copilote"},
]
_WORKZONE_TAB_KEYS = {t["key"] for t in WORKZONE_TABS}


def user_tab_order(user) -> list:
    """Ordre des onglets enregistré par l'utilisateur (liste de clés valides, sans
    doublon), ou [] si aucun. Tolérant aux préférences NULL/anciennes."""
    prefs = getattr(user, "preferences", None) or {}
    raw = prefs.get("tab_order") if isinstance(prefs, dict) else None
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for key in raw:
        if key in _WORKZONE_TAB_KEYS and key not in seen:
            seen.add(key)
            out.append(key)
    return out


@bp.get("/settings/preferences")
@login_required
def get_settings_preferences():
    """Préférences UI + catalogue des onglets (pour l'écran Réglages)."""
    return jsonify({
        "tabs": WORKZONE_TABS,
        "tab_order": user_tab_order(g.user),
    }), 200


@bp.post("/settings/tab-order")
@login_required
def set_settings_tab_order():
    """Enregistre l'ordre des onglets de la fiche poste pour l'utilisateur courant.

    Body : ``{"order": ["remote", "hardware", ...]}`` — les clés inconnues sont
    ignorées, les doublons supprimés. Une liste vide réinitialise (ordre par défaut).
    """
    data = request.get_json(silent=True) or {}
    raw = data.get("order")
    if raw is not None and not isinstance(raw, list):
        return jsonify({"error": "order doit être une liste"}), 400

    order = []
    seen = set()
    for key in (raw or []):
        if key in _WORKZONE_TAB_KEYS and key not in seen:
            seen.add(key)
            order.append(key)

    # Fusion non destructive avec les autres préférences éventuelles.
    prefs = dict(g.user.preferences or {})
    prefs["tab_order"] = order
    g.user.preferences = prefs
    # SQLAlchemy ne détecte pas toujours la mutation d'un JSON en place : on
    # réaffecte l'attribut (ci-dessus) ET on marque l'objet modifié par sécurité.
    db.session.add(g.user)

    write_audit(
        action="settings.tab_order",
        user_id=g.user.id,
        details={"order": order},
        commit=False,
    )
    db.session.commit()
    return jsonify({"tab_order": order}), 200


# --------------------------------------------------------------------------
# Réglages de l'utilisateur courant — mot de passe & MFA
# --------------------------------------------------------------------------
@bp.post("/settings/password")
@login_required
def settings_password():
    """Change le mot de passe de l'utilisateur courant.

    Body : ``{current_password, new_password}``. Vérifie l'ancien mot de passe,
    impose un nouveau d'au moins 8 caractères, met à jour le hash et audite
    ``settings.password``. 200 ``{ok}`` / 400 si trop court / 401 si ancien faux.
    """
    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    user = g.user
    if not verify_password(user.password_hash, current_password):
        return jsonify({"error": "mot de passe actuel incorrect"}), 401
    if len(new_password) < 8:
        return jsonify({"error": "le nouveau mot de passe doit faire au moins 8 caractères"}), 400

    user.password_hash = hash_password(new_password)
    write_audit(action="settings.password", user_id=user.id, details={}, commit=False)
    db.session.commit()
    return jsonify({"ok": True}), 200


@bp.get("/settings/mfa")
@login_required
def settings_mfa_status():
    """Indique si le MFA est activé pour l'utilisateur courant."""
    return jsonify({"enabled": bool(g.user.mfa_enabled)}), 200


@bp.post("/settings/mfa/setup")
@login_required
def settings_mfa_setup():
    """Génère un secret TOTP en attente et renvoie l'URI otpauth + QR code PNG.

    Le secret est stocké EN ATTENTE dans la session Flask
    (``session['pending_mfa_secret']``) ; il n'est confirmé qu'au passage par
    ``/settings/mfa/enable``. Renvoie ``{secret, otpauth_uri, qr_png_base64}`` où
    ``qr_png_base64`` est un data-URI ``data:image/png;base64,...``.
    """
    user = g.user
    secret = pyotp.random_base32()
    session["pending_mfa_secret"] = secret

    otpauth_uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name="TrueSight"
    )

    img = qrcode.make(otpauth_uri)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    qr_png_base64 = f"data:image/png;base64,{encoded}"

    return (
        jsonify(
            {
                "secret": secret,
                "otpauth_uri": otpauth_uri,
                "qr_png_base64": qr_png_base64,
            }
        ),
        200,
    )


@bp.post("/settings/mfa/enable")
@login_required
def settings_mfa_enable():
    """Active le MFA après vérification du code TOTP contre le secret en attente.

    Body : ``{code}``. Vérifie le code contre ``session['pending_mfa_secret']``
    (fenêtre ±1). Si OK : persiste le secret, active le MFA, purge la session en
    attente et audite ``settings.mfa.enable``. 200 ``{ok}`` / 400 si code invalide
    ou aucun secret en attente.
    """
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().replace(" ", "")

    pending_secret = session.get("pending_mfa_secret")
    if not pending_secret:
        return jsonify({"error": "aucun secret MFA en attente"}), 400

    if not pyotp.TOTP(pending_secret).verify(code, valid_window=1):
        return jsonify({"error": "code MFA invalide"}), 400

    user = g.user
    user.mfa_secret = pending_secret
    user.mfa_enabled = True
    session.pop("pending_mfa_secret", None)
    write_audit(action="settings.mfa.enable", user_id=user.id, details={}, commit=False)
    db.session.commit()
    return jsonify({"ok": True}), 200


@bp.post("/settings/mfa/disable")
@login_required
def settings_mfa_disable():
    """Désactive le MFA après vérification du mot de passe.

    Body : ``{password}``. Si OK : efface le secret, désactive le MFA et audite
    ``settings.mfa.disable``. 200 ``{ok}`` / 401 si mot de passe faux.
    """
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""

    user = g.user
    if not verify_password(user.password_hash, password):
        return jsonify({"error": "mot de passe incorrect"}), 401

    user.mfa_secret = None
    user.mfa_enabled = False
    write_audit(action="settings.mfa.disable", user_id=user.id, details={}, commit=False)
    db.session.commit()
    return jsonify({"ok": True}), 200
