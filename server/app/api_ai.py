"""Blueprint API du Copilote IA (réservé aux administrateurs).

`POST /api/v1/ai/chat` : un tour de conversation. Le modèle lit la télémétrie via
des outils LECTURE SEULE et peut renvoyer des **propositions** d'action (jamais
exécutées) que l'UI fait confirmer via les endpoints existants déjà audités.

Chaque échange est journalisé (`ai.query`) avec des métadonnées (longueur du
message, outils appelés, types de propositions, usage) — jamais le texte brut.
"""
import logging

from flask import Blueprint, g, jsonify, request

from .ai import run_chat_turn
from .security import admin_required, write_audit

bp = Blueprint("api_ai", __name__, url_prefix="/api/v1/ai")
_logger = logging.getLogger("truesight.api_ai")


@bp.post("/chat")
@admin_required
def chat():
    """Un tour de Copilote. Body : ``{message, agent_id?, history?}``."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message requis"}), 400

    raw_agent = data.get("agent_id")
    agent_id = str(raw_agent) if raw_agent else None
    history = data.get("history")

    result = run_chat_turn(message, agent_id=agent_id, history=history)

    write_audit(
        action="ai.query",
        user_id=g.user.id,
        target_agent=agent_id,
        details={
            "message_len": len(message),
            "tool_calls": result.get("tool_calls", []),
            "proposal_kinds": [p.get("kind") for p in result.get("proposals", [])],
            "usage": result.get("usage", {}),
            "disabled": bool(result.get("disabled")),
            "error": bool(result.get("error")),
        },
    )
    return jsonify(result), 200
