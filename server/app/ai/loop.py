"""Boucle tool-use manuelle, bornée — orchestration du tour de conversation.

Le modèle peut appeler des outils LECTURE SEULE (``tools.dispatch``) autant que
nécessaire, et ``propose_action`` (validé par ``proposals.validate``) qui n'exécute
rien — il produit une proposition à confirmer côté humain. La boucle est bornée par
``AI_MAX_TOOL_ITERS`` (garde-fou coût / ``--timeout 300`` gunicorn).

L'historique renvoyé est volontairement « propre » (uniquement les tours user /
assistant en texte) : la tuyauterie d'outils ne vit que le temps du tour courant,
ce qui évite tout problème de cohérence ``tool_call_id`` entre tours.
"""
from __future__ import annotations

import json
import logging

from flask import current_app

from . import client, prompts, proposals, tools

_logger = logging.getLogger("truesight.ai.loop")

_HISTORY_MAX = 20
_MSG_MAX = 4000
_TOOL_RESULT_MAX = 8000


def _accumulate(total, usage):
    if isinstance(usage, dict):
        total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        total["completion_tokens"] += int(usage.get("completion_tokens") or 0)


def _sanitize_history(history):
    """Ne conserve que des tours user/assistant en texte (anti-injection de rôle)."""
    out = []
    if isinstance(history, list):
        for m in history[-_HISTORY_MAX:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                out.append({"role": m["role"], "content": m["content"][:_MSG_MAX]})
    return out


def run_chat_turn(message, agent_id=None, history=None):
    """Exécute un tour de Copilote. Renvoie un dict prêt à sérialiser pour l'UI."""
    history = _sanitize_history(history)
    if not client.is_configured():
        return {
            "reply": "Le Copilote IA n'est pas configuré sur ce serveur (clé API absente).",
            "proposals": [], "history": history, "usage": {}, "tool_calls": [], "disabled": True,
        }

    max_iters = int(current_app.config.get("AI_MAX_TOOL_ITERS") or 5)
    tool_specs = tools.READ_TOOL_SPECS + [proposals.PROPOSE_ACTION_SPEC]
    user_msg = {"role": "user", "content": str(message)[:_MSG_MAX]}
    messages = [{"role": "system", "content": prompts.build_system()}] + history + [user_msg]
    ctx = {"agent_id": agent_id}

    proposals_out, tool_call_names = [], []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    reply = ""

    try:
        for _ in range(max_iters):
            result = client.create_chat(messages, tool_specs)
            _accumulate(usage_total, result.usage)
            messages.append(result.raw_message or {"role": "assistant", "content": result.text or ""})

            if not result.tool_calls:
                reply = result.text
                break

            for tc in result.tool_calls:
                tool_call_names.append(tc.name)
                try:
                    args = json.loads(tc.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (ValueError, TypeError):
                    args = {}

                if tc.name == "propose_action":
                    prop, err = proposals.validate(args, agent_id)
                    if err:
                        tool_result = {"status": "rejected", "error": err}
                    else:
                        proposals_out.append(prop)
                        tool_result = {"status": "queued_for_human_review", "proposal_id": prop["id"]}
                else:
                    tool_result = tools.dispatch(tc.name, args, ctx)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False)[:_TOOL_RESULT_MAX],
                })
        else:
            reply = ("Je n'ai pas pu finaliser l'analyse en un nombre raisonnable d'étapes. "
                     "Reformulez ou précisez votre demande.")
    except client.AIConfigError:
        return {
            "reply": "Le Copilote IA n'est pas configuré (clé API absente).",
            "proposals": [], "history": history, "usage": usage_total, "tool_calls": tool_call_names, "disabled": True,
        }
    except client.AIClientError as exc:
        _logger.warning("Copilote : appel IA en échec : %s", exc)
        return {
            "reply": f"Le service IA a renvoyé une erreur. Détail : {exc}",
            "proposals": proposals_out, "history": history, "usage": usage_total,
            "tool_calls": tool_call_names, "error": True,
        }

    new_history = (history + [user_msg, {"role": "assistant", "content": reply or ""}])[-_HISTORY_MAX:]
    return {
        "reply": reply or "",
        "proposals": proposals_out,
        "history": new_history,
        "usage": usage_total,
        "tool_calls": tool_call_names,
    }
