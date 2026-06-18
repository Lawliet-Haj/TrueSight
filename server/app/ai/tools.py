"""Outils LECTURE SEULE exposés au modèle + leur exécution (dispatch).

Le modèle peut lire librement la télémétrie du parc via ces outils, mais ne mute
JAMAIS l'état (la seule action possible passe par ``propose_action``, géré dans
``loop.py`` → ``proposals.py``).

**Minimisation (contexte médical)** : les sérialiseurs ci-dessous retirent
volontairement les identifiants non nécessaires au diagnostic IT —
``logged_in_user`` (Metric), ``serial_number`` et ``mac_addresses`` (matériel).
Aucune IP n'est exposée.

Chaque outil s'adosse aux mêmes requêtes/served-helpers que le dashboard
(``api_dashboard``) pour rester cohérent et ne pas dupliquer la logique.
"""
from __future__ import annotations

from datetime import timedelta

from flask import current_app
from sqlalchemy import or_

from .. import scripts_catalog, software_catalog
from ..api_dashboard import (
    _agent_display_name,
    _is_online,
    _iso_utc,
    _latest_metric,
    _num,
    _parse_uuid,
    _sec_dict,
    _security_summary,
)
from ..extensions import db
from ..health import agent_health
from ..models import (
    Agent,
    AgentSecurity,
    Alert,
    AlertRule,
    Command,
    CommandResult,
    HardwareInventory,
    Metric,
    Site,
    SoftwareInventory,
    utcnow,
)

# --------------------------------------------------------------------------
# Schémas d'outils (format function-calling OpenAI). Descriptions PRESCRIPTIVES
# sur *quand* appeler (les modèles déclenchent mieux avec une condition claire).
# --------------------------------------------------------------------------
_AGENT_ID_PROP = {
    "type": "string",
    "description": "UUID du poste. Optionnel : par défaut le poste de la conversation.",
}

