"""Outil ``propose_action`` + validation serveur des actions proposées par le modèle.

Principe de sûreté : le modèle ne peut PAS exécuter d'action. Il appelle
``propose_action`` ; on **valide** ici les arguments en réutilisant exactement les
validateurs/constructeurs des endpoints réels (``software_catalog``, le catalogue de
scripts, l'ensemble autorisé des actions rapides), puis on renvoie à l'UI une
**proposition structurée** avec :
- un ``preview`` fidèle (le shell + la commande qui s'exécutera réellement) ;
- un bloc ``confirm`` indiquant l'endpoint EXISTANT (déjà audité) à appeler et le
  corps exact à poster.

Aucune écriture en base ici. La confirmation (humaine) repasse par les endpoints
audités (`command.create` / `software.install` / `command.quickaction`).
"""
from __future__ import annotations

import uuid

from .. import scripts_catalog, software_catalog
from ..api_dashboard import _QUICK_ACTIONS, _clean_message_text

PROPOSE_ACTION_SPEC = {
    "type": "function",
    "function": {
        "name": "propose_action",
        "description": (
            "PROPOSER une action corrective sur le poste — elle n'est PAS exécutée, l'admin doit "
            "la confirmer. Préférer un script du catalogue (`run_script`) ou le catalogue logiciel "
            "plutôt qu'une commande libre. Champs selon `kind` : "
            "run_script→script_key ; install_software→source(catalog|winget|url)+catalog_key|winget_id|url(+exe_args) ; "
            "uninstall_software→source(registry|winget)+software_name|winget_id ; "
            "quick_action→action(lock|restart|logoff|message)(+message_text) ; "
            "run_command→shell+command_text(+timeout_seconds)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["run_script", "install_software", "uninstall_software", "quick_action", "run_command"],
                },
                "rationale": {"type": "string", "description": "Pourquoi cette action, en une phrase (FR)."},
                "script_key": {"type": "string"},
                "source": {"type": "string", "enum": ["catalog", "winget", "url", "registry"]},
                "catalog_key": {"type": "string"},
                "winget_id": {"type": "string"},
                "url": {"type": "string"},
                "exe_args": {"type": "string"},
                "software_name": {"type": "string"},
                "action": {"type": "string", "enum": ["lock", "restart", "logoff", "message"]},
                "message_text": {"type": "string"},
                "shell": {"type": "string", "enum": ["powershell", "cmd"]},
                "command_text": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["kind", "rationale"],
            "additionalProperties": False,
        },
    },
}


def _mk(kind, agent_id, rationale, danger, preview, endpoint, body):
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "agent_id": agent_id,
        "rationale": rationale,
        "danger": bool(danger),
        "preview": preview,
        "confirm": {"endpoint": endpoint, "method": "POST", "body": body},
    }


