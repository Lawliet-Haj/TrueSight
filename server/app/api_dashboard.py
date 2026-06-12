"""Blueprint API JSON du dashboard (cf. SPEC §3).

Toutes les routes nécessitent une session authentifiée (``login_required``).
La création de commandes et le journal d'audit sont réservés aux administrateurs
(``admin_required``).
"""
import re
import uuid
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .extensions import db
from .models import (
    Agent,
    AuditLog,
    Command,
    CommandResult,
    HardwareInventory,
    Metric,
    RemoteSession,
    SoftwareInventory,
    User,
)
from .models import utcnow
from .security import (
    admin_required,
    generate_session_token,
    hash_token,
    login_required,
    store_session_token,
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
@bp.get("/agents")
@login_required
def list_agents():
    """Liste des agents avec statut online/offline calculé et dernières métriques."""
    threshold = current_app.config["OFFLINE_THRESHOLD_SECONDS"]
    agents = db.session.query(Agent).order_by(Agent.hostname.asc()).all()

    out = []
    for agent in agents:
        metric = _latest_metric(agent.id)
        out.append(
            {
                "id": str(agent.id),
                "hostname": agent.hostname,
                "os_version": agent.os_version,
                "status": "online" if _is_online(agent, threshold) else "offline",
                "last_seen_at": _iso_utc(agent.last_seen_at),
                "cpu_pct": _num(metric.cpu_pct) if metric else None,
                "ram_used_pct": _num(metric.ram_used_pct) if metric else None,
                "tags": agent.tags or [],
                "is_active": agent.is_active,
            }
        )
    return jsonify(out), 200


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

    payload = {
        "id": str(agent.id),
        "machine_id": agent.machine_id,
        "hostname": agent.hostname,
        "agent_version": agent.agent_version,
        "os_version": agent.os_version,
        "enrolled_at": _iso_utc(agent.enrolled_at),
        "last_seen_at": _iso_utc(agent.last_seen_at),
        "is_active": agent.is_active,
        "tags": agent.tags or [],
        "status": "online" if _is_online(agent, threshold) else "offline",
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
