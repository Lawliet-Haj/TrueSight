"""Blueprint API agent (cf. SPEC §2).

Tous les endpoints sont sous ``/api/v1``. Auth ``Bearer <agent_token>`` sauf
``/enroll`` (qui s'appuie sur le ``enrollment_token`` partagé dans le corps).
"""
import time
from collections import defaultdict
from datetime import date, timedelta, timezone
from threading import Lock

from flask import Blueprint, current_app, g, jsonify, request

from .extensions import db
from .models import (
    Agent,
    AgentSecurity,
    Command,
    CommandResult,
    HardwareInventory,
    Metric,
    RemoteSession,
    Site,
    SoftwareInventory,
)
from .models import utcnow
from .security import (
    agent_required,
    agent_required_by_command,
    generate_agent_token,
    hash_token,
)

# Durée de validité de l'appariement d'une session de bureau à distance.
# Au-delà, une session encore « requested » est considérée expirée (REMOTE.md §7).
REMOTE_SESSION_TTL_SECONDS = 60

bp = Blueprint("api_agent", __name__, url_prefix="/api/v1")


# --------------------------------------------------------------------------
# Rate-limiting mémoire simple sur /enroll (anti-bruteforce du token partagé)
# --------------------------------------------------------------------------
_ENROLL_WINDOW_SECONDS = 60
_ENROLL_MAX_ATTEMPTS = 20
_enroll_hits: dict[str, list[float]] = defaultdict(list)
_enroll_lock = Lock()