def validate(args, agent_id):
    """Valide les arguments de ``propose_action``. Renvoie ``(proposal, None)`` ou ``(None, msg)``."""
    args = args or {}
    if not agent_id:
        return None, "agent_id requis pour proposer une action"
    kind = (args.get("kind") or "").strip()
    rationale = (args.get("rationale") or "").strip()[:500]
    base = f"/api/v1/agents/{agent_id}"

    if kind == "run_script":
        key = (args.get("script_key") or "").strip()
        entry = next((s for s in scripts_catalog.public_catalog() if s["key"] == key), None)
        if entry is None:
            return None, f"script inconnu : {key}"
        return _mk(
            kind, agent_id, rationale, bool(entry.get("danger")),
            {"shell": entry["shell"], "command_text": entry["command_text"], "timeout": entry["timeout"]},
            base + "/commands",
            {"shell": entry["shell"], "command_text": entry["command_text"], "timeout_seconds": entry["timeout"]},
        ), None

    if kind == "install_software":
        source = (args.get("source") or "").strip().lower()
        if source == "catalog":
            key = (args.get("catalog_key") or "").strip()
            wid = software_catalog.catalog_winget_id(key)
            if not wid:
                return None, f"application inconnue au catalogue : {key}"
            shell, text, timeout = software_catalog.build_winget_install(wid)
            body = {"source": "catalog", "key": key}
        elif source == "winget":
            wid = (args.get("winget_id") or "").strip()
            if not software_catalog.valid_winget_id(wid):
                return None, "winget_id invalide"
            shell, text, timeout = software_catalog.build_winget_install(wid)
            body = {"source": "winget", "winget_id": wid}
        elif source == "url":
            url = (args.get("url") or "").strip()
            if not software_catalog.valid_url(url):
                return None, "url invalide (HTTPS, .msi/.exe)"
            exe_args = software_catalog.clean_name(args.get("exe_args"), 200)
            shell, text, timeout = software_catalog.build_url_install(url, exe_args or None)
            body = {"source": "url", "url": url}
            if exe_args:
                body["exe_args"] = exe_args
        else:
            return None, "source d'installation invalide (catalog|winget|url)"
        return _mk(kind, agent_id, rationale, True,
                   {"shell": shell, "command_text": text, "timeout": timeout},
                   base + "/software/install", body), None

    if kind == "uninstall_software":
        source = (args.get("source") or "registry").strip().lower()
        if source == "registry":
            name = software_catalog.clean_name(args.get("software_name"))
            if not name:
                return None, "software_name requis"
            shell, text, timeout = software_catalog.build_registry_uninstall(name)
            body = {"source": "registry", "name": name}
        elif source == "winget":
            wid = (args.get("winget_id") or "").strip()
            if wid:
                if not software_catalog.valid_winget_id(wid):
                    return None, "winget_id invalide"
                shell, text, timeout = software_catalog.build_winget_uninstall(winget_id=wid)
                body = {"source": "winget", "winget_id": wid}
            else:
                name = software_catalog.clean_name(args.get("software_name"))
                if not name:
                    return None, "winget_id ou software_name requis"
                shell, text, timeout = software_catalog.build_winget_uninstall(name=name)
                body = {"source": "winget", "name": name}
        else:
            return None, "source de désinstallation invalide (registry|winget)"
        return _mk(kind, agent_id, rationale, True,
                   {"shell": shell, "command_text": text, "timeout": timeout},
                   base + "/software/uninstall", body), None

    if kind == "quick_action":
        action = (args.get("action") or "").strip().lower()
        if action not in ("lock", "restart", "logoff", "message"):
            return None, "action invalide (lock|restart|logoff|message)"
        if action == "message":
            text = _clean_message_text(args.get("message_text") or "")
            if not text:
                return None, "message_text requis pour l'action message"
            preview_cmd = f'msg * "{text}"'
            body = {"action": "message", "text": text}
            danger = False
        else:
            preview_cmd = _QUICK_ACTIONS[action]
            body = {"action": action}
            danger = action in ("restart", "logoff")
        return _mk(kind, agent_id, rationale, danger,
                   {"shell": "cmd", "command_text": preview_cmd, "timeout": 30},
                   base + "/quick-action", body), None

    if kind == "run_command":
        shell = (args.get("shell") or "").strip().lower()
        command_text = args.get("command_text") or ""
        if shell not in ("powershell", "cmd"):
            return None, "shell invalide (powershell|cmd)"
        if not command_text.strip():
            return None, "command_text requis"
        try:
            timeout = int(args.get("timeout_seconds", 120))
            if timeout <= 0 or timeout > 3600:
                timeout = 120
        except (TypeError, ValueError):
            timeout = 120
        return _mk(kind, agent_id, rationale, True,
                   {"shell": shell, "command_text": command_text, "timeout": timeout},
                   base + "/commands",
                   {"shell": shell, "command_text": command_text, "timeout_seconds": timeout}), None

    return None, f"type d'action inconnu : {kind}"