READ_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_agent_detail",
            "description": (
                "Fiche complète d'un poste : santé et raisons, dernier relevé CPU/RAM/disque, "
                "matériel, sécurité (antivirus + MAJ), nb de logiciels. À appeler EN PREMIER "
                "pour diagnostiquer un poste."
            ),
            "parameters": {
                "type": "object",
                "properties": {"agent_id": _AGENT_ID_PROP},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metrics",
            "description": (
                "Résumé agrégé des métriques (min/max/moyenne CPU et RAM, dernier relevé) sur "
                "une fenêtre. À appeler pour juger si un pic est ponctuel ou soutenu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": _AGENT_ID_PROP,
                    "hours": {"type": "integer", "description": "Fenêtre en heures (1 à 744, défaut 24)."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_software",
            "description": (
                "Liste des logiciels installés sur le poste (filtrable). À appeler pour vérifier "
                "la présence/version d'une application avant de proposer une (dés)installation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": _AGENT_ID_PROP,
                    "q": {"type": "string", "description": "Filtre sur le nom ou l'éditeur."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "Alertes du poste (offline/disk_low/cpu_high/ram_high). À appeler pour comprendre pourquoi un poste est en alerte.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": _AGENT_ID_PROP,
                    "status": {"type": "string", "enum": ["active", "all"], "description": "Défaut: active."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_command_result",
            "description": "Résultat d'une commande déjà exécutée (statut, code de sortie, stdout/stderr tronqués). À appeler pour expliquer un échec.",
            "parameters": {
                "type": "object",
                "properties": {"command_id": {"type": "string", "description": "UUID de la commande."}},
                "required": ["command_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_scripts",
            "description": (
                "Catalogue de scripts de maintenance 1-clic (réseau, système, impression, disque, "
                "sécurité). À appeler AVANT de proposer une action pour préférer un script curé au "
                "PowerShell libre."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Filtre sur le libellé/la catégorie."},
                    "category": {"type": "string", "description": "Catégorie exacte (optionnel)."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_software_catalog",
            "description": "Catalogue d'applications installables en silence (winget). À appeler avant de proposer une installation depuis le catalogue.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string", "description": "Filtre sur le libellé/l'ID."}},
                "additionalProperties": False,
            },
        },
    },
]


# --------------------------------------------------------------------------
# Sérialiseurs (minimisés)
# --------------------------------------------------------------------------
def _resolve_aid(args, ctx):
    raw = (args or {}).get("agent_id") or (ctx or {}).get("agent_id")
    return _parse_uuid(raw) if raw else None


def _agent_detail(aid):
    agent = db.session.get(Agent, aid)
    if agent is None:
        return {"error": "poste introuvable"}
    cfg = current_app.config
    hw = db.session.get(HardwareInventory, aid)
    metric = _latest_metric(aid)
    sec = db.session.get(AgentSecurity, aid)
    alert_types = {
        t for (t,) in db.session.query(AlertRule.type)
        .join(Alert, Alert.rule_id == AlertRule.id)
        .filter(Alert.agent_id == aid, Alert.resolved_at.is_(None))
        .all()
    }
    health, reasons = agent_health(agent, metric, alert_types, cfg, _sec_dict(sec))
    site = db.session.get(Site, agent.site_id) if agent.site_id else None

    hardware = None
    if hw is not None:
        # serial_number / mac_addresses VOLONTAIREMENT omis (minimisation).
        hardware = {
            "manufacturer": hw.manufacturer,
            "model": hw.model,
            "cpu_model": hw.cpu_model,
            "cpu_cores": hw.cpu_cores,
            "ram_total_mb": hw.ram_total_mb,
            "disks": hw.disks or [],
        }

    last_metric = None
    if metric is not None:
        # logged_in_user VOLONTAIREMENT omis (minimisation).
        last_metric = {
            "ts": _iso_utc(metric.ts),
            "cpu_pct": _num(metric.cpu_pct),
            "ram_used_pct": _num(metric.ram_used_pct),
            "disk_free_gb": metric.disk_free or {},
            "uptime_seconds": metric.uptime_seconds,
        }

    return {
        "id": str(agent.id),
        "name": _agent_display_name(agent),
        "hostname": agent.hostname,
        "os_version": agent.os_version,
        "agent_version": agent.agent_version,
        "site_name": site.name if site else None,
        "tags": agent.tags or [],
        "status": "online" if _is_online(agent, cfg["OFFLINE_THRESHOLD_SECONDS"]) else "offline",
        "health": health,
        "health_reasons": reasons,
        "security": _security_summary(sec),
        "hardware": hardware,
        "last_metric": last_metric,
        "software_count": db.session.query(SoftwareInventory).filter_by(agent_id=aid).count(),
    }


def _metrics_summary(aid, hours):
    since = utcnow() - timedelta(hours=hours)
    rows = (
        db.session.query(Metric)
        .filter(Metric.agent_id == aid, Metric.ts >= since)
        .order_by(Metric.ts.asc())
        .all()
    )
    if not rows:
        return {"hours": hours, "points": 0}

    def _stats(vals):
        if not vals:
            return None
        return {"min": round(min(vals), 1), "max": round(max(vals), 1), "avg": round(sum(vals) / len(vals), 1)}

    cpu = [float(r.cpu_pct) for r in rows if r.cpu_pct is not None]
    ram = [float(r.ram_used_pct) for r in rows if r.ram_used_pct is not None]
    latest = rows[-1]
    return {
        "hours": hours,
        "points": len(rows),
        "cpu_pct": _stats(cpu),
        "ram_used_pct": _stats(ram),
        "latest_ts": _iso_utc(latest.ts),
        "latest_disk_free_gb": latest.disk_free or {},
        "latest_uptime_seconds": latest.uptime_seconds,
    }


def _software(aid, q):
    query = db.session.query(SoftwareInventory).filter_by(agent_id=aid)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(SoftwareInventory.name.ilike(like), SoftwareInventory.publisher.ilike(like)))
    rows = query.order_by(SoftwareInventory.name.asc()).limit(200).all()
    return [{"name": r.name, "version": r.version, "publisher": r.publisher} for r in rows]


def _alerts(aid, status):
    query = db.session.query(Alert, AlertRule).outerjoin(AlertRule, Alert.rule_id == AlertRule.id)
    if aid:
        query = query.filter(Alert.agent_id == aid)
    if status != "all":
        query = query.filter(Alert.resolved_at.is_(None))
    rows = query.order_by(Alert.triggered_at.desc()).limit(100).all()
    return [
        {
            "type": rule.type if rule else None,
            "threshold": _num(rule.threshold) if rule else None,
            "triggered_at": _iso_utc(a.triggered_at),
            "resolved_at": _iso_utc(a.resolved_at),
            "active": a.resolved_at is None,
        }
        for a, rule in rows
    ]


def _command_result(command_id):
    cid = _parse_uuid(command_id)
    if cid is None:
        return {"error": "command_id invalide"}
    cmd = db.session.get(Command, cid)
    if cmd is None:
        return {"error": "commande introuvable"}
    res = db.session.get(CommandResult, cid)
    out = {"status": cmd.status, "shell": cmd.shell, "command_text": cmd.command_text}
    if res is not None:
        out["exit_code"] = res.exit_code
        out["stdout"] = (res.stdout or "")[:4000]
        out["stderr"] = (res.stderr or "")[:4000]
    return out


def _scripts(q, category):
    items = scripts_catalog.public_catalog()
    if category:
        items = [s for s in items if s["category"].lower() == category.lower()]
    if q:
        ql = q.lower()
        items = [s for s in items if ql in s["label"].lower() or ql in s["command_text"].lower() or ql in s["category"].lower()]
    return [{"key": s["key"], "label": s["label"], "category": s["category"], "danger": s["danger"]} for s in items]


def _software_catalog(q):
    items = software_catalog.public_catalog()
    if q:
        ql = q.lower()
        items = [s for s in items if ql in s["label"].lower() or ql in s["winget_id"].lower() or ql in s["category"].lower()]
    return items


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------
def dispatch(name, args, ctx):
    """Exécute un outil lecture seule et renvoie un dict borné (sérialisé en JSON)."""
    args = args or {}
    if name == "get_agent_detail":
        aid = _resolve_aid(args, ctx)
        return _agent_detail(aid) if aid else {"error": "agent_id requis"}
    if name == "get_metrics":
        aid = _resolve_aid(args, ctx)
        if not aid:
            return {"error": "agent_id requis"}
        try:
            hours = max(1, min(int(args.get("hours", 24)), 24 * 31))
        except (TypeError, ValueError):
            hours = 24
        return _metrics_summary(aid, hours)
    if name == "get_software":
        aid = _resolve_aid(args, ctx)
        return {"software": _software(aid, (args.get("q") or "").strip())} if aid else {"error": "agent_id requis"}
    if name == "list_alerts":
        aid = _resolve_aid(args, ctx)  # optionnel ; si présent on cadre sur le poste
        status = (args.get("status") or "active").strip().lower()
        return {"alerts": _alerts(aid, status if status in ("active", "all") else "active")}
    if name == "get_command_result":
        return _command_result(args.get("command_id") or "")
    if name == "search_scripts":
        return {"scripts": _scripts((args.get("q") or "").strip(), (args.get("category") or "").strip())}
    if name == "list_software_catalog":
        return {"catalog": _software_catalog((args.get("q") or "").strip())}
    return {"error": f"outil inconnu : {name}"}