def _enroll_rate_limited(ip: str) -> bool:
    """Renvoie True si l'IP a dépassé le quota d'appels à /enroll sur la fenêtre glissante."""
    now = time.monotonic()
    with _enroll_lock:
        hits = _enroll_hits[ip]
        # Purge des hits hors fenêtre.
        cutoff = now - _ENROLL_WINDOW_SECONDS
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= _ENROLL_MAX_ATTEMPTS:
            return True
        hits.append(now)
        return False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _json_body() -> dict:
    """Récupère le corps JSON de la requête, ou {} si absent/invalide."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _parse_install_date(value):
    """Convertit une date ISO (YYYY-MM-DD) en objet date, tolérant aux valeurs absentes."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _to_decimal_str(value):
    """Normalise une valeur numérique pour les colonnes Numeric (ou None)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ws_base_url() -> str:
    """Construit la base WebSocket (ws:// ou wss://) depuis l'URL d'hôte de la requête.

    ``request.host_url`` tient compte du scheme/host externes grâce à ProxyFix
    (X-Forwarded-Proto / X-Forwarded-Host). On dérive ws<->http : https→wss,
    http→ws (cf. CONTRAT REMOTE).
    """
    base = request.host_url.rstrip("/")  # ex. "https://parc.medicofi.fr"
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def _remote_session_for_agent(agent: Agent, ws_path: str) -> dict | None:
    """Renvoie le champ ``remote_session`` pour un agent, ou None.

    Sélectionne la session ``requested`` la plus récente de l'agent. Si elle a
    dépassé le TTL d'appariement (~60 s), elle est marquée ``expired`` et on
    renvoie None. Sinon on renvoie {session_id, token, ws_url, kind, shell} où
    ``ws_url`` pointe vers ``ws_path`` (côté agent : ``/ws/remote/agent``).
    L'agent lit ``kind`` pour décider entre capture écran ('remote') et terminal
    interactif ('terminal'), et ``shell`` indique le shell à lancer dans ce dernier cas.

    Le jeton en clair provient du cache mémoire (la base ne stocke que le hash) ;
    s'il n'est plus disponible (ex. redémarrage worker), la session est inexploitable
    → on la marque ``expired`` et on renvoie None.
    """
    from .security import pop_session_token, forget_session_token

    sess = (
        db.session.query(RemoteSession)
        .filter(RemoteSession.agent_id == agent.id, RemoteSession.status == "requested")
        .order_by(RemoteSession.requested_at.desc())
        .first()
    )
    if sess is None:
        return None

    requested_at = sess.requested_at
    if requested_at is not None and requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=timezone.utc)

    # Expiration au-delà du TTL d'appariement.
    if requested_at is None or (utcnow() - requested_at) > timedelta(
        seconds=REMOTE_SESSION_TTL_SECONDS
    ):
        sess.status = "expired"
        db.session.commit()
        forget_session_token(sess.id)
        return None

    token = pop_session_token(sess.id)
    if not token:
        # Jeton en clair perdu (worker redémarré) : session inexploitable.
        sess.status = "expired"
        db.session.commit()
        forget_session_token(sess.id)
        return None

    return {
        "session_id": str(sess.id),
        "token": token,
        "ws_url": f"{_ws_base_url()}{ws_path}?token={token}",
        "kind": sess.kind,
        "shell": sess.shell,
    }


# --------------------------------------------------------------------------
# 2.1 POST /enroll
# --------------------------------------------------------------------------
@bp.post("/enroll")
def enroll():
    """Enrôlement d'un poste. Idempotent sur ``machine_id`` (rotation du token si déjà connu)."""
    ip = request.remote_addr or "unknown"
    if _enroll_rate_limited(ip):
        return jsonify({"error": "trop de tentatives d'enrôlement"}), 429

    data = _json_body()
    enrollment_token = data.get("enrollment_token")
    machine_id = data.get("machine_id")

    expected = current_app.config["ENROLLMENT_TOKEN"]
    if not enrollment_token or enrollment_token != expected:
        return jsonify({"error": "token d'enrôlement invalide"}), 401

    if not machine_id or not isinstance(machine_id, str):
        return jsonify({"error": "machine_id requis"}), 400

    hostname = data.get("hostname")
    os_version = data.get("os_version")
    agent_version = data.get("agent_version")

    # Génère un nouveau token à chaque enrôlement (rotation).
    token = generate_agent_token()
    token_h = hash_token(token)

    agent = db.session.query(Agent).filter_by(machine_id=machine_id).one_or_none()
    if agent is None:
        agent = Agent(
            machine_id=machine_id,
            hostname=hostname,
            os_version=os_version,
            agent_version=agent_version,
            token_hash=token_h,
            is_active=True,
            tags=[],
        )
        db.session.add(agent)
    else:
        # Révocation effective (SPEC §5) : un agent désactivé NE se réactive PAS
        # tout seul en se réenrôlant. Seule une réactivation admin explicite le
        # remet en service. On refuse l'enrôlement d'un poste révoqué.
        if not agent.is_active:
            return jsonify({"error": "agent révoqué"}), 403
        # Réenrôlement légitime : rotation du token + MAJ des métadonnées.
        agent.hostname = hostname or agent.hostname
        agent.os_version = os_version or agent.os_version
        agent.agent_version = agent_version or agent.agent_version
        agent.token_hash = token_h

    # Emplacement (installeur par site) : le poste indique son site via config.ini.
    # On l'affecte UNIQUEMENT s'il n'a pas déjà d'emplacement (ne pas écraser une
    # affectation manuelle de l'admin). Le site est créé s'il n'existe pas encore.
    _assign_enroll_site(agent, data.get("site"))

    db.session.commit()

    return jsonify({"agent_id": str(agent.id), "agent_token": token}), 200


def _assign_enroll_site(agent: Agent, raw_site) -> None:
    """Affecte l'agent à un emplacement nommé (find-or-create), si pertinent."""
    if not raw_site or not isinstance(raw_site, str):
        return
    name = raw_site.strip()[:60]
    if not name or agent.site_id is not None:
        return
    site = (
        db.session.query(Site)
        .filter(db.func.lower(Site.name) == name.lower())
        .one_or_none()
    )
    if site is None:
        site = Site(name=name)
        db.session.add(site)
        db.session.flush()
    agent.site_id = site.id


# --------------------------------------------------------------------------
# 2.2 POST /agents/{agent_id}/heartbeat
# --------------------------------------------------------------------------
@bp.post("/agents/<agent_id>/heartbeat")
@agent_required
def heartbeat(agent_id):
    """Ping + insertion d'un point de métriques. Renvoie les commandes en attente + la config."""
    agent: Agent = g.agent
    data = _json_body()
    metrics = data.get("metrics") or {}

    agent.last_seen_at = utcnow()

    # Rafraîchit les métadonnées si l'agent les envoie (sans ré-enrôlement) :
    # corrige p.ex. un libellé d'OS obsolète ou une montée de version d'agent.
    if isinstance(data.get("os_version"), str) and data["os_version"]:
        agent.os_version = data["os_version"]
    if isinstance(data.get("agent_version"), str) and data["agent_version"]:
        agent.agent_version = data["agent_version"]
    if isinstance(data.get("hostname"), str) and data["hostname"]:
        agent.hostname = data["hostname"]

    metric = Metric(
        agent_id=agent.id,
        ts=utcnow(),
        cpu_pct=_to_decimal_str(metrics.get("cpu_pct")),
        ram_used_pct=_to_decimal_str(metrics.get("ram_used_pct")),
        disk_free=metrics.get("disk_free") or {},
        uptime_seconds=_safe_int(metrics.get("uptime_seconds")),
        logged_in_user=metrics.get("logged_in_user"),
    )
    db.session.add(metric)
    db.session.commit()

    pending_count = (
        db.session.query(Command)
        .filter(Command.agent_id == agent.id, Command.status == "pending")
        .count()
    )

    config = {
        "heartbeat_interval": current_app.config.get("AGENT_HEARTBEAT_INTERVAL", 30),
        "command_poll_interval": current_app.config.get("AGENT_COMMAND_POLL_INTERVAL", 8),
    }
    # Signalisation bureau à distance : présent si une session est en attente.
    remote_session = _remote_session_for_agent(agent, "/ws/remote/agent")
    # Signalisation auto-update : présent si une release plus récente est publiée.
    agent_update = _agent_update_for(agent)
    return (
        jsonify(
            {
                "ok": True,
                "pending_commands": pending_count,
                "config": config,
                "remote_session": remote_session,
                "agent_update": agent_update,
            }
        ),
        200,
    )


def _agent_update_for(agent: Agent) -> dict | None:
    """Renvoie le manifeste de mise à jour pour un agent, ou None.

    Présent uniquement si l'auto-update est activé ET qu'une release courante,
    strictement plus récente que la version rapportée par l'agent, existe avec
    son fichier disponible sur le disque. L'agent télécharge ensuite ``url``
    (endpoint ``/agents/<id>/package``, même Bearer).
    """
    if not current_app.config.get("AGENT_AUTO_UPDATE_ENABLED", True):
        return None
    from .releases import current_release_available, version_gt

    rel = current_release_available()
    if rel is None or not version_gt(rel.version, agent.agent_version):
        return None
    base = request.host_url.rstrip("/")
    return {
        "version": rel.version,
        "url": f"{base}/api/v1/agents/{agent.id}/package",
        "sha256": rel.sha256,
        "size": rel.size,
    }


def _safe_int(value):
    """Convertit en entier de façon tolérante (ou None)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 2.3 POST /agents/{agent_id}/inventory
# --------------------------------------------------------------------------
@bp.post("/agents/<agent_id>/inventory")
@agent_required
def inventory(agent_id):
    """Upsert de l'inventaire matériel + remplacement complet de l'inventaire logiciel."""
    agent: Agent = g.agent
    data = _json_body()
    hardware = data.get("hardware") or {}
    software = data.get("software") or []
    security = data.get("security")

    now = utcnow()

    # --- Upsert matériel (1 ligne par agent) ---
    hw = db.session.get(HardwareInventory, agent.id)
    if hw is None:
        hw = HardwareInventory(agent_id=agent.id)
        db.session.add(hw)
    hw.manufacturer = hardware.get("manufacturer")
    hw.model = hardware.get("model")
    hw.serial_number = hardware.get("serial_number")
    hw.cpu_model = hardware.get("cpu_model")
    hw.cpu_cores = _safe_int(hardware.get("cpu_cores"))
    hw.ram_total_mb = _safe_int(hardware.get("ram_total_mb"))
    hw.disks = hardware.get("disks") or []
    hw.mac_addresses = hardware.get("mac_addresses") or []
    hw.collected_at = now

    # --- Remplacement complet du logiciel pour cet agent ---
    db.session.query(SoftwareInventory).filter_by(agent_id=agent.id).delete(
        synchronize_session=False
    )
    if isinstance(software, list):
        for item in software:
            if not isinstance(item, dict):
                continue
            db.session.add(
                SoftwareInventory(
                    agent_id=agent.id,
                    name=item.get("name"),
                    version=item.get("version"),
                    publisher=item.get("publisher"),
                    install_date=_parse_install_date(item.get("install_date")),
                    collected_at=now,
                )
            )

    # --- Upsert sécurité (Defender + MAJ Windows), si fourni ---
    if isinstance(security, dict):
        sec = db.session.get(AgentSecurity, agent.id)
        if sec is None:
            sec = AgentSecurity(agent_id=agent.id)
            db.session.add(sec)
        sec.defender = security.get("defender") or {}
        sec.windows_update = security.get("windows_update") or {}
        sec.collected_at = now

    db.session.commit()
    return jsonify({"ok": True}), 200


# --------------------------------------------------------------------------
# 2.4 GET /agents/{agent_id}/commands
# --------------------------------------------------------------------------
@bp.get("/agents/<agent_id>/commands")
@agent_required
def get_commands(agent_id):
    """Récupère les commandes ``pending`` de l'agent et les passe à ``dispatched``."""
    agent: Agent = g.agent
    now = utcnow()

    pending = (
        db.session.query(Command)
        .filter(Command.agent_id == agent.id, Command.status == "pending")
        .order_by(Command.created_at.asc())
        .all()
    )

    out = []
    for cmd in pending:
        cmd.status = "dispatched"
        cmd.dispatched_at = now
        out.append(
            {
                "id": str(cmd.id),
                "shell": cmd.shell,
                "command_text": cmd.command_text,
                "timeout_seconds": cmd.timeout_seconds,
            }
        )

    db.session.commit()

    # Signalisation bureau à distance : présent si une session est en attente.
    remote_session = _remote_session_for_agent(agent, "/ws/remote/agent")
    return jsonify({"commands": out, "remote_session": remote_session}), 200


# --------------------------------------------------------------------------
# 2.5 POST /commands/{command_id}/result
# --------------------------------------------------------------------------
@bp.post("/commands/<command_id>/result")
@agent_required_by_command
def post_result(command_id):
    """Réception du résultat d'exécution d'une commande (upsert + maj statut).

    L'authentification (décorateur ``agent_required_by_command``) garantit déjà
    que le Bearer token correspond à l'agent propriétaire de la commande et
    expose celle-ci via ``g.command``.
    """
    cmd: Command = g.command
    cmd_uuid = cmd.id

    data = _json_body()
    exit_code = _safe_int(data.get("exit_code"))
    stdout = data.get("stdout") or ""
    stderr = data.get("stderr") or ""
    duration = _to_decimal_str(data.get("duration_seconds"))

    # Troncature à 1 Mo (cf. SPEC §2.5).
    max_bytes = current_app.config.get("COMMAND_OUTPUT_MAX_BYTES", 1024 * 1024)
    stdout = _truncate(stdout, max_bytes)
    stderr = _truncate(stderr, max_bytes)

    now = utcnow()
    result = db.session.get(CommandResult, cmd_uuid)
    if result is None:
        result = CommandResult(command_id=cmd_uuid)
        db.session.add(result)
    result.exit_code = exit_code
    result.stdout = stdout
    result.stderr = stderr
    result.duration_seconds = duration
    result.received_at = now

    # Statut final.
    if str(data.get("status", "")).lower() == "timeout":
        cmd.status = "timeout"
    elif exit_code is None or exit_code != 0:
        cmd.status = "error"
    else:
        cmd.status = "done"
    cmd.completed_at = now

    db.session.commit()
    return jsonify({"ok": True}), 200


def _truncate(text: str, max_bytes: int) -> str:
    """Tronque une chaîne à ``max_bytes`` octets UTF-8 sans casser un caractère."""
    if text is None:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + "\n[...sortie tronquée à 1 Mo...]"
